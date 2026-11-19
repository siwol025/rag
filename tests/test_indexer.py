import json
import logging

import psycopg
import pytest
from google.genai import errors

from fakes import LLM_SETTINGS, MINUTE_QUOTA, QUESTION, FakeGenai, fake_vector, quota_error
from slack_rag import indexer, llm
from slack_rag.config import Settings
from slack_rag.taxonomy import load_taxonomy

pytestmark = pytest.mark.anyio
MODEL = LLM_SETTINGS["embedding_model"]
TAXONOMY = load_taxonomy()
SERVICE, OTHER_SERVICE = list(TAXONOMY.services)[:2]
CATEGORY = next(iter(TAXONOMY.categories))


def write_doc(folder, doc_id, *, title="가상 장애 회고", service=SERVICE, cause=None):
    """섹션마다 doc_id가 들어가 문서끼리 입력이 겹치지 않는 가상 문서."""
    (folder / f"{doc_id}.md").write_text(
        f'+++\nid = "{doc_id}"\ntitle = "{title}"\nservice = "{service}"\ncategory = "{CATEGORY}"\n'
        'doc_type = "postmortem"\ndate = 2099-01-02\n+++\n\n'
        f"## 증상\n{doc_id} 증상\n\n## 원인\n{cause or doc_id + ' 원인'}\n\n"
        f"## 조치\n{doc_id} 조치\n\n## 재발 방지\n{doc_id} 재발 방지\n",
        encoding="utf-8",
    )


async def index(aconn, fake, folder, model=MODEL):
    return await indexer.index_once(aconn, fake, model, folder)


def rows(conn, sql):
    return conn.execute(sql).fetchall()


def held_index_locks(conn):
    """다른 연결에서 본 색인 잠금 수. 같은 연결로 다시 잡아 보면 재진입이라 해제 여부를 알 수 없다."""
    return rows(conn, f"SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = {indexer.INDEX_LOCK_KEY}")


async def test_first_run_embeds_every_section_in_one_batch_and_records_usage(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    write_doc(tmp_path, "pm-2099-002")
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert (result.embedded, result.saved, result.deleted, result.stopped) == (8, 2, 0, None)
    [call] = fake.embed_calls  # 입력 합이 BATCH_MAX_CHARS보다 작아 묶음 하나
    assert call["inputs"][1] == llm.document_input("가상 장애 회고", "원인", "pm-2099-001 원인")
    assert rows(conn, "SELECT doc_id, ordinal, heading, embedding_model FROM chunks ORDER BY 1, 2")[:2] == [
        ("pm-2099-001", 1, "증상", MODEL), ("pm-2099-001", 2, "원인", MODEL),
    ]
    assert rows(conn, "SELECT model, texts, input_tokens FROM index_usage") == [
        (MODEL, 8, sum(len(text) for text in call["inputs"])),  # 가짜는 글자 수를 토큰으로
    ]
    assert held_index_locks(conn) == [(0,)]  # 성공한 회차가 advisory lock을 풀었음(다른 연결에서 확인)


async def test_second_run_over_same_documents_calls_embedding_zero_times(conn, aconn, tmp_path):
    # 로드맵 완료 기준 ①: 같은 문서 묶음을 두 번 색인하면 두 번째에는 임베딩 호출이 0회
    write_doc(tmp_path, "pm-2099-001")
    write_doc(tmp_path, "pm-2099-002")
    await index(aconn, FakeGenai(QUESTION), tmp_path)
    before = rows(conn, "SELECT doc_id, indexed_at FROM documents ORDER BY 1")
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert fake.embed_calls == []
    assert (result.embedded, result.saved, result.deleted, result.changed) == (0, 0, 0, False)
    assert rows(conn, "SELECT doc_id, indexed_at FROM documents ORDER BY 1") == before  # DB 쓰기도 없음
    assert rows(conn, "SELECT count(*) FROM index_usage") == [(1,)]


async def test_editing_one_section_embeds_only_that_section(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    await index(aconn, FakeGenai(QUESTION), tmp_path)
    write_doc(tmp_path, "pm-2099-001", cause="고친 원인")
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert [call["inputs"] for call in fake.embed_calls] == [[llm.document_input("가상 장애 회고", "원인", "고친 원인")]]
    assert (result.embedded, result.saved) == (1, 1)
    assert rows(conn, "SELECT text FROM chunks WHERE ordinal = 2") == [("고친 원인",)]


async def test_metadata_only_change_updates_document_without_embedding(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    await index(aconn, FakeGenai(QUESTION), tmp_path)
    write_doc(tmp_path, "pm-2099-001", service=OTHER_SERVICE)  # 꼬리표는 임베딩 입력에 들어가지 않음
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert fake.embed_calls == []
    assert (result.embedded, result.saved) == (0, 1)
    assert rows(conn, "SELECT service FROM documents") == [(OTHER_SERVICE,)]
    assert rows(conn, "SELECT count(*) FROM chunks") == [(4,)]


async def test_title_change_reembeds_every_section_of_that_document(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    write_doc(tmp_path, "pm-2099-002")
    await index(aconn, FakeGenai(QUESTION), tmp_path)
    write_doc(tmp_path, "pm-2099-001", title="고친 제목")  # 제목은 임베딩 입력에 들어감
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert [len(call["inputs"]) for call in fake.embed_calls] == [4]
    assert (result.embedded, result.saved) == (4, 1)


async def test_removed_or_broken_file_drops_document_from_index(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    write_doc(tmp_path, "pm-2099-002")
    await index(aconn, FakeGenai(QUESTION), tmp_path)
    (tmp_path / "pm-2099-001.md").unlink()
    (tmp_path / "pm-2099-002.md").write_text("앞머리 없음", encoding="utf-8")  # 형식이 깨짐(04-rag-document-search-plan 가정 C4)
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert fake.embed_calls == []
    assert (result.deleted, list(result.rejected)) == (2, ["pm-2099-002.md"])
    assert rows(conn, "SELECT count(*) FROM documents") == [(0,)]
    assert rows(conn, "SELECT count(*) FROM chunks") == [(0,)]  # ON DELETE CASCADE


async def test_changing_embedding_model_reembeds_everything(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    await index(aconn, FakeGenai(QUESTION), tmp_path, model="old-embedding-model")
    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)
    assert (result.embedded, result.saved) == (4, 1)
    assert rows(conn, "SELECT DISTINCT embedding_model FROM chunks") == [(MODEL,)]  # 옛 벡터는 남지 않음


async def test_rate_limit_stops_the_round_and_next_round_continues(conn, aconn, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "RATE_LIMIT_WAIT_S", 0)  # 구현이 틀려 대기 루프를 써도 실제로 기다리지 않게
    monkeypatch.setattr(indexer, "BATCH_MAX_TEXTS", 4)  # 문서 하나 = 묶음 하나
    for i in (1, 2, 3):
        write_doc(tmp_path, f"pm-2099-00{i}")
    answers = iter([None, quota_error(MINUTE_QUOTA)])  # 첫 묶음 성공, 두 번째 묶음에서 분당 429
    fake = FakeGenai(QUESTION, embed=lambda inputs: next(answers))
    result = await index(aconn, fake, tmp_path)
    assert len(fake.embed_calls) == 2  # 기다렸다 다시 부르지 않음(TO-014)
    assert result.saved == 1 and result.stopped.startswith("RateLimited")
    assert rows(conn, "SELECT doc_id FROM documents") == [("pm-2099-001",)]  # 성공한 문서는 커밋됨

    fake = FakeGenai(QUESTION)
    result = await index(aconn, fake, tmp_path)  # 다음 회차
    assert [len(call["inputs"]) for call in fake.embed_calls] == [4, 4]  # 남은 두 문서만
    assert (result.saved, result.stopped) == (2, None)


async def test_server_error_also_stops_the_round(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    error = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}})
    result = await index(aconn, FakeGenai(QUESTION, embed=error), tmp_path)  # 5xx도 기다리지 않고 멈춤(TO-014)
    assert result.stopped.startswith("ServerError") and result.saved == 0


async def test_batches_follow_the_character_limit(conn, aconn, tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "BATCH_MAX_CHARS", 1)  # 문서 하나만으로 넘음 → 문서마다 따로 보냄
    write_doc(tmp_path, "pm-2099-001")
    write_doc(tmp_path, "pm-2099-002")
    fake = FakeGenai(QUESTION)
    await index(aconn, fake, tmp_path)
    assert [len(call["inputs"]) for call in fake.embed_calls] == [4, 4]
    assert rows(conn, "SELECT count(*) FROM index_usage") == [(2,)]


async def test_only_one_indexing_runs_at_a_time(conn, aconn, database_url, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as other:
        await other.execute("SELECT pg_advisory_lock(%s)", (indexer.INDEX_LOCK_KEY,))  # 다른 색인(CLI)이 도는 중
        fake = FakeGenai(QUESTION)
        result = await index(aconn, fake, tmp_path)
    assert result.locked is True
    assert fake.embed_calls == [] and rows(conn, "SELECT count(*) FROM documents") == [(0,)]
    result = await index(aconn, FakeGenai(QUESTION), tmp_path)  # 다른 연결이 닫혀 잠금이 풀린 뒤
    assert (result.locked, result.saved) == (False, 1)


async def test_missing_docs_folder_fails_the_round_and_keeps_the_index(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    await index(aconn, FakeGenai(QUESTION), tmp_path)
    fake = FakeGenai(QUESTION)
    with pytest.raises(FileNotFoundError):  # 저장소 루트가 아닌 곳에서 실행한 CLI, bind mount 누락
        await index(aconn, fake, tmp_path / "missing")
    assert fake.embed_calls == []
    assert rows(conn, "SELECT count(*) FROM documents") == [(1,)]  # 없는 폴더를 "문서 0개"로 보고 지우지 않음
    assert rows(conn, "SELECT count(*) FROM chunks") == [(4,)]
    assert held_index_locks(conn) == [(0,)]  # 실패한 회차도 advisory lock을 풀었음(다른 연결에서 확인)


async def test_vector_count_mismatch_fails_the_round_after_recording_usage(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    with pytest.raises(ValueError):  # SDK 약속이 깨진 것: 멈춤(stopped)으로 덮지 않음(04-rag-document-search-plan 3.5)
        await index(aconn, FakeGenai(QUESTION, embed=lambda inputs: [fake_vector("x")]), tmp_path)
    assert rows(conn, "SELECT texts FROM index_usage") == [(4,)]  # 응답을 받았으니 토큰은 기록(검증 전)
    assert rows(conn, "SELECT count(*) FROM documents") == [(0,)]


async def test_document_is_saved_all_or_nothing(conn, aconn, tmp_path):
    write_doc(tmp_path, "pm-2099-001")
    nan_second = lambda inputs: [fake_vector(t) if i != 1 else [float("nan")] * 768 for i, t in enumerate(inputs)]
    with pytest.raises(psycopg.DataError):  # pgvector는 NaN을 받지 않음: 두 번째 조각 INSERT에서 실패
        await index(aconn, FakeGenai(QUESTION, embed=nan_second), tmp_path)
    assert rows(conn, "SELECT count(*) FROM documents") == [(0,)]  # 문서 행도 함께 되돌려짐
    assert rows(conn, "SELECT count(*) FROM chunks") == [(0,)]
    assert held_index_locks(conn) == [(0,)]  # DB 오류로 실패한 회차도 잠금을 풀었음


@pytest.fixture
def settings(database_url, conn):  # conn이 표를 비움
    return Settings(
        _env_file=None,
        database_url=database_url,
        redis_url="redis://localhost:6379/0",  # 필수 키지만 색인은 Redis를 쓰지 않음
        jobs_api_key="test-api-key",
        slack_target_channel_ids={"C0TARGET01"},
        **LLM_SETTINGS,
    )


async def test_round_logs_only_when_something_changed(settings, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    write_doc(tmp_path, "pm-2099-001")
    fake = FakeGenai(QUESTION)
    await indexer.run_round(settings.database_url, fake, MODEL, tmp_path)
    await indexer.run_round(settings.database_url, fake, MODEL, tmp_path)  # 바뀐 것 없음 → 로그 없음
    done = [r.fields for r in caplog.records if r.getMessage() == "index_done"]
    assert done == [{"embedded": 4, "saved": 1, "deleted": 0, "rejected": {}, "stopped": None, "locked": False}]


class StopLoop(BaseException):
    """반복문을 끝내려고 테스트가 던진다(Exception이 아니라 회차 실패로 잡히지 않음)."""


async def test_loop_runs_at_start_and_keeps_going_after_a_failed_round(settings, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="slack_rag")
    rounds = []

    async def fake_round(database_url, client, embedding_model, docs_dir):
        rounds.append(docs_dir)
        if len(rounds) == 1:
            raise RuntimeError("API가 아직 스키마를 적용하지 않음")
        if len(rounds) == 3:
            raise StopLoop

    monkeypatch.setattr(indexer, "run_round", fake_round)
    monkeypatch.setattr(indexer, "INDEX_INTERVAL_S", 0)
    monkeypatch.setattr(llm, "make_client", lambda settings: FakeGenai(QUESTION))
    with pytest.raises(StopLoop):
        await indexer.run_forever(settings)
    assert rounds == [indexer.DOCS_DIR] * 3  # 시작하자마자 1회, 실패한 회차 뒤에도 계속
    assert [r.getMessage() for r in caplog.records if r.name == "slack_rag.indexer"] == ["index_failed"]


def test_cli_runs_one_round_prints_result_and_fails_when_stopped(settings, tmp_path, monkeypatch, capsys):
    # 동기 테스트: main()이 asyncio.run을 부른다(anyio 표시는 코루틴 테스트에만 적용됨)
    write_doc(tmp_path, "pm-2099-001")
    monkeypatch.setattr(indexer, "Settings", lambda: settings)
    monkeypatch.setattr(indexer, "DOCS_DIR", tmp_path)
    monkeypatch.setattr(indexer, "configure_logging", lambda: None)  # capsys 스트림에 로그 처리기를 붙이지 않게
    monkeypatch.setattr(llm, "make_client", lambda settings: FakeGenai(QUESTION))
    assert indexer.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "embedded": 4, "saved": 1, "deleted": 0, "rejected": {}, "stopped": None, "locked": False,
    }

    write_doc(tmp_path, "pm-2099-002")
    monkeypatch.setattr(llm, "make_client", lambda settings: FakeGenai(QUESTION, embed=quota_error(MINUTE_QUOTA)))
    assert indexer.main([]) == 1  # 멈춘 회차는 실패 종료 → 사람이 알아챔
    assert json.loads(capsys.readouterr().out)["stopped"].startswith("RateLimited")
