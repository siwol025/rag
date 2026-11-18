"""작업 처리 그래프(LangGraph): 분류 → 유형별 경로. 노드 사이 상태는 PostgreSQL 체크포인트에 남는다."""
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
from slack_rag.db import load_job_text, record_llm_usage
from slack_rag.taxonomy import UNKNOWN, load_taxonomy

logger = logging.getLogger("slack_rag.graph")

EMPTY_MESSAGE = {"request_type": "other", "service": UNKNOWN, "category": UNKNOWN}


class JobState(TypedDict):
    """체크포인트에 저장되는 상태. JSON 값만 둔다(엄격 역직렬화, 3.2). 비밀값을 넣지 않는다."""

    text: str                       # 입력: Slack 메시지 본문(파일만 올린 메시지는 빈 문자열)
    classification: dict[str, str]  # classify 출력: request_type, service, category


@dataclass(frozen=True)
class Context:
    """실행마다 노드에 넘기는 자원(LangGraph 실행 문맥). 체크포인트에 저장되지 않는다(3.12)."""

    job_id: UUID
    job_try: int             # arq 시도 번호. llm_usage에 남긴다
    db: AsyncConnectionPool  # 앱 테이블용 풀(워커 ctx["db"])
    llm: Any                 # google.genai.Client(API 키를 가진 객체는 여기에만). 테스트는 가짜
    model: str               # 경량 역할의 모델 ID(설정 LLM_LIGHT_MODEL)


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


async def answer_question(state: JobState, runtime: Runtime[Context]) -> None:
    """질의응답 경로(임시 대체). 문서 검색·답변 단계에서 채운다."""
    logger.info("node_reached", extra={"fields": {"job_id": str(runtime.context.job_id), "node": "answer_question"}})


async def propose_fix(state: JobState, runtime: Runtime[Context]) -> None:
    """코드 수정 경로(임시 대체). 원인 분석·수정안 단계에서 채운다."""
    logger.info("node_reached", extra={"fields": {"job_id": str(runtime.context.job_id), "node": "propose_fix"}})


def build(checkpoints: AsyncConnectionPool) -> CompiledStateGraph:
    """그래프를 만든다. 실행 중인 이벤트 루프 안에서 불러야 한다(AsyncPostgresSaver 요구)."""
    builder = StateGraph(JobState, context_schema=Context)
    builder.add_node("classify", classify)  # retry_policy 없음: 재시도는 arq 작업 재시도 한 층(3.5)
    builder.add_node("answer_question", answer_question)
    builder.add_node("propose_fix", propose_fix)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify", route, {"question": "answer_question", "code_fix": "propose_fix", "other": END}
    )
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
