from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jobs_api_key: str = Field(min_length=1)
    slack_target_channel_ids: set[str] = Field(min_length=1)
