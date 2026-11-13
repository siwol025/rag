import pytest

from slack_rag.filters import ignore_reason
from slack_rag.schemas import SlackMessageEvent

TARGETS = {"C0TARGET01"}


def reason(payload: dict) -> str | None:
    return ignore_reason(SlackMessageEvent.model_validate(payload), TARGETS)


def test_accepts_top_level_human_message(make_event):
    assert reason(make_event()) is None


def test_ignores_message_in_other_channel(make_event):
    assert reason(make_event(channel="C0OTHER001")) == "channel_not_targeted"


@pytest.mark.parametrize(
    "subtype",
    ["message_changed", "message_deleted", "bot_message", "channel_join", "thread_broadcast"],
)
def test_ignores_subtype_other_than_file_share(make_event, subtype):
    assert reason(make_event(subtype=subtype)) == "has_subtype"


def test_accepts_human_message_with_file_attachment(make_event):
    # 사람이 스크린샷 등을 첨부하면 Slack이 subtype: file_share를 붙임 → 접수(1.5)
    assert reason(make_event(subtype="file_share")) is None


def test_ignores_file_share_posted_by_bot(make_event):
    assert reason(make_event(subtype="file_share", bot_id="B0OURBOT01")) == "not_human"


def test_ignores_app_message_with_bot_id(make_event):
    # chat.postMessage로 앱이 올린 메시지: subtype 없이 bot_id와 봇 user가 옴
    assert reason(make_event(bot_id="B0OURBOT01", user="U0BOTUSER1")) == "not_human"


def test_ignores_message_without_user(make_event):
    payload = make_event()
    del payload["user"]
    assert reason(payload) == "not_human"


def test_ignores_thread_reply(make_event):
    assert reason(make_event(ts="1794381700.000200", thread_ts="1794381600.000100")) == "thread_reply"


def test_accepts_thread_parent_whose_thread_ts_equals_ts(make_event):
    assert reason(make_event(thread_ts="1794381600.000100")) is None
