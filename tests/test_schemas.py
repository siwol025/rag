import pytest
from pydantic import ValidationError

from slack_rag.schemas import SlackMessageEvent


def test_parses_human_message_and_ignores_unknown_fields(make_event):
    event = SlackMessageEvent.model_validate(
        make_event(team="T0TEAM0001", blocks=[{"type": "rich_text"}])
    )
    assert (event.channel, event.ts, event.user) == ("C0TARGET01", "1794381600.000100", "U0HUMAN01")
    assert (event.subtype, event.bot_id, event.thread_ts) == (None, None, None)


def test_accepts_message_changed_event_without_user_and_text():
    # Slack 문서의 message_changed 내부 event 예시 모양
    event = SlackMessageEvent.model_validate({
        "type": "message",
        "subtype": "message_changed",
        "hidden": True,
        "channel": "C0TARGET01",
        "ts": "1358878755.000001",
        "message": {"type": "message", "user": "U0HUMAN01", "text": "수정됨"},
    })
    assert event.subtype == "message_changed"
    assert event.user is None


@pytest.mark.parametrize("missing", ["channel", "ts"])
def test_rejects_event_without_dedup_key(make_event, missing):
    payload = make_event()
    del payload[missing]
    with pytest.raises(ValidationError):
        SlackMessageEvent.model_validate(payload)


def test_rejects_malformed_ts(make_event):
    with pytest.raises(ValidationError):
        SlackMessageEvent.model_validate(make_event(ts="not-a-ts"))


def test_rejects_non_message_event(make_event):
    with pytest.raises(ValidationError):
        SlackMessageEvent.model_validate(make_event(type="reaction_added"))
