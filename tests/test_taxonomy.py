import pytest

from slack_rag.taxonomy import UNKNOWN, load_taxonomy


def test_list_file_gives_lowercase_ids_with_display_names():
    taxonomy = load_taxonomy()
    assert len(taxonomy.services) >= 2 and len(taxonomy.categories) >= 2  # tests/fakes.py가 앞의 두 ID를 씀
    for ids in (taxonomy.services, taxonomy.categories):
        for id_, name in ids.items():
            assert id_ == id_.lower() and id_ != UNKNOWN
            assert name  # 표시 이름은 따로


@pytest.mark.parametrize("bad_id", ["Payment-API", "payment_api", "unknown"])
def test_rejects_id_that_is_not_a_stable_lowercase_tag(tmp_path, bad_id):
    path = tmp_path / "taxonomy.toml"
    path.write_text(f'[services]\n{bad_id} = "x"\n\n[categories]\nserver-error = "y"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_taxonomy(path)


def test_rejects_duplicate_id(tmp_path):
    path = tmp_path / "taxonomy.toml"
    path.write_text('[services]\npayment-api = "a"\npayment-api = "b"\n\n[categories]\nx = "y"\n', encoding="utf-8")
    with pytest.raises(ValueError):  # tomllib.TOMLDecodeError는 ValueError
        load_taxonomy(path)
