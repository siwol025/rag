import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from slack_rag import job_queue, worker
from slack_rag.config import Settings
from slack_rag.db import create_job
from slack_rag.main import create_app

pytestmark = pytest.mark.anyio

API_KEY = "test-api-key"
AUTH = {"X-API-Key": API_KEY}
HANGING_WORKER = Path(__file__).with_name("hanging_worker.py")


@pytest.fixture
def settings(database_url, redis_url, conn):  # conn이 jobs를, redis_url이 Redis를 비움
    return Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
    )


def make_job(conn, ts="1794381600.000100"):
    job_id, _, _ = create_job(
        conn, channel_id="C0TARGET01", message_ts=ts, user_id="U0HUMAN01", message_text="결제 API 5xx 급증"
    )
    return job_id


def job_row(conn, job_id):
    return conn.execute("SELECT status, attempts FROM jobs WHERE id = %s", (job_id,)).fetchone()


async def enqueue(settings, job_id):
    redis = await job_queue.connect(settings.redis_url)
    try:
        await job_queue.enqueue(redis, job_id)
    finally:
        await redis.aclose()


async def run_burst(settings, **overrides):
    """대기 작업이 없어질 때까지 워커를 돌린다(arq burst 모드). poll_delay는 테스트 속도용."""
    w = worker.create_worker(settings, burst=True, poll_delay=0.01, handle_signals=False, **overrides)
    try:
        await w.async_run()
    finally:
        await w.close()


async def test_enqueued_job_is_marked_done(settings, conn):
    job_id = make_job(conn)
    await enqueue(settings, job_id)
    await run_burst(settings)
    status, attempts, started_at, finished_at = conn.execute(
        "SELECT status, attempts, started_at, finished_at FROM jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert (status, attempts) == ("done", 1)
    assert started_at <= finished_at


async def test_worker_start_enqueues_jobs_left_only_in_db(settings, conn):
    job_ids = [make_job(conn, ts=f"1794381600.{i:06d}") for i in range(3)]  # 적재 실패로 DB에만 남음
    await run_burst(settings)
    assert [job_row(conn, job_id) for job_id in job_ids] == [("done", 1)] * 3


async def test_worker_does_not_process_finished_job_again(settings, conn, monkeypatch):
    handled = []

    async def record(job_id):
        handled.append(job_id)

    monkeypatch.setattr(worker, "handle_job", record)
    job_id = make_job(conn)
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (job_id,))
    await enqueue(settings, job_id)  # 오래된 중복이나 경합으로 끝난 작업이 큐에 다시 들어온 상황
    await run_burst(settings)
    assert handled == []
    assert job_row(conn, job_id) == ("done", 0)


async def test_worker_logs_queue_wait_and_processing_time_with_job_id(settings, conn, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = make_job(conn)
    await run_burst(settings)
    logged = {r.getMessage(): getattr(r, "fields", {}) for r in caplog.records if r.name == "slack_rag.worker"}
    assert logged["job_started"]["job_id"] == str(job_id)
    assert logged["job_started"]["queue_wait_ms"] >= 0
    assert logged["job_done"]["job_id"] == str(job_id)
    assert logged["job_done"]["duration_ms"] >= 0


async def raise_error(job_id):
    raise RuntimeError("처리 실패")


async def hang(job_id):
    await anyio.sleep(3600)


@pytest.mark.parametrize("handler", [raise_error, hang], ids=["exception", "timeout"])
async def test_job_failing_every_try_is_isolated_as_failed(settings, conn, monkeypatch, handler):
    monkeypatch.setattr(worker, "handle_job", handler)
    monkeypatch.setattr(worker, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(worker, "JOB_TIMEOUT_S", 0.2)
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("failed", worker.MAX_TRIES, "max_tries_exceeded")


async def test_two_workers_process_each_job_exactly_once(settings, conn, monkeypatch):
    handled = []

    async def record(job_id):
        handled.append(job_id)
        await anyio.sleep(0.05)  # 두 워커가 겹쳐 돌게 함

    monkeypatch.setattr(worker, "handle_job", record)
    job_ids = [make_job(conn, ts=f"1794381600.{i:06d}") for i in range(10)]
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_burst, settings)
        tg.start_soon(run_burst, settings)
    assert sorted(handled) == sorted(job_ids)
    assert conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall() == [("done", 10)]


async def wait_for_status(conn, job_id, expected, timeout=30):
    with anyio.fail_after(timeout):
        while job_row(conn, job_id)[0] != expected:
            await anyio.sleep(0.1)


async def test_job_of_killed_worker_is_processed_again(settings, conn, tmp_path):
    job_id = make_job(conn)  # 큐 적재는 띄운 워커의 시작 보정이 함
    env = os.environ | {
        "DATABASE_URL": settings.database_url,
        "REDIS_URL": settings.redis_url,
        "JOBS_API_KEY": settings.jobs_api_key,
        "SLACK_TARGET_CHANNEL_IDS": json.dumps(sorted(settings.slack_target_channel_ids)),
    }
    # cwd=tmp_path: 사용자의 .env를 읽지 않게
    proc = subprocess.Popen([sys.executable, str(HANGING_WORKER)], env=env, cwd=tmp_path)
    try:
        await wait_for_status(conn, job_id, "running")
    finally:
        proc.kill()  # SIGKILL: arq가 정리할 틈 없이 죽음
        proc.wait()

    await run_burst(settings)  # 실행 중 표시가 만료(약 11초)되면 2번째 시도로 가져가 완료

    assert job_row(conn, job_id) == ("done", 2)


async def test_hundred_requests_get_202_at_once_and_all_become_done(settings, conn, make_event):
    with TestClient(create_app(settings)) as client:
        codes = [
            client.post("/jobs", json=make_event(ts=f"1794381600.{i:06d}"), headers=AUTH).status_code
            for i in range(100)
        ]
    assert codes == [202] * 100  # 워커가 돌기 전에 모두 202 → 응답이 처리를 기다리지 않음

    await run_burst(settings)

    assert conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall() == [("done", 100)]


async def test_db_failure_at_startup_reports_real_cause_not_keyerror(settings, monkeypatch):
    # 워커가 시작할 때 DB에 붙지 못하면 arq는 shutdown도 부른다. 그때 원인(PoolTimeout) 대신
    # ctx["db"]가 없어 KeyError가 나면 안 된다(F2 리뷰 지적 #1).
    from psycopg_pool import PoolTimeout

    closed = []

    class UnreachablePool:
        def __init__(self, *args, **kwargs):
            pass

        async def open(self, wait=False, timeout=30.0):
            raise PoolTimeout("db unreachable")

        async def close(self, timeout=5.0):
            closed.append(True)

    monkeypatch.setattr(worker, "AsyncConnectionPool", UnreachablePool)
    w = worker.create_worker(settings)
    ctx = {}

    with pytest.raises(PoolTimeout):
        await w.on_startup(ctx)
    await w.on_shutdown(ctx)  # KeyError 없이 풀을 닫아야 한다

    assert closed == [True]
