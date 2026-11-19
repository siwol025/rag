"""색인할 문서 읽기(순수): TOML 앞머리 검증과 섹션 나누기. DB·LLM을 모른다(04-rag-document-search-plan 3.3).

형식(TO-013): 맨 앞 +++ … +++ 사이에 TOML 앞머리, 그 뒤 ## 증상 / 원인 / 조치 / 재발 방지 네 섹션.
섹션 하나가 조각(청크) 하나다(04-rag-document-search-plan 가정 C5).
"""
import datetime
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from slack_rag.taxonomy import ID_PATTERN, Taxonomy

SECTION_HEADINGS = ("증상", "원인", "조치", "재발 방지")  # 이 순서로 하나씩(TO-013)
MAX_SECTION_CHARS = 1_500  # 넘으면 파일 거부: 임베딩 입력이 조용히 잘리지 않게(TO-011, 04-rag-document-search-plan 가정 C5)
FRONT_MATTER = re.compile(r"\A\+\+\+\n(.*?)\n\+\+\+\n(.*)\Z", re.DOTALL)
HEADING = re.compile(r"^## (.+)$", re.MULTILINE)


class DocumentRejected(ValueError):
    """형식이 맞지 않아 색인하지 않는 문서. 메시지는 짧은 고정 이유(본문·앞머리 값을 넣지 않음)."""


class FrontMatter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # 모르는 키, 따옴표 친 날짜 거부

    id: str
    title: str = Field(min_length=1)
    service: str
    category: str
    doc_type: Literal["postmortem", "runbook"]
    date: datetime.date


@dataclass(frozen=True)
class Section:
    ordinal: int  # 1부터. 조각 ID는 f"{doc_id}#{ordinal}"
    heading: str
    text: str


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    service: str
    category: str
    doc_type: str
    date: datetime.date
    content_hash: str  # 파일 전체 sha256: 같으면 색인이 이 문서를 건너뛴다(04-rag-document-search-plan 3.5)
    sections: tuple[Section, ...]


def parse_document(path: Path, taxonomy: Taxonomy) -> Document:
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    if match is None:
        raise DocumentRejected("front matter (+++ ... +++) is missing")
    try:
        meta = FrontMatter.model_validate(tomllib.loads(match.group(1)))
    except (tomllib.TOMLDecodeError, ValidationError):
        raise DocumentRejected("front matter must have exactly id, title, service, category, doc_type, date") from None
    if meta.id != path.stem or not ID_PATTERN.fullmatch(meta.id):
        raise DocumentRejected("id must be a lowercase id equal to the file name")
    if meta.service not in taxonomy.services or meta.category not in taxonomy.categories:
        raise DocumentRejected("service or category is not in the taxonomy")
    parts = HEADING.split(match.group(2))  # [첫 ## 앞, 제목1, 본문1, 제목2, 본문2, …]
    if parts[0].strip():
        raise DocumentRejected("text outside sections")
    headings = tuple(heading.strip() for heading in parts[1::2])
    texts = [text.strip() for text in parts[2::2]]
    if headings != SECTION_HEADINGS:
        raise DocumentRejected("sections must be " + " / ".join(SECTION_HEADINGS) + " in this order")
    if not all(texts):
        raise DocumentRejected("empty section")
    if any(len(text) > MAX_SECTION_CHARS for text in texts):
        raise DocumentRejected(f"section longer than {MAX_SECTION_CHARS} characters")
    return Document(
        doc_id=meta.id,
        title=meta.title,
        service=meta.service,
        category=meta.category,
        doc_type=meta.doc_type,
        date=meta.date,
        content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        sections=tuple(Section(i, h, t) for i, (h, t) in enumerate(zip(headings, texts), start=1)),
    )


def load_documents(folder: Path, taxonomy: Taxonomy) -> tuple[list[Document], dict[str, str]]:
    """폴더의 *.md를 파일 이름순으로 읽는다. (올바른 문서, {거부된 파일 이름: 이유})."""
    documents, rejected = [], {}
    for path in sorted(folder.glob("*.md")):
        try:
            documents.append(parse_document(path, taxonomy))
        except DocumentRejected as exc:
            rejected[path.name] = str(exc)
    return documents, rejected
