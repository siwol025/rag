import logging

import pytest
from fastapi.testclient import TestClient

from slack_rag.config import Settings
from slack_rag.main import create_app

API_KEY = "test-api-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def client(database_url, conn):  # conn이 jobs를 비움
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        jobs_api_key=API_KEY,
        slack_target_channel_ids={"C0TARGET01"},
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
