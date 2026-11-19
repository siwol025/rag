"""문서 색인(오프라인): 바뀐 조각만 임베딩해 pgvector에 넣는다(TO-014).

실행: python -m slack_rag.indexer          → 1회(CLI). 결과 JSON 출력, 멈췄거나 잠겨 있었으면 종료 코드 1
      python -m slack_rag.indexer --loop   → 시작 때 1회, 이후 INDEX_INTERVAL_S마다(compose indexer 컨테이너)
arq·Redis를 쓰지 않는다(04-rag-document-search-plan 3.6: TO-004 경계 그대로).
"""
import argparse
import asyncio
import hashlib
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from slack_rag import llm
from slack_rag.config import Settings
from slack_rag.db import (
    delete_documents,
    indexed_documents,
    record_index_usage,
    save_document,
    stored_embeddings,
    vector_literal,
)
from slack_rag.documents import Document, load_documents
from slack_rag.logs import configure_logging
from slack_rag.taxonomy import load_taxonomy

logger = logging.getLogger("slack_rag.indexer")

DOCS_DIR = Path("sample_docs")  # 색인 폴더(샘플 전용, TO-013). 실행 위치 기준: 로컬은 저장소 루트, 컨테이너는 /app
INDEX_INTERVAL_S = 300          # 주기 실행 간격(TO-014)
INDEX_LOCK_KEY = 72_001         # 색인 전용 advisory lock 번호. 한 번에 하나만 돈다(TO-014). 다른 용도로 쓰지 않는다
BATCH_MAX_CHARS = 20_000        # 임베딩 요청 하나의 입력 글자 합 상한: 분당 토큰 30K 아래(04-rag-document-search-plan 가정 C6)
BATCH_MAX_TEXTS = 100           # 임베딩 요청 하나의 텍스트 수 상한(04-rag-document-search-plan 가정 C6)


@dataclass
class IndexResult:
    embedded: int = 0           # 이번 회차에 임베딩한 텍스트 수
    saved: int = 0              # 새로 쓰거나 바꾼 문서 수
    deleted: int = 0            # 지운 문서 수(폴더에 없거나 형식이 깨짐)
    rejected: dict[str, str] = field(default_factory=dict)  # 거부한 파일 이름 → 짧은 이유
    stopped: str | None = None  # 임베딩 호출이 실패해 회차를 멈춘 이유(다음 회차가 이어 감)
    locked: bool = False        # 다른 색인이 돌고 있어 아무것도 하지 않음

    @property
    def changed(self) -> bool:
        return bool(self.saved or self.deleted or self.rejected or self.stopped)


def input_hash(embedding_model: str, embedding_input: str) -> str:
    """재사용 판단의 유일한 기준(04-rag-document-search-plan 2.4): 모델·차원·실제 입력이 같으면 같은 벡터다."""
    return hashlib.sha256(f"{embedding_model}\n{llm.EMBEDDING_DIMS}\n{embedding_input}".encode()).hexdigest()


async def index_once(
    conn: psycopg.AsyncConnection, client: Any, embedding_model: str, docs_dir: Path
) -> IndexResult:
    """한 회차: 폴더와 DB를 맞춘다. conn은 autocommit 연결(문서마다 짧은 트랜잭션, TO-005)."""
    result = IndexResult()
    cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (INDEX_LOCK_KEY,))
    if not (await cur.fetchone())[0]:  # 다른 색인(CLI 또는 주기 실행)이 도는 중: 기다리지 않는다
        result.locked = True
        return result
    try:
        await _sync(conn, client, embedding_model, docs_dir, result)
    finally:
        if not conn.broken:  # 연결이 끊겼으면 PostgreSQL이 잠금을 이미 풀었다. 해제 실패로 원래 예외를 가리지 않게
            await conn.execute("SELECT pg_advisory_unlock(%s)", (INDEX_LOCK_KEY,))
    return result


async def _sync(conn: psycopg.AsyncConnection, client: Any, model: str, docs_dir: Path, result: IndexResult) -> None:
    if not docs_dir.is_dir():  # 없는 폴더를 "문서 0개"로 읽으면 색인이 전부 지워진다(04-rag-document-search-plan 가정 C3)
        raise FileNotFoundError(f"docs folder not found: {docs_dir}")
    documents, result.rejected = load_documents(docs_dir, load_taxonomy())
    indexed = await indexed_documents(conn)
    gone = sorted(set(indexed) - {doc.doc_id for doc in documents})  # 지운 파일, 형식이 깨진 파일(04-rag-document-search-plan 가정 C4)
    if gone:
        await delete_documents(conn, gone)
        result.deleted = len(gone)
    changed = [doc for doc in documents if indexed.get(doc.doc_id) != (doc.content_hash, model)]
    texts = {doc.doc_id: [llm.document_input(doc.title, s.heading, s.text) for s in doc.sections] for doc in changed}
    vectors = await stored_embeddings(conn, [input_hash(model, t) for doc_texts in texts.values() for t in doc_texts])
    waiting = []  # (문서, 그 문서에 새로 필요한 입력)
    for doc in changed:
        missing = [t for t in texts[doc.doc_id] if input_hash(model, t) not in vectors]
        if missing:
            waiting.append((doc, missing))
        else:  # 재사용만으로 됨(예: 앞머리 꼬리표만 바뀜)
            await _save(conn, doc, texts[doc.doc_id], vectors, model, result)
    for batch in _batches(waiting):
        new = [t for t in dict.fromkeys(t for _, missing in batch for t in missing) if input_hash(model, t) not in vectors]
        if new:
            try:
                response = await llm.embed_documents(client, model, new)  # 트랜잭션 밖에서(TO-005)
            except Exception as exc:  # 429·거절·5xx: 기다리지 않고 멈춤, 다음 회차가 해시로 이어 감(TO-014)
                result.stopped = f"{type(exc).__name__}: {exc}"
                return
            input_tokens = llm.embedding_usage_of(response)["input_tokens"]
            await record_index_usage(conn, model=model, texts=len(new), input_tokens=input_tokens)  # 검증 전에 기록
            for text, vector in zip(new, llm.vectors_of(response, len(new))):
                vectors[input_hash(model, text)] = vector_literal(vector)
            result.embedded += len(new)
        for doc, _ in batch:
            await _save(conn, doc, texts[doc.doc_id], vectors, model, result)


def _batches(waiting: list[tuple[Document, list[str]]]) -> Iterator[list[tuple[Document, list[str]]]]:
    """문서 단위로 묶는다: 새 입력의 글자 합 ≤ BATCH_MAX_CHARS, 개수 ≤ BATCH_MAX_TEXTS(04-rag-document-search-plan 가정 C6)."""
    batch, chars, count = [], 0, 0
    for doc, missing in waiting:
        size = sum(len(text) for text in missing)
        if batch and (chars + size > BATCH_MAX_CHARS or count + len(missing) > BATCH_MAX_TEXTS):
            yield batch
            batch, chars, count = [], 0, 0
        batch.append((doc, missing))
        chars += size
        count += len(missing)
    if batch:
        yield batch


async def _save(
    conn: psycopg.AsyncConnection, doc: Document, texts: list[str], vectors: dict[str, str], model: str,
    result: IndexResult,
) -> None:
    hashes = [input_hash(model, text) for text in texts]
    await save_document(conn, doc, [(s, h, vectors[h]) for s, h in zip(doc.sections, hashes)], model)
    result.saved += 1


async def run_round(database_url: str, client: Any, embedding_model: str, docs_dir: Path) -> IndexResult:
    """회차 하나: 연결을 열고 색인한 뒤 닫는다(잠금도 함께 풀림). 바뀐 게 있을 때만 로그를 남긴다(TO-014)."""
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        result = await index_once(conn, client, embedding_model, docs_dir)
    if result.changed:
        logger.info("index_done", extra={"fields": asdict(result)})  # 파일 이름·개수·짧은 이유만
    return result


async def run_forever(settings: Settings) -> None:
    """시작 때 1회, 이후 INDEX_INTERVAL_S마다. 회차가 실패해도(API가 아직 스키마를 안 만듦 등) 다음 회차를 돈다."""
    client = llm.make_client(settings)
    try:
        while True:
            try:
                await run_round(settings.database_url, client, settings.embedding_model, DOCS_DIR)
            except Exception as exc:
                logger.error("index_failed", extra={"fields": {"error": repr(exc)}})
            await asyncio.sleep(INDEX_INTERVAL_S)
    finally:
        await client.aio.aclose()


async def run_once(settings: Settings) -> IndexResult:
    client = llm.make_client(settings)
    try:
        return await run_round(settings.database_url, client, settings.embedding_model, DOCS_DIR)
    finally:
        await client.aio.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m slack_rag.indexer", description="sample_docs/ 문서를 색인한다")
    parser.add_argument("--loop", action="store_true", help=f"시작 때 1회, 이후 {INDEX_INTERVAL_S}초마다(컨테이너용)")
    args = parser.parse_args(argv)
    configure_logging()
    settings = Settings()
    if args.loop:
        asyncio.run(run_forever(settings))
        return 0
    result = asyncio.run(run_once(settings))
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 1 if result.stopped or result.locked else 0


if __name__ == "__main__":
    sys.exit(main())
