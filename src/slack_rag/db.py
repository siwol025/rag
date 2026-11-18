from datetime import timedelta
from pathlib import Path
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def apply_schema(conn: psycopg.Connection) -> None:
    """앱 테이블(schema.sql)과 LangGraph 체크포인트 테이블을 만든다.

    conn은 autocommit이어야 한다: 체크포인트 인덱스가 CREATE INDEX CONCURRENTLY(트랜잭션 안 불가, 3.2).
    """
    conn.execute(SCHEMA_SQL)
    PostgresSaver(conn).setup()  # checkpoint_migrations를 보고 아직 안 한 마이그레이션만 실행


def create_job(
    conn: psycopg.Connection,
    *,
    channel_id: str,
    message_ts: str,
    user_id: str,
    message_text: str,
) -> tuple[UUID, bool, str]:
    """작업을 만들고 (job_id, 새로 만들었는지, 현재 상태)를 돌려준다. 같은 (channel_id, message_ts)면 기존 작업."""
    row = conn.execute(
        """
        INSERT INTO jobs (channel_id, message_ts, user_id, message_text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel_id, message_ts) DO NOTHING
        RETURNING id, status
        """,
        (channel_id, message_ts, user_id, message_text),
    ).fetchone()
    if row is not None:
        return row[0], True, row[1]
    row = conn.execute(
        "SELECT id, status FROM jobs WHERE channel_id = %s AND message_ts = %s",
        (channel_id, message_ts),
    ).fetchone()
    return row[0], False, row[1]


async def claim_job(conn: psycopg.AsyncConnection, job_id: UUID, job_try: int) -> timedelta | None:
    """작업을 처리 중으로 바꾸고 접수 후 기다린 시간을 돌려준다. 가져갈 수 없으면 None.

    대기 중인 작업, 또는 더 앞선 시도(죽었거나 종료된 워커)가 처리 중으로 둔 작업만 가져간다(3.4).
    """
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'running', attempts = %(job_try)s, started_at = now()
        WHERE id = %(job_id)s
          AND (status = 'queued' OR (status = 'running' AND attempts < %(job_try)s))
        RETURNING now() - created_at
        """,
        {"job_id": job_id, "job_try": job_try},
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def complete_job(
    conn: psycopg.AsyncConnection, job_id: UUID, job_try: int, classification: dict[str, str]
) -> bool:
    """처리 중인 작업을 완료로 바꾸며 분류도 함께 쓴다. 그 사이 더 나중 시도가 가져갔으면 둘 다 쓰지 않고 False."""
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'done', finished_at = now(),
            request_type = %(request_type)s, service = %(service)s, category = %(category)s
        WHERE id = %(job_id)s AND status = 'running' AND attempts = %(job_try)s
        """,
        {"job_id": job_id, "job_try": job_try, **classification},
    )
    return cur.rowcount == 1


async def fail_job(conn: psycopg.AsyncConnection, job_id: UUID, error: str) -> bool:
    """끝나지 않은 작업을 실패로 격리하고 True. 이미 끝난 작업은 그대로 두고 False."""
    cur = await conn.execute(
        """
        UPDATE jobs SET status = 'failed', error = %s, finished_at = now()
        WHERE id = %s AND status IN ('queued', 'running')
        """,
        (error, job_id),
    )
    return cur.rowcount == 1


async def queued_job_ids(conn: psycopg.AsyncConnection) -> list[UUID]:
    """아직 어느 워커도 가져가지 않은 작업(오래된 순)."""
    cur = await conn.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at")
    return [row[0] for row in await cur.fetchall()]


async def load_job_text(conn: psycopg.AsyncConnection, job_id: UUID) -> str:
    """그래프의 첫 입력. 큐에는 job_id만 있으므로 본문은 DB에서 읽는다(TO-001)."""
    cur = await conn.execute("SELECT message_text FROM jobs WHERE id = %s", (job_id,))
    return (await cur.fetchone())[0]


async def record_llm_usage(
    conn: psycopg.AsyncConnection,
    *,
    job_id: UUID,
    job_try: int,
    node: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int,
    cached_tokens: int,
) -> None:
    """LLM 호출 1번을 기록한다. 비용은 저장하지 않고 조회 때 단가로 환산한다(3.8)."""
    await conn.execute(
        """
        INSERT INTO llm_usage
            (job_id, job_try, node, model, input_tokens, output_tokens, thoughts_tokens, cached_tokens)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (job_id, job_try, node, model, input_tokens, output_tokens, thoughts_tokens, cached_tokens),
    )
