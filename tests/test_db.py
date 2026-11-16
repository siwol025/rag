from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest

from slack_rag.db import apply_schema, claim_job, complete_job, create_job, fail_job, queued_job_ids

JOB = {
    "channel_id": "C0TARGET01",
    "message_ts": "1794381600.000100",
    "user_id": "U0HUMAN01",
    "message_text": "결제 API 5xx 급증",
}


def test_first_message_creates_job(conn):
    job_id, created, _ = create_job(conn, **JOB)
    assert created is True
    assert isinstance(job_id, UUID)


def test_same_channel_and_ts_returns_existing_job(conn):
    first_id, _, _ = create_job(conn, **JOB)
    second_id, created, _ = create_job(conn, **JOB | {"message_text": "재전송"})
    assert (second_id, created) == (first_id, False)


def test_same_ts_in_other_channel_is_another_job(conn):
    first_id, _, _ = create_job(conn, **JOB)
    other_id, created, _ = create_job(conn, **JOB | {"channel_id": "C0TARGET02"})
    assert created is True
    assert other_id != first_id


def test_concurrent_duplicates_create_exactly_one_job(database_url, conn):
    def insert(_):
        with psycopg.connect(database_url) as c:  # 블록이 끝날 때 커밋
            return create_job(c, **JOB)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(insert, range(8)))

    assert len({job_id for job_id, _, _ in results}) == 1
    assert sum(created for _, created, _ in results) == 1


def test_new_job_is_queued_and_duplicate_reports_current_status(conn):
    job_id, created, status = create_job(conn, **JOB)
    assert (created, status) == (True, "queued")
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (job_id,))
    assert create_job(conn, **JOB) == (job_id, False, "done")


F1_JOBS_TABLE = """
CREATE TABLE jobs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id   text        NOT NULL,
    message_ts   text        NOT NULL,
    user_id      text        NOT NULL,
    message_text text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_channel_id_message_ts_key UNIQUE (channel_id, message_ts)
)
"""


def test_schema_upgrade_marks_rows_saved_before_f2_as_queued(conn):
    conn.execute("DROP TABLE jobs")
    conn.execute(F1_JOBS_TABLE)  # F1 시절 테이블과 행
    conn.execute(
        "INSERT INTO jobs (channel_id, message_ts, user_id, message_text)"
        " VALUES ('C0TARGET01', '1794381600.000100', 'U0HUMAN01', 'F1 행')"
    )
    apply_schema(conn)
    row = conn.execute("SELECT status, attempts, started_at, finished_at, error FROM jobs").fetchone()
    assert row == ("queued", 0, None, None, None)


def job_row(conn, job_id):
    return conn.execute("SELECT status, attempts, error FROM jobs WHERE id = %s", (job_id,)).fetchone()


@pytest.mark.anyio
async def test_claim_takes_queued_job_only_once_per_try(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    waited = await claim_job(aconn, job_id, 1)
    assert waited is not None and waited >= timedelta(0)  # 접수 후 기다린 시간
    assert await claim_job(aconn, job_id, 1) is None      # 같은 시도로는 두 번(두 워커) 못 가져감
    assert job_row(conn, job_id) == ("running", 1, None)


@pytest.mark.anyio
async def test_later_try_takes_over_running_job_and_earlier_try_cannot_complete(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    await claim_job(aconn, job_id, 1)                      # 1번째 시도의 워커가 처리 중에 죽음
    assert await claim_job(aconn, job_id, 2) is not None   # arq가 다시 실행한 2번째 시도
    assert await complete_job(aconn, job_id, 1) is False   # 1번째가 늦게 끝나도 덮어쓰지 못함
    assert await complete_job(aconn, job_id, 2) is True
    assert job_row(conn, job_id) == ("done", 2, None)


@pytest.mark.anyio
@pytest.mark.parametrize("final_status", ["done", "failed"])
async def test_finished_job_cannot_be_claimed(conn, aconn, final_status):
    job_id, _, _ = create_job(conn, **JOB)
    conn.execute("UPDATE jobs SET status = %s WHERE id = %s", (final_status, job_id))
    assert await claim_job(aconn, job_id, 1) is None


@pytest.mark.anyio
async def test_fail_job_isolates_only_unfinished_job(conn, aconn):
    running_id, _, _ = create_job(conn, **JOB)
    done_id, _, _ = create_job(conn, **JOB | {"message_ts": "1794381700.000200"})
    await claim_job(aconn, running_id, 1)
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = %s", (done_id,))

    await fail_job(aconn, running_id, "max_tries_exceeded")
    await fail_job(aconn, done_id, "max_tries_exceeded")

    assert job_row(conn, running_id) == ("failed", 1, "max_tries_exceeded")
    assert job_row(conn, done_id) == ("done", 0, None)


@pytest.mark.anyio
async def test_queued_job_ids_lists_waiting_jobs_oldest_first(conn, aconn):
    first, _, _ = create_job(conn, **JOB)
    second, _, _ = create_job(conn, **JOB | {"message_ts": "1794381700.000200"})
    started, _, _ = create_job(conn, **JOB | {"message_ts": "1794381800.000300"})
    await claim_job(aconn, started, 1)
    assert await queued_job_ids(aconn) == [first, second]
