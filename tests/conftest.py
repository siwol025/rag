import os

# Docker Desktop(macOS)에서 testcontainers의 정리용 Ryuk 컨테이너가 소켓 마운트 실패나
# 포트 매핑 경합으로 간헐적으로 실패함. 컨테이너는 아래 fixture의 with 블록이 정리하므로 Ryuk를 끈다.
# testcontainers 설정은 import 시점에 읽히므로 import 전에 지정한다.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from testcontainers.community.postgres import PostgresContainer  # noqa: E402
from testcontainers.community.redis import RedisContainer  # noqa: E402

from fakes import LLM_SETTINGS, QUESTION, SAMPLE_DOCS, FakeGenai  # noqa: E402
from slack_rag import indexer  # noqa: E402
from slack_rag.db import apply_schema  # noqa: E402

POSTGRES_IMAGE = "pgvector/pgvector:0.8.7-pg18-trixie"  # pgvector 포함(TO-012). docker-compose.yml과 같은 이미지
REDIS_IMAGE = "redis:8.10.3-alpine"  # docker-compose.yml과 같은 이미지


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
        conn.execute("TRUNCATE jobs, llm_usage, documents, chunks, index_usage")  # chunks는 documents를 참조
        yield conn


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"  # arq와 psycopg 비동기 풀은 asyncio에서 돈다


@pytest.fixture
async def aconn(database_url, conn):  # conn이 jobs를 비움
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as aconn:
        yield aconn


@pytest.fixture
async def indexed_samples(database_url, conn):  # conn이 표를 먼저 비움
    """sample_docs/ 12개를 가짜 임베딩으로 색인한다(48조각, 네트워크 없음, 수십 ms)."""
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as aconn:
        result = await indexer.index_once(aconn, FakeGenai(QUESTION), LLM_SETTINGS["embedding_model"], SAMPLE_DOCS)
    assert (result.saved, result.rejected) == (12, {})


def redis_url_of(container: RedisContainer) -> str:
    return f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture
def redis_url(redis_container):
    client = redis_container.get_client()
    client.flushdb()
    client.close()
    return redis_url_of(redis_container)


@pytest.fixture
def stoppable_redis():
    """테스트 중에 멈출 Redis. 세션 Redis와 따로 띄운다. (컨테이너, URL)."""
    with RedisContainer(REDIS_IMAGE) as container:
        yield container, redis_url_of(container)
