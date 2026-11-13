from pathlib import Path
from uuid import UUID

import psycopg

SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_SQL)


def create_job(
    conn: psycopg.Connection,
    *,
    channel_id: str,
    message_ts: str,
    user_id: str,
    message_text: str,
) -> tuple[UUID, bool]:
    """작업을 만들고 (job_id, 새로 만들었는지)를 돌려준다. 같은 (channel_id, message_ts)면 기존 작업."""
    row = conn.execute(
        """
        INSERT INTO jobs (channel_id, message_ts, user_id, message_text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel_id, message_ts) DO NOTHING
        RETURNING id
        """,
        (channel_id, message_ts, user_id, message_text),
    ).fetchone()
    if row is not None:
        return row[0], True
    row = conn.execute(
        "SELECT id FROM jobs WHERE channel_id = %s AND message_ts = %s",
        (channel_id, message_ts),
    ).fetchone()
    return row[0], False
