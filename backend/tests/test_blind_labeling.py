from datetime import date, datetime, timedelta
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
    _as_date,
    _as_datetime,
    _resolve_date_range,
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
    BLIND_LABEL_MIN_TOP1_SCORE,
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


_QUESTION_FORM_ID_UNSET = object()


def _build_threshold_event(
    *,
    event_id: str,
    conversation_id: str,
    source_kind: str,
    top_rerank_score: float,
    candidate_snapshot: list[dict],
    candidate_origins: list[str],
    question_form_id: str | None | object = _QUESTION_FORM_ID_UNSET,
) -> RetrievalQualityEvent:
    return RetrievalQualityEvent(
        id=event_id,
        idempotency_key=f"key-{event_id}",
        source_system="test",
        conversation_id=conversation_id,
        question_form_id=(
            conversation_id
            if question_form_id is _QUESTION_FORM_ID_UNSET
            else question_form_id
        ),
        request_id=f"request-{event_id}",
        source_kind=source_kind,
        query_text="分数门槛测试",
        candidate_count=len(candidate_snapshot),
        top_knowledge_id=str(candidate_snapshot[0]["knowledge_id"]),
        top_rerank_score=top_rerank_score,
        score_threshold=0.42,
        outcome="accepted",
        request_status="success",
        candidate_snapshot=candidate_snapshot,
        event_metadata={"candidate_origins": candidate_origins},
        created_at=datetime.utcnow(),
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
            question_form_id="question-form-1",
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
            question_form_id="question-form-standard",
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


def _seed_extra_business_events(db, count=2):
    """补齐合格业务事件，使 ``ensure_work_orders`` 能物化出 ``1 + count`` 条工单。

    默认 fixture 只种了一条合格业务事件（``event-1``）。需要多条工单的用例
    必须自己补齐 —— 否则断言会依赖错误的种子假设（这正是 2026-10-10 之前
    这批用例残留 ``== 3`` 却一直没被执行到的原因）。
    """

    for index in range(1, count + 1):
        db.add(
            _build_threshold_event(
                event_id=f"event-extra-{index}",
                conversation_id=f"conversation-extra-{index}",
                source_kind="reply",
                top_rerank_score=0.9,
                candidate_snapshot=[
                    {
                        "knowledge_id": f"biz-{index}",
                        "rank": 1,
                        "title": f"业务知识 {index}",
                        "final_score": 0.9 - index * 0.01,
                        "knowledge_origin": "business_accumulation",
                    }
                ],
                candidate_origins=["business_accumulation"],
            )
        )
    db.commit()


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
    assert assignments[0].work_order.question_form_id == "question-form-1"
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-standard").count() == 0

    reused, same_assignments = claim_batch(db, "u1", target_count=1)
    assert reused.id == batch.id
    assert [item.id for item in same_assignments] == [assignments[0].id]


def test_event_without_question_form_id_is_recorded_but_never_materialized(db):
    """严格口径：没有工单号的事件照常入库，但绝不进入盲标池。"""

    db.add(
        _build_threshold_event(
            event_id="event-no-question-form",
            conversation_id="conversation-no-question-form",
            source_kind="reply",
            top_rerank_score=0.9,
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
            candidate_origins=["business_accumulation"] * 3,
            question_form_id=None,
        )
    )
    db.commit()

    ensure_work_orders(db, 0)
    db.commit()
    assert (
        db.query(BlindLabelWorkOrder)
        .filter_by(conversation_id="conversation-no-question-form")
        .count()
        == 0
    )
    assert (
        db.query(BlindLabelWorkOrder)
        .filter_by(question_form_id="conversation-no-question-form")
        .count()
        == 0
    )
    recorded = db.get(RetrievalQualityEvent, "event-no-question-form")
    assert recorded is not None
    assert recorded.question_form_id is None


def test_event_with_question_form_id_is_materialized_with_the_upstream_number(db):
    """有工单号的事件仍然照常物化，并把工单号写进工单记录。"""

    db.add(
        _build_threshold_event(
            event_id="event-with-question-form",
            conversation_id="conversation-with-question-form",
            source_kind="reply",
            top_rerank_score=0.9,
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
            candidate_origins=["business_accumulation"] * 3,
            question_form_id="2092919668271485492",
        )
    )
    db.commit()

    ensure_work_orders(db, 0)
    db.commit()
    work_order = (
        db.query(BlindLabelWorkOrder)
        .filter_by(question_form_id="2092919668271485492")
        .one()
    )
    assert work_order.conversation_id == "conversation-with-question-form"


def test_top1_score_below_sixty_percent_is_not_materialized_but_boundary_passes(db):
    for event_id, conversation_id, score in (
        ("event-low-score", "conversation-low-score", 0.59),
        ("event-boundary-score", "conversation-boundary-score", BLIND_LABEL_MIN_TOP1_SCORE),
    ):
        db.add(
            _build_threshold_event(
                event_id=event_id,
                conversation_id=conversation_id,
                source_kind="reply",
                top_rerank_score=score,
                candidate_snapshot=[
                    {
                        "knowledge_id": "biz-1",
                        "rank": 1,
                        "title": "业务知识",
                        "final_score": score,
                        "knowledge_origin": "business_accumulation",
                    }
                ],
                candidate_origins=["business_accumulation"],
            )
        )
    db.add(
        _build_threshold_event(
            event_id="event-reply-fallback-score",
            conversation_id="conversation-reply-fallback-score",
            source_kind="reply",
            top_rerank_score=BLIND_LABEL_MIN_TOP1_SCORE,
            candidate_snapshot=[
                {
                    "knowledge_id": "biz-2",
                    "rank": 1,
                    "title": "历史业务知识",
                    "knowledge_origin": "business_accumulation",
                }
            ],
            candidate_origins=["business_accumulation"],
        )
    )
    db.commit()

    assert ensure_work_orders(db, 10) == 3
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-low-score").count() == 0
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-boundary-score").count() == 1
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-reply-fallback-score").count() == 1


def test_existing_low_score_work_order_is_not_assigned_to_new_batch(db):
    db.query(RetrievalQualityEvent).filter_by(id="event-1").update({"source_kind": "standard"})
    db.add(
        BlindLabelWorkOrder(
            id="wo-low-score",
            conversation_id="conversation-low-work-order",
            query_text="历史低分工单",
            candidate_snapshot=[
                {
                    "candidate_ref": "candidate-low-score",
                    "knowledge_id": "biz-1",
                    "rank": 1,
                    "title": "低分业务知识",
                    "content": {"type": "text", "value": "正文"},
                    "final_score": 0.59,
                    "knowledge_origin": "business_accumulation",
                }
            ],
        )
    )
    db.commit()

    _, assignments = claim_batch(db, "u1", target_count=1)

    assert assignments == []


def test_combined_event_does_not_use_global_score_for_business_top1(db):
    db.add(
        RetrievalQualityEvent(
            id="event-combined-missing-business-score",
            idempotency_key="key-combined-missing-business-score",
            source_system="test",
            conversation_id="conversation-combined-missing-business-score",
            question_form_id="question-form-combined-missing-business-score",
            request_id="request-combined-missing-business-score",
            source_kind="combined",
            query_text="组合池分数测试",
            candidate_count=2,
            top_knowledge_id="std-1",
            top_rerank_score=0.95,
            score_threshold=0.42,
            outcome="accepted",
            request_status="success",
            candidate_snapshot=[
                {
                    "knowledge_id": "std-1",
                    "rank": 1,
                    "title": "总部知识",
                    "final_score": 0.95,
                    "knowledge_origin": "headquarters_standard",
                },
                {
                    "knowledge_id": "biz-1",
                    "rank": 2,
                    "title": "业务知识",
                    "knowledge_origin": "business_accumulation",
                },
            ],
            event_metadata={
                "candidate_origins": ["headquarters_standard", "business_accumulation"]
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    assert ensure_work_orders(db, 10) == 1
    assert db.query(BlindLabelWorkOrder).filter_by(
        conversation_id="conversation-combined-missing-business-score"
    ).count() == 0


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
            question_form_id="question-form-newest",
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
            candidate_snapshot=[{"candidate_ref": "cand-two-old", "final_score": 0.90}],
            source_created_at=now - timedelta(days=3),
        ),
        "one-new": BlindLabelWorkOrder(
            id="wo-one-new",
            conversation_id="priority-one-new",
            query_text="已有一人标注的新工单",
            candidate_snapshot=[{"candidate_ref": "cand-one-new", "final_score": 0.90}],
            source_created_at=now - timedelta(days=1),
        ),
        "one-old": BlindLabelWorkOrder(
            id="wo-one-old",
            conversation_id="priority-one-old",
            query_text="已有一人标注的旧工单",
            candidate_snapshot=[{"candidate_ref": "cand-one-old", "final_score": 0.90}],
            source_created_at=now - timedelta(days=2),
        ),
        "fresh": BlindLabelWorkOrder(
            id="wo-fresh",
            conversation_id="priority-fresh",
            query_text="尚未分配的最新工单",
            candidate_snapshot=[{"candidate_ref": "cand-fresh", "final_score": 0.90}],
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


def test_batch_summary_reports_target_size_and_items_actually_loaded(db):
    """池子不足时批次被提前装满：total 仍是固定口径 50，真实条数看 assigned。"""

    _seed_extra_business_events(db)
    assert ensure_work_orders(db, BLIND_LABEL_BATCH_SIZE) == 3
    batch, rows = claim_batch(db, "u1")
    assert len(rows) == 3
    assert batch.status == "active"

    summary = batch_summary(db, batch)
    assert summary["total"] == BLIND_LABEL_BATCH_SIZE
    assert summary["assigned"] == 3
    # 前端用 max(completed, assigned) 渲染「当前 N 条批次」，因此 3 条批次显示 3 而不是 50。
    assert max(summary["completed"], summary["assigned"]) == 3

    for assignment in rows:
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")
    db.commit()

    completed_summary = batch_summary(db, batch)
    # 固定目标 50 未被填满时批次保持 active（现有完成判定要求 assigned/completed 均达标），
    # 但真实条数已经是 3：界面必须显示 3/3 而不是 3/50。
    assert completed_summary["assigned"] == 3
    assert completed_summary["completed"] == 3
    assert max(completed_summary["completed"], completed_summary["assigned"]) == 3
    assert completed_summary["total"] == BLIND_LABEL_BATCH_SIZE


def test_my_assignments_completed_tab_does_not_expose_the_active_batch(db):
    """「我的已标注」拉取历史时必须清空在途批次视图，不能把进行中的批次当成本页上下文。"""

    _seed_extra_business_events(db)
    assert ensure_work_orders(db, BLIND_LABEL_BATCH_SIZE) == 3
    batch, rows = claim_batch(db, "u1")
    assert batch.status == "active"

    completed_response = list_my_assignments(
        batch_id=None,
        assignment_status="completed",
        include_released=False,
        page=1,
        page_size=50,
        db=db,
        current_user=db.get(User, "u1"),
    )
    assert completed_response["items"] == []
    assert completed_response["total"] == 0
    # 后端仍返回批次上下文，由前端在 completed 页签清空；这里锁定它不能是进行中的批次口径。
    assert completed_response["batch"]["status"] == "active"

    for assignment in rows:
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")
    db.commit()

    done_response = list_my_assignments(
        batch_id=None,
        assignment_status="completed",
        include_released=False,
        page=1,
        page_size=50,
        db=db,
        current_user=db.get(User, "u1"),
    )
    assert done_response["total"] == 3
    assert done_response["batch"]["completed"] == 3
    # 批次未填满 50，不会被自动置为 completed；已完成条目仍能出现在历史页签。
    assert done_response["batch"]["status"] == "active"


def _load_my_assignments(db, user, **overrides):
    kwargs = dict(
        batch_id=None,
        assignment_status="completed",
        include_released=False,
        page=1,
        page_size=50,
        db=db,
        current_user=user,
    )
    kwargs.update(overrides)
    return list_my_assignments(**kwargs)


def test_my_assignments_accepts_annotation_activity_date_range(db):
    """「我的已标注」的时间筛选：日期为闭区间，缺省时保持原有全量行为。"""

    _seed_extra_business_events(db)
    assert ensure_work_orders(db, BLIND_LABEL_BATCH_SIZE) == 3
    _, rows = claim_batch(db, "u1")
    for assignment in rows:
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")
    db.commit()

    user = db.get(User, "u1")
    today = datetime.utcnow().date()

    # 不带日期（旧行为）：全部历史
    assert _load_my_assignments(db, user)["total"] == 3
    # 当天（前端默认）
    assert _load_my_assignments(db, user, start_date=today)["total"] == 3
    assert _load_my_assignments(db, user, start_date=today, end_date=today)["total"] == 3
    # 结束日期按自然日闭区间处理，明天的零点才是排他上界
    assert _load_my_assignments(db, user, end_date=today)["total"] == 3
    # 过去的区间与未来的区间都取不到今天的活动
    assert (
        _load_my_assignments(
            db,
            user,
            start_date=today - timedelta(days=3),
            end_date=today - timedelta(days=1),
        )["total"]
        == 0
    )
    assert _load_my_assignments(db, user, start_date=today + timedelta(days=1))["total"] == 0
    # 兼容 date_from / date_to 别名
    assert _load_my_assignments(db, user, date_from=today, date_to=today)["total"] == 3


def test_my_assignments_date_filter_follows_activity_time_and_rejects_inverted_range(db):
    """时间筛选按标注活动时间判定（分配/完成/标注提交任一命中即可），并拒绝倒置区间。"""

    _seed_extra_business_events(db)
    assert ensure_work_orders(db, BLIND_LABEL_BATCH_SIZE) == 3
    _, rows = claim_batch(db, "u1")
    for assignment in rows:
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")
    db.commit()

    three_days_ago = datetime.utcnow() - timedelta(days=3)
    for assignment in rows:
        assignment.assigned_at = three_days_ago
        assignment.started_at = three_days_ago
        assignment.completed_at = three_days_ago
    db.commit()
    db.expire_all()

    user = db.get(User, "u1")
    today = datetime.utcnow().date()

    # 分配与完成时间都在 3 天前，只剩标注提交时间落在今天 → 仍算「今天标注的」
    assert _load_my_assignments(db, user, start_date=today, end_date=today)["total"] == 3
    # 3 天前那一天通过分配/完成时间命中
    assert (
        _load_my_assignments(
            db,
            user,
            start_date=today - timedelta(days=3),
            end_date=today - timedelta(days=3),
        )["total"]
        == 3
    )

    with pytest.raises(HTTPException) as error:
        _load_my_assignments(
            db,
            user,
            start_date=today,
            end_date=today - timedelta(days=1),
        )
    assert error.value.status_code == 422


def test_my_assignments_ignores_fastapi_query_placeholders_when_called_directly(db):
    """直接调用路由函数时 Query(None) 占位符不能被当成筛选值。"""

    from fastapi import Query

    assert _as_datetime(Query(None)) is None
    assert _as_date(Query(None)) is None
    assert _resolve_date_range(Query(None), Query(None), Query(None), Query(None)) == (None, None)
    assert _resolve_date_range(None, None, date(2026, 10, 9), date(2026, 10, 9)) == (
        datetime(2026, 10, 9, 0, 0),
        datetime(2026, 10, 10, 0, 0),
    )


class _StubVerifier:
    """替身：按号码给结论，未配置的号码返回 None（无法判定）。"""

    def __init__(self, verdicts: dict[str, bool] | None = None) -> None:
        self.verdicts = dict(verdicts or {})
        self.calls: list[str] = []
        self.stats = {"checks": 0, "present": 0, "missing": 0, "undecided": 0}

    def verify(self, question_form_id: str) -> bool | None:
        self.calls.append(question_form_id)
        self.stats["checks"] += 1
        verdict = self.verdicts.get(question_form_id)
        if verdict is True:
            self.stats["present"] += 1
        elif verdict is False:
            self.stats["missing"] += 1
        else:
            self.stats["undecided"] += 1
        return verdict


def test_ensure_work_orders_skips_numbers_the_upstream_does_not_know(db):
    """上游明确「查不到」的工单号不得建单，并把结论落库以避免重复请求。"""

    verifier = _StubVerifier({"question-form-1": False})
    assert ensure_work_orders(db, 1, verifier=verifier) == 0
    assert verifier.calls == ["question-form-1"]
    db.commit()

    event = db.get(RetrievalQualityEvent, "event-1")
    assert event.work_order_verified is False
    assert db.query(BlindLabelWorkOrder).count() == 0

    # 第二轮即使上游改口也不该再为已判定的事件建单（SQL 层已排除），
    # 且不会重复请求上游。
    retry = _StubVerifier({"question-form-1": True})
    assert ensure_work_orders(db, 1, verifier=retry) == 0
    assert retry.calls == []


def test_ensure_work_orders_skips_events_reported_as_conversation_identity(db):
    """助手自报号码来自会话 ID 的事件直接跳过，连上游都不必请求。"""

    event = db.get(RetrievalQualityEvent, "event-1")
    event.conversation_id_kind = "conversation"
    db.commit()

    verifier = _StubVerifier({"question-form-1": True})
    assert ensure_work_orders(db, 1, verifier=verifier) == 0
    assert verifier.calls == []
    assert db.query(BlindLabelWorkOrder).count() == 0


def test_ensure_work_orders_records_a_verified_work_order(db):
    """上游能查到工单详情时正常建单，并把核验结论写成 True。"""

    verifier = _StubVerifier({"question-form-1": True})
    assert ensure_work_orders(db, 1, verifier=verifier) == 1
    db.commit()

    assert verifier.calls == ["question-form-1"]
    assert db.get(RetrievalQualityEvent, "event-1").work_order_verified is True
    work_order = db.query(BlindLabelWorkOrder).one()
    assert work_order.question_form_id == "question-form-1"


def test_ensure_work_orders_stays_fail_open_when_verification_is_undecided(db):
    """没有 Cookie/上游异常时（None）行为与改造前一致：照常建单。"""

    verifier = _StubVerifier()
    assert ensure_work_orders(db, 1, verifier=verifier) == 1
    db.commit()

    assert verifier.calls == ["question-form-1"]
    assert verifier.stats["undecided"] == 1
    assert db.get(RetrievalQualityEvent, "event-1").work_order_verified is None
    assert db.query(BlindLabelWorkOrder).count() == 1


def test_ensure_work_orders_blocks_numbers_the_access_log_proved_chat_only(db, monkeypatch):
    """Cookie 判定不了时，访问日志证明只当过会话号的号码不得建单。"""

    monkeypatch.setattr(
        "app.services.blind_labeling.log_verdict_for",
        lambda number, **kwargs: "session",
    )
    verifier = _StubVerifier()
    assert ensure_work_orders(db, 1, verifier=verifier) == 0
    db.commit()

    assert verifier.calls == ["question-form-1"]
    assert db.get(RetrievalQualityEvent, "event-1").work_order_verified is False
    assert db.query(BlindLabelWorkOrder).count() == 0

    # 结论已落库，第二轮在 SQL 层就被排除。
    assert ensure_work_orders(db, 1, verifier=_StubVerifier()) == 0
    assert db.query(BlindLabelWorkOrder).count() == 0


def test_ensure_work_orders_allows_numbers_with_work_order_detail_traffic(db, monkeypatch):
    """访问日志里有工单详情流量的号码正常建单，并记 work_order_verified=True。"""

    monkeypatch.setattr(
        "app.services.blind_labeling.log_verdict_for",
        lambda number, **kwargs: "real",
    )
    assert ensure_work_orders(db, 1, verifier=_StubVerifier()) == 1
    db.commit()

    assert db.get(RetrievalQualityEvent, "event-1").work_order_verified is True
    assert db.query(BlindLabelWorkOrder).count() == 1


def test_ensure_work_orders_prefers_the_upstream_verifier_over_the_access_log(db, monkeypatch):
    """上游有结论时不再查日志（日志只是兜底证据）。"""

    calls: list[str] = []

    def _log_verdict(number, **kwargs):
        calls.append(number)
        return "session"

    monkeypatch.setattr("app.services.blind_labeling.log_verdict_for", _log_verdict)
    assert ensure_work_orders(db, 1, verifier=_StubVerifier({"question-form-1": True})) == 1
    assert calls == []
