from slack_rag.schemas import SlackMessageEvent

ACCEPTED_SUBTYPES = {"file_share"}  # 사람의 파일 첨부 메시지(1.5)


def ignore_reason(event: SlackMessageEvent, target_channel_ids: set[str]) -> str | None:
    """작업으로 접수하지 않을 이벤트면 사유를, 접수할 이벤트면 None을 돌려준다."""
    if event.channel not in target_channel_ids:
        return "channel_not_targeted"
    if event.subtype is not None and event.subtype not in ACCEPTED_SUBTYPES:
        return "has_subtype"
    if event.bot_id is not None or event.user is None:
        return "not_human"
    if event.thread_ts is not None and event.thread_ts != event.ts:
        return "thread_reply"
    return None
