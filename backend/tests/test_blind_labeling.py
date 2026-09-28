from datetime import datetime, timedelta
from inspect import signature
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.models.blind_labeling import (
    BlindLabelAnnotation,
    BlindLabelArbitration,
    BlindLabelAssignment,
    BlindLabelBatch,
    BlindLabelWorkOrder,
)
from app.models.integration import RetrievalQualityEvent
from app.models.knowledge import Category, Knowledge, KnowledgeStatus
from app.models.user import User
from app.routes.blind_labeling import (
    arbitrate_work_order,
    get_my_batch,
    list_my_assignments,
    release_assignment_compat,
    release_my_assignment,
    require_blind_annotator,
    require_blind_label_release_access,
)
from app.schemas.blind_labeling import BlindLabelClaimRequest, BlindLabelReleaseRequest
from app.services.blind_labeling import (
    BLIND_LABEL_AUTO_RELEASE_REASON,
    BLIND_LABEL_BATCH_SIZE,
    _choose_assignments,
    batch_summary,
    claim_batch,
    consensus_for_work_order,
    current_batch,
    ensure_work_orders,
    mark_assignment_started,
    overview,
    public_assignment_detail,
    release_expired_assignments,
    release_assignment,
    submit_assignment,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        User.__table__,
        Category.__table__,
        Knowledge.__table__,
        RetrievalQualityEvent.__table__,
        BlindLabelBatch.__table__,
        BlindLabelWorkOrder.__table__,
        BlindLabelAssignment.__table__,
        BlindLabelAnnotation.__table__,
        BlindLabelArbitration.__table__,
    ]
    for table in tables:
        table.create(engine)
    session = sessionmaker(bind=engine)()
    category = Category(id="cat-phone", name="手机", level=1)
    session.add(category)
    for index in range(1, 4):
        session.add(
            Knowledge(
                id=f"biz-{index}",
                knowledge_origin="business_accumulation",
                business_type="aggregated",
                title=f"业务知识 {index}",
                content={"type": "text", "value": f"正文 {index}"},
                category_id=category.id,
                status=KnowledgeStatus.PUBLISHED,
                created_by="seed",
            )
        )
    session.add(
        Knowledge(
            id="std-1",
            knowledge_origin="headquarters_standard",
            business_type="aggregated",
            title="总部知识",
            content={"type": "text", "value": "总部正文"},
            category_id=category.id,
            status=KnowledgeStatus.PUBLISHED,
            created_by="seed",
        )
    )
    for user_id in ("u1", "u2", "u3", "u4"):
        session.add(
            User(
                id=user_id,
                username=user_id,
                password_hash="unused",
                role="junior_support",
                is_active=True,
            )
        )
    session.add(
        RetrievalQualityEvent(
            id="event-1",
            idempotency_key="event-key-1",
            source_system="test",
            conversation_id="conversation-1",
            request_id="request-1",
            source_kind="reply",
            query_text="手机屏幕问题",
            candidate_count=4,
            top_knowledge_id="biz-1",
            top_rerank_score=0.9,
            score_threshold=0.42,
            outcome="accepted",
            request_status="success",
            candidate_snapshot=[
                {
                    "knowledge_id": f"biz-{index}",
                    "rank": index,
                    "title": f"业务知识 {index}",
                    "final_score": 0.9 - index * 0.01,
                    "knowledge_origin": "business_accumulation",
                }
                for index in range(1, 4)
            ],
            event_metadata={"candidate_origins": ["business_accumulation"] * 3},
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        RetrievalQualityEvent(
            id="event-standard",
            idempotency_key="event-key-standard",
            source_system="test",
            conversation_id="conversation-standard",
            request_id="request-standard",
            source_kind="standard",
            query_text="总部问题",
            candidate_count=1,
            top_knowledge_id="std-1",
            top_rerank_score=0.9,
            score_threshold=0.42,
            outcome="accepted",
            request_status="success",
            candidate_snapshot=[
                {
                    "knowledge_id": "std-1",
                    "rank": 1,
                    "title": "总部知识",
                    "knowledge_origin": "headquarters_standard",
                }
            ],
            event_metadata={"candidate_origins": ["headquarters_standard"]},
            created_at=datetime.utcnow(),
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _all_refs(assignment):
    return [item["candidate_ref"] for item in assignment.work_order.candidate_snapshot]


def _labels(assignment, verdict="referable"):
    refs = _all_refs(assignment)
    if verdict == "referable":
        # The new workflow records only the one selected best candidate.
        refs = refs[:1]
    return [
        {
            "candidate_ref": ref,
            "verdict": verdict,
            "reason_code": "topic_irrelevant" if verdict == "not_referable" else "",
            "reason": "测试" if verdict == "not_referable" else "",
        }
        for ref in refs
    ]


def test_claim_materializes_business_top3_and_reuses_active_batch(db):
    assert ensure_work_orders(db, 1) == 1
    batch, assignments = claim_batch(db, "u1", target_count=1)
    db.commit()
    assert batch.status == "active"
    assert batch.target_count == BLIND_LABEL_BATCH_SIZE
    assert len(assignments) == 1
    assert len(assignments[0].work_order.candidate_snapshot) == 3
    assert assignments[0].work_order.conversation_id == "conversation-1"
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-standard").count() == 0

    reused, same_assignments = claim_batch(db, "u1", target_count=1)
    assert reused.id == batch.id
    assert [item.id for item in same_assignments] == [assignments[0].id]


def test_ensure_work_orders_materializes_newest_eligible_event_first(db):
    now = datetime.utcnow()
    db.query(RetrievalQualityEvent).filter_by(id="event-1").update(
        {"created_at": now - timedelta(days=2)}
    )
    db.add(
        RetrievalQualityEvent(
            id="event-newest",
            idempotency_key="event-key-newest",
            source_system="test",
            conversation_id="conversation-newest",
            request_id="request-newest",
            source_kind="reply",
            query_text="最新手机问题",
            candidate_count=3,
            top_knowledge_id="biz-1",
            top_rerank_score=0.9,
            score_threshold=0.42,
            outcome="accepted",
            request_status="success",
            candidate_snapshot=[
                {
                    "knowledge_id": f"biz-{index}",
                    "rank": index,
                    "title": f"业务知识 {index}",
                    "final_score": 0.9 - index * 0.01,
                    "knowledge_origin": "business_accumulation",
                }
                for index in range(1, 4)
            ],
            event_metadata={"candidate_origins": ["business_accumulation"] * 3},
            created_at=now,
        )
    )
    db.flush()

    assert ensure_work_orders(db, 1) == 1
    work_order = db.query(BlindLabelWorkOrder).one()

    assert work_order.source_event_id == "event-newest"
    assert work_order.source_created_at == now


def test_choose_assignments_fills_existing_work_orders_before_newest_fresh_items(db):
    now = datetime.utcnow()
    db.add(
        User(
            id="u5",
            username="u5",
            password_hash="unused",
            role="junior_support",
            is_active=True,
        )
    )
    work_orders = {
        "two-old": BlindLabelWorkOrder(
            id="wo-two-old",
            conversation_id="priority-two-old",
            query_text="已有两人标注的旧工单",
            candidate_snapshot=[{"candidate_ref": "cand-two-old"}],
            source_created_at=now - timedelta(days=3),
        ),
        "one-new": BlindLabelWorkOrder(
            id="wo-one-new",
            conversation_id="priority-one-new",
            query_text="已有一人标注的新工单",
            candidate_snapshot=[{"candidate_ref": "cand-one-new"}],
            source_created_at=now - timedelta(days=1),
        ),
        "one-old": BlindLabelWorkOrder(
            id="wo-one-old",
            conversation_id="priority-one-old",
            query_text="已有一人标注的旧工单",
            candidate_snapshot=[{"candidate_ref": "cand-one-old"}],
            source_created_at=now - timedelta(days=2),
        ),
        "fresh": BlindLabelWorkOrder(
            id="wo-fresh",
            conversation_id="priority-fresh",
            query_text="尚未分配的最新工单",
            candidate_snapshot=[{"candidate_ref": "cand-fresh"}],
            source_created_at=now,
        ),
    }
    db.add_all(work_orders.values())
    batches = {
        user_id: BlindLabelBatch(
            id=f"priority-batch-{user_id}",
            user_id=user_id,
            target_count=BLIND_LABEL_BATCH_SIZE,
            status="active",
        )
        for user_id in ("u1", "u2", "u4", "u5")
    }
    db.add_all(batches.values())
    db.flush()
    db.add_all(
        [
            BlindLabelAssignment(
                id="priority-two-u1",
                batch_id=batches["u1"].id,
                work_order_id=work_orders["two-old"].id,
                user_id="u1",
                status="assigned",
            ),
            BlindLabelAssignment(
                id="priority-two-u2",
                batch_id=batches["u2"].id,
                work_order_id=work_orders["two-old"].id,
                user_id="u2",
                status="assigned",
            ),
            BlindLabelAssignment(
                id="priority-one-new-u1",
                batch_id=batches["u1"].id,
                work_order_id=work_orders["one-new"].id,
                user_id="u1",
                status="assigned",
            ),
            BlindLabelAssignment(
                id="priority-one-old-u1",
                batch_id=batches["u1"].id,
                work_order_id=work_orders["one-old"].id,
                user_id="u1",
                status="assigned",
            ),
        ]
    )
    db.flush()

    first = _choose_assignments(db, batches["u4"], "u4", needed=1)
    db.flush()
    assert [item.work_order_id for item in first] == [work_orders["two-old"].id]
    assert (
        db.query(BlindLabelAssignment)
        .filter_by(work_order_id=work_orders["two-old"].id)
        .count()
        == 3
    )

    second = _choose_assignments(db, batches["u5"], "u5", needed=1)
    assert [item.work_order_id for item in second] == [work_orders["one-new"].id]


def test_claim_batch_is_fixed_to_50_and_normalizes_legacy_active_batch(db):
    legacy_batch = BlindLabelBatch(
        id="legacy-batch",
        user_id="u1",
        target_count=1,
        status="active",
    )
    db.add(legacy_batch)
    db.commit()

    batch, rows = claim_batch(db, "u1", target_count=500)

    assert batch.id == legacy_batch.id
    assert batch.target_count == BLIND_LABEL_BATCH_SIZE
    assert batch_summary(db, batch)["total"] == BLIND_LABEL_BATCH_SIZE
    assert len(rows) == 1


def test_current_batch_does_not_auto_claim_after_a_completed_batch(db):
    completed = BlindLabelBatch(
        id="completed-batch",
        user_id="u1",
        target_count=BLIND_LABEL_BATCH_SIZE,
        status="completed",
        completed_at=datetime.utcnow(),
    )
    db.add(completed)
    db.flush()

    visible, visible_rows = current_batch(db, "u1")

    assert visible.id == completed.id
    assert visible.status == "completed"
    assert visible_rows == []
    assert db.query(BlindLabelBatch).filter_by(user_id="u1").count() == 1

    next_batch, _ = claim_batch(db, "u1")

    assert next_batch.id != completed.id
    assert next_batch.status == "active"
    assert db.query(BlindLabelBatch).filter_by(user_id="u1").count() == 2


def test_submitting_the_last_item_completes_batch_and_next_claim_is_explicit(db):
    ensure_work_orders(db, 1)
    batch, rows = claim_batch(db, "u1", target_count=1)
    # Use a one-item target to exercise the same completion transition without
    # manufacturing fifty fixture work orders; normal claims remain fixed at
    # BLIND_LABEL_BATCH_SIZE.
    batch.target_count = 1
    assignment = rows[0]

    submit_assignment(db, assignment, _labels(assignment))
    db.commit()

    assert batch.status == "completed"
    assert batch.completed_at is not None
    visible, visible_rows = current_batch(db, "u1")
    assert visible.id == batch.id
    assert visible_rows == []

    next_batch, next_rows = claim_batch(db, "u1")
    assert next_batch.id != batch.id
    assert next_batch.status == "active"
    assert next_batch.target_count == BLIND_LABEL_BATCH_SIZE
    assert next_rows == []


def test_get_my_batch_is_read_only_after_completion(db):
    completed = BlindLabelBatch(
        id="completed-readonly-batch",
        user_id="u1",
        target_count=BLIND_LABEL_BATCH_SIZE,
        status="completed",
        completed_at=datetime.utcnow(),
    )
    db.add(completed)
    db.commit()

    response = get_my_batch(target_count=BLIND_LABEL_BATCH_SIZE, db=db, current_user=db.get(User, "u1"))

    assert response["batch"]["id"] == completed.id
    assert response["batch"]["status"] == "completed"
    assert response["items"] == []
    assert db.query(BlindLabelBatch).filter_by(user_id="u1").count() == 1


def test_claim_request_rejects_non_fixed_batch_size():
    assert BlindLabelClaimRequest().target_count == BLIND_LABEL_BATCH_SIZE
    assert BlindLabelClaimRequest(targetCount=BLIND_LABEL_BATCH_SIZE).target_count == BLIND_LABEL_BATCH_SIZE
    with pytest.raises(ValidationError):
        BlindLabelClaimRequest(targetCount=1)
    with pytest.raises(ValidationError):
        BlindLabelClaimRequest(targetCount=500)


def test_claim_routes_reject_client_attempts_to_change_batch_size(db):
    from app.core.database import get_db
    from app.main import app
    from app.routes.auth import get_current_user

    current_user = db.get(User, "u1")

    def override_get_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: current_user
    client = TestClient(app)
    try:
        assert client.get("/api/v1/blind-labeling/my-batch?target_count=1").status_code == 422
        assert (
            client.post(
                "/api/v1/blind-labeling/my-batch:claim?target_count=500"
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/blind-labeling/my-batch:claim", json={"targetCount": 1}
            ).status_code
            == 422
        )
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_three_users_are_capped_and_consensus_never_writes_legacy_event(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    for index, assignment in enumerate(assignments):
        verdict = "referable" if index < 2 else "not_referable"
        submit_assignment(
            db,
            assignment,
            _labels(assignment, verdict),
            task_reason_code="knowledge_missing" if verdict == "not_referable" else "",
        )
    db.commit()
    work_order = assignments[0].work_order
    consensus = consensus_for_work_order(db, work_order)
    assert consensus["status"] == "majority"
    assert consensus["needs_arbitration"] is True
    assert consensus["candidate_results"][0]["preliminary_verdict"] == "referable"
    assert consensus["candidate_results"][0]["status"] == "needs_arbitration"
    assert consensus["candidate_results"][0]["needs_arbitration"] is True
    assert db.query(RetrievalQualityEvent).filter_by(id="event-1").one().review_status == "unreviewed"
    assert db.query(BlindLabelAssignment).filter_by(work_order_id=work_order.id).count() == 3
    majority_overview = overview(db)
    assert majority_overview["summary"]["disputed"] == 1
    assert majority_overview["summary"]["consensus_rate"] == 0

    _, fourth_rows = claim_batch(db, "u4", target_count=1)
    assert fourth_rows == []


def test_single_choice_consensus_does_not_treat_omitted_candidates_as_negative_votes(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])

    refs = _all_refs(assignments[0])
    for assignment, ref in zip(assignments, refs, strict=True):
        submit_assignment(
            db,
            assignment,
            [{"candidate_ref": ref, "verdict": "referable"}],
        )
    db.commit()

    consensus = consensus_for_work_order(db, assignments[0].work_order)

    assert consensus["status"] == "majority"
    assert consensus["needs_arbitration"] is True
    assert all(
        result["referable_count"] == 1
        and result["not_referable_count"] == 0
        and result["status"] == "needs_arbitration"
        for result in consensus["candidate_results"]
    )


def test_expired_assignment_is_automatically_reclaimed_and_frees_its_slot(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    now = datetime.utcnow()
    assignment.assigned_at = now - timedelta(hours=24)
    assert release_expired_assignments(db, now=now, timeout_seconds=24 * 60 * 60) == 1
    db.commit()
    assert assignment.status == "released"
    assert assignment.release_reason == BLIND_LABEL_AUTO_RELEASE_REASON
    listed = list_my_assignments(
        batch_id=None,
        assignment_status=None,
        include_released=False,
        page=1,
        page_size=50,
        db=db,
        current_user=db.get(User, "u1"),
    )
    assert listed["batch"]["released_count"] == 1
    with pytest.raises(ValueError, match="超时自动回收"):
        submit_assignment(db, assignment, _labels(assignment))
    with pytest.raises(ValueError, match="超时自动回收"):
        mark_assignment_started(db, assignment)
    assert assignment.status == "released"
    _, again = claim_batch(db, "u1", target_count=1)
    assert again == []
    # 其他标注员可以占用系统回收后释放的工单名额。
    _, another = claim_batch(db, "u2", target_count=1)
    assert len(another) == 1


def test_auto_release_keeps_unexpired_and_completed_assignments(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    now = datetime.utcnow()
    assignment.assigned_at = now - timedelta(hours=24) + timedelta(seconds=1)
    assert release_expired_assignments(db, now=now, timeout_seconds=24 * 60 * 60) == 0
    submit_assignment(db, assignment, _labels(assignment))
    assignment.assigned_at = now - timedelta(days=2)
    assert release_expired_assignments(db, now=now, timeout_seconds=24 * 60 * 60) == 0
    assert assignment.status == "completed"


def test_starting_a_stale_assignment_cannot_revive_an_auto_released_task(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    db.execute(
        update(BlindLabelAssignment)
        .where(BlindLabelAssignment.id == assignment.id)
        .values(
            status="released",
            release_reason=BLIND_LABEL_AUTO_RELEASE_REASON,
            released_at=datetime.utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    assert assignment.status == "assigned"
    with pytest.raises(ValueError, match="超时自动回收"):
        mark_assignment_started(db, assignment)
    assert assignment.status == "released"


def test_public_detail_does_not_leak_ids_scores_or_other_annotations(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    detail = public_assignment_detail(db, assignment)
    assert detail["candidates"][0]["candidate_ref"]
    assert "knowledge_id" not in detail["candidates"][0]
    assert "final_score" not in detail["candidates"][0]
    assert "knowledge_origin" not in detail["candidates"][0]
    assert "selected" not in detail["candidates"][0]


def test_arbitration_resolves_disputed_consensus_and_overview_aliases(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    for index, assignment in enumerate(assignments):
        verdict = "referable" if index < 2 else "not_referable"
        submit_assignment(
            db,
            assignment,
            _labels(assignment, verdict),
            task_reason_code="knowledge_missing" if verdict == "not_referable" else "",
        )
    db.commit()
    work_order = assignments[0].work_order
    for index, candidate in enumerate(work_order.candidate_snapshot, start=1):
        db.add(
            BlindLabelArbitration(
                id=f"arb-{index}",
                work_order_id=work_order.id,
                candidate_ref=candidate["candidate_ref"],
                verdict="not_referable",
                reason="仲裁",
                arbitrated_by="admin",
            )
        )
    db.commit()
    assert consensus_for_work_order(db, work_order)["status"] == "arbitrated"
    assert all(
        result["status"] == "arbitrated"
        for result in consensus_for_work_order(db, work_order)["candidate_results"]
    )
    result = overview(db, dimension="disputed")
    assert result["summary"]["total"] == 0


def test_referable_reason_is_optional_but_not_referable_reason_is_required(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    refs = _all_refs(assignment)
    annotations = [
        {"candidate_ref": refs[0], "verdict": "referable", "reason_code": "", "reason": ""},
        {"candidate_ref": refs[1], "verdict": "not_referable", "reason_code": "content_outdated", "reason": ""},
    ]

    created = submit_assignment(db, assignment, annotations, note="整体补充说明")

    assert [item.reason_code for item in created] == ["", "content_outdated"]
    assert assignment.note == "整体补充说明"
    assert assignment.task_reason_code == ""
    assert consensus_for_work_order(db, assignment.work_order)["status"] == "pending"


def test_all_not_referable_requires_three_candidate_reasons_but_not_task_reason(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    annotations = _labels(assignment, "not_referable")

    created = submit_assignment(
        db,
        assignment,
        annotations,
    )

    assert len(created) == len(_all_refs(assignment))
    assert assignment.task_reason_code == ""


def test_partial_all_not_referable_submission_is_rejected_atomically(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    refs = _all_refs(assignment)
    partial = [
        {"candidate_ref": refs[0], "verdict": "not_referable", "reason_code": "topic_irrelevant"},
        {"candidate_ref": refs[1], "verdict": "not_referable", "reason_code": "content_outdated"},
    ]

    with pytest.raises(ValueError, match="分别选择每条候选的原因"):
        submit_assignment(db, assignment, partial)

    assert assignment.status != "completed"
    assert db.query(BlindLabelAnnotation).filter_by(assignment_id=assignment.id).count() == 0


def test_multiple_referable_candidates_are_rejected(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    refs = _all_refs(assignment)
    duplicate_best = [
        {"candidate_ref": refs[0], "verdict": "referable"},
        {"candidate_ref": refs[1], "verdict": "referable"},
    ]

    with pytest.raises(ValueError, match="只能选择一条可参考"):
        submit_assignment(db, assignment, duplicate_best)

    assert assignment.status != "completed"
    assert db.query(BlindLabelAnnotation).filter_by(assignment_id=assignment.id).count() == 0


def test_reason_codes_reject_invalid_combinations_and_allow_legacy_task_reason(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    refs = _all_refs(assignment)
    invalid = [
        {"candidate_ref": refs[0], "verdict": "referable", "reason_code": "topic_irrelevant", "reason": ""},
        {"candidate_ref": refs[1], "verdict": "referable", "reason_code": "", "reason": ""},
        {"candidate_ref": refs[2], "verdict": "referable", "reason_code": "", "reason": ""},
    ]
    with pytest.raises(ValueError, match="可参考原因"):
        submit_assignment(db, assignment, invalid)

    mixed = [
        {"candidate_ref": refs[0], "verdict": "referable", "reason_code": "", "reason": ""},
        {"candidate_ref": refs[1], "verdict": "not_referable", "reason_code": "topic_irrelevant", "reason": ""},
    ]
    created = submit_assignment(db, assignment, mixed, task_reason_code="knowledge_missing")
    assert len(created) == 2


def test_post_claim_and_overview_compatibility_fields(db):
    ensure_work_orders(db, 1)
    batch, rows = claim_batch(db, "u1", target_count=1)
    summary = batch_summary(db, batch)
    assert summary["total"] == BLIND_LABEL_BATCH_SIZE
    assert summary["assigned"] == 1
    assert summary["pending"] == BLIND_LABEL_BATCH_SIZE - 1
    assert rows[0].id

    result = overview(db, dimension="consensus")
    assert "work_orders" in result
    assert "people" in result
    assert result["summary"]["consensus_rate"] == 0
    assert result["summary"]["resolution_rate"] == 0


def test_admin_is_not_auto_assigned_a_personal_batch():
    checker = require_blind_annotator()
    with pytest.raises(Exception) as raised:
        checker(User(id="admin", username="admin", role="super_admin", is_active=True))
    assert getattr(raised.value, "status_code", None) == 403

    support = User(
        id="support",
        username="support",
        role="junior_support",
        is_active=True,
    )
    assert checker(support) is support


def test_arbitrator_can_release_another_users_assignment_without_personal_batch_access(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    admin = User(
        id="admin",
        username="admin",
        password_hash="unused",
        role="super_admin",
        is_active=True,
    )
    db.add(admin)
    db.commit()

    release_access = require_blind_label_release_access()
    assert release_access(admin) is admin
    assert (
        signature(release_my_assignment).parameters["current_user"].default.dependency(
            admin
        )
        is admin
    )
    assert (
        signature(release_assignment_compat).parameters["current_user"].default.dependency(
            admin
        )
        is admin
    )
    assert (
        signature(arbitrate_work_order).parameters["current_user"].default.dependency(
            admin
        )
        is admin
    )

    response = release_my_assignment(
        assignment.id,
        BlindLabelReleaseRequest(reason="管理员释放"),
        db,
        admin,
    )

    db.refresh(assignment)
    assert response == {"status": "released", "assignment_id": assignment.id}
    assert assignment.status == "released"

    with pytest.raises(HTTPException) as denied:
        release_my_assignment(assignment.id, None, db, db.get(User, "u2"))
    assert denied.value.status_code == 403


def test_blind_label_api_contract_exposes_claim_detail_and_overview_routes():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/blind-labeling/my-batch:claim" in paths
    assert "/api/v1/blind-labeling/overview" in paths
    assert "/api/v1/blind-labeling/work-orders/{work_order_id}" in paths
    assert "post" in paths["/api/v1/blind-labeling/assignments/{assignment_id}/submit"]
