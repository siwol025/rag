"""작업 처리 그래프(LangGraph): 분류 → 유형별 경로(질의응답은 문서 검색 → 답변). 노드 사이 상태는 PostgreSQL 체크포인트에 남는다."""
import logging
import time
from dataclasses import dataclass
from typing import Any, TypedDict
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from slack_rag import llm
from slack_rag.db import load_job_text, record_llm_usage, search_chunks
from slack_rag.taxonomy import UNKNOWN, load_taxonomy

logger = logging.getLogger("slack_rag.graph")

EMPTY_MESSAGE = {"request_type": "other", "service": UNKNOWN, "category": UNKNOWN}
SEARCH_K = 3           # 답변에 넣는 조각 수 상한. ①에서 기준 이상이 이만큼이면 ②로 넓히지 않는다(04-rag-document-search-plan 가정 C7)
MIN_SIMILARITY = 0.78  # 코사인 유사도 기준. live 측정(2026-11-19)의 권장 범위 0.716~0.802 중 ①단계 넓히기가 되는 0.761 초과 값(04-rag-document-search-plan 가정 C7)
NOT_FOUND = {"found": False, "answer": "관련 문서를 찾지 못했습니다", "citations": []}  # 근거 없음: 모델 호출 없음(TO-016)


class JobState(TypedDict):
    """체크포인트에 저장되는 상태. JSON 값만 둔다(엄격 역직렬화, 3.2). 비밀값을 넣지 않는다."""

    text: str                       # 입력: Slack 메시지 본문(파일만 올린 메시지는 빈 문자열)
    classification: dict[str, str]  # classify 출력: request_type, service, category
    retrieval: dict[str, Any]       # retrieve 출력: stage, filters, chunks(본문 포함, 04-rag-document-search-plan 2.4)
    answer: dict[str, Any]          # answer_question 출력: found, answer, citations


@dataclass(frozen=True)
class Context:
    """실행마다 노드에 넘기는 자원(LangGraph 실행 문맥). 체크포인트에 저장되지 않는다(3.12)."""

    job_id: UUID
    job_try: int             # arq 시도 번호. llm_usage에 남긴다
    db: AsyncConnectionPool  # 앱 테이블용 풀(워커 ctx["db"])
    llm: Any                 # google.genai.Client(API 키를 가진 객체는 여기에만). 테스트는 가짜
    model: str               # 경량 역할의 모델 ID(설정 LLM_LIGHT_MODEL): 분류와 답변
    embedding_model: str     # 임베딩 모델 ID(설정 EMBEDDING_MODEL)


def checkpoint_pool(database_url: str) -> AsyncConnectionPool:
    """체크포인터 전용 풀. AsyncPostgresSaver가 요구하는 연결 설정을 쓴다(3.2). 여는 것은 호출한 쪽이."""
    return AsyncConnectionPool(
        database_url,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    )


def thread_config(job_id: UUID) -> dict[str, Any]:
    # configurable의 문자열·숫자는 체크포인트 메타데이터로 복사된다 → thread_id 말고는 넣지 않는다(1.6)
    return {"configurable": {"thread_id": str(job_id)}}


async def classify(state: JobState, runtime: Runtime[Context]) -> dict[str, Any]:
    """요청 유형·서비스·카테고리를 경량 모델로 고른다(3.10, 3.11)."""
    ctx = runtime.context
    started = time.monotonic()
    if not state["text"].strip():  # 파일만 올린 메시지, 공백·줄바꿈만 있는 본문: 모델을 부르지 않는다(가정 C5)
        classification = dict(EMPTY_MESSAGE)
    else:
        taxonomy = load_taxonomy()  # 작업마다 읽는다: 목록을 고치면 다음 작업부터 반영(3.10)
        response = await llm.request_classification(ctx.llm, ctx.model, state["text"], taxonomy)
        async with ctx.db.connection() as conn:  # 검증 전에 기록: 출력이 틀려도 토큰은 썼다(3.8)
            await record_llm_usage(
                conn, job_id=ctx.job_id, job_try=ctx.job_try, node="classify", model=ctx.model, **llm.usage_of(response)
            )
        classification = llm.parse_classification(response, taxonomy)
    duration_ms = round((time.monotonic() - started) * 1000)
    fields = {"node": "classify", "job_id": str(ctx.job_id), "job_try": ctx.job_try, **classification}
    logger.info("node_done", extra={"fields": {**fields, "duration_ms": duration_ms}})  # 본문은 넣지 않는다(3.8)
    return {"classification": classification}


def route(state: JobState) -> str:
    return state["classification"]["request_type"]


def search_stages(classification: dict[str, str]) -> list[dict[str, str]]:
    """검색 조건 단계(TO-015): ① 서비스+카테고리 → ② 서비스. unknown인 조건은 빼고, 같은 조건은 한 번만."""
    first = {key: classification[key] for key in ("service", "category") if classification[key] != UNKNOWN}
    second = {key: value for key, value in first.items() if key == "service"}
    return [first] if second == first else [first, second]


async def find_chunks(
    conn: Any, query_vector: list[float], classification: dict[str, str], embedding_model: str
) -> dict[str, Any]:
    """단계마다 검색해 기준 이상인 조각만 남긴다. SEARCH_K개를 채우면 그 단계에서 멈춘다(TO-015)."""
    for stage, filters in enumerate(search_stages(classification), start=1):
        rows = await search_chunks(conn, query_vector, embedding_model, k=SEARCH_K, **filters)
        chunks = [row for row in rows if row["similarity"] >= MIN_SIMILARITY]
        if len(chunks) >= SEARCH_K:
            break
    return {"stage": stage, "filters": filters, "chunks": chunks}


async def retrieve(state: JobState, runtime: Runtime[Context]) -> dict[str, Any]:
    """질문을 한 번 임베딩해, 분류 꼬리표로 좁힌 범위에서 비슷한 조각을 찾는다(TO-015)."""
    ctx = runtime.context
    started = time.monotonic()
    response = await llm.embed_query(ctx.llm, ctx.embedding_model, state["text"])
    async with ctx.db.connection() as conn:  # 검증 전에 기록: 응답을 받았으면 토큰은 썼다(04-rag-document-search-plan 3.12)
        await record_llm_usage(
            conn,
            job_id=ctx.job_id,
            job_try=ctx.job_try,
            node="retrieve",
            model=ctx.embedding_model,
            **llm.embedding_usage_of(response),
        )
    [vector] = llm.vectors_of(response, 1)
    async with ctx.db.connection() as conn:
        retrieval = await find_chunks(conn, vector, state["classification"], ctx.embedding_model)
    fields = {
        "node": "retrieve",
        "job_id": str(ctx.job_id),
        "job_try": ctx.job_try,
        "stage": retrieval["stage"],
        "filters": retrieval["filters"],
        "chunks": len(retrieval["chunks"]),
        "top_similarity": max((chunk["similarity"] for chunk in retrieval["chunks"]), default=None),
    }
    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info("node_done", extra={"fields": {**fields, "duration_ms": duration_ms}})  # 질문·조각 본문은 넣지 않는다
    return {"retrieval": retrieval}


async def answer_question(state: JobState, runtime: Runtime[Context]) -> dict[str, Any]:
    """검색된 조각을 근거로 답한다. 근거가 없으면 모델을 부르지 않는다(TO-016)."""
    ctx = runtime.context
    started = time.monotonic()
    chunks = state["retrieval"]["chunks"]
    if not chunks:
        answer = dict(NOT_FOUND)
    else:
        response = await llm.request_answer(ctx.llm, ctx.model, state["text"], chunks)
        async with ctx.db.connection() as conn:  # 검증 전에 기록(04-rag-document-search-plan 3.12)
            await record_llm_usage(
                conn,
                job_id=ctx.job_id,
                job_try=ctx.job_try,
                node="answer_question",
                model=ctx.model,
                **llm.usage_of(response),
            )
        answer = llm.parse_answer(response, [chunk["chunk_id"] for chunk in chunks])
    fields = {
        "node": "answer_question",
        "job_id": str(ctx.job_id),
        "job_try": ctx.job_try,
        "found": answer["found"],
        "citations": len(answer["citations"]),
    }
    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info("node_done", extra={"fields": {**fields, "duration_ms": duration_ms}})  # 답변 본문은 넣지 않는다
    return {"answer": answer}


async def propose_fix(state: JobState, runtime: Runtime[Context]) -> None:
    """코드 수정 경로(임시 대체). 원인 분석·수정안 단계에서 채운다."""
    logger.info("node_reached", extra={"fields": {"job_id": str(runtime.context.job_id), "node": "propose_fix"}})


def build(checkpoints: AsyncConnectionPool) -> CompiledStateGraph:
    """그래프를 만든다. 실행 중인 이벤트 루프 안에서 불러야 한다(AsyncPostgresSaver 요구)."""
    builder = StateGraph(JobState, context_schema=Context)
    builder.add_node("classify", classify)  # retry_policy 없음: 재시도는 arq 작업 재시도 한 층(3.5)
    builder.add_node("retrieve", retrieve)
    builder.add_node("answer_question", answer_question)
    builder.add_node("propose_fix", propose_fix)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges("classify", route, {"question": "retrieve", "code_fix": "propose_fix", "other": END})
    builder.add_edge("retrieve", "answer_question")  # 검색 결과는 답변만 쓴다(code_fix 검색은 TO-015에 따라 나중에)
    builder.add_edge("answer_question", END)
    builder.add_edge("propose_fix", END)
    saver = AsyncPostgresSaver(checkpoints, serde=JsonPlusSerializer(allowed_msgpack_modules=None))  # 안전 타입만 복원
    return builder.compile(checkpointer=saver)


async def run(compiled: CompiledStateGraph, context: Context) -> dict[str, Any]:
    """작업 하나를 실행하고 최종 State를 돌려준다.

    체크포인트가 없으면 본문을 입력으로 새로 시작하고, 있으면 입력 없이 마지막 체크포인트에서 이어 간다.
    입력을 다시 넘기면 LangGraph가 START부터 다시 돌아 끝난 분류를 또 부르므로 이 분기가 핵심이다(3.3).
    """
    config = thread_config(context.job_id)
    graph_input: dict[str, Any] | None = None
    if (await compiled.aget_state(config)).created_at is None:
        async with context.db.connection() as conn:
            graph_input = {"text": await load_job_text(conn, context.job_id)}
    return await compiled.ainvoke(graph_input, config, context=context, durability="sync")
