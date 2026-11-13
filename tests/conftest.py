import os

# Docker Desktop(macOS)에서 testcontainers의 정리용 Ryuk 컨테이너가 소켓 마운트 실패나
# 포트 매핑 경합으로 간헐적으로 실패함. 컨테이너는 아래 fixture의 with 블록이 정리하므로 Ryuk를 끈다.
# testcontainers 설정은 import 시점에 읽히므로 import 전에 지정한다.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from testcontainers.community.postgres import PostgresContainer  # noqa: E402

from slack_rag.db import apply_schema  # noqa: E402

POSTGRES_IMAGE = "postgres:18.6-alpine"  # docker-compose.yml과 같은 이미지


@pytest.fixture
def make_event():
    def _make(**overrides):
        return {
            "type": "message",
            "channel": "C0TARGET01",
            "user": "U0HUMAN01",
            "text": "결제 API 5xx 급증",
            "ts": "1794381600.000100",
            "event_ts": "1794381600.000100",
            "channel_type": "channel",
        } | overrides

    return _make


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as postgres:
        url = postgres.get_connection_url()  # driver=None → "postgresql://…"
        with psycopg.connect(url, autocommit=True) as conn:
            apply_schema(conn)
        yield url


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE jobs")
        yield conn
