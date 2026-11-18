import logging

import pytest
from psycopg_pool import AsyncConnectionPool

from fakes import CODE_FIX, OTHER, QUESTION, FakeGenai
from slack_rag import graph
from slack_rag.db import create_job

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pools(database_url, conn):  # conn이 jobs·llm_usage를 비움
    async with (
        AsyncConnectionPool(database_url, open=False) as db,
        graph.checkpoint_pool(database_url) as checkpoints,
    ):
        yield db, checkpoints


def make_job(conn, text):
    job_id, _, _ = create_job(
        conn, channel_id="C0TARGET01", message_ts="1794381600.000100", user_id="U0HUMAN01", message_text=text
    )
    return job_id


def usage_rows(conn, job_id):
    return conn.execute(
        "SELECT job_try, node, model FROM llm_usage WHERE job_id = %s ORDER BY id", (job_id,)
    ).fetchall()


async def run(pools, job_id, fake, job_try=1):
    db, checkpoints = pools
    context = graph.Context(job_id=job_id, job_try=job_try, db=db, llm=fake, model="fake-light-model")
    return await graph.run(graph.build(checkpoints), context)


@pytest.mark.parametrize(
    ("answer", "reached"),
    [(QUESTION, ["answer_question"]), (CODE_FIX, ["propose_fix"]), (OTHER, [])],
    ids=["question", "code_fix", "other"],
)
async def test_classification_picks_path_and_records_usage(pools, conn, caplog, answer, reached):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = make_job(conn, "결제 API 5xx 급증")
    fake = FakeGenai(answer)
    state = await run(pools, job_id, fake)
    assert state["classification"] == answer
    assert [r.fields["node"] for r in caplog.records if r.getMessage() == "node_reached"] == reached
    assert usage_rows(conn, job_id) == [(1, "classify", "fake-light-model")]
    assert "결제 API 5xx 급증" in fake.calls[0]["contents"]  # 본문은 DB에서 읽어 입력으로


@pytest.mark.parametrize("text", ["", "  \n\t \n"], ids=["empty", "whitespace-only"])
async def test_empty_message_is_other_without_calling_model(pools, conn, text):
    job_id = make_job(conn, text)  # 파일만 올린 메시지(01-event-ingestion-plan 1.5), 공백·줄바꿈만 있는 본문(가정 C5)
    fake = FakeGenai(QUESTION)
    state = await run(pools, job_id, fake)
    assert state["classification"] == OTHER
    assert fake.calls == []
    assert usage_rows(conn, job_id) == []


async def test_classify_logs_node_done_with_labels_but_not_message(pools, conn, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    secret_text = "결제 API 5xx 급증, 고객 이메일 kim@example.com"
    job_id = make_job(conn, secret_text)
    await run(pools, job_id, FakeGenai(QUESTION), job_try=2)

    [done] = [r.fields for r in caplog.records if r.getMessage() == "node_done"]
    assert done == {"node": "classify", "job_id": str(job_id), "job_try": 2, **QUESTION, "duration_ms": done["duration_ms"]}
    assert done["duration_ms"] >= 0
    logged = " ".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    assert "kim@example.com" not in logged and "결제 API 5xx 급증" not in logged  # 본문은 어느 로그에도 없음


async def test_usage_is_recorded_even_when_model_output_is_invalid(pools, conn):
    job_id = make_job(conn, "결제 API 5xx 급증")
    with pytest.raises(ValueError):
        await run(pools, job_id, FakeGenai("not json"))
    assert usage_rows(conn, job_id) == [(1, "classify", "fake-light-model")]  # 응답을 받은 호출은 기록


async def test_rerun_resumes_from_checkpoint_without_calling_model_again(pools, conn, monkeypatch):
    tries = []

    async def fail_first_time(state, runtime):
        tries.append(runtime.context.job_try)
        if len(tries) == 1:
            raise RuntimeError("분류 뒤 노드에서 실패")

    monkeypatch.setattr(graph, "answer_question", fail_first_time)  # build()가 이 이름을 읽는다
    job_id = make_job(conn, "예전에 결제 장애 있었나요?")
    fake = FakeGenai(QUESTION)

    with pytest.raises(RuntimeError):
        await run(pools, job_id, fake, job_try=1)
    state = await run(pools, job_id, fake, job_try=2)  # arq가 다시 실행한 2번째 시도

    assert state["classification"] == QUESTION
    assert len(fake.calls) == 1  # 분류를 다시 부르지 않음
    assert tries == [1, 2]       # 실패한 노드만 다시 실행
    assert usage_rows(conn, job_id) == [(1, "classify", "fake-light-model")]
