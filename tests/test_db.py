from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest

from fakes import CODE_FIX, QUESTION
from slack_rag.db import (
    apply_schema,
    claim_job,
    complete_job,
    create_job,
    fail_job,
    load_job_text,
    queued_job_ids,
    record_llm_usage,
)

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


JOBS_TABLE_BEFORE_STATUS = """
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


def test_schema_upgrade_marks_rows_saved_before_status_columns_as_queued(conn):
    conn.execute("DROP TABLE jobs")
    conn.execute(JOBS_TABLE_BEFORE_STATUS)  # 상태 컬럼이 생기기 전 테이블과 행
    conn.execute(
        "INSERT INTO jobs (channel_id, message_ts, user_id, message_text)"
        " VALUES ('C0TARGET01', '1794381600.000100', 'U0HUMAN01', '예전 행')"
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
    await claim_job(aconn, job_id, 1)                                # 1번째 시도의 워커가 처리 중에 죽음
    assert await claim_job(aconn, job_id, 2) is not None             # arq가 다시 실행한 2번째 시도
    assert await complete_job(aconn, job_id, 1, QUESTION) is False   # 1번째가 늦게 끝나도 덮어쓰지 못함
    assert await complete_job(aconn, job_id, 2, QUESTION) is True
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

    assert await fail_job(aconn, running_id, "max_tries_exceeded") is True  # 실제로 바꿈
    assert await fail_job(aconn, done_id, "max_tries_exceeded") is False    # 이미 끝난 작업은 그대로

    assert job_row(conn, running_id) == ("failed", 1, "max_tries_exceeded")
    assert job_row(conn, done_id) == ("done", 0, None)


@pytest.mark.anyio
async def test_queued_job_ids_lists_waiting_jobs_oldest_first(conn, aconn):
    first, _, _ = create_job(conn, **JOB)
    second, _, _ = create_job(conn, **JOB | {"message_ts": "1794381700.000200"})
    started, _, _ = create_job(conn, **JOB | {"message_ts": "1794381800.000300"})
    await claim_job(aconn, started, 1)
    assert await queued_job_ids(aconn) == [first, second]


USAGE = {"input_tokens": 120, "output_tokens": 15, "thoughts_tokens": 4, "cached_tokens": 0}


@pytest.mark.anyio
async def test_record_llm_usage_appends_a_row_per_call(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    for job_try in (1, 2):  # 같은 노드를 다시 불렀으면(재시도) 실제 호출마다 한 줄
        await record_llm_usage(aconn, job_id=job_id, job_try=job_try, node="classify", model="fake-light-model", **USAGE)
    rows = conn.execute(
        "SELECT job_try, node, model, input_tokens, output_tokens, thoughts_tokens, cached_tokens"
        " FROM llm_usage WHERE job_id = %s ORDER BY id",
        (job_id,),
    ).fetchall()
    assert rows == [(1, "classify", "fake-light-model", 120, 15, 4, 0), (2, "classify", "fake-light-model", 120, 15, 4, 0)]


@pytest.mark.anyio
async def test_load_job_text_reads_message_body(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    assert await load_job_text(aconn, job_id) == JOB["message_text"]


@pytest.mark.anyio
async def test_classification_is_saved_only_by_current_try(conn, aconn):
    job_id, _, _ = create_job(conn, **JOB)
    await claim_job(aconn, job_id, 1)
    await claim_job(aconn, job_id, 2)
    await complete_job(aconn, job_id, 2, QUESTION)
    await complete_job(aconn, job_id, 1, CODE_FIX)  # 늦게 끝난 앞 시도의 결과
    row = conn.execute("SELECT request_type, service, category FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert row == tuple(QUESTION.values())


@pytest.mark.anyio
async def test_answer_is_saved_with_completion_only_by_current_try(conn, aconn):
    answer = {"found": True, "answer": "있었습니다.", "citations": ["pm-2026-001#1"]}
    job_id, _, _ = create_job(conn, **JOB)
    other_id, _, _ = create_job(conn, **JOB | {"message_ts": "1794381700.000200"})
    await claim_job(aconn, job_id, 1)
    await claim_job(aconn, job_id, 2)
    await claim_job(aconn, other_id, 1)
    assert await complete_job(aconn, job_id, 2, QUESTION, answer) is True
    assert await complete_job(aconn, job_id, 1, QUESTION, {**answer, "answer": "늦게 끝난 앞 시도"}) is False
    assert await complete_job(aconn, other_id, 1, CODE_FIX) is True  # 답변이 없는 작업
    assert conn.execute("SELECT answer FROM jobs WHERE id = %s", (job_id,)).fetchone() == (answer,)
    assert conn.execute("SELECT answer IS NULL FROM jobs WHERE id = %s", (other_id,)).fetchone() == (True,)  # JSON null 아님
