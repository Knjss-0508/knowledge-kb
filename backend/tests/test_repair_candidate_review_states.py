from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from app.scripts import repair_candidate_review_states as script
from backend.tests.test_candidate_review_state import _candidate_item


@pytest.mark.parametrize("apply", [False, True])
def test_scoped_repair_preserves_draft_and_human_truth_and_skips_locked(apply):
    item = _candidate_item(model_review={
        "suggested_action": "submit_for_human_review",
        "knowledge_value": "worthy", "confidence": 0.95,
    })
    item.candidate_payload["selection"] = {
        "eligible": False, "reasons": ["自动审核：auto_approved"],
    }
    locked = _candidate_item(knowledge_id="knowledge-1")
    locked.id = "locked"
    before = deepcopy(item.candidate_payload)
    locked_before = deepcopy(locked.__dict__)
    db = MagicMock()
    query = db.query.return_value.filter.return_value
    query.all.return_value = [item, locked]
    query.with_for_update.return_value = query

    results = script.repair_candidate_states(
        db, [item.id, "locked", "missing", item.id], apply=apply,
    )
    assert len(results) == 3
    assert results[0]["before"] == "pending"
    assert results[0]["after"] == "ready"
    assert results[0]["result"] == ("applied" if apply else "preview")
    assert results[1]["result"] == results[2]["result"] == "skipped"
    assert locked.__dict__ == locked_before
    assert item.candidate_payload["knowledge"] == before["knowledge"]
    assert item.candidate_payload["human_review"] == before["human_review"]
    assert db.commit.call_count == int(apply)
    assert db.rollback.call_count == int(not apply)
    assert query.with_for_update.call_count == int(apply)
    assert query.filter.call_count == 0
    filters = db.query.return_value.filter.call_args.args
    assert list(filters[0].right.value) == [item.id, "locked", "missing"]


@pytest.mark.parametrize("ids", [[], [str(index) for index in range(101)]])
def test_repair_requires_explicit_bounded_ids(ids):
    db = MagicMock()
    with pytest.raises(ValueError):
        script.repair_candidate_states(db, ids)
    db.query.assert_not_called()


def test_repair_rolls_back_on_error(monkeypatch):
    item = _candidate_item()
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [item]
    def fail(*args):
        raise RuntimeError("state calculation failed")
    monkeypatch.setattr(script, "_persist_candidate_state", fail)
    with pytest.raises(RuntimeError, match="state calculation failed"):
        script.repair_candidate_states(db, [item.id])
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


@pytest.mark.parametrize("apply", [False, True])
def test_database_preview_rolls_back_and_apply_updates_only_selected_candidate(apply):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    model = script.IntegrationIngestion
    engine = create_engine("sqlite://")
    model.__table__.create(engine)
    try:
        with Session(engine) as db:
            for key, source in [("selected", "answer-hub"), ("untouched", "answer-hub"), ("excel", "excel")]:
                values = deepcopy(_candidate_item().__dict__)
                values.update(id=key, idempotency_key=key, source_system=source)
                values["candidate_payload"]["model_review"] = {
                    "suggested_action": "submit_for_human_review",
                    "knowledge_value": "worthy", "confidence": 0.95,
                }
                values["candidate_payload"]["selection"] = {
                    "eligible": False, "reasons": ["自动审核：auto_approved"],
                }
                db.add(model(**values))
            db.commit()
            original = deepcopy(db.get(model, "selected").candidate_payload)
            result = script.repair_candidate_states(db, ["selected", "excel"], apply=apply)
            assert result[0]["after"] == "ready"
            assert result[1]["result"] == "skipped"
            db.expire_all()
            selected = db.get(model, "selected")
            assert selected.review_status == ("ready" if apply else "pending")
            if not apply:
                assert selected.candidate_payload == original
            assert selected.candidate_payload["knowledge"] == original["knowledge"]
            assert selected.candidate_payload["human_review"] == original["human_review"]
            assert db.get(model, "untouched").review_status == "pending"
            assert db.get(model, "excel").review_status == "pending"
    finally:
        engine.dispose()
