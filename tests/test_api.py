import logging

import psycopg
import pytest
from arq.constants import default_queue_name
from fastapi.testclient import TestClient

from fakes import LLM_SETTINGS
from slack_rag.config import Settings
from slack_rag.db import create_job
from slack_rag.main import create_app

API_KEY = "test-api-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def client(database_url, redis_url, conn):  # conn이 jobs를, redis_url이 Redis를 비움
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
        **LLM_SETTINGS,
    )
    with TestClient(create_app(settings)) as client:  # with로 lifespan(풀, 스키마) 실행
        yield client


def count_jobs(conn) -> int:
    return conn.execute("SELECT count(*) FROM jobs").fetchone()[0]


def test_new_message_returns_202_with_job_id_and_saves_job(client, conn, make_event):
    res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 202
    assert res.json()["status"] == "accepted"
    assert res.json()["job_id"]
    assert count_jobs(conn) == 1


def test_same_message_again_returns_same_job_without_new_record(client, conn, make_event):
    first = client.post("/jobs", json=make_event(), headers=AUTH).json()
    res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "job_id": first["job_id"]}
    assert count_jobs(conn) == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ts": "1794381700.000200", "thread_ts": "1794381600.000100"}, "thread_reply"),
        ({"subtype": "message_changed"}, "has_subtype"),
        ({"subtype": "message_deleted"}, "has_subtype"),
        ({"bot_id": "B0OURBOT01"}, "not_human"),
        ({"channel": "C0OTHER001"}, "channel_not_targeted"),
    ],
)
def test_filtered_event_returns_200_ignored_without_record(client, conn, make_event, overrides, reason):
    res = client.post("/jobs", json=make_event(**overrides), headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"status": "ignored", "reason": reason}
    assert count_jobs(conn) == 0


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key"}])
def test_rejects_missing_or_wrong_api_key(client, conn, make_event, headers):
    res = client.post("/jobs", json=make_event(), headers=headers)
    assert res.status_code == 401
    assert count_jobs(conn) == 0


def test_invalid_payload_returns_422(client, make_event):
    payload = make_event()
    del payload["ts"]
    assert client.post("/jobs", json=payload, headers=AUTH).status_code == 422


def test_accepted_job_is_logged_with_its_job_id(client, make_event, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = client.post("/jobs", json=make_event(), headers=AUTH).json()["job_id"]
    logged = [getattr(r, "fields", {}) for r in caplog.records]
    assert any(
        str(f.get("job_id")) == job_id and f.get("message_ts") == "1794381600.000100" for f in logged
    )


def arq_queue_ids(redis_container) -> list[str]:
    """arq 기본 큐(정렬 집합)에 있는 작업 ID."""
    client = redis_container.get_client()
    try:
        return [member.decode() for member in client.zrange(default_queue_name, 0, -1)]
    finally:
        client.close()


def save_job(conn, event) -> str:
    """앞선 요청이 DB 저장 뒤 적재에 실패(503)한 상황을 만든다."""
    job_id, _, _ = create_job(
        conn,
        channel_id=event["channel"],
        message_ts=event["ts"],
        user_id=event["user"],
        message_text=event["text"],
    )
    return str(job_id)


def test_new_job_is_enqueued_by_its_job_id(client, redis_container, make_event):
    job_id = client.post("/jobs", json=make_event(), headers=AUTH).json()["job_id"]
    assert arq_queue_ids(redis_container) == [job_id]


def test_retry_of_saved_but_not_enqueued_job_enqueues_it(client, conn, redis_container, make_event):
    job_id = save_job(conn, make_event())
    res = client.post("/jobs", json=make_event(), headers=AUTH)  # n8n 재시도
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "job_id": job_id}
    assert arq_queue_ids(redis_container) == [job_id]


@pytest.mark.parametrize("job_status", ["running", "done", "failed"])
def test_duplicate_of_started_job_is_not_enqueued(client, conn, redis_container, make_event, job_status):
    job_id = save_job(conn, make_event())
    conn.execute("UPDATE jobs SET status = %s WHERE id = %s", (job_status, job_id))
    res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 202
    assert res.json() == {"status": "duplicate", "job_id": job_id}
    assert arq_queue_ids(redis_container) == []


def test_returns_503_and_keeps_job_queued_when_queue_is_down(database_url, conn, stoppable_redis, make_event):
    container, redis_url = stoppable_redis
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
        **LLM_SETTINGS,  # 필수 키
    )
    with TestClient(create_app(settings)) as client:
        container.get_wrapped_container().stop()  # 앱이 뜬 뒤 Redis 중단
        res = client.post("/jobs", json=make_event(), headers=AUTH)
    assert res.status_code == 503
    assert res.json() == {"detail": "queue unavailable"}
    assert conn.execute("SELECT status FROM jobs").fetchall() == [("queued",)]


def test_app_start_creates_checkpoint_and_usage_tables_in_fresh_database(database_url, redis_url, conn):
    conn.execute("DROP DATABASE IF EXISTS fresh_db WITH (FORCE)")
    conn.execute("CREATE DATABASE fresh_db")
    fresh_url = database_url.rsplit("/", 1)[0] + "/fresh_db"
    settings = Settings(
        _env_file=None,
        database_url=fresh_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
        **LLM_SETTINGS,
    )
    with TestClient(create_app(settings)):  # lifespan이 스키마를 처음 적용(CREATE INDEX CONCURRENTLY 포함)
        pass
    with psycopg.connect(fresh_url) as fresh:
        tables = fresh.execute(
            "SELECT count(*) FROM pg_tables"
            " WHERE tablename IN ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'llm_usage')"
        ).fetchone()[0]
        fresh.execute("SELECT request_type, service, category FROM jobs LIMIT 0")  # 분류 컬럼이 있어야 함
    conn.execute("DROP DATABASE fresh_db WITH (FORCE)")
    assert tables == 4


def test_app_start_creates_vector_extension_and_document_tables_in_fresh_database(database_url, redis_url, conn):
    conn.execute("DROP DATABASE IF EXISTS fresh_rag_db WITH (FORCE)")
    conn.execute("CREATE DATABASE fresh_rag_db")
    fresh_url = database_url.rsplit("/", 1)[0] + "/fresh_rag_db"
    settings = Settings(
        _env_file=None,
        database_url=fresh_url,
        redis_url=redis_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
        **LLM_SETTINGS,
    )
    for _ in range(2):  # 두 번째 시작(이미 다 있음)도 통과해야 함(IF NOT EXISTS, TO-005)
        with TestClient(create_app(settings)):
            pass
    with psycopg.connect(fresh_url) as fresh:
        extension = fresh.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        tables = fresh.execute(
            "SELECT count(*) FROM pg_tables WHERE tablename IN ('documents', 'chunks', 'index_usage')"
        ).fetchone()[0]
        column = fresh.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
            " WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        ).fetchone()[0]
        fresh.execute("SELECT answer FROM jobs LIMIT 0")  # 답변 컬럼이 있어야 함
    conn.execute("DROP DATABASE fresh_rag_db WITH (FORCE)")
    assert extension is not None
    assert tables == 3
    assert column == "vector(768)"  # llm.EMBEDDING_DIMS와 같은 값
