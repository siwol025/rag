"""분류 체계(서비스·카테고리). 목록을 읽는 곳은 load_taxonomy() 하나다(TO-006).

나중에 서비스 카탈로그 연동으로 바꿀 때 이 함수 안의 "파일 읽기"만 "카탈로그 조회"로 바꾼다.
"""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

TAXONOMY_PATH = Path(__file__).with_name("taxonomy.toml")
UNKNOWN = "unknown"  # 목록에 없거나 판단할 수 없을 때. 파일에는 쓰지 않고 코드가 항상 덧붙인다
ID_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # 바뀌지 않는 영문 소문자 꼬리표


@dataclass(frozen=True)
class Taxonomy:
    services: dict[str, str]    # ID → 표시 이름
    categories: dict[str, str]


def load_taxonomy(path: Path = TAXONOMY_PATH) -> Taxonomy:
    data = tomllib.loads(path.read_text(encoding="utf-8"))  # 같은 ID가 두 번 나오면 TOMLDecodeError
    taxonomy = Taxonomy(services=data["services"], categories=data["categories"])
    for ids in (taxonomy.services, taxonomy.categories):
        for id_ in ids:
            if not ID_PATTERN.fullmatch(id_) or id_ == UNKNOWN:
                raise ValueError(f"taxonomy id must be lowercase letters, digits and hyphens, not {UNKNOWN!r}: {id_!r}")
    return taxonomy
