"""Activity summaries carry only allowed, typed keys."""

from __future__ import annotations

from uuid import uuid4

import pytest

from beluno.modules.activity.events import ActivityItem, ActivityType, item


def test_ids_become_strings_and_only_allowed_keys_pass() -> None:
    participant = uuid4()
    joined = item(ActivityType.MEMBER_JOINED, participant_id=participant, role="member")
    assert joined.summary == {"participant_id": str(participant), "role": "member"}
    for text_key in ("description", "notes", "display_name", "token", "address"):
        with pytest.raises(ValueError, match="not allowed"):
            ActivityItem(ActivityType.EXPENSE_ADDED, {text_key: "anything"})
    with pytest.raises(TypeError):
        ActivityItem(ActivityType.MEMBER_LEFT, {"participant_id": participant})  # type: ignore[dict-item]
