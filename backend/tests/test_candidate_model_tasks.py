from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.models.integration import CandidateModelAutomation, CandidateModelTask, IntegrationIngestion
from app.models.user import User
from app.routes import integration
from app.routes.auth import get_current_user
from app.schemas.integration import (
    CandidateReviewBatchAnnotateResponse, CandidateReviewAnnotateResult,
    CandidateReviewBatchSubmitResponse, CandidateReviewSubmitResult,
)
from app.services.candidate_model_tasks import (
    enqueue_task, process_next_candidate_model_task, schedule_automatic_candidates, task_snapshot,
)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "tasks.db"))
    for model in (User, IntegrationIngestion, CandidateModelTask, CandidateModelAutomation):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(User(id="reviewer", username="reviewer", password_hash="unused",
                    role="senior_support", is_active=True))
        db.add(CandidateModelAutomation(id="default", enabled=False, requested_by="reviewer"))
        db.commit()
    yield factory
    engine.dispose()


def add_candidate(db, identity, **changes):
    values = dict(
        id=identity, event_id=identity, idempotency_key=identity,
        source_system="answer-hub", source_conversation_id=identity,
        review_status="pending_model_annotation", status="candidate_pending",
        candidate_payload={"knowledge": {
            "title": "无人机实名状态核对",
            "content": {"blocks": [{"type": "text", "value": "核对登记状态"}]},
            "recommended_reply": "请核对登记状态。",
            "business_type": "self_operated", "category_id": "cat-process",
            "applicable_categories": ["无人机"], "evidence_excerpt": "来源明确登记状态",
        }}, review_metadata={},
    )
    values.update(changes)
    item = IntegrationIngestion(**values)
    db.add(item)
    return item


@pytest.mark.parametrize("batch_size", [20, 50, 100])
def test_http_accepts_batch_without_running_any_model_and_returns_persisted_progress(sessions, monkeypatch, batch_size):
    monkeypatch.setattr(integration, "annotate_transcribed_candidate",
                        lambda *_args, **_kwargs: pytest.fail("HTTP request must not call a model"))
    app = FastAPI()
    app.include_router(integration.router)
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    with sessions() as db:
        user = db.get(User, "reviewer")
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        response = client.post("/integration/candidate-model-tasks", json={
            "ingestion_ids": ["candidate-" + str(n) for n in range(batch_size)],
        })
        assert response.status_code == 202
        data = response.json()
        assert (data["total"], data["completed"], data["status"]) == (batch_size, 0, "queued")
        assert client.get("/integration/candidate-model-tasks/" + data["id"]).json()["total"] == batch_size
        duplicate = client.post("/integration/candidate-model-tasks", json={
            "ingestion_ids": ["candidate-0"],
        })
        assert duplicate.status_code == 409


def test_worker_checkpoints_each_item_and_continues_after_failure(sessions, monkeypatch):
    calls = []
    def run(body, db, user):
        identity = body.ingestion_ids[0]
        assert body.apply_model_revisions and body.submit_ready
        calls.append(identity)
        if identity == "bad":
            raise RuntimeError("fixture failure")
        return CandidateReviewBatchAnnotateResponse(
            updated=1, failed=0, results=[
                CandidateReviewAnnotateResult(ingestion_id=identity, status="updated")
            ],
        )
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        job = enqueue_task(db, ["one", "bad", "three"], "reviewer")
        identity = job.id
        db.commit()
    for count in (1, 2, 3):
        assert process_next_candidate_model_task(sessions)
        with sessions() as db:
            snapshot = task_snapshot(db.get(CandidateModelTask, identity))
            assert snapshot["completed"] == count
    assert calls == ["one", "bad", "three"]
    assert snapshot["status"] == "completed"
    assert (snapshot["updated"], snapshot["failed"]) == (2, 1)
    assert not process_next_candidate_model_task(sessions)


def test_expired_lease_resumes_only_unfinished_candidate(sessions, monkeypatch):
    calls = []
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", lambda body, *_: (
        calls.append(body.ingestion_ids[0]) or CandidateReviewBatchAnnotateResponse(
            updated=1, failed=0, results=[])
    ))
    with sessions() as db:
        job = enqueue_task(db, ["done", "remaining"], "reviewer")
        job.status = "running"
        job.lease_token = "old-worker"
        job.lease_expires_at = datetime.utcnow() + timedelta(minutes=1)
        job.results = [{"updated": 1, "results": []}]
        identity = job.id
        db.commit()
    assert not process_next_candidate_model_task(sessions)
    with sessions() as db:
        db.get(CandidateModelTask, identity).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert calls == ["remaining"]
    with sessions() as db:
        assert db.get(CandidateModelTask, identity).status == "completed"


def test_interrupted_revision_is_not_applied_twice_and_later_candidates_continue(sessions, monkeypatch):
    calls = []
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", lambda body, *_: (
        calls.extend(body.ingestion_ids) or CandidateReviewBatchAnnotateResponse(
            updated=1, failed=0, results=[])
    ))
    with sessions() as db:
        job = enqueue_task(db, ["interrupted", "next"], "reviewer")
        identity = job.id
        job.status = "running"
        job.current_ingestion_id = "interrupted"
        job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert calls == []
    assert process_next_candidate_model_task(sessions)
    assert calls == ["next"]
    with sessions() as db:
        snapshot = task_snapshot(db.get(CandidateModelTask, identity))
        assert snapshot["status"] == "completed"
        assert snapshot["results"][0]["error_code"] == "MODEL_WORKFLOW_INTERRUPTED"


def test_old_failed_tasks_are_available_on_history_pages(sessions):
    with sessions() as db:
        for n in range(12):
            db.add(CandidateModelTask(
                id=f"history-{n:02}", status="completed", source="automatic",
                requested_by="reviewer", ingestion_ids=[str(n)],
                results=[{"failed": 1, "results": [{"ingestion_id": str(n), "status": "failed"}]}],
                created_at=datetime(2026, 10, 1) + timedelta(minutes=n),
            ))
        db.commit()
        page = integration.get_candidate_model_automation(2, db, db.get(User, "reviewer"))
        assert page["task_total"] == 12
        assert [task["id"] for task in page["tasks"]] == ["history-01", "history-00"]


def test_automatic_scan_is_opt_in_and_preserves_reviewed_and_submitted_rows(sessions):
    with sessions() as db:
        add_candidate(db, "eligible")
        add_candidate(db, "human", reviewed_at=datetime.utcnow())
        add_candidate(db, "submitted", knowledge_id="K-1")
        assert schedule_automatic_candidates(db) is None
        db.get(CandidateModelAutomation, "default").enabled = True
        db.commit()
        task = schedule_automatic_candidates(db)
        assert task.ingestion_ids == ["eligible"]
        db.commit()
        assert schedule_automatic_candidates(db) is None


def test_auto_processed_version_waits_for_draft_change_or_manual_retry(sessions, monkeypatch):
    calls = []
    def run(body, *_):
        calls.extend(body.ingestion_ids)
        return CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(ingestion_id=body.ingestion_ids[0], status="failed")
        ])
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        add_candidate(db, "candidate")
        db.get(CandidateModelAutomation, "default").enabled = True
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert not process_next_candidate_model_task(sessions)
    with sessions() as db:
        item = db.get(IntegrationIngestion, "candidate")
        item.review_metadata = {**item.review_metadata, "draft_version": 2}
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert calls == ["candidate", "candidate"]


def test_permission_revoked_before_work_prevents_model_calls(sessions, monkeypatch):
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews",
                        lambda *_: pytest.fail("revoked actor must not execute"))
    with sessions() as db:
        job = enqueue_task(db, ["candidate"], "reviewer")
        identity = job.id
        db.get(User, "reviewer").is_active = False
        db.commit()
    assert process_next_candidate_model_task(sessions)
    with sessions() as db:
        assert db.get(CandidateModelTask, identity).status == "failed"


def test_disabling_automatic_work_stops_remaining_items(sessions, monkeypatch):
    calls = []
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", lambda body, *_: (
        calls.extend(body.ingestion_ids) or CandidateReviewBatchAnnotateResponse(
            updated=1, failed=0, results=[])
    ))
    with sessions() as db:
        add_candidate(db, "first")
        add_candidate(db, "second")
        db.get(CandidateModelAutomation, "default").enabled = True
        db.commit()
    assert process_next_candidate_model_task(sessions)
    with sessions() as db:
        db.get(CandidateModelAutomation, "default").enabled = False
        db.commit()
    process_next_candidate_model_task(sessions)
    assert len(calls) == 1
    with sessions() as db:
        assert db.query(CandidateModelTask).one().status == "cancelled"


def test_unexpected_auto_failure_does_not_loop_the_same_candidate(sessions, monkeypatch):
    def crash(*_):
        raise RuntimeError("unexpected fixture failure")
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", crash)
    with sessions() as db:
        add_candidate(db, "candidate")
        db.get(CandidateModelAutomation, "default").enabled = True
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert not process_next_candidate_model_task(sessions)


@pytest.mark.parametrize("batch_size", [1, 50, 100])
def test_real_worker_reuses_annotation_and_keeps_risky_candidate_out_of_submission(sessions, monkeypatch, batch_size):
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *_args, **_kwargs: ("Prompt", "v1"))
    def annotate(candidate, **_):
        assert candidate["draft"]["business_type"] == "aggregated"
        return {"knowledge_value": "worthy", "confidence": 0.99,
                "suggested_action": "hold_for_evidence", "draft_quality": "hold_for_evidence"}
    monkeypatch.setattr(integration, "annotate_transcribed_candidate", annotate)
    monkeypatch.setattr(integration, "submit_candidate_reviews", lambda *_: pytest.fail("risk must not submit"))
    with sessions() as db:
        ids = ["candidate-" + str(n) for n in range(batch_size)]
        for candidate_id in ids:
            add_candidate(db, candidate_id)
        job = enqueue_task(db, ids, "reviewer")
        identity = job.id
        db.commit()
    for _ in ids:
        assert process_next_candidate_model_task(sessions)
    with sessions() as db:
        item = db.get(IntegrationIngestion, ids[-1])
        assert item.review_status == "hold_for_evidence"
        assert item.knowledge_id is None
        assert item.candidate_payload["knowledge"]["business_type"] == "aggregated"
        snapshot = task_snapshot(db.get(CandidateModelTask, identity))
        assert snapshot["updated"] == batch_size
        assert snapshot["completed"] == batch_size
        assert snapshot["submitted"] == 0


def test_worker_submits_approved_candidate_through_existing_submission_gate(sessions, monkeypatch):
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate",
                        lambda *_args, **_kwargs: ("Prompt", "v1"))
    monkeypatch.setattr(integration, "annotate_transcribed_candidate", lambda *_args, **_kwargs: {
        "knowledge_value": "worthy", "confidence": 0.99,
        "suggested_action": "submit_for_human_review", "draft_quality": "approved",
    })
    def submit(body, db, _user):
        item = db.get(IntegrationIngestion, body.ingestion_ids[0])
        assert item.review_status == "ready"
        assert item.candidate_payload["knowledge"]["business_type"] == "aggregated"
        item.review_status = "submitted"
        item.knowledge_id = "K-1"
        db.commit()
        return CandidateReviewBatchSubmitResponse(
            submitted=1, failed=0, reused=0, results=[
                CandidateReviewSubmitResult(
                    ingestion_id=item.id, status="submitted", knowledge_id=item.knowledge_id,
                )
            ],
        )
    monkeypatch.setattr(integration, "submit_candidate_reviews", submit)
    with sessions() as db:
        add_candidate(db, "candidate")
        task = enqueue_task(db, ["candidate"], "reviewer")
        task_id = task.id
        db.commit()
    assert process_next_candidate_model_task(sessions)
    with sessions() as db:
        snapshot = task_snapshot(db.get(CandidateModelTask, task_id))
        assert snapshot["submitted"] == 1
        assert snapshot["results"][0]["knowledge_id"] == "K-1"


def test_task_migration_creates_opt_in_settings_and_can_roll_back():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261027_01_candidate_tasks.py"
    spec = importlib.util.spec_from_file_location("candidate_task_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert connection.execute(text("SELECT enabled FROM candidate_model_automation")).scalar() == 0
        assert "current_ingestion_id" in {
            column["name"] for column in inspect(connection).get_columns("candidate_model_tasks")
        }
        module.downgrade()
        assert inspect(connection).get_table_names() == []
    engine.dispose()
