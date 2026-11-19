"""테스트 대역과 공용 값. 테스트 모듈과 테스트가 띄우는 워커 프로세스(tests/*_worker.py)가 함께 쓴다."""
import hashlib
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

from google.genai import errors, types

from slack_rag.llm import EMBEDDING_DIMS
from slack_rag.taxonomy import UNKNOWN, load_taxonomy

LLM_SETTINGS = {  # 실제 키·모델 아님
    "gemini_api_key": "test-gemini-key",
    "llm_light_model": "fake-light-model",
    "embedding_model": "fake-embedding-model",
}
SAMPLE_DOCS = Path(__file__).parents[1] / "sample_docs"  # 저장소의 가상 샘플 문서(TO-013)

# 목록 파일을 사용자가 바꿔도 테스트가 깨지지 않게 실제 목록의 앞 두 ID를 쓴다(3.10)
_TAXONOMY = load_taxonomy()
SERVICE, OTHER_SERVICE = list(_TAXONOMY.services)[:2]
CATEGORY, OTHER_CATEGORY = list(_TAXONOMY.categories)[:2]

QUESTION = {"request_type": "question", "service": SERVICE, "category": CATEGORY}
CODE_FIX = {"request_type": "code_fix", "service": OTHER_SERVICE, "category": OTHER_CATEGORY}
OTHER = {"request_type": "other", "service": UNKNOWN, "category": UNKNOWN}


def response_with(answer):
    """실제 SDK 응답 타입. answer가 dict면 JSON으로, str이면 그대로 모델 출력 텍스트로."""
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=120, candidates_token_count=15, thoughts_token_count=4, total_token_count=139
        ),
    )


FAKE_MIN_SIMILARITY = 0.20  # fake_vector 척도의 "근거 있음" 기준(04-rag-document-search-plan 3.13)


def fake_vector(text):
    """결정적 가짜 임베딩: 공백을 뺀 글자 2개 묶음마다 768칸 중 한 칸에 1을 더하고 길이 1로 맞춘다.
    글자를 많이 공유할수록 코사인 유사도가 높다(의미는 모름)."""
    counts = [0.0] * EMBEDDING_DIMS
    chars = "".join(text.split())
    for pair in zip(chars, chars[1:]):
        counts[int.from_bytes(hashlib.sha256("".join(pair).encode()).digest()[:4], "big") % EMBEDDING_DIMS] += 1.0
    norm = math.sqrt(sum(count * count for count in counts)) or 1.0  # 글자가 1개 이하면 영벡터
    return [count / norm for count in counts]


DOCUMENT_ID = re.compile(r'<document id="([^"]+)">')


def cite_first(contents):
    """답변 요청의 첫 조각(유사도 1등)을 인용한 가짜 답."""
    return {"found": True, "answer": "가짜 답변입니다.", "citations": DOCUMENT_ID.findall(contents)[:1]}


def classify_then_cite(classification):
    """FakeGenai(answer=…)용: 분류 요청이면 classification, 답변 요청(<question>으로 시작)이면 cite_first."""
    return lambda contents: cite_first(contents) if contents.startswith("<question>") else classification


class FakeGenai:
    """google.genai.Client 대역: client.aio.models.generate_content·embed_content와 client.aio.aclose만 흉내 낸다.

    answer: 생성 호출의 결과. 분류·답변 dict, 모델 출력 str, 던질 예외, 또는 contents를 받아 그중 하나를 돌려주는 함수.
    embed: 임베딩 호출의 결과. None이면 입력마다 fake_vector, 예외면 던짐, 함수면 입력 목록을 받아 벡터 목록·None·예외.
    """

    def __init__(self, answer, embed=None):
        self.answer = answer
        self.embed = embed
        self.calls = []        # 생성 호출: 받은 {"model", "contents", "config"}
        self.embed_calls = []  # 임베딩 호출: 받은 {"model", "inputs", "config"}
        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._generate_content, embed_content=self._embed_content),
            aclose=self._aclose,
        )

    async def _generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        answer = self.answer(contents) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return response_with(answer)

    async def _embed_content(self, *, model, contents, config):
        # 문자열 목록이면 실제 SDK는 벡터 1개로 합친다(묶음 함정, 04-rag-document-search-plan 1.6) → 여기서 막는다
        assert all(isinstance(c, types.Content) and len(c.parts) == 1 for c in contents), "wrap each text in Content"
        inputs = [c.parts[0].text for c in contents]
        self.embed_calls.append({"model": model, "inputs": inputs, "config": config})
        result = self.embed(inputs) if callable(self.embed) else self.embed
        if isinstance(result, Exception):
            raise result
        vectors = [fake_vector(text) for text in inputs] if result is None else result
        return types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=vector) for vector in vectors],
            usage_metadata=types.UsageMetadata(prompt_token_count=sum(len(text) for text in inputs)),  # 글자 수를 토큰으로
        )

    async def _aclose(self):
        pass


DAILY_QUOTA = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"      # 일일 한도
MINUTE_QUOTA = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"  # 분당 한도


def quota_error(quota_id):
    """Gemini API 무료 요금제 한도 초과(429) 흉내. 본문 형식은 커뮤니티 보고 기준(1.6, 미확인)."""
    return errors.ClientError(429, {"error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"},
        ],
    }})
