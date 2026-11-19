import datetime

import pytest

from fakes import SAMPLE_DOCS
from slack_rag.documents import DocumentRejected, load_documents, parse_document
from slack_rag.taxonomy import load_taxonomy

TAXONOMY = load_taxonomy()
SERVICE, CATEGORY = next(iter(TAXONOMY.services)), next(iter(TAXONOMY.categories))

FRONT = f"""+++
id = "pm-2099-001"
title = "가상 장애 회고"
service = "{SERVICE}"
category = "{CATEGORY}"
doc_type = "postmortem"
date = 2099-01-02
+++
"""
BODY = "\n## 증상\n증상 본문\n\n## 원인\n원인 본문\n\n## 조치\n조치 본문\n\n## 재발 방지\n재발 방지 본문\n"


def write(tmp_path, text, name="pm-2099-001.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_document_becomes_four_sections_with_metadata(tmp_path):
    doc = parse_document(write(tmp_path, FRONT + BODY), TAXONOMY)
    assert (doc.doc_id, doc.title, doc.service, doc.category, doc.doc_type) == (
        "pm-2099-001", "가상 장애 회고", SERVICE, CATEGORY, "postmortem",
    )
    assert doc.date == datetime.date(2099, 1, 2)
    assert [(s.ordinal, s.heading, s.text) for s in doc.sections] == [
        (1, "증상", "증상 본문"), (2, "원인", "원인 본문"), (3, "조치", "조치 본문"), (4, "재발 방지", "재발 방지 본문"),
    ]
    assert len(doc.content_hash) == 64


def test_content_hash_changes_only_when_file_changes(tmp_path):
    first = parse_document(write(tmp_path, FRONT + BODY), TAXONOMY).content_hash
    same = parse_document(write(tmp_path, FRONT + BODY), TAXONOMY).content_hash
    edited = parse_document(write(tmp_path, FRONT + BODY.replace("원인 본문", "고친 원인")), TAXONOMY).content_hash
    assert first == same != edited


@pytest.mark.parametrize(
    ("text", "name", "reason"),
    [
        (BODY, "pm-2099-001.md", r"front matter \(\+\+\+ \.\.\. \+\+\+\) is missing"),
        (FRONT.replace('doc_type = "postmortem"', 'doc_type = "memo"') + BODY, "pm-2099-001.md", "front matter must have exactly"),
        (FRONT.replace("date = 2099-01-02", 'date = "2099-01-02"') + BODY, "pm-2099-001.md", "front matter must have exactly"),
        (FRONT.replace("+++\n", '+++\nowner = "x"\n', 1) + BODY, "pm-2099-001.md", "front matter must have exactly"),
        (FRONT.replace(f'service = "{SERVICE}"', 'service = "unknown"') + BODY, "pm-2099-001.md", "not in the taxonomy"),
        (FRONT.replace(f'category = "{CATEGORY}"', 'category = "admin"') + BODY, "pm-2099-001.md", "not in the taxonomy"),
        (FRONT + BODY, "other-name.md", "equal to the file name"),
        (FRONT + "머리말\n" + BODY, "pm-2099-001.md", "text outside sections"),
        (FRONT + BODY.replace("## 조치", "## 대응"), "pm-2099-001.md", "in this order"),
        (FRONT + BODY.replace("재발 방지 본문", ""), "pm-2099-001.md", "empty section"),
        (FRONT + BODY.replace("원인 본문", "가" * 1_501), "pm-2099-001.md", "longer than 1500"),
    ],
    ids=[
        "no-front-matter", "bad-doc-type", "quoted-date", "extra-key", "unknown-service", "category-outside-list",
        "id-not-file-name", "text-outside-sections", "wrong-heading", "empty-section", "section-too-long",
    ],
)
def test_rejects_document_outside_the_format(tmp_path, text, name, reason):
    with pytest.raises(DocumentRejected, match=reason):  # 의도한 규칙에 걸렸는지까지
        parse_document(write(tmp_path, text, name), TAXONOMY)


def test_folder_load_keeps_valid_documents_and_lists_rejected_files(tmp_path):
    write(tmp_path, FRONT + BODY)
    write(tmp_path, "형식 없음", "broken.md")
    (tmp_path / "notes.txt").write_text("md가 아니면 읽지 않음", encoding="utf-8")
    documents, rejected = load_documents(tmp_path, TAXONOMY)
    assert [doc.doc_id for doc in documents] == ["pm-2099-001"]
    assert list(rejected) == ["broken.md"]
    assert "형식 없음" not in rejected["broken.md"]  # 이유에 본문을 넣지 않음


def test_repository_sample_documents_are_all_valid():
    documents, rejected = load_documents(SAMPLE_DOCS, TAXONOMY)
    assert rejected == {}
    assert len(documents) == 12
    assert sum(len(doc.sections) for doc in documents) == 48
