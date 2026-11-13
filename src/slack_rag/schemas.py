from typing import Literal

from pydantic import BaseModel, Field


class SlackMessageEvent(BaseModel):
    """n8n Slack Trigger가 그대로 넘기는 Slack 내부 event 객체(message 타입)."""

    type: Literal["message"]
    channel: str = Field(min_length=1)
    ts: str = Field(pattern=r"^\d+\.\d+$")
    user: str | None = None
    text: str = ""
    subtype: str | None = None
    bot_id: str | None = None
    thread_ts: str | None = None
