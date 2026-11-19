from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from slack_rag.documents import Document, Section

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
    conn: psycopg.AsyncConnection,
    job_id: UUID,
    job_try: int,
    classification: dict[str, str],
    answer: dict[str, Any] | None = None,
) -> bool:
    """처리 중인 작업을 완료로 바꾸며 분류·답변도 함께 쓴다. 그 사이 더 나중 시도가 가져갔으면 쓰지 않고 False.

    answer는 질의응답만(나머지는 None → SQL NULL. Jsonb(None)은 JSON null이 되므로 감싸지 않는다).
    """
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'done', finished_at = now(),
            request_type = %(request_type)s, service = %(service)s, category = %(category)s, answer = %(answer)s
        WHERE id = %(job_id)s AND status = 'running' AND attempts = %(job_try)s
        """,
        {"job_id": job_id, "job_try": job_try, **classification, "answer": None if answer is None else Jsonb(answer)},
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


def vector_literal(values: list[float]) -> str:
    """pgvector 텍스트 입력 '[0.1,-0.2,…]'. SQL에서 %s::vector로 바꾼다(파이썬 pgvector 패키지 없이, 04-rag-document-search-plan 3.7)."""
    return "[" + ",".join(map(str, values)) + "]"


async def indexed_documents(conn: psycopg.AsyncConnection) -> dict[str, tuple[str, str | None]]:
    """색인된 문서마다 (content_hash, 조각의 embedding_model). 색인이 문서를 건너뛸지 판단한다(04-rag-document-search-plan 3.5)."""
    cur = await conn.execute(
        """
        SELECT d.doc_id, d.content_hash, min(c.embedding_model)
        FROM documents d LEFT JOIN chunks c USING (doc_id)
        GROUP BY d.doc_id, d.content_hash
        """
    )
    return {doc_id: (content_hash, model) for doc_id, content_hash, model in await cur.fetchall()}


async def delete_documents(conn: psycopg.AsyncConnection, doc_ids: list[str]) -> None:
    """문서를 지운다. 조각은 ON DELETE CASCADE로 함께 지워진다."""
    await conn.execute("DELETE FROM documents WHERE doc_id = ANY(%s)", (doc_ids,))


async def stored_embeddings(conn: psycopg.AsyncConnection, input_hashes: list[str]) -> dict[str, str]:
    """입력 해시 → 이미 저장된 벡터(텍스트 그대로). 같은 입력은 다시 임베딩하지 않는다(TO-014)."""
    cur = await conn.execute(
        "SELECT DISTINCT ON (input_hash) input_hash, embedding::text FROM chunks WHERE input_hash = ANY(%s)",
        (input_hashes,),
    )
    return dict(await cur.fetchall())


async def save_document(
    conn: psycopg.AsyncConnection, doc: Document, chunks: list[tuple[Section, str, str]], embedding_model: str
) -> None:
    """문서 하나를 한 트랜잭션으로 바꿔 넣는다: 문서 행 upsert + 조각 전부 다시 쓰기(TO-014).

    chunks = (섹션, 입력 해시, 벡터 텍스트). 임베딩은 이 함수 밖(트랜잭션 밖)에서 끝낸다(TO-005).
    """
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO documents (doc_id, title, service, category, doc_type, doc_date, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (doc_id) DO UPDATE SET
                title = EXCLUDED.title, service = EXCLUDED.service, category = EXCLUDED.category,
                doc_type = EXCLUDED.doc_type, doc_date = EXCLUDED.doc_date,
                content_hash = EXCLUDED.content_hash, indexed_at = now()
            """,
            (doc.doc_id, doc.title, doc.service, doc.category, doc.doc_type, doc.date, doc.content_hash),
        )
        await conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc.doc_id,))
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO chunks (doc_id, ordinal, heading, text, input_hash, embedding_model, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                """,
                [
                    (doc.doc_id, section.ordinal, section.heading, section.text, input_hash, embedding_model, vector)
                    for section, input_hash, vector in chunks
                ],
            )


async def record_index_usage(conn: psycopg.AsyncConnection, *, model: str, texts: int, input_tokens: int) -> None:
    """색인 임베딩 호출 1번을 기록한다(04-rag-document-search-plan 가정 C2). 작업(jobs)과 무관하므로 llm_usage와 따로 둔다."""
    await conn.execute(
        "INSERT INTO index_usage (model, texts, input_tokens) VALUES (%s, %s, %s)", (model, texts, input_tokens)
    )


async def search_chunks(
    conn: psycopg.AsyncConnection,
    query_vector: list[float],
    embedding_model: str,
    *,
    k: int,
    service: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """코사인 거리순 정확 검색(근사 인덱스 없음, TO-012). service·category가 None이면 그 조건을 걸지 않는다(TO-015).

    같은 임베딩 모델로 만든 조각만 비교한다(모델을 바꾸는 중에 섞이지 않게).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT c.doc_id || '#' || c.ordinal AS chunk_id, c.doc_id, d.title, d.doc_date::text AS date,
                   c.heading, c.text, 1 - (c.embedding <=> %(q)s::vector) AS similarity
            FROM chunks c JOIN documents d USING (doc_id)
            WHERE c.embedding_model = %(model)s
              AND (%(service)s::text IS NULL OR d.service = %(service)s)
              AND (%(category)s::text IS NULL OR d.category = %(category)s)
            ORDER BY c.embedding <=> %(q)s::vector
            LIMIT %(k)s
            """,
            {
                "q": vector_literal(query_vector),
                "model": embedding_model,
                "service": service,
                "category": category,
                "k": k,
            },
        )
        return await cur.fetchall()
