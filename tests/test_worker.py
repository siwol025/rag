import json
import logging
import os
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from arq import Retry
from fastapi.testclient import TestClient
from google.genai import errors
from psycopg_pool import AsyncConnectionPool

from fakes import (
    CODE_FIX,
    DAILY_QUOTA,
    FAKE_MIN_SIMILARITY,
    LLM_SETTINGS,
    MINUTE_QUOTA,
    OTHER,
    QUESTION,
    FakeGenai,
    cite_first,
    classify_then_cite,
    quota_error,
)
from slack_rag import graph, job_queue, llm, worker
from slack_rag.config import Settings
from slack_rag.db import create_job
from slack_rag.main import create_app

pytestmark = pytest.mark.anyio

API_KEY = "test-api-key"
AUTH = {"X-API-Key": API_KEY}
HANGING_WORKER = Path(__file__).with_name("hanging_worker.py")
DECOY_QUESTION = "결제 API에서 5xx 응답이 급증하고 커넥션 풀이 고갈됐던 장애가 있었나요?"
PAYMENT_5XX = {"request_type": "question", "service": "payment-api", "category": "server-error"}
RAG_QUERIES = tomllib.loads(Path(__file__).with_name("rag_queries.toml").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """워커가 만드는 Gemini 클라이언트를 가짜로 바꾼다. 자동 테스트는 실제 Gemini를 부르지 않는다(3.13)."""
    fake = FakeGenai(QUESTION)
    monkeypatch.setattr(llm, "make_client", lambda settings: fake)
    return fake


@pytest.fixture
def settings(database_url, redis_url, conn):  # conn이 jobs를, redis_url이 Redis를 비움
    return Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
        **LLM_SETTINGS,
    )


def worker_env(settings):
    """띄울 워커 프로세스의 환경 변수(테스트 설정 그대로). cwd를 tmp_path로 두어 사용자의 .env를 읽지 않게 한다."""
    return os.environ | {
        "DATABASE_URL": settings.database_url,
        "REDIS_URL": settings.redis_url,
        "JOBS_API_KEY": settings.jobs_api_key,
        "SLACK_TARGET_CHANNEL_IDS": json.dumps(sorted(settings.slack_target_channel_ids)),
        "GEMINI_API_KEY": settings.gemini_api_key.get_secret_value(),
        "LLM_LIGHT_MODEL": settings.llm_light_model,
        "EMBEDDING_MODEL": settings.embedding_model,
    }


def make_job(conn, ts="1794381600.000100", text="결제 API 5xx 급증"):
    job_id, _, _ = create_job(conn, channel_id="C0TARGET01", message_ts=ts, user_id="U0HUMAN01", message_text=text)
    return job_id


def job_row(conn, job_id):
    return conn.execute("SELECT status, attempts FROM jobs WHERE id = %s", (job_id,)).fetchone()


async def enqueue(settings, job_id):
    redis = await job_queue.connect(settings.redis_url)
    try:
        await job_queue.enqueue(redis, job_id)
    finally:
        await redis.aclose()


@asynccontextmanager
async def started_worker_ctx(settings, job_try):
    """arq 없이 run_job을 직접 부를 ctx. 워커 시작 처리(풀·그래프·LLM 클라이언트)를 그대로 쓴다."""
    w = worker.create_worker(settings, handle_signals=False)
    redis = await job_queue.connect(settings.redis_url)
    ctx = {"redis": redis, "job_try": job_try}
    try:
        await w.on_startup(ctx)
        yield ctx
    finally:
        await w.on_shutdown(ctx)
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

    async def record(ctx, job_id, job_try):
        handled.append(job_id)
        return {"classification": OTHER}  # handle_job은 최종 State를 돌려준다

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


async def raise_error(ctx, job_id, job_try):
    raise RuntimeError("처리 실패")


async def hang(ctx, job_id, job_try):
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


async def test_record_run_logs_job_failed_only_when_it_isolates(settings, conn, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    done_id = make_job(conn)  # 3번째 시도가 done을 커밋한 직후 종료된 작업
    running_id = make_job(conn, ts="1794381700.000200")
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (done_id,))
    conn.execute("UPDATE jobs SET status = 'running', attempts = 3 WHERE id = %s", (running_id,))

    async with AsyncConnectionPool(settings.database_url, open=False) as db:
        for job_id in (done_id, running_id):  # arq의 4번째(기록용) 실행
            await worker.run_job({"job_try": worker.MAX_TRIES + 1, "db": db}, str(job_id))

    events = [(r.getMessage(), r.fields["job_id"]) for r in caplog.records if r.name == "slack_rag.worker"]
    assert events == [("job_skipped", str(done_id)), ("job_failed", str(running_id))]
    assert job_row(conn, done_id) == ("done", 0)


async def test_two_workers_process_each_job_exactly_once(settings, conn, monkeypatch):
    handled = []

    async def record(ctx, job_id, job_try):
        handled.append(job_id)
        await anyio.sleep(0.05)  # 두 워커가 겹쳐 돌게 함
        return {"classification": OTHER}  # handle_job은 최종 State를 돌려준다

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
    proc = subprocess.Popen([sys.executable, str(HANGING_WORKER)], env=worker_env(settings), cwd=tmp_path)
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
    # ctx["db"]가 없어 KeyError가 나면 안 된다(큐 적재·워커 구현 리뷰 지적 #1).
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


async def test_processed_job_records_classification_and_model_usage(settings, conn):
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, request_type, service, category FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("done", *QUESTION.values())
    usage = conn.execute(
        "SELECT job_try, node, model FROM llm_usage WHERE job_id = %s ORDER BY id", (job_id,)
    ).fetchall()
    assert usage == [(1, "classify", "fake-light-model"), (1, "retrieve", "fake-embedding-model")]


async def test_rate_limited_job_waits_in_place_and_finishes_in_the_same_try(settings, conn, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "RATE_LIMIT_WAIT_S", 0)  # 실제로 60초씩 기다리지 않게
    answers = iter([quota_error(MINUTE_QUOTA)] * 2 + [QUESTION])  # 분당 429 두 번 뒤 성공
    fake_llm.answer = lambda contents: next(answers)
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, attempts, request_type FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("done", 1, "question")  # 시도 번호를 쓰지 않고 같은 시도로 완료
    assert len(fake_llm.calls) == 3


async def test_rate_limit_beyond_max_waits_retries_the_job_using_a_try(settings, conn, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "RATE_LIMIT_WAIT_S", 0)
    fake_llm.answer = quota_error(MINUTE_QUOTA)  # 계속 분당 429
    job_id = make_job(conn)
    async with started_worker_ctx(settings, job_try=1) as ctx:
        with pytest.raises(Retry) as retry:
            await worker.run_job(ctx, str(job_id))
    assert retry.value.defer_score == worker.RETRY_DELAY_S * 1000  # 일반 일시 오류와 같이 10초 뒤
    assert len(fake_llm.calls) == llm.RATE_LIMIT_MAX_WAITS + 1     # 처음 1번 + 대기 뒤 5번
    assert job_row(conn, job_id) == ("running", 1)


async def test_daily_quota_exhausted_job_is_isolated_without_more_calls(settings, conn, fake_llm):
    fake_llm.answer = quota_error(DAILY_QUOTA)
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("failed", 1, "llm_quota_exhausted")
    assert len(fake_llm.calls) == 1  # 오늘은 풀리지 않으므로 다시 부르지 않음


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_rejected_request_is_isolated_without_retry(settings, conn, fake_llm, caplog, code):
    caplog.set_level(logging.INFO, logger="slack_rag")
    fake_llm.answer = errors.ClientError(code, {"error": {"code": code, "status": "REJECTED", "message": "bad request"}})
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("failed", 1, "llm_request_rejected")
    assert len(fake_llm.calls) == 1  # 다시 보내도 같은 답이므로 시도 3번을 쓰지 않음
    [failed] = [r.fields for r in caplog.records if r.getMessage() == "job_failed"]
    assert str(code) in failed["detail"]  # 원인(응답 상태)이 로그에 남음


async def test_sample_messages_get_classification_saved_and_route_chosen(settings, conn, caplog, fake_llm, make_event):
    samples = {  # 본문 → (가짜 모델 응답, node_reached를 남기는 노드)
        "예전에 결제 5xx 장애 있었나요?": (QUESTION, None),  # 검색·답변 노드는 node_done을 남김(node_reached 없음)
        "주문 API 타임아웃 급증, 원인 찾아 고쳐 주세요": (CODE_FIX, "propose_fix"),
        "오늘 점심 같이 드실 분?": (OTHER, None),
        "": (OTHER, None),       # 파일만 올린 메시지: 모델을 부르지 않음
        " \n\t": (OTHER, None),  # 공백·줄바꿈만: 모델을 부르지 않음(가정 C5)
    }
    fake_llm.answer = lambda contents: next(
        answer for text, (answer, _) in samples.items() if text.strip() and text in contents
    )
    with TestClient(create_app(settings)) as client:
        job_ids = {
            text: client.post("/jobs", json=make_event(ts=f"1794381600.{i:06d}", text=text), headers=AUTH).json()["job_id"]
            for i, text in enumerate(samples)
        }
    caplog.set_level(logging.INFO, logger="slack_rag")

    await run_burst(settings)

    for text, (answer, _) in samples.items():
        row = conn.execute(
            "SELECT status, request_type, service, category FROM jobs WHERE id = %s", (job_ids[text],)
        ).fetchone()
        assert row == ("done", *answer.values())
    reached = {r.fields["job_id"]: r.fields["node"] for r in caplog.records if r.getMessage() == "node_reached"}
    assert reached == {job_ids[text]: node for text, (_, node) in samples.items() if node}
    question_id = job_ids["예전에 결제 5xx 장애 있었나요?"]
    done = [r.fields["node"] for r in caplog.records if r.getMessage() == "node_done" and r.fields["job_id"] == question_id]
    assert done == ["classify", "retrieve", "answer_question"]  # question 경로: 분류 → 검색 → 답변
    assert len(fake_llm.calls) == 3  # 분류 3번. 색인된 문서가 없어 답변 모델은 부르지 않음


KILLED_AFTER_CLASSIFY_WORKER = Path(__file__).with_name("killed_after_classify_worker.py")


async def wait_until_retrieved(settings, job_id, timeout=30):
    """체크포인트에 검색 결과가 저장될 때까지 기다린다(그 뒤에 죽여야 '분류·검색 뒤 종료'가 된다)."""
    async with graph.checkpoint_pool(settings.database_url) as checkpoints:
        compiled = graph.build(checkpoints)
        with anyio.fail_after(timeout):
            while "retrieval" not in (await compiled.aget_state(graph.thread_config(job_id))).values:
                await anyio.sleep(0.1)


async def test_reprocessing_after_kill_does_not_call_classifier_again(settings, conn, tmp_path, fake_llm):
    job_id = make_job(conn)  # 큐 적재는 띄운 워커의 시작 보정이 함
    proc = subprocess.Popen(
        [sys.executable, str(KILLED_AFTER_CLASSIFY_WORKER)], env=worker_env(settings), cwd=tmp_path
    )
    try:
        await wait_until_retrieved(settings, job_id)  # 분류만 기다리면 검색 중에 죽여 결과가 흔들림
    finally:
        proc.kill()  # SIGKILL: 분류·검색은 끝났고 답변 노드에서 멈춘 중
        proc.wait()

    fake_llm.answer = CODE_FIX  # 다시 불리면 다른 라벨이 저장되어 드러난다
    await run_burst(settings)   # 실행 중 표시가 만료(약 15초)되면 2번째 시도가 체크포인트에서 이어 감

    row = conn.execute(
        "SELECT status, attempts, request_type, service, category FROM jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row == ("done", 2, *QUESTION.values())
    assert fake_llm.calls == [] and fake_llm.embed_calls == []  # 분류도 질문 임베딩도 다시 부르지 않음
    usage = conn.execute("SELECT job_try, node FROM llm_usage WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
    assert usage == [(1, "classify"), (1, "retrieve")]


SLOW_WORKER = Path(__file__).with_name("slow_worker.py")


async def test_sigterm_lets_running_job_finish_without_using_a_try(settings, conn, tmp_path):
    job_id = make_job(conn)
    proc = subprocess.Popen([sys.executable, str(SLOW_WORKER)], env=worker_env(settings), cwd=tmp_path)
    try:
        await wait_for_status(conn, job_id, "running")
        proc.terminate()       # SIGTERM = docker compose stop
        proc.wait(timeout=30)  # 작업을 끝낸 뒤 스스로 종료
    finally:
        proc.kill()
        proc.wait()
    assert job_row(conn, job_id) == ("done", 1)  # 취소·재실행 없이 첫 시도로 완료


async def test_question_job_saves_answer_with_completion(settings, conn, fake_llm, indexed_samples, monkeypatch):
    monkeypatch.setattr(graph, "MIN_SIMILARITY", FAKE_MIN_SIMILARITY)
    fake_llm.answer = classify_then_cite(PAYMENT_5XX)
    job_id = make_job(conn, text=DECOY_QUESTION)
    await run_burst(settings)
    row = conn.execute("SELECT status, request_type, answer FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("done", "question", {"found": True, "answer": "가짜 답변입니다.", "citations": ["pm-2026-001#2"]})


async def test_job_that_is_not_a_question_has_no_answer(settings, conn, fake_llm):
    fake_llm.answer = CODE_FIX
    job_id = make_job(conn)
    await run_burst(settings)
    assert conn.execute("SELECT status, answer IS NULL FROM jobs WHERE id = %s", (job_id,)).fetchone() == ("done", True)


async def test_daily_quota_on_question_embedding_isolates_job_without_more_calls(settings, conn, fake_llm):
    fake_llm.embed = quota_error(DAILY_QUOTA)  # 분류는 성공, 질문 임베딩에서 일일 한도
    job_id = make_job(conn)
    await run_burst(settings)
    row = conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == ("failed", 1, "llm_quota_exhausted")
    assert len(fake_llm.embed_calls) == 1


async def test_known_incident_questions_find_their_documents_in_top_results(
    settings, conn, fake_llm, indexed_samples, make_event, monkeypatch
):
    # 가짜 임베딩이라 "배관"(입력 형식·꼬리표 선필터·단계 넓히기·정렬·저장)을 본다. 의미 품질은 test_live_rag.py
    monkeypatch.setattr(graph, "MIN_SIMILARITY", FAKE_MIN_SIMILARITY)
    queries = RAG_QUERIES["queries"]
    labels = {q["text"]: {"request_type": "question", "service": q["service"], "category": q["category"]} for q in queries}
    fake_llm.answer = lambda contents: cite_first(contents) if contents.startswith("<question>") else next(
        label for text, label in labels.items() if text in contents
    )
    with TestClient(create_app(settings)) as client:
        job_ids = {
            q["text"]: client.post(
                "/jobs", json=make_event(ts=f"1794381600.{i:06d}", text=q["text"]), headers=AUTH
            ).json()["job_id"]
            for i, q in enumerate(queries)
        }

    await run_burst(settings)

    assert conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall() == [("done", len(queries))]
    async with graph.checkpoint_pool(settings.database_url) as checkpoints:
        compiled = graph.build(checkpoints)
        for q in queries:
            values = (await compiled.aget_state(graph.thread_config(job_ids[q["text"]]))).values
            top = [chunk["doc_id"] for chunk in values["retrieval"]["chunks"]]  # 기준 이상, 최대 SEARCH_K개
            assert q["expected"] in top, (q["text"], top)
            answer = conn.execute("SELECT answer FROM jobs WHERE id = %s", (job_ids[q["text"]],)).fetchone()[0]
            assert answer["found"] is True and answer["citations"][0].startswith(q["expected"] + "#")
