from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jobs_api_key: str = Field(min_length=1)
    slack_target_channel_ids: set[str] = Field(min_length=1)
    gemini_api_key: SecretStr = Field(min_length=1)  # Gemini API(AI Studio) 키(TO-008). repr·로그에는 ****
    llm_light_model: str = Field(min_length=1)       # 경량 역할의 모델 ID(TO-007). 예: gemini-3.5-flash-lite
    embedding_model: str = Field(min_length=1)       # 임베딩 모델 ID(TO-011). 예: gemini-embedding-2
