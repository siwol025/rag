import json
import logging
from uuid import UUID

from slack_rag.logs import JsonFormatter


def test_formats_record_as_one_json_line_with_fields():
    record = logging.makeLogRecord({
        "name": "slack_rag.api",
        "levelname": "INFO",
        "msg": "job_accepted",
        "fields": {"job_id": UUID("6f1c2a7e-0b7d-4a43-9a52-3c1d0e8f9b21"), "channel_id": "C0TARGET01"},
    })
    line = JsonFormatter().format(record)

    assert "\n" not in line
    data = json.loads(line)
    assert data["event"] == "job_accepted"
    assert data["level"] == "INFO"
    assert data["job_id"] == "6f1c2a7e-0b7d-4a43-9a52-3c1d0e8f9b21"
    assert data["channel_id"] == "C0TARGET01"
