import pytest
from pydantic import ValidationError

from slack_rag.config import Settings


def test_reads_dotenv_shared_with_compose(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "JOBS_API_KEY", "SLACK_TARGET_CHANNEL_IDS"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=unrelated\n"
        "DATABASE_URL=postgresql://u:p@localhost:5432/db\n"
        "JOBS_API_KEY=secret\n"
        "SLACK_TARGET_CHANNEL_IDS='[\"C0TARGET01\", \"C0TARGET02\"]'\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.slack_target_channel_ids == {"C0TARGET01", "C0TARGET02"}


def test_rejects_empty_api_key():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql://u:p@localhost:5432/db",
            jobs_api_key="",
            slack_target_channel_ids={"C0TARGET01"},
        )
