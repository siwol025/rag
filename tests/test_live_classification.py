"""실제 Gemini 분류 품질 확인(수동). 기본은 건너뜀.

실행: LIVE_LLM=1 LIVE_LLM_RPM=<무료 RPM> uv run pytest tests/test_live_classification.py -s
(프로젝트 루트에서: .env의 키를 읽음). 무료 요금제 한도를 쓰므로 샘플 수만큼(12회)만,
분당 한도에서 계산한 간격(60 / RPM + 여유)을 두고 부른다. 분당 429가 나면 llm.py가 60초씩 기다린다.
돌리는 시점: 모델·프롬프트·분류 체계를 바꿀 때, 유료 전환 전(3.13).
"""
import os
import tomllib
from pathlib import Path

import anyio
import pytest

from slack_rag import llm
from slack_rag.config import Settings
from slack_rag.taxonomy import load_taxonomy

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="실제 Gemini 호출(LIVE_LLM=1일 때만)"),
]

SAMPLES = tomllib.loads(Path(__file__).with_name("classification_samples.toml").read_text(encoding="utf-8"))["samples"]
LABELS = ("request_type", "service", "category")
MIN_ACCURACY = 0.8  # 세 라벨이 모두 맞은 샘플 비율의 하한. 샘플이 늘면 다시 정한다(3.13)
# 무료 요금제 분당 요청 한도(AI Studio 화면에서 확인, 6.7-2). 모르면 보수적으로 5(호출 간격 13초)
RPM = int(os.environ.get("LIVE_LLM_RPM", "5"))
CALL_INTERVAL_S = 60 / RPM + 1  # 한도 안에 들도록 1초 여유


async def test_sample_messages_against_expected_labels():
    settings = Settings()
    client = llm.make_client(settings)
    taxonomy = load_taxonomy()
    results = []
    try:
        for i, sample in enumerate(SAMPLES):
            if i:
                await anyio.sleep(CALL_INTERVAL_S)
            response = await llm.request_classification(client, settings.llm_light_model, sample["text"], taxonomy)
            results.append((sample, llm.parse_classification(response, taxonomy), llm.usage_of(response)))
    finally:
        await client.aio.aclose()
        client.close()

    wrong = []
    for sample, got, usage in results:
        want = {key: sample[key] for key in LABELS}
        if got != want:
            wrong.append(f"{sample['text'][:40]!r} want={want} got={got}")
        print("OK  " if got == want else "DIFF", sample["text"][:40], "got", got, usage)
    for key in LABELS:
        matched = sum(got[key] == sample[key] for sample, got, _ in results)
        print(f"{key}: {matched}/{len(results)} 일치")
    accuracy = 1 - len(wrong) / len(results)
    print(f"정확도(세 라벨 모두 일치): {accuracy:.2f} (기준 {MIN_ACCURACY})")
    print("틀린 목록:", *wrong, sep="\n  ")
    assert accuracy >= MIN_ACCURACY, f"분류 정확도 {accuracy:.2f} < {MIN_ACCURACY}:\n" + "\n".join(wrong)
