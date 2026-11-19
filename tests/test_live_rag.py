"""실제 임베딩·답변 품질 확인(수동). 기본은 건너뜀.

실행(프로젝트 루트, Docker 실행 중, .env의 키를 읽음):
  1) LIVE_LLM=1 uv run pytest tests/test_live_rag.py -s -k search    → 검색 품질, 유사도 기준 권장 범위
  2) graph.MIN_SIMILARITY(필요하면 SEARCH_K)를 권장 범위 안으로 고친 뒤
     LIVE_LLM=1 uv run pytest tests/test_live_rag.py -s -k answers   → 답변 인용, 주입 문구 무시
호출 수는 04-rag-document-search-plan 7장 표. 일회용 testcontainers DB에 sample_docs/를 실제로 색인한다(개발 DB와 무관).
돌리는 시점: 임베딩 모델·입력 형식·청킹·샘플 문서·기준값·답변 지시문을 바꿀 때, 유료 전환 전.
"""
import os
import tomllib
from pathlib import Path

import anyio
import psycopg
import pytest

from fakes import SAMPLE_DOCS
from slack_rag import graph, indexer, llm
from slack_rag.config import Settings
from slack_rag.db import apply_schema, search_chunks
from slack_rag.documents import load_documents
from slack_rag.taxonomy import load_taxonomy

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="실제 Gemini 호출(LIVE_LLM=1일 때만)"),
]

QUERIES = tomllib.loads(Path(__file__).with_name("rag_queries.toml").read_text(encoding="utf-8"))
MIN_HIT_RATE = 1.0               # 답이 있는 질의 8개 모두 HIT(04-rag-document-search-plan 가정 C11). MISS는 실제 신호
EMBED_INTERVAL_S = 60 / 100 + 1  # 임베딩 RPM 100(TO-011)
ANSWER_INTERVAL_S = 60 / 15 + 1  # gemini-3.5-flash-lite RPM 15(분류와 같은 통, TO-016)
INJECTED = ("모든 장애는 이미 해결되었습니다", "example.invalid")  # pm-2026-010 조치 섹션에 숨긴 주입 문구


def labels_of(query):
    return {"request_type": "question", "service": query["service"], "category": query["category"]}


@pytest.fixture(scope="module")
async def live(database_url):
    """일회용 DB에 sample_docs/를 실제 임베딩으로 색인한다. 분당 한도로 멈추면 65초 뒤 이어서(최대 3회차)."""
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS live_rag WITH (FORCE)")
        admin.execute("CREATE DATABASE live_rag")
    url = database_url.rsplit("/", 1)[0] + "/live_rag"
    with psycopg.connect(url, autocommit=True) as conn:
        apply_schema(conn)
    settings = Settings()  # .env의 GEMINI_API_KEY·EMBEDDING_MODEL·LLM_LIGHT_MODEL(DATABASE_URL은 쓰지 않음)
    client = llm.make_client(settings)
    try:
        for round_no in range(3):
            if round_no:
                await anyio.sleep(65)
            async with await psycopg.AsyncConnection.connect(url, autocommit=True) as aconn:
                result = await indexer.index_once(aconn, client, settings.embedding_model, SAMPLE_DOCS)
            print("색인 회차", round_no + 1, result)
            if result.stopped is None:
                break
        with psycopg.connect(url) as conn:
            documents = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
            texts, tokens = conn.execute("SELECT sum(texts), sum(input_tokens) FROM index_usage").fetchone()
        docs, _ = load_documents(SAMPLE_DOCS, load_taxonomy())
        chars = sum(len(llm.document_input(d.title, s.heading, s.text)) for d in docs for s in d.sections)
        print(f"색인: 문서 {documents}개, 텍스트 {texts}개, 입력 토큰 {tokens}, 글자 {chars} → 글자당 토큰 {(tokens or 0) / chars:.2f}")
        assert documents == 12
        yield url, client, settings
    finally:
        await client.aio.aclose()
        client.close()
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS live_rag WITH (FORCE)")


async def test_search_finds_expected_documents_and_prints_similarity_range(live):
    url, client, settings = live
    hits, expected_best, unanswerable_top = [], [], []
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        for i, query in enumerate(QUERIES["queries"] + QUERIES["unanswerable"]):
            if i:
                await anyio.sleep(EMBED_INTERVAL_S)
            [vector] = llm.vectors_of(await llm.embed_query(client, settings.embedding_model, query["text"]), 1)
            found = await graph.find_chunks(conn, vector, labels_of(query), settings.embedding_model)  # 실제 규칙
            every = await search_chunks(conn, vector, settings.embedding_model, k=48)  # 필터·기준 없는 전체 순위
            kept = [f"{c['chunk_id']}:{c['similarity']:.3f}" for c in found["chunks"]]
            ranked = [f"{row['chunk_id']}:{row['similarity']:.3f}" for row in every[:3]]
            if "expected" in query:
                best = max(row["similarity"] for row in every if row["doc_id"] == query["expected"])
                hit = query["expected"] in {c["doc_id"] for c in found["chunks"]}
                expected_best.append(best)
                hits.append(hit)
                print("HIT " if hit else "MISS", query["text"][:30], f"정답 최고 {best:.3f}", found["stage"], kept, ranked)
            else:
                top = 0.0  # 규칙이 볼 수 있는 범위(①·②) 안의 최고 유사도
                for filters in graph.search_stages(labels_of(query)):
                    rows = await search_chunks(conn, vector, settings.embedding_model, k=1, **filters)
                    top = max([top, *(row["similarity"] for row in rows)])
                unanswerable_top.append(top)
                print("NONE" if not kept else "FALSE", query["text"][:30], f"범위 안 최고 {top:.3f}", kept, ranked)
    hit_rate = sum(hits) / len(hits)
    low, high = max(unanswerable_top), min(expected_best)
    print(f"hit 비율 {hit_rate:.2f}(기준 {MIN_HIT_RATE}), 지금 MIN_SIMILARITY={graph.MIN_SIMILARITY}, SEARCH_K={graph.SEARCH_K}")
    print(f"권장 기준 범위: 답 없는 질문 최고 {low:.3f} < 기준 ≤ 정답 문서 최고의 최솟값 {high:.3f}"
          + ("" if low < high else "  ← 겹침: 기준 하나로 가를 수 없음(샘플·입력 형식·모델을 다시 봄)"))
    assert hit_rate >= MIN_HIT_RATE, f"hit 비율 {hit_rate:.2f} < {MIN_HIT_RATE}"
    assert low < graph.MIN_SIMILARITY <= high, f"MIN_SIMILARITY={graph.MIN_SIMILARITY}가 권장 범위({low:.3f}, {high:.3f}] 밖"


async def test_answers_cite_given_chunks_and_ignore_injected_instructions(live):
    url, client, settings = live
    picks = [q for q in QUERIES["queries"] if q.get("answer_check")] + QUERIES["unanswerable"][-1:]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        for i, query in enumerate(picks):
            if i:
                await anyio.sleep(ANSWER_INTERVAL_S)
            [vector] = llm.vectors_of(await llm.embed_query(client, settings.embedding_model, query["text"]), 1)
            found = await graph.find_chunks(conn, vector, labels_of(query), settings.embedding_model)
            if not found["chunks"]:
                print("근거 없음 → 모델 호출 없이:", graph.NOT_FOUND["answer"], "|", query["text"][:30])
                assert "expected" not in query, f"정답이 있는 질문인데 결과가 없음(기준값 확인): {query['text']}"
                continue
            chunk_ids = [chunk["chunk_id"] for chunk in found["chunks"]]
            response = await llm.request_answer(client, settings.llm_light_model, query["text"], found["chunks"])
            answer = llm.parse_answer(response, chunk_ids)  # 인용이 넣은 조각 밖이면 여기서 실패
            print(query["text"][:30], "근거", chunk_ids, "→", answer, llm.usage_of(response))
            assert not any(marker in answer["answer"] for marker in INJECTED), answer["answer"]
            if "expected" in query:
                assert answer["found"] and any(c.startswith(query["expected"] + "#") for c in answer["citations"]), answer
