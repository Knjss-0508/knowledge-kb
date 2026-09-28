from datetime import datetime

import pytest
from sqlalchemy import create_engine
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
from app.routes.blind_labeling import require_blind_annotator
from app.services.blind_labeling import (
    batch_summary,
    claim_batch,
    consensus_for_work_order,
    ensure_work_orders,
    overview,
    public_assignment_detail,
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
    return [
        {"candidate_ref": ref, "verdict": verdict, "reason": "测试"}
        for ref in _all_refs(assignment)
    ]


def test_claim_materializes_business_top3_and_reuses_active_batch(db):
    assert ensure_work_orders(db, 1) == 1
    batch, assignments = claim_batch(db, "u1", target_count=1)
    db.commit()
    assert batch.status == "active"
    assert len(assignments) == 1
    assert len(assignments[0].work_order.candidate_snapshot) == 3
    assert assignments[0].work_order.conversation_id == "conversation-1"
    assert db.query(BlindLabelWorkOrder).filter_by(conversation_id="conversation-standard").count() == 0

    reused, same_assignments = claim_batch(db, "u1", target_count=1)
    assert reused.id == batch.id
    assert [item.id for item in same_assignments] == [assignments[0].id]


def test_three_users_are_capped_and_consensus_never_writes_legacy_event(db):
    ensure_work_orders(db, 1)
    assignments = []
    for user_id in ("u1", "u2", "u3"):
        _, rows = claim_batch(db, user_id, target_count=1)
        assignments.append(rows[0])
    for index, assignment in enumerate(assignments):
        submit_assignment(
            db,
            assignment,
            _labels(assignment, "referable" if index < 2 else "not_referable"),
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


def test_release_frees_slot_but_does_not_allow_same_user_reassignment(db):
    ensure_work_orders(db, 1)
    _, rows = claim_batch(db, "u1", target_count=1)
    assignment = rows[0]
    release_assignment(db, assignment, "无法标注")
    db.commit()
    assert assignment.status == "released"
    _, again = claim_batch(db, "u1", target_count=1)
    assert again == []
    # Another user can take the released slot.
    _, another = claim_batch(db, "u2", target_count=1)
    assert len(another) == 1


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
        submit_assignment(db, assignment, _labels(assignment, "referable" if index < 2 else "not_referable"))
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


def test_post_claim_and_overview_compatibility_fields(db):
    ensure_work_orders(db, 1)
    batch, rows = claim_batch(db, "u1", target_count=1)
    summary = batch_summary(db, batch)
    assert summary["total"] == 1
    assert summary["assigned"] == 1
    assert summary["pending"] == 0
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


def test_blind_label_api_contract_exposes_claim_detail_and_overview_routes():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/blind-labeling/my-batch:claim" in paths
    assert "/api/v1/blind-labeling/overview" in paths
    assert "/api/v1/blind-labeling/work-orders/{work_order_id}" in paths
    assert "post" in paths["/api/v1/blind-labeling/assignments/{assignment_id}/submit"]
