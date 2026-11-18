"""테스트 대역과 공용 값. 테스트 모듈과 테스트가 띄우는 워커 프로세스(tests/*_worker.py)가 함께 쓴다."""
import json
from types import SimpleNamespace

from google.genai import errors, types

from slack_rag.taxonomy import UNKNOWN, load_taxonomy

LLM_SETTINGS = {"gemini_api_key": "test-gemini-key", "llm_light_model": "fake-light-model"}  # 실제 키·모델 아님

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


class FakeGenai:
    """google.genai.Client 대역: client.aio.models.generate_content와 client.aio.aclose만 흉내 낸다.

    answer: 매 호출의 결과. 분류 dict·모델 출력 str·던질 예외, 또는 contents를 받아 그중 하나를 돌려주는 함수.
    """

    def __init__(self, answer):
        self.answer = answer
        self.calls = []  # 받은 {"model", "contents", "config"}
        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._generate_content), aclose=self._aclose
        )

    async def _generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        answer = self.answer(contents) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return response_with(answer)

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
