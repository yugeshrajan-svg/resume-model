import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.api.routes_recruiting import ActivityIn, ics_text


def test_interview_and_feedback_require_complete_inputs():
    candidate = uuid.uuid4()
    for fields in (
        {"kind": "interview", "candidate_id": candidate},
        {"kind": "feedback", "candidate_id": candidate, "notes": "Evidence"},
        {"kind": "offer", "candidate_id": candidate},
        {"kind": "note"},
        {"kind": "task", "due_at": "2026-10-07T12:00:00"},
    ):
        with pytest.raises(ValidationError):
            ActivityIn(title="Test record", **fields)
    start = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ActivityIn(kind="interview", title="Interview", candidate_id=candidate, due_at=start, end_at=start)
    assert ActivityIn(kind="task", title="Follow up").candidate_id is None


def test_calendar_escapes_injection_and_folds_unicode_without_breaking_characters():
    start = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        status="open",
        title="Review " + "能力" * 80,
        notes="Notes\r\nATTENDEE:malicious",
        due_at=start,
        end_at=start + timedelta(hours=1),
        details={"location": "Office, room;1"},
    )
    text = ics_text(row)
    assert "\r\nATTENDEE:" not in text
    assert "DESCRIPTION:Notes\\nATTENDEE:malicious" in text
    assert "LOCATION:Office\\, room\\;1" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "能力" * 80 in text.replace("\r\n ", "")
    assert "STATUS:CONFIRMED" in text
    row.status = "cancelled"
    assert "STATUS:CANCELLED" in ics_text(row)
