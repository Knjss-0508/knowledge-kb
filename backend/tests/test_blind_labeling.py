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
    POOL_STATE_ACTIVE,
    POOL_STATE_RETIRED,
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
    BLIND_LABEL_MAX_ASSIGNMENTS,
    BLIND_LABEL_MAX_TOP1_SCORE_EXCLUSIVE,
    _choose_assignments,
    batch_summary,
    claim_batch,
    consensus_for_work_order,
    current_batch,
    ensure_work_orders,
    is_session_number,
    mark_assignment_started,
    overview,
    public_assignment_detail,
    release_expired_assignments,
    release_assignment,
    retire_session_work_orders,
    session_number_snapshot,
    submit_assignment,
    work_order_status,
)
from app.services.work_order_log_verdicts import VERDICT_SESSION


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
    for user_id in ("u1", "u2", "u3", "u4", "u5", "u6"):
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
            top_rerank_score=0.79,
            score_threshold=0.42,
            outcome="accepted",
            request_status="success",
            candidate_snapshot=[
                {
                    "knowledge_id": f"biz-{index}",
                    "rank": index,
                    "title": f"业务知识 {index}",
                    "final_score": 0.79 - index * 0.01,
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
                top_rerank_score=0.79,
                candidate_snapshot=[
                    {
                        "knowledge_id": f"biz-{index}",
                        "rank": 1,
                        "title": f"业务知识 {index}",
                        "final_score": 0.79 - index * 0.01,
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
            top_rerank_score=0.79,
            candidate_snapshot=[
                {
                    "knowledge_id": f"biz-{index}",
                    "rank": index,
                    "title": f"业务知识 {index}",
                    "final_score": 0.79 - index * 0.01,
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
            top_rerank_score=0.79,
            candidate_snapshot=[
                {
                    "knowledge_id": f"biz-{index}",
                    "rank": index,
                    "title": f"业务知识 {index}",
                    "final_score": 0.79 - index * 0.01,
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


def test_top1_score_below_eighty_percent_is_materialized_but_boundary_and_above_are_not(db):
    for event_id, conversation_id, score in (
        ("event-low-score", "conversation-low-score", 0.59),
        (
            "event-near-boundary-score",
            "conversation-near-boundary-score",
            BLIND_LABEL_MAX_TOP1_SCORE_EXCLUSIVE - 0.000001,
        ),
        (
            "event-boundary-score",
            "conversation-boundary-score",
            BLIND_LABEL_MAX_TOP1_SCORE_EXCLUSIVE,
        ),
        (
            "event-high-score",
            "conversation-high-score",
            BLIND_LABEL_MAX_TOP1_SCORE_EXCLUSIVE + 0.000001,
        ),
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
            top_rerank_score=BLIND_LABEL_MAX_TOP1_SCORE_EXCLUSIVE - 0.000001,
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

    assert ensure_work_orders(db, 10) == 4
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-low-score").count() == 1
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-near-boundary-score").count() == 1
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-boundary-score").count() == 0
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-high-score").count() == 0
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-reply-fallback-score").count() == 1


def test_existing_high_score_work_order_is_not_assigned_to_new_batch(db):
    db.query(RetrievalQualityEvent).filter_by(id="event-1").update({"source_kind": "standard"})
    db.add(
        BlindLabelWorkOrder(
            id="wo-high-score",
            conversation_id="conversation-high-work-order",
            query_text="历史高分工单",
            candidate_snapshot=[
                {
                    "candidate_ref": "candidate-high-score",
                    "knowledge_id": "biz-1",
                    "rank": 1,
                    "title": "高分业务知识",
                    "content": {"type": "text", "value": "正文"},
                    "final_score": BLIND_LABEL_MAX_TOP1_SCORE_EXCLUSIVE,
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
            top_rerank_score=0.79,
            score_threshold=0.42,
            outcome="accepted",
            request_status="success",
            candidate_snapshot=[
                {
                    "knowledge_id": f"biz-{index}",
                    "rank": index,
                    "title": f"业务知识 {index}",
                    "final_score": 0.79 - index * 0.01,
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
            id="u7",
            username="u7",
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
                candidate_snapshot=[{"candidate_ref": "cand-two-old", "final_score": 0.79}],
            source_created_at=now - timedelta(days=3),
        ),
        "one-new": BlindLabelWorkOrder(
            id="wo-one-new",
            conversation_id="priority-one-new",
            query_text="已有一人标注的新工单",
                candidate_snapshot=[{"candidate_ref": "cand-one-new", "final_score": 0.79}],
            source_created_at=now - timedelta(days=1),
        ),
        "one-old": BlindLabelWorkOrder(
            id="wo-one-old",
            conversation_id="priority-one-old",
            query_text="已有一人标注的旧工单",
                candidate_snapshot=[{"candidate_ref": "cand-one-old", "final_score": 0.79}],
            source_created_at=now - timedelta(days=2),
        ),
        "fresh": BlindLabelWorkOrder(
            id="wo-fresh",
            conversation_id="priority-fresh",
            query_text="尚未分配的最新工单",
                candidate_snapshot=[{"candidate_ref": "cand-fresh", "final_score": 0.79}],
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
        for user_id in ("u1", "u2", "u4", "u5", "u6", "u7")
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
    assert [item.work_order_id for item in second] == [work_orders["two-old"].id]
    third = _choose_assignments(db, batches["u6"], "u6", needed=1)
    assert [item.work_order_id for item in third] == [work_orders["two-old"].id]
    fourth = _choose_assignments(db, batches["u7"], "u7", needed=1)
    assert [item.work_order_id for item in fourth] == [work_orders["one-new"].id]


def test_claim_path_tolerates_a_work_order_that_already_has_a_completed_assignment(db):
    """回归（2026-10-14 生产事故）：领单路径不得把「单列投影行」当完整分配行。

    ``work_order_status`` 在没有预加载分配时用的是
    ``db.query(BlindLabelAssignment.status)`` 的单列投影；#194 之后这份投影被
    无条件透传给 ``consensus_for_work_order``，``_completed_assignments`` 于是
    读 ``item.completed_at`` 抛 ``AttributeError: completed_at``。只要候选工单有
    至少一条已完成分配，``POST /api/v1/blind-labeling/my-batch:claim`` 就整体 500。
    """

    now = datetime.utcnow()
    work_order = BlindLabelWorkOrder(
        id="wo-already-completed",
        conversation_id="priority-already-completed",
        query_text="已有一人完成标注的工单",
        candidate_snapshot=[
            {"candidate_ref": "cand-already-completed", "final_score": 0.79}
        ],
        source_created_at=now - timedelta(days=1),
    )
    db.add(work_order)
    batches = {
        user_id: BlindLabelBatch(
            id=f"completed-batch-{user_id}",
            user_id=user_id,
            target_count=BLIND_LABEL_BATCH_SIZE,
            status="active",
        )
        for user_id in ("u1", "u4")
    }
    db.add_all(batches.values())
    db.flush()
    db.add(
        BlindLabelAssignment(
            id="completed-assign-u1",
            batch_id=batches["u1"].id,
            work_order_id=work_order.id,
            user_id="u1",
            status="completed",
            completed_at=now,
        )
    )
    db.flush()

    # 1) 直接调用判定：应返回 partial，而不是抛 AttributeError: completed_at
    resolved_status, _ = work_order_status(db, work_order)
    assert resolved_status == "partial"

    # 2) 真实领单路径：另一名标注员仍然能领到这张尚未满员的工单
    chosen = _choose_assignments(db, batches["u4"], "u4", needed=1)
    assert [item.work_order_id for item in chosen] == [work_order.id]


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


def test_five_users_are_capped_and_four_of_five_requires_arbitration_without_legacy_write(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3", "u4", "u5"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    for index, assignment in enumerate(assignments):
        verdict = "referable" if index < 4 else "not_referable"
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
    assert consensus["required_annotators"] == BLIND_LABEL_MAX_ASSIGNMENTS == 5
    assert consensus["candidate_results"][0]["preliminary_verdict"] == "referable"
    assert consensus["candidate_results"][0]["status"] == "needs_arbitration"
    assert consensus["candidate_results"][0]["needs_arbitration"] is True
    assert db.query(RetrievalQualityEvent).filter_by(id="event-1").one().review_status == "unreviewed"
    assert db.query(BlindLabelAssignment).filter_by(work_order_id=work_order.id).count() == 5
    consensus_overview = overview(db)
    assert consensus_overview["summary"]["disputed"] == 1
    assert consensus_overview["summary"]["unanimous_count"] == 0
    assert consensus_overview["summary"]["consensus_rate"] == 0

    _, sixth_rows = claim_batch(db, "u6", target_count=1)
    assert sixth_rows == []


def test_five_of_five_same_choice_reaches_unanimous_consensus(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3", "u4", "u5"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    for assignment in assignments:
        submit_assignment(db, assignment, _labels(assignment, "referable"))
    db.commit()

    consensus = consensus_for_work_order(db, assignments[0].work_order)

    assert consensus["status"] == "unanimous"
    assert consensus["needs_arbitration"] is False
    assert consensus["candidate_results"][0]["status"] == "unanimous"
    assert consensus["candidate_results"][0]["consensus_verdict"] == "referable"
    assert all(
        result["status"] == "not_selected"
        for result in consensus["candidate_results"][1:]
    )


def test_single_choice_consensus_does_not_treat_omitted_candidates_as_negative_votes(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3", "u4", "u5"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])

    refs = _all_refs(assignments[0])
    for index, assignment in enumerate(assignments):
        ref = refs[index % len(refs)]
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
        result["referable_count"] in {1, 2}
        and result["not_referable_count"] == 0
        and result["status"] == "needs_arbitration"
        for result in consensus["candidate_results"]
    )


def test_four_of_five_all_not_referable_requires_arbitration(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3", "u4", "u5"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])

    for index, assignment in enumerate(assignments):
        verdict = "not_referable" if index < 4 else "referable"
        submit_assignment(
            db,
            assignment,
            _labels(assignment, verdict),
            task_reason_code="knowledge_missing" if verdict == "not_referable" else "",
        )
    db.commit()

    consensus = consensus_for_work_order(db, assignments[0].work_order)

    assert consensus["status"] == "majority"
    assert consensus["needs_arbitration"] is True
    assert consensus["candidate_results"][0]["status"] == "needs_arbitration"
    assert consensus["candidate_results"][0]["consensus_verdict"] is None
    assert all(
        result["status"] == "not_selected"
        for result in consensus["candidate_results"][1:]
    )


def test_fewer_than_five_completed_annotations_remain_pending(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])

    for assignment in assignments:
        submit_assignment(db, assignment, _labels(assignment, "referable"))
    db.commit()

    consensus = consensus_for_work_order(db, assignments[0].work_order)

    assert consensus["status"] == "pending"
    assert consensus["completed_annotators"] == 3
    assert consensus["needs_arbitration"] is False


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
    for user_id in ("u1", "u2", "u3", "u4", "u5"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    for index, assignment in enumerate(assignments):
        verdict = "referable" if index < 3 else "not_referable"
        submit_assignment(
            db,
            assignment,
            _labels(assignment, verdict),
            task_reason_code="knowledge_missing" if verdict == "not_referable" else "",
        )
    db.commit()
    work_order = assignments[0].work_order
    before_arbitration = consensus_for_work_order(db, work_order)
    assert before_arbitration["status"] == "majority"
    assert before_arbitration["needs_arbitration"] is True
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


def test_legacy_multi_choice_keeps_unanimous_candidate_out_of_arbitration(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    work_order = assignments[0].work_order
    work_order.assignment_limit = 3
    work_order.candidate_snapshot = work_order.candidate_snapshot[:2]
    first_ref, second_ref = _all_refs(assignments[0])
    for assignment in assignments:
        submit_assignment(db, assignment, _labels(assignment, "referable"))
    for index, assignment in enumerate(assignments):
        verdict = "referable" if index < 2 else "not_referable"
        db.add(
            BlindLabelAnnotation(
                id=f"legacy-multi-choice-{index}",
                assignment_id=assignment.id,
                work_order_id=work_order.id,
                user_id=assignment.user_id,
                candidate_ref=second_ref,
                verdict=verdict,
                reason_code="topic_irrelevant" if verdict == "not_referable" else "",
                reason="历史多选测试" if verdict == "not_referable" else "",
            )
        )
    db.commit()

    consensus = consensus_for_work_order(db, work_order)
    results = {item["candidate_ref"]: item for item in consensus["candidate_results"]}

    assert consensus["status"] == "majority"
    assert consensus["needs_arbitration"] is True
    assert results[first_ref]["status"] == "unanimous"
    assert results[first_ref]["needs_arbitration"] is False
    assert results[second_ref]["status"] == "needs_arbitration"
    assert results[second_ref]["needs_arbitration"] is True


def test_legacy_three_annotator_arbitration_remains_terminal_after_five_vote_upgrade(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    work_order = assignments[0].work_order
    work_order.assignment_limit = 3
    for index, assignment in enumerate(assignments):
        verdict = "referable" if index < 2 else "not_referable"
        submit_assignment(
            db,
            assignment,
            _labels(assignment, verdict),
            task_reason_code="knowledge_missing" if verdict == "not_referable" else "",
        )
    for index, candidate in enumerate(work_order.candidate_snapshot, start=1):
        db.add(
            BlindLabelArbitration(
                id=f"legacy-arb-{index}",
                work_order_id=work_order.id,
                candidate_ref=candidate["candidate_ref"],
                verdict="not_referable",
                reason="历史仲裁",
                arbitrated_by="admin",
            )
        )
    db.commit()

    consensus = consensus_for_work_order(db, work_order)

    assert consensus["status"] == "arbitrated"
    assert consensus["completed_annotators"] == 3
    assert consensus["needs_arbitration"] is False


def test_legacy_three_annotator_consensus_stays_completed_and_is_not_reassigned(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    work_order = assignments[0].work_order
    work_order.assignment_limit = 3
    for assignment in assignments:
        submit_assignment(db, assignment, _labels(assignment, "referable"))
    db.commit()

    consensus = consensus_for_work_order(db, work_order)

    assert consensus["status"] == "unanimous"
    assert consensus["required_annotators"] == 3
    assert consensus["needs_arbitration"] is False

    _, fourth_rows = claim_batch(db, "u4", target_count=1)
    assert fourth_rows == []


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


def test_my_assignments_paginates_beyond_the_first_page(db):
    """「我的已标注」必须能翻到第一页之后的记录：条数取全量 total，且分页不重不漏。"""

    _seed_extra_business_events(db)
    assert ensure_work_orders(db, BLIND_LABEL_BATCH_SIZE) == 3
    _, rows = claim_batch(db, "u1")
    for assignment in rows:
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")
    db.commit()

    user = db.get(User, "u1")

    first = _load_my_assignments(db, user, page=1, page_size=2)
    assert first["total"] == 3
    assert first["total_pages"] == 2
    assert first["page"] == 1
    assert first["page_size"] == 2
    assert len(first["items"]) == 2

    second = _load_my_assignments(db, user, page=2, page_size=2)
    # 第二页同样返回全量 total，前端才能显示「共 N 条」并算出正确页数。
    assert second["total"] == 3
    assert second["total_pages"] == 2
    assert len(second["items"]) == 1

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids & second_ids == set()
    assert len(first_ids | second_ids) == 3

    # 超出末页返回空页，但 total 仍是筛选后的全量条数。
    beyond = _load_my_assignments(db, user, page=3, page_size=2)
    assert beyond["items"] == []
    assert beyond["total"] == 3
    assert beyond["total_pages"] == 2


def test_my_assignments_pagination_follows_the_date_filter(db):
    """分页与时间筛选同一口径：页码在筛选后的结果集上生效。"""

    _seed_extra_business_events(db)
    assert ensure_work_orders(db, BLIND_LABEL_BATCH_SIZE) == 3
    _, rows = claim_batch(db, "u1")
    for assignment in rows:
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")
    db.commit()

    user = db.get(User, "u1")
    today = datetime.utcnow().date()
    past = today - timedelta(days=5)

    # 过去的区间没有活动记录：total 为 0，页数兜底为 1，不会返回上一区间的残留数据。
    empty = _load_my_assignments(db, user, start_date=past, end_date=past, page=1, page_size=2)
    assert empty["items"] == []
    assert empty["total"] == 0
    assert empty["total_pages"] == 1

    filtered = _load_my_assignments(db, user, start_date=today, end_date=today, page=2, page_size=2)
    assert filtered["total"] == 3
    assert filtered["total_pages"] == 2
    assert len(filtered["items"]) == 1


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


def test_ensure_work_orders_blocks_numbers_proved_chat_only_by_the_log_tail(db, monkeypatch):
    """Cookie 判定不了、状态文件也还没轮到它时，访问日志尾部是最后一道实时防线。

    这正是 2026-10-14 生产上 2110212947606246403 的场景：Cookie 门禁自我禁用后
    整批 fail-open，而号码在建单前 3~5 秒才第一次出现在访问日志里，worker 的
    300 秒轮询状态文件还查不到它。
    """

    monkeypatch.setattr(
        "app.services.blind_labeling.log_verdict_for",
        lambda number, **kwargs: None,
    )
    tail_calls: list[dict] = []

    def _tail(**kwargs):
        tail_calls.append(kwargs)
        return {"question-form-1": VERDICT_SESSION}

    monkeypatch.setattr("app.services.blind_labeling.log_tail_signals", _tail)

    verifier = _StubVerifier()
    assert ensure_work_orders(db, 1, verifier=verifier) == 0
    db.commit()

    # 上游仍然只被问了一次；判定结论落库后第二轮在 SQL 层就被排除。
    assert verifier.calls == ["question-form-1"]
    assert db.get(RetrievalQualityEvent, "event-1").work_order_verified is False
    assert db.query(BlindLabelWorkOrder).count() == 0
    assert len(tail_calls) == 1


def test_ensure_work_orders_scans_the_log_tail_once_per_run(db, monkeypatch):
    """多个候选只允许读一次有界窗口：绝不允许按号码逐个读日志。"""

    _seed_extra_business_events(db)
    monkeypatch.setattr(
        "app.services.blind_labeling.log_verdict_for",
        lambda number, **kwargs: None,
    )
    tail_calls: list[int] = []

    def _tail(**kwargs):
        tail_calls.append(1)
        return {}

    monkeypatch.setattr("app.services.blind_labeling.log_tail_signals", _tail)

    assert ensure_work_orders(db, 3, verifier=_StubVerifier()) == 3
    assert len(tail_calls) == 1


def test_session_number_snapshot_caches_and_never_treats_unknown_as_session(monkeypatch):
    """派发热路径每 60 秒最多读一次；未知号码绝不当成会话号。"""

    loads: list[int] = []

    def _load():
        loads.append(1)
        return frozenset({"2108536379561477121"})

    monkeypatch.setattr("app.services.blind_labeling._load_session_numbers", _load)
    monkeypatch.setattr(
        "app.services.blind_labeling._SESSION_NUMBER_CACHE",
        {"numbers": frozenset(), "expires_at": 0.0},
    )

    assert is_session_number("2108536379561477121") is True
    assert session_number_snapshot() == frozenset({"2108536379561477121"})
    assert len(loads) == 1
    # KB 自己的复核请求不过 zzdy 的 nginx，「日志里完全没这个号」是常见情况。
    assert is_session_number("2108536379561477199") is False
    assert is_session_number("") is False
    assert is_session_number(None) is False


def test_choose_assignments_never_hands_out_proven_session_numbers(db, monkeypatch):
    """派发侧两道防线：SQL 排除 + 逐候选复查，都不再交出会话号工单。"""

    _seed_extra_business_events(db, 1)
    verifier = _StubVerifier({"question-form-1": True, "conversation-extra-1": True})
    assert ensure_work_orders(db, 2, verifier=verifier) == 2

    batch = BlindLabelBatch(
        id="batch-dispatch",
        user_id="u1",
        target_count=BLIND_LABEL_BATCH_SIZE,
        status="active",
    )
    db.add(batch)
    db.commit()

    monkeypatch.setattr(
        "app.services.blind_labeling.session_number_snapshot",
        lambda: frozenset({"question-form-1"}),
    )
    monkeypatch.setattr(
        "app.services.blind_labeling.is_session_number",
        lambda number: str(number or "").strip() == "question-form-1",
    )
    created = _choose_assignments(db, batch, "u1", 2)
    assert [item.work_order.question_form_id for item in created] == [
        "conversation-extra-1"
    ]
    db.commit()

    # 只留 Python 一道防线（模拟会话号集合超出 SQL 参数上限、SQL 未排除的情况）。
    monkeypatch.setattr(
        "app.services.blind_labeling.session_number_snapshot",
        lambda: frozenset(),
    )
    second = _choose_assignments(db, batch, "u2", 2)
    assert [item.work_order.question_form_id for item in second] == [
        "conversation-extra-1"
    ]


def test_retired_work_order_is_not_dispatchable_and_not_re_materialized(db, monkeypatch):
    """退池后：不再派发，也不会被同一条事件重新物化成第二张工单。"""

    assert ensure_work_orders(db, 1, verifier=_StubVerifier({"question-form-1": True})) == 1
    db.commit()

    monkeypatch.setattr(
        "app.services.blind_labeling._load_session_numbers",
        lambda: frozenset({"question-form-1"}),
    )
    assert retire_session_work_orders(db)["retired"] == 1
    db.commit()

    batch = BlindLabelBatch(
        id="batch-retired",
        user_id="u1",
        target_count=BLIND_LABEL_BATCH_SIZE,
        status="active",
    )
    db.add(batch)
    db.commit()
    monkeypatch.setattr(
        "app.services.blind_labeling.session_number_snapshot",
        lambda: frozenset({"question-form-1"}),
    )
    assert _choose_assignments(db, batch, "u1", 1) == []

    # existing_conversations / existing_question_forms 必须保持「不过滤退池」，
    # 否则同一条事件会被重新物化成第二张工单，历史判定也就白做了。
    retry = _StubVerifier({"question-form-1": True})
    assert ensure_work_orders(db, 1, verifier=retry) == 0
    assert retry.calls == []
    assert db.query(BlindLabelWorkOrder).count() == 1
    assert db.query(BlindLabelWorkOrder).one().pool_state == POOL_STATE_RETIRED


def test_retire_session_work_orders_releases_in_flight_and_keeps_completed_annotations(
    db, monkeypatch
):
    """退池只改状态：在途派发被释放，已完成的标注与记录原样保留。"""

    _seed_extra_business_events(db, 1)
    verifier = _StubVerifier({"question-form-1": True, "conversation-extra-1": True})
    assert ensure_work_orders(db, 2, verifier=verifier) == 2
    orders = {order.question_form_id: order for order in db.query(BlindLabelWorkOrder)}
    session_order = orders["question-form-1"]
    control_order = orders["conversation-extra-1"]

    batch = BlindLabelBatch(
        id="batch-retire",
        user_id="u1",
        target_count=BLIND_LABEL_BATCH_SIZE,
        status="active",
    )
    db.add(batch)
    db.flush()
    in_flight = BlindLabelAssignment(
        id="as-in-flight",
        batch_id=batch.id,
        work_order_id=session_order.id,
        user_id="u2",
        status="in_progress",
    )
    to_complete = BlindLabelAssignment(
        id="as-completed",
        batch_id=batch.id,
        work_order_id=session_order.id,
        user_id="u1",
        status="assigned",
    )
    control = BlindLabelAssignment(
        id="as-control",
        batch_id=batch.id,
        work_order_id=control_order.id,
        user_id="u3",
        status="assigned",
    )
    db.add_all([in_flight, to_complete, control])
    db.commit()

    annotations = submit_assignment(
        db, to_complete, _labels(to_complete), task_reason_code=""
    )
    db.commit()
    assert db.get(BlindLabelAssignment, to_complete.id).status == "completed"

    monkeypatch.setattr(
        "app.services.blind_labeling._load_session_numbers",
        lambda: frozenset({"question-form-1"}),
    )
    result = retire_session_work_orders(db)
    db.commit()

    assert result == {"retired": 1, "released": 1, "scanned": 1}
    db.refresh(session_order)
    assert session_order.pool_state == POOL_STATE_RETIRED
    assert session_order.retire_reason == "session_number"
    assert session_order.retired_at is not None

    released = db.get(BlindLabelAssignment, in_flight.id)
    assert released.status == "released"
    assert released.release_reason == "session_number"
    assert released.released_at is not None

    kept = db.get(BlindLabelAssignment, to_complete.id)
    assert kept.status == "completed"
    assert kept.release_reason == ""
    assert db.query(BlindLabelAnnotation).filter_by(assignment_id=kept.id).count() == len(
        annotations
    )
    # 对照：真工单与它的在途派发完全不受影响。
    assert db.get(BlindLabelAssignment, control.id).status == "assigned"
    assert db.get(BlindLabelWorkOrder, control_order.id).pool_state == POOL_STATE_ACTIVE

    # 幂等：第二轮不再计数、不再释放，标注记录一条不少。
    assert retire_session_work_orders(db) == {"retired": 0, "released": 0, "scanned": 0}
    db.commit()
    assert db.query(BlindLabelAnnotation).count() == len(annotations)


def test_overview_hides_retired_work_orders_unless_queried_by_number(db, monkeypatch):
    """池内列表与统计默认排除退池工单，但按号码查证时仍然可读（审计）。"""

    assert ensure_work_orders(db, 1, verifier=_StubVerifier({"question-form-1": True})) == 1
    order = db.query(BlindLabelWorkOrder).one()
    batch = BlindLabelBatch(
        id="batch-overview",
        user_id="u1",
        target_count=BLIND_LABEL_BATCH_SIZE,
        status="active",
    )
    db.add(batch)
    db.flush()
    db.add(
        BlindLabelAssignment(
            id="as-overview",
            batch_id=batch.id,
            work_order_id=order.id,
            user_id="u1",
            status="assigned",
        )
    )
    db.commit()
    assert [item["question_form_id"] for item in overview(db)["work_orders"]] == [
        "question-form-1"
    ]

    monkeypatch.setattr(
        "app.services.blind_labeling._load_session_numbers",
        lambda: frozenset({"question-form-1"}),
    )
    assert retire_session_work_orders(db)["retired"] == 1
    db.commit()

    assert overview(db)["work_orders"] == []
    assert overview(db)["summary"]["work_orders"] == 0
    assert [
        item["question_form_id"]
        for item in overview(db, conversation_id="conversation-1")["work_orders"]
    ] == ["question-form-1"]
    assert [
        item["question_form_id"]
        for item in overview(db, include_retired=True)["work_orders"]
    ] == ["question-form-1"]


def test_starting_an_assignment_released_by_retire_explains_the_reason(db, monkeypatch):
    """标注员手上的单被系统退池释放后，得到的是可理解的提示而不是 500。"""

    assert ensure_work_orders(db, 1, verifier=_StubVerifier({"question-form-1": True})) == 1
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    db.commit()

    monkeypatch.setattr(
        "app.services.blind_labeling._load_session_numbers",
        lambda: frozenset({"question-form-1"}),
    )
    assert retire_session_work_orders(db)["released"] == 1
    db.commit()
    db.refresh(assignment)
    assert assignment.status == "released"

    with pytest.raises(ValueError, match="系统退池"):
        mark_assignment_started(db, assignment)
    with pytest.raises(ValueError, match="系统退池"):
        submit_assignment(db, assignment, _labels(assignment), task_reason_code="")

