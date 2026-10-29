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


def test_network_outage_pauses_all_work_until_explicit_resume(sessions, monkeypatch):
    calls = []

    def run(body, *_args):
        identity = body.ingestion_ids[0]
        calls.append(identity)
        return CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(
                ingestion_id=identity, status="failed", error_code="NETWORK_ERROR",
                error_message="模型连接被拒绝，已尝试3次。",
            ),
        ])

    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        for identity in ("one", "two", "three"):
            add_candidate(db, identity)
        db.get(CandidateModelAutomation, "default").enabled = True
        task = enqueue_task(db, ["one", "two", "three"], "reviewer")
        task_id = task.id
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert process_next_candidate_model_task(sessions)
    for _ in range(4):
        assert not process_next_candidate_model_task(sessions)
    assert calls == ["one", "two"]
    with sessions() as db:
        config = db.get(CandidateModelAutomation, "default")
        assert config.paused_at is not None
        assert config.consecutive_network_failures == 2
        task = db.get(CandidateModelTask, task_id)
        assert task.status == "queued"
        assert len(task.results) == 2
        assert db.get(IntegrationIngestion, "three").review_metadata == {}
        with pytest.raises(integration.HTTPException) as error:
            integration.create_candidate_model_task(
                integration.CandidateModelTaskCreate(ingestion_ids=["other"]),
                db, db.get(User, "reviewer"),
            )
        assert error.value.status_code == 409
        assert schedule_automatic_candidates(db) is None
        # Saving the auto switch must not silently override the safety pause.
        integration.update_candidate_model_automation(
            integration.CandidateModelAutomationUpdate(enabled=True), db, db.get(User, "reviewer"),
        )
        assert config.paused_at is not None
        integration.resume_candidate_model_processing(db, db.get(User, "reviewer"))
        assert config.paused_at is None
        assert config.consecutive_network_failures == 0
    assert process_next_candidate_model_task(sessions)
    assert calls == ["one", "two", "three"]


def test_model_response_resets_network_failure_streak(sessions, monkeypatch):
    responses = ["TIMEOUT", "INVALID_REPLY_COMPLETION", "NETWORK_ERROR"]

    def run(body, *_args):
        return CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(
                ingestion_id=body.ingestion_ids[0], status="failed", error_code=responses.pop(0),
            ),
        ])

    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        enqueue_task(db, ["one", "two", "three"], "reviewer")
        db.commit()
    for _ in range(3):
        assert process_next_candidate_model_task(sessions)
    with sessions() as db:
        config = db.get(CandidateModelAutomation, "default")
        assert config.paused_at is None
        assert config.consecutive_network_failures == 1


def test_failed_manual_work_is_not_picked_up_by_auto_scan_after_resume(sessions, monkeypatch):
    calls = []

    def run(body, *_args):
        identity = body.ingestion_ids[0]
        calls.append(identity)
        return CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(ingestion_id=identity, status="failed", error_code="NETWORK_ERROR"),
        ])

    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        for identity in ("first", "second"):
            add_candidate(db, identity)
        db.get(CandidateModelAutomation, "default").enabled = True
        enqueue_task(db, ["first", "second"], "reviewer")
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert process_next_candidate_model_task(sessions)
    with sessions() as db:
        integration.resume_candidate_model_processing(db, db.get(User, "reviewer"))
    assert not process_next_candidate_model_task(sessions)
    assert calls == ["first", "second"]
    # An explicit manual retry still works for the same draft.
    with sessions() as db:
        assert enqueue_task(db, ["first"], "reviewer") is not None
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert calls == ["first", "second", "first"]


def test_prequeued_auto_task_skips_draft_attempted_by_legacy_sync_before_resume(sessions, monkeypatch):
    calls = []

    def run(body, db, _user):
        identity = body.ingestion_ids[0]
        calls.append(identity)
        if identity != "three":
            # The legacy wrapper must release the config transaction before model I/O.
            assert not db.in_transaction()
        return CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(
                ingestion_id=identity, status="failed",
                error_code="NETWORK_ERROR" if identity != "three" else "INVALID_ANNOTATION_RESULT",
            ),
        ])

    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        for identity in ("one", "two", "three"):
            add_candidate(db, identity)
        db.get(CandidateModelAutomation, "default").enabled = True
        task = schedule_automatic_candidates(db)
        task_id = task.id
        db.commit()
        with pytest.raises(integration.HTTPException) as error:
            integration.guarded_model_annotate_candidate_reviews(
                integration.CandidateReviewBatchModelAnnotate(ingestion_ids=["one", "two", "three"]),
                db, db.get(User, "reviewer"),
            )
        assert error.value.status_code == 503
        assert calls == ["one", "two"]
        integration.resume_candidate_model_processing(db, db.get(User, "reviewer"))
    for _ in range(3):
        assert process_next_candidate_model_task(sessions)
    assert calls == ["one", "two", "three"]
    with sessions() as db:
        snapshot = task_snapshot(db.get(CandidateModelTask, task_id))
        assert {entry["ingestion_id"]: entry["status"] for entry in snapshot["results"]} == {
            "one": "skipped", "two": "skipped", "three": "failed",
        }


@pytest.mark.parametrize("code,limit", [("NETWORK_ERROR", 2), ("HTTP_401", 1)])
def test_legacy_sync_http_endpoint_cannot_bypass_pause_or_keep_calling_during_outage(sessions, monkeypatch, code, limit):
    calls = []

    def run(body, *_args):
        calls.extend(body.ingestion_ids)
        return CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(ingestion_id=body.ingestion_ids[0], status="failed", error_code=code),
        ])

    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    app = FastAPI()
    app.include_router(integration.router)
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    with sessions() as db:
        user = db.get(User, "reviewer")
        for identity in ("one", "two", "three"):
            add_candidate(db, identity)
        db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        request = {"ingestion_ids": ["one", "two", "three"]}
        assert client.post("/integration/candidate-reviews:batch-model-annotate", json=request).status_code == 503
        assert len(calls) == limit
        assert client.post("/integration/candidate-reviews:batch-model-annotate", json=request).status_code == 409
        assert len(calls) == limit
    with sessions() as db:
        assert db.get(IntegrationIngestion, "one").review_metadata["automatic_model_workflow"]["draft_version"] == 1
        assert db.get(IntegrationIngestion, "three").review_metadata == {}


def test_legacy_sync_endpoint_refuses_concurrent_candidate_execution(sessions, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def busy(_engine):
        yield False

    monkeypatch.setattr(integration, "candidate_model_execution_slot", busy)
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews",
                        lambda *_args: pytest.fail("busy execution slot must not call the model"))
    with sessions() as db:
        with pytest.raises(integration.HTTPException) as error:
            integration.guarded_model_annotate_candidate_reviews(
                integration.CandidateReviewBatchModelAnnotate(ingestion_ids=["one"]),
                db, db.get(User, "reviewer"),
            )
        assert error.value.status_code == 409


@pytest.mark.parametrize("acquired,crash", [(True, False), (True, True), (False, False)])
def test_postgres_candidate_slot_releases_only_owned_lock_even_on_exception(acquired, crash):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app.services.candidate_model_tasks import candidate_model_execution_slot

    connection = MagicMock()
    connection.execute.return_value.scalar.return_value = acquired
    engine = MagicMock()
    engine.dialect = SimpleNamespace(name="postgresql")
    engine.connect.return_value.__enter__.return_value = connection
    try:
        with candidate_model_execution_slot(engine) as locked:
            assert locked is acquired
            if crash:
                raise RuntimeError("simulated processing exception")
    except RuntimeError:
        assert crash
    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert statements == ["SELECT pg_try_advisory_lock(735216, 20261027)"] + (
        ["SELECT pg_advisory_unlock(735216, 20261027)"] if acquired else []
    )
    assert connection.commit.call_count == (2 if acquired else 1)


@pytest.mark.parametrize("error_code", ["CONFIG_MISSING", "HTTP_401", "HTTP_403"])
def test_configuration_errors_pause_immediately_and_resume_keeps_auto_switch_off(sessions, monkeypatch, error_code):
    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", lambda body, *_: (
        CandidateReviewBatchAnnotateResponse(updated=0, failed=1, results=[
            CandidateReviewAnnotateResult(
                ingestion_id=body.ingestion_ids[0], status="failed", error_code=error_code,
            ),
        ])
    ))
    with sessions() as db:
        enqueue_task(db, ["first", "second"], "reviewer")
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert not process_next_candidate_model_task(sessions)
    with sessions() as db:
        config = db.get(CandidateModelAutomation, "default")
        assert config.paused_at is not None
        assert error_code in config.pause_reason
        integration.resume_candidate_model_processing(db, db.get(User, "reviewer"))
        assert not config.enabled


def test_reannotation_network_error_retains_service_code_and_pauses_worker(sessions, monkeypatch):
    from app.services.model_annotation import ModelAnnotationError

    calls = []
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *_args, **_kw: ("Prompt", "v1"))

    def annotate(*_args, **_kwargs):
        calls.append("annotation")
        if len(calls) % 2 == 0:
            raise ModelAnnotationError("连接失败（ConnectError），已尝试3次。", "NETWORK_ERROR", True)
        return {
            "knowledge_value": "worthy", "confidence": 0.99,
            "suggested_action": "revise_draft", "draft_quality": "revision_required",
        }

    monkeypatch.setattr(integration, "annotate_transcribed_candidate", annotate)
    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", lambda *_args, **_kw: {
        "revised_content": "来源支持的核对结论", "revised_recommended_reply": "请核对来源。",
        "change_summary": ["补充核对结论"], "evidence_used": ["来源明确登记状态"], "limitations": [],
    })
    with sessions() as db:
        for identity in ("first", "second", "third"):
            add_candidate(db, identity)
        task = enqueue_task(db, ["first", "second", "third"], "reviewer")
        task_id = task.id
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert process_next_candidate_model_task(sessions)
    assert not process_next_candidate_model_task(sessions)
    assert len(calls) == 4
    with sessions() as db:
        snapshot = task_snapshot(db.get(CandidateModelTask, task_id))
        assert snapshot["revisions_applied"] == 2
        assert snapshot["failed"] == 2
        assert all(row["error_code"] == "NETWORK_ERROR" for row in snapshot["results"])
        assert db.get(CandidateModelAutomation, "default").paused_at is not None


@pytest.mark.parametrize("error_code", ["NETWORK_ERROR", "TIMEOUT", "HTTP_429", "HTTP_503", "HTTP_502"])
def test_service_failures_in_submission_or_revision_also_pause(sessions, monkeypatch, error_code):
    def run(body, *_args):
        return CandidateReviewBatchAnnotateResponse(updated=1, failed=0, submission_failed=1, results=[
            CandidateReviewAnnotateResult(
                ingestion_id=body.ingestion_ids[0], status="updated",
                submission_status="failed", error_code=error_code,
            ),
        ])

    monkeypatch.setattr(integration, "model_annotate_candidate_reviews", run)
    with sessions() as db:
        enqueue_task(db, ["one", "two", "three"], "reviewer")
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert process_next_candidate_model_task(sessions)
    assert not process_next_candidate_model_task(sessions)


def test_compact_task_summary_and_failure_filter_cover_old_history(sessions):
    with sessions() as db:
        for n in range(46):
            db.add(CandidateModelTask(
                id=f"history-{n:02}", status="completed", source="manual",
                requested_by="reviewer", ingestion_ids=[str(n)],
                results=[{"updated": 1, "submission_failed": int(n == 0), "results": [
                    {"ingestion_id": str(n), "status": "updated",
                     "submission_status": "failed" if n == 0 else "submitted"},
                ]}],
                created_at=datetime(2026, 10, 1) + timedelta(minutes=n),
            ))
        db.commit()
        user = db.get(User, "reviewer")
        summary = integration.get_candidate_model_automation(0, db, user)
        assert summary["tasks"] == []
        assert summary["task_total"] == 46
        assert summary["failed_task_total"] == 1
        failures = integration.get_candidate_model_automation(
            1, db, user, task_filter="failures", include_results=False,
        )
        assert failures["task_total"] == 1
        assert failures["tasks"][0]["id"] == "history-00"
        assert failures["tasks"][0]["results"] == []
        detail = integration.get_candidate_model_task("history-00", db, user)
        assert detail["results"][0]["submission_status"] == "failed"


def test_worker_persists_exhausted_reply_failure_and_processes_next_candidate(sessions, monkeypatch):
    import json
    import httpx
    from app.services import model_annotation

    calls, waits = [], []
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate",
                        lambda *_args, **_kwargs: ("Prompt", "v1"))
    monkeypatch.setattr(model_annotation, "resolve_call_config", lambda *_args: {
        "base_url": "http://model.invalid/v1", "api_key": "test-placeholder",
        "model": "deepseek-flash", "timeout_seconds": 5,
    })
    monkeypatch.setattr(model_annotation.time, "sleep", waits.append)
    monkeypatch.setattr(integration, "submit_candidate_reviews",
                        lambda *_args: pytest.fail("failed or evidence-held candidate must not submit"))

    def post(*args, **kwargs):
        prompt = kwargs["json"]["messages"][1]["content"]
        if "FIRST_FAILURE" in prompt:
            calls.append("failed")
            raise httpx.ConnectError("private connection details")
        if "知识草稿推荐回复补充器" in prompt:
            calls.append("completion")
            result = {"recommended_reply": "请核对登记状态。",
                      "evidence_sufficiency": "sufficient", "evidence_used": ["来源明确登记状态"]}
        else:
            calls.append("annotation")
            result = {"knowledge_value": "pending", "draft_quality": "hold_for_evidence",
                      "confidence": 0.99, "reason": "还需补充核验材料"}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
        })

    monkeypatch.setattr(model_annotation.httpx, "post", post)
    with sessions() as db:
        first = add_candidate(db, "first")
        second = add_candidate(db, "second")
        for item, title in ((first, "FIRST_FAILURE"), (second, "登记状态核对")):
            item.candidate_payload = {
                **item.candidate_payload,
                "knowledge": {**item.candidate_payload["knowledge"], "recommended_reply": "", "title": title},
            }
        task = enqueue_task(db, ["first", "second"], "reviewer")
        task_id = task.id
        db.commit()
    assert process_next_candidate_model_task(sessions)
    assert process_next_candidate_model_task(sessions)
    assert calls == ["failed", "failed", "failed", "completion", "annotation"]
    with sessions() as db:
        snapshot = task_snapshot(db.get(CandidateModelTask, task_id))
        assert (snapshot["completed"], snapshot["failed"], snapshot["updated"]) == (2, 1, 1)
        assert snapshot["status"] == "completed"
        error = snapshot["results"][0]
        assert error["error_code"] == "NETWORK_ERROR"
        assert "ConnectError" in error["error_message"]
        first = db.get(IntegrationIngestion, "first")
        assert first.candidate_payload["knowledge"]["recommended_reply"] == ""
        assert first.review_metadata["reply_completion"]["status"] == "failed"
        second = db.get(IntegrationIngestion, "second")
        assert second.review_status == "hold_for_evidence"
        assert second.candidate_payload["knowledge"]["recommended_reply"] == "请核对登记状态。"
        assert len(second.review_metadata["draft_revision_history"]) == 1


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


def test_pause_migration_preserves_existing_switch_and_task_progress_and_rolls_back():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        modules = []
        for filename in ("20261027_01_candidate_tasks.py", "20261028_01_candidate_pause.py"):
            path = Path(__file__).resolve().parents[1] / "migrations/versions" / filename
            spec = importlib.util.spec_from_file_location("migration_" + filename[:11], path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = Operations(MigrationContext.configure(connection))
            modules.append(module)
        modules[0].upgrade()
        connection.execute(text("UPDATE candidate_model_automation SET enabled=1, requested_by='existing'"))
        connection.execute(text(
            "INSERT INTO candidate_model_tasks "
            "(id,status,source,requested_by,ingestion_ids,results,error_message,created_at,updated_at) "
            "VALUES ('existing','queued','manual','existing',:ids,:results,'','2026-10-28','2026-10-28')"
        ), {"ids": '["a","b"]', "results": '[{"updated":1,"results":[]}]'})
        modules[1].upgrade()
        row = connection.execute(text(
            "SELECT enabled,requested_by,consecutive_network_failures,paused_at,pause_reason "
            "FROM candidate_model_automation"
        )).one()
        assert tuple(row) == (1, "existing", 0, None, "")
        assert connection.execute(text("SELECT results FROM candidate_model_tasks")).scalar() == '[{"updated":1,"results":[]}]'
        modules[1].downgrade()
        assert "paused_at" not in {col["name"] for col in inspect(connection).get_columns("candidate_model_automation")}
        assert connection.execute(text("SELECT enabled FROM candidate_model_automation")).scalar() == 1
        modules[0].downgrade()
        assert inspect(connection).get_table_names() == []
    engine.dispose()
