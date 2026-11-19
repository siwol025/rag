import logging

import pytest
from psycopg_pool import AsyncConnectionPool

from fakes import CODE_FIX, FAKE_MIN_SIMILARITY, OTHER, QUESTION, FakeGenai, classify_then_cite, fake_vector
from slack_rag import graph, llm
from slack_rag.db import create_job, search_chunks

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
    context = graph.Context(
        job_id=job_id, job_try=job_try, db=db, llm=fake, model="fake-light-model", embedding_model=EMBEDDING_MODEL
    )
    return await graph.run(graph.build(checkpoints), context)


@pytest.mark.parametrize(
    ("answer", "reached", "usage"),
    [
        (QUESTION, [], [(1, "classify", "fake-light-model"), (1, "retrieve", "fake-embedding-model")]),
        (CODE_FIX, ["propose_fix"], [(1, "classify", "fake-light-model")]),
        (OTHER, [], [(1, "classify", "fake-light-model")]),
    ],
    ids=["question", "code_fix", "other"],
)
async def test_classification_picks_path_and_records_usage(pools, conn, caplog, answer, reached, usage):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = make_job(conn, "결제 API 5xx 급증")
    fake = FakeGenai(answer)
    state = await run(pools, job_id, fake)
    assert state["classification"] == answer
    assert [r.fields["node"] for r in caplog.records if r.getMessage() == "node_reached"] == reached
    assert usage_rows(conn, job_id) == usage  # 시도·노드·모델 전부. question은 질문 임베딩까지(문서가 없어 답변 호출 없음)
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

    [done] = [r.fields for r in caplog.records if r.getMessage() == "node_done" and r.fields["node"] == "classify"]
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
    assert len(fake.calls) == 1 and len(fake.embed_calls) == 1  # 분류도 질문 임베딩도 다시 부르지 않음
    assert tries == [1, 2]       # 실패한 노드만 다시 실행
    assert usage_rows(conn, job_id) == [(1, "classify", "fake-light-model"), (1, "retrieve", EMBEDDING_MODEL)]


EMBEDDING_MODEL = "fake-embedding-model"
DECOY_QUESTION = "결제 API에서 5xx 응답이 급증하고 커넥션 풀이 고갈됐던 장애가 있었나요?"
PG_QUESTION = "결제할 때 PG사 쪽 응답이 늦어서 결제 5xx가 났던 적이 있나요?"
UNRELATED_QUESTION = "사내 식당 점심 메뉴가 언제 바뀌나요?"
PAYMENT_5XX = {"request_type": "question", "service": "payment-api", "category": "server-error"}
NO_LABELS = {"request_type": "question", "service": "unknown", "category": "unknown"}


@pytest.fixture
def fake_threshold(monkeypatch):
    monkeypatch.setattr(graph, "MIN_SIMILARITY", FAKE_MIN_SIMILARITY)  # 가짜 임베딩 척도의 기준(04-rag-document-search-plan 3.13)


def query_vector(text):
    return fake_vector(llm.query_input(text))


async def test_decoy_from_other_service_ranks_first_without_filter_and_drops_out_with_it(indexed_samples, aconn):
    vector = query_vector(DECOY_QUESTION)
    unfiltered = await search_chunks(aconn, vector, EMBEDDING_MODEL, k=3)
    filtered = await search_chunks(aconn, vector, EMBEDDING_MODEL, k=3, service="payment-api")
    assert unfiltered[0]["chunk_id"] == "pm-2026-005#1"  # 함정: 주문 API 5xx(증상 문장이 거의 같음)
    assert filtered[0]["chunk_id"] == "pm-2026-001#2"    # 먼저 좁히면 결제 문서가 1등
    assert "pm-2026-005" not in {row["doc_id"] for row in filtered}
    assert filtered[0]["title"] == "결제 승인 API 5xx 급증 장애 회고" and filtered[0]["date"] == "2026-03-14"
    assert 0 < filtered[0]["similarity"] <= 1


async def test_search_ignores_vectors_from_another_embedding_model(indexed_samples, aconn):
    assert await search_chunks(aconn, query_vector(DECOY_QUESTION), "other-embedding-model", k=3) == []


def test_search_stages_drop_unknown_labels_and_repeated_stage():
    assert graph.search_stages(PAYMENT_5XX) == [
        {"service": "payment-api", "category": "server-error"}, {"service": "payment-api"},
    ]
    assert graph.search_stages({**PAYMENT_5XX, "category": "unknown"}) == [{"service": "payment-api"}]
    assert graph.search_stages({**PAYMENT_5XX, "service": "unknown"}) == [{"category": "server-error"}, {}]
    assert graph.search_stages(NO_LABELS) == [{}]


async def test_search_stops_at_first_stage_when_it_fills_k(indexed_samples, aconn, fake_threshold):
    found = await graph.find_chunks(aconn, query_vector(DECOY_QUESTION), PAYMENT_5XX, EMBEDDING_MODEL)
    assert (found["stage"], found["filters"]) == (1, {"service": "payment-api", "category": "server-error"})
    assert [chunk["doc_id"] for chunk in found["chunks"]] == ["pm-2026-001"] * graph.SEARCH_K


async def test_search_widens_to_service_when_first_stage_finds_fewer_than_k(indexed_samples, aconn, fake_threshold):
    found = await graph.find_chunks(aconn, query_vector(PG_QUESTION), PAYMENT_5XX, EMBEDDING_MODEL)
    assert (found["stage"], found["filters"]) == (2, {"service": "payment-api"})
    assert found["chunks"][0]["doc_id"] == "pm-2026-003"  # 원인이 PG사라 external-dependency로 붙은 문서
    sims = [chunk["similarity"] for chunk in found["chunks"]]
    assert sims == sorted(sims, reverse=True) and min(sims) >= FAKE_MIN_SIMILARITY


async def test_search_keeps_only_chunks_above_threshold(indexed_samples, aconn, fake_threshold):
    found = await graph.find_chunks(aconn, query_vector(UNRELATED_QUESTION), NO_LABELS, EMBEDDING_MODEL)
    assert (found["stage"], found["filters"], found["chunks"]) == (1, {}, [])


async def test_question_is_answered_from_retrieved_chunks_with_usage_for_each_call(
    indexed_samples, pools, conn, fake_threshold
):
    job_id = make_job(conn, DECOY_QUESTION)
    fake = FakeGenai(classify_then_cite(PAYMENT_5XX))
    state = await run(pools, job_id, fake)
    assert state["retrieval"]["stage"] == 1
    assert state["answer"] == {"found": True, "answer": "가짜 답변입니다.", "citations": ["pm-2026-001#2"]}
    assert [call["inputs"] for call in fake.embed_calls] == [[llm.query_input(DECOY_QUESTION)]]  # 질문 임베딩 1번
    [_, answer_call] = fake.calls
    assert answer_call["contents"].startswith(f"<question>\n{DECOY_QUESTION}\n</question>")
    assert usage_rows(conn, job_id) == [
        (1, "classify", "fake-light-model"), (1, "retrieve", EMBEDDING_MODEL), (1, "answer_question", "fake-light-model"),
    ]


async def test_question_without_relevant_documents_gets_fixed_answer_without_calling_model(
    indexed_samples, pools, conn, fake_threshold
):
    job_id = make_job(conn, UNRELATED_QUESTION)
    fake = FakeGenai(classify_then_cite(NO_LABELS))
    state = await run(pools, job_id, fake)
    assert state["answer"] == graph.NOT_FOUND == {"found": False, "answer": "관련 문서를 찾지 못했습니다", "citations": []}
    assert len(fake.calls) == 1  # 분류만. 답변 모델은 부르지 않음(TO-016)
    assert [node for _, node, _ in usage_rows(conn, job_id)] == ["classify", "retrieve"]


async def test_retrieve_and_answer_log_node_done_without_question_documents_or_answer(
    indexed_samples, pools, conn, caplog, fake_threshold
):
    caplog.set_level(logging.INFO, logger="slack_rag")
    job_id = make_job(conn, DECOY_QUESTION)
    await run(pools, job_id, FakeGenai(classify_then_cite(PAYMENT_5XX)))
    done = {r.fields["node"]: r.fields for r in caplog.records if r.getMessage() == "node_done"}
    assert {k: v for k, v in done["retrieve"].items() if k not in ("duration_ms", "top_similarity")} == {
        "node": "retrieve", "job_id": str(job_id), "job_try": 1, "stage": 1,
        "filters": {"service": "payment-api", "category": "server-error"}, "chunks": 3,
    }
    assert done["retrieve"]["top_similarity"] >= FAKE_MIN_SIMILARITY
    assert {k: v for k, v in done["answer_question"].items() if k != "duration_ms"} == {
        "node": "answer_question", "job_id": str(job_id), "job_try": 1, "found": True, "citations": 1,
    }
    logged = " ".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    assert "커넥션 풀" not in logged and "가짜 답변" not in logged  # 질문·문서·답변 본문은 어느 로그에도 없음


async def test_rerun_after_answer_failure_does_not_embed_the_question_again(indexed_samples, pools, conn, fake_threshold):
    second = {"found": True, "answer": "두 번째 답", "citations": ["pm-2026-001#2"]}
    answers = iter([PAYMENT_5XX, "not json", second])  # 분류 → 답변(출력 검증 실패) → 재시도의 답변
    fake = FakeGenai(lambda contents: next(answers))
    job_id = make_job(conn, DECOY_QUESTION)
    with pytest.raises(ValueError):  # 워커에서는 Retry(10초)
        await run(pools, job_id, fake, job_try=1)
    state = await run(pools, job_id, fake, job_try=2)
    assert state["answer"] == second
    assert len(fake.embed_calls) == 1  # 검색 결과는 체크포인트에서
    assert usage_rows(conn, job_id) == [
        (1, "classify", "fake-light-model"), (1, "retrieve", EMBEDDING_MODEL),
        (1, "answer_question", "fake-light-model"), (2, "answer_question", "fake-light-model"),
    ]


async def test_retrieve_records_usage_before_checking_vectors(pools, conn):
    job_id = make_job(conn, DECOY_QUESTION)
    fake = FakeGenai(classify_then_cite(PAYMENT_5XX), embed=lambda inputs: [[0.1] * 767])  # 차원이 틀린 응답
    with pytest.raises(ValueError):  # 워커에서는 Retry(10초)
        await run(pools, job_id, fake)
    assert [node for _, node, _ in usage_rows(conn, job_id)] == ["classify", "retrieve"]  # 응답을 받았으니 토큰은 기록(검증 전)
