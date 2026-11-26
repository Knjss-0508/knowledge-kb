import json

import pytest
from pydantic import ValidationError

from app.services.blind_label_replay import ReplayManifest
from scripts.prepare_blind_replay_review_packet import RecallAudit, build_packet


def test_packet_is_non_runnable_and_contains_only_review_requirements():
    packet = build_packet(RecallAudit.model_validate({
        "schema_version": 2,
        "scope": "retained_event_candidate_presence_not_review",
        "hints": [
            {
                "work_order_id": "w1", "knowledge_id": "k1", "finding": "not_seen_in_checked_events",
                "original_event_rank": None, "events_checked": 1,
                "query_text": "do not export",
            },
            {
                "work_order_id": "w2", "knowledge_id": "k2", "finding": "present_in_original_event",
                "original_event_rank": 4, "events_checked": 1,
                "has_structured_scope_fields": False, "has_recorded_reply": False,
            },
        ],
    }))
    assert packet["runnable"] is False
    assert packet["summary"] == {
        "cases": 2, "recall_candidate_review": 1, "ranking_candidate_review": 1, "replay_ready": 0,
    }
    assert packet["cases"][0]["request_template"]["normalizedQuestion"] is None
    assert packet["cases"][0]["review_id"] is None
    assert "request_not_supplied_or_reviewed" in packet["cases"][1]["blockers"]
    assert "do not export" not in json.dumps(packet)
    with pytest.raises(ValidationError):
        ReplayManifest.model_validate(packet)


def test_packet_rejects_wrong_audit_scope():
    with pytest.raises(ValidationError):
        RecallAudit.model_validate({"schema_version": 2, "scope": "not-an-audit", "hints": []})
