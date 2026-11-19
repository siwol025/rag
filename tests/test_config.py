import pytest
from pydantic import ValidationError

from fakes import LLM_SETTINGS
from slack_rag.config import Settings

ENV_NAMES = (
    "DATABASE_URL", "REDIS_URL", "JOBS_API_KEY", "SLACK_TARGET_CHANNEL_IDS", "GEMINI_API_KEY", "LLM_LIGHT_MODEL",
    "EMBEDDING_MODEL",
)
BASE = {
    "database_url": "postgresql://u:p@localhost:5432/db",
    "redis_url": "redis://localhost:6379/0",
    "jobs_api_key": "secret",
    "slack_target_channel_ids": {"C0TARGET01"},
}


def test_reads_dotenv_shared_with_compose(tmp_path, monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=unrelated\n"
        "DATABASE_URL=postgresql://u:p@localhost:5432/db\n"
        "REDIS_URL=redis://localhost:6379/0\n"
        "JOBS_API_KEY=secret\n"
        "SLACK_TARGET_CHANNEL_IDS='[\"C0TARGET01\", \"C0TARGET02\"]'\n"
        "GEMINI_API_KEY=secret-gemini-key\n"
        "LLM_LIGHT_MODEL=gemini-3.5-flash-lite\n"
        "EMBEDDING_MODEL=gemini-embedding-2\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.slack_target_channel_ids == {"C0TARGET01", "C0TARGET02"}
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.llm_light_model == "gemini-3.5-flash-lite"
    assert settings.embedding_model == "gemini-embedding-2"
    assert settings.gemini_api_key.get_secret_value() == "secret-gemini-key"
    assert "secret-gemini-key" not in repr(settings)  # 로그·예외에 찍혀도 값이 드러나지 않음


def test_rejects_empty_api_key():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **BASE | {"jobs_api_key": ""}, **LLM_SETTINGS)  # 빈 키 때문에만 실패하도록


def test_requires_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    base = {k: v for k, v in BASE.items() if k != "redis_url"}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **base, **LLM_SETTINGS)


@pytest.mark.parametrize("missing", ["gemini_api_key", "llm_light_model", "embedding_model"])
def test_requires_gemini_settings(monkeypatch, missing):
    monkeypatch.delenv(missing.upper(), raising=False)  # 셸에 같은 이름이 있어도
    llm = {k: v for k, v in LLM_SETTINGS.items() if k != missing}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **BASE, **llm)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **BASE, **llm, **{missing: ""})  # 빈 값도 거부
