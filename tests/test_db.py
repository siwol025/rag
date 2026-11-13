from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import psycopg

from slack_rag.db import create_job

JOB = {
    "channel_id": "C0TARGET01",
    "message_ts": "1794381600.000100",
    "user_id": "U0HUMAN01",
    "message_text": "결제 API 5xx 급증",
}


def test_first_message_creates_job(conn):
    job_id, created = create_job(conn, **JOB)
    assert created is True
    assert isinstance(job_id, UUID)


def test_same_channel_and_ts_returns_existing_job(conn):
    first_id, _ = create_job(conn, **JOB)
    second_id, created = create_job(conn, **JOB | {"message_text": "재전송"})
    assert (second_id, created) == (first_id, False)


def test_same_ts_in_other_channel_is_another_job(conn):
    first_id, _ = create_job(conn, **JOB)
    other_id, created = create_job(conn, **JOB | {"channel_id": "C0TARGET02"})
    assert created is True
    assert other_id != first_id


def test_concurrent_duplicates_create_exactly_one_job(database_url, conn):
    def insert(_):
        with psycopg.connect(database_url) as c:  # 블록이 끝날 때 커밋
            return create_job(c, **JOB)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(insert, range(8)))

    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
