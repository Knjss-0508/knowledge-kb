from datetime import datetime
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.integration import EvaluationSample, IntegrationIngestion
from app.models.knowledge import Knowledge, KnowledgeStatus
from app.services.evaluation_samples import (
    freeze_evaluation_snapshot,
    latest_evaluation_samples,
    stable_topic_split,
)
from app.routes.confidence_training import _rows


def _ingestion(
    *,
    ingestion_id: str = "ing-1",
    human_label: str = "worthy",
    model_label: str = "worthy",
) -> IntegrationIngestion:
    return IntegrationIngestion(
        id=ingestion_id,
        event_id="batch-1",
        idempotency_key=f"key-{ingestion_id}",
        source_system="answer-hub",
        source_conversation_id="conversation-topic-1",
        processing_metadata={"batch_id": "published-batch-7"},
        selection_metadata={
            "topic_group_id": "topic-7",
            "answer_hub_replay_input": {
                "topic_id": "TOP-7",
                "产品类型": "笔记本",
                "主题事实证据包": {"facts": [{"fact_id": "F01"}]},
            },
        },
        candidate_payload={
            "knowledge": {
                "title": "屏幕无显示",
                "content": {"blocks": [{"type": "text", "value": "检查连接"}]},
                "recommended_reply": "建议检查连接。",
                "evidence_excerpt": "用户描述屏幕问题",
            },
            "model_review": {
                "knowledge_value": model_label,
                "draft_quality": "approved",
                "suggested_action": "submit_for_human_review",
                "model_name": "model-v3",
                "resolved_model_version": "model-v3.2",
                "prompt_version": "review-v4",
                "confidence": 0.94,
            },
            "human_review": {
                "knowledge_value": human_label,
                "draft_disposition": "approved" if human_label == "worthy" else None,
                "reviewer": "reviewer-1",
                "reviewed_at": "2026-10-01T12:00:00",
            },
        },
        review_metadata={},
        review_status="ready" if human_label == "worthy" else "rejected",
        reviewed_by="reviewer-1",
        reviewed_at=datetime(2026, 10, 1, 12, 0),
        status="candidate_ready",
    )


def _session():
    engine = create_engine("sqlite://")
    Knowledge.__table__.create(engine)
    IntegrationIngestion.__table__.create(engine)
    EvaluationSample.__table__.create(engine)
    return engine, Session(engine)


def test_topic_split_is_stable_and_assigns_a_topic_as_one_unit():
    topic_groups = [f"topic-{index}" for index in range(30)]
    assignments = {group: stable_topic_split(group) for group in topic_groups}

    assert assignments == {
        group: stable_topic_split(group) for group in reversed(topic_groups)
    }
    assert set(assignments.values()) <= {"train", "validation", "test"}
    assert len(set(assignments.values())) == 3
    assert stable_topic_split("same-topic") == stable_topic_split("same-topic")


def test_frozen_sample_preserves_original_model_output_and_truth_after_source_changes():
    engine, db = _session()
    try:
        source = _ingestion()
        db.add(source)
        db.commit()

        first = freeze_evaluation_snapshot(db)
        first_sample = latest_evaluation_samples(db)[0]
        frozen_model = dict(first_sample.model_output)
        frozen_truth = dict(first_sample.human_truth)
        assert first_sample.candidate_id == "ing-1"
        assert first_sample.source_batch == "published-batch-7"
        assert first_sample.topic_group == "topic-7"
        assert first_sample.draft_version.startswith("draft-0-")
        assert first_sample.model_version == "model-v3.2"
        assert first_sample.prompt_version == "review-v4"
        assert first_sample.reviewed_at == datetime(2026, 10, 1, 12, 0)
        assert first_sample.dataset_split == stable_topic_split("topic-7")
        assert (
            first_sample.draft_snapshot["answer_hub_replay_input"]["topic_id"]
            == "TOP-7"
        )

        payload = dict(source.candidate_payload)
        payload["model_review"] = {
            **payload["model_review"],
            "knowledge_value": "unworthy",
            "prompt_version": "review-v5",
        }
        payload["human_review"] = {
            **payload["human_review"],
            "knowledge_value": "unworthy",
        }
        source.candidate_payload = payload
        db.commit()

        second = freeze_evaluation_snapshot(db)
        snapshots = (
            db.query(EvaluationSample)
            .filter(EvaluationSample.candidate_id == source.id)
            .order_by(EvaluationSample.created_at.asc())
            .all()
        )

        assert first["snapshot_id"] != second["snapshot_id"]
        assert len(snapshots) == 2
        assert snapshots[0].model_output == frozen_model
        assert snapshots[0].human_truth == frozen_truth
        assert snapshots[1].model_output["prompt_version"] == "review-v5"
        assert snapshots[1].human_truth["knowledge_value"] == "unworthy"
        assert first_sample.dataset_split == stable_topic_split("topic-7")
    finally:
        db.close()
        engine.dispose()


def test_changed_draft_cannot_freeze_previous_human_approval():
    engine, db = _session()
    try:
        source = _ingestion()
        source.candidate_payload["knowledge"]["content"] = "尚未重新审核的新正文"
        source.review_metadata = {
            "draft_version": 2,
            "human_review_draft_version": 1,
            "draft_revision_history": [{"draft_version": 2, "source": "human_edit"}],
            "model_annotation": {
                "status": "stale_after_draft_revision",
                "draft_version": 2,
            },
        }
        db.add(source)
        db.commit()

        result = freeze_evaluation_snapshot(db)

        assert result["sample_count"] == 0
        assert latest_evaluation_samples(db) == []
        assert source.candidate_payload["human_review"]["draft_disposition"] == "approved"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("versioned", [False, True], ids=["legacy-v1", "bound-v2"])
def test_website_value_review_freezes_with_its_verified_draft_version(versioned):
    engine, db = _session()
    try:
        source = _ingestion()
        source.candidate_payload["human_review"] = {
            "knowledge_value": "worthy",
            "usability": "usable",
            "decision": "approved",
        }
        if versioned:
            source.review_metadata = {
                "draft_version": 2,
                "human_review_draft_version": 2,
                "draft_revision_history": [{"draft_version": 2}],
                "model_annotation": {"status": "completed", "draft_version": 2},
            }
        db.add(source)
        db.commit()

        result = freeze_evaluation_snapshot(db)
        sample = latest_evaluation_samples(db)[0]

        assert result["sample_count"] == 1
        assert sample.human_truth["draft_disposition"] == "approved"
        assert sample.human_truth["draft_version"] == (2 if versioned else 1)
        assert sample.human_truth["draft_version_source"] == (
            "human_review_draft_version" if versioned else "legacy_v1"
        )
        assert "draft_disposition" not in source.candidate_payload["human_review"]
    finally:
        db.close()
        engine.dispose()


def test_revised_draft_needs_fresh_model_and_human_reviews_before_a_new_snapshot():
    engine, db = _session()
    try:
        source = _ingestion()
        db.add(source)
        db.commit()
        first = freeze_evaluation_snapshot(db)
        original = latest_evaluation_samples(db)[0]

        source.candidate_payload = {
            **source.candidate_payload,
            "knowledge": {**source.candidate_payload["knowledge"], "content": "修订后的正文"},
        }
        source.review_metadata = {
            "draft_version": 2,
            "human_review_draft_version": 1,
            "draft_revision_history": [{"draft_version": 2}],
            "model_annotation": {"status": "stale_after_draft_revision", "draft_version": 2},
        }
        db.commit()
        assert freeze_evaluation_snapshot(db)["sample_count"] == 0

        source.review_metadata = {
            **source.review_metadata,
            "model_annotation": {"status": "completed", "draft_version": 2},
        }
        db.commit()
        assert freeze_evaluation_snapshot(db)["sample_count"] == 0
        assert latest_evaluation_samples(db)[0].snapshot_id == first["snapshot_id"]

        source.review_metadata = {**source.review_metadata, "human_review_draft_version": 2}
        source.candidate_payload = {
            **source.candidate_payload,
            "human_review": {"knowledge_value": "worthy", "usability": "usable", "decision": "approved"},
        }
        db.commit()
        refreshed = freeze_evaluation_snapshot(db)
        current = latest_evaluation_samples(db)[0]

        assert refreshed["sample_count"] == 1
        assert refreshed["snapshot_id"] != first["snapshot_id"]
        assert current.draft_snapshot["content"] == "修订后的正文"
        assert current.human_truth["draft_version"] == 2
        assert original.draft_snapshot["content"] == {
            "blocks": [{"type": "text", "value": "检查连接"}]
        }
        assert original.human_truth["draft_version"] == 1
    finally:
        db.close()
        engine.dispose()


def test_same_normalized_topic_title_never_crosses_partitions_without_an_explicit_topic_id():
    engine, db = _session()
    try:
        first = _ingestion(ingestion_id="ing-topic-a")
        second = _ingestion(ingestion_id="ing-topic-b")
        first.selection_metadata = {}
        second.selection_metadata = {}
        second.source_conversation_id = "another-conversation"
        second.event_id = "another-batch"
        db.add_all([first, second])
        db.commit()

        freeze_evaluation_snapshot(db)
        samples = latest_evaluation_samples(db)

        assert len(samples) == 2
        assert {sample.topic_group for sample in samples} == {"title:屏幕无显示"}
        assert len({sample.dataset_split for sample in samples}) == 1
    finally:
        db.close()
        engine.dispose()


def test_published_snapshot_uses_published_knowledge_not_candidate_draft():
    engine, db = _session()
    try:
        source = _ingestion()
        source.knowledge_id = "knowledge-1"
        source.candidate_payload["knowledge"]["recommended_reply"] = "候选草稿中的回复"
        db.add(Knowledge(
            id="knowledge-1",
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            title="已发布标题",
            content={
                "blocks": [{"type": "text", "value": "已发布正文"}],
                "recommended_reply": "已发布回复",
            },
            category_id="category-1",
            status=KnowledgeStatus.PUBLISHED,
            created_by="reviewer-1",
        ))
        db.add(source)
        db.commit()

        freeze_evaluation_snapshot(db)
        sample = latest_evaluation_samples(db)[0]

        assert sample.published_knowledge_snapshot["title"] == "已发布标题"
        assert sample.published_knowledge_snapshot["content"]["blocks"][0]["value"] == "已发布正文"
        assert "recommended_reply" not in sample.published_knowledge_snapshot["content"]
        assert sample.published_knowledge_snapshot["recommended_reply"] == "已发布回复"
        assert sample.draft_snapshot["recommended_reply"] == "候选草稿中的回复"
    finally:
        db.close()
        engine.dispose()


def test_freezing_published_samples_loads_knowledge_in_one_query():
    engine, db = _session()
    try:
        sources = []
        for index in range(4):
            source = _ingestion(ingestion_id=f"ing-{index}")
            source.knowledge_id = f"knowledge-{index}"
            sources.append(source)
            db.add(Knowledge(
                id=source.knowledge_id,
                knowledge_origin="business_accumulation",
                business_type="self_operated",
                title=f"已发布标题 {index}",
                content={
                    "blocks": [{"type": "text", "value": f"已发布正文 {index}"}],
                    "recommended_reply": f"已发布回复 {index}",
                },
                category_id="category-1",
                status=KnowledgeStatus.PUBLISHED,
                created_by="reviewer-1",
            ))
        db.add_all(sources)
        db.commit()
        source_rows = (
            db.query(IntegrationIngestion)
            .order_by(IntegrationIngestion.id.asc())
            .all()
        )

        knowledge_selects = []

        def capture_statement(_conn, _cursor, statement, _parameters, _context, _many):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and " from knowledge_items " in normalized:
                knowledge_selects.append(normalized)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            result = freeze_evaluation_snapshot(db, source_rows=source_rows)
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

        assert result["sample_count"] == 4
        assert len(knowledge_selects) == 1
    finally:
        db.close()
        engine.dispose()


def test_route_snapshot_rows_do_not_trigger_deferred_queries():
    engine, db = _session()
    try:
        db.add_all([_ingestion(ingestion_id=f"ing-{index}") for index in range(4)])
        db.commit()
        db.expunge_all()
        source_selects = []

        def capture_statement(_conn, _cursor, statement, _parameters, _context, _many):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and " from integration_ingestions " in normalized:
                source_selects.append(normalized)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            result = freeze_evaluation_snapshot(db, _rows(db, for_snapshot=True))
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

        assert result["sample_count"] == 4
        assert len(source_selects) == 1
        assert {sample.topic_group for sample in latest_evaluation_samples(db)} == {"topic-7"}
    finally:
        db.close()
        engine.dispose()


def test_evaluation_sample_rejects_orm_updates():
    engine, db = _session()
    try:
        source = _ingestion()
        db.add(source)
        db.commit()
        freeze_evaluation_snapshot(db)

        sample = db.query(EvaluationSample).first()
        sample.human_truth = {"knowledge_value": "unworthy"}
        with pytest.raises(ValueError, match="immutable"):
            db.commit()
    finally:
        db.close()
        engine.dispose()


def test_sqlite_migration_blocks_raw_updates_and_deletes():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20261012_02_add_evaluation_samples.py"
    )
    spec = importlib.util.spec_from_file_location(
        "evaluation_samples_migration",
        migration_path,
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()

        with Session(engine) as db:
            db.add(EvaluationSample(
                id="sample-1",
                snapshot_id="snapshot-1",
                candidate_id="candidate-1",
                source_batch="batch-1",
                topic_group="topic-1",
                draft_version="draft-1",
                draft_snapshot={},
                published_knowledge_snapshot={},
                draft_revision_history=[],
                model_output={},
                model_version="model-v1",
                prompt_version="prompt-v1",
                human_truth={"knowledge_value": "worthy"},
                dataset_split="train",
                sample_hash="hash-1",
                created_at=datetime(2026, 10, 1),
            ))
            db.commit()

        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE evaluation_samples SET model_version='model-v2' WHERE id='sample-1'")
                )

        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM evaluation_samples WHERE id='sample-1'")
                )
    finally:
        engine.dispose()
