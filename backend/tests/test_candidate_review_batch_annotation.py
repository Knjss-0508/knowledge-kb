from types import SimpleNamespace
from unittest.mock import MagicMock

from app.routes.integration import _candidate_content, annotate_candidate_reviews
from app.schemas.integration import CandidateReviewBatchAnnotate


def test_batch_annotation_marks_pending_candidate_unworthy() -> None:
    item = SimpleNamespace(
        id="ing-1",
        source_system="answer-hub",
        review_status="pending",
        status="candidate_pending",
        knowledge_id=None,
        candidate_payload={
            "knowledge": {"title": "模型失败后拆出的单条候选"},
            "selection": {"eligible": False},
            "model_review": {"knowledge_value": "pending"},
            "human_review": {},
        },
        selection_metadata={},
        review_metadata={},
        reviewed_by=None,
        reviewed_at=None,
        error_code="MODEL_FAILED",
        error_message="模型调用失败",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = item

    response = annotate_candidate_reviews(
        CandidateReviewBatchAnnotate(
            ingestion_ids=["ing-1"],
            knowledge_value="unworthy",
            include_in_training=True,
        ),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert response.updated == 1
    assert response.failed == 0
    assert item.review_status == "rejected"
    assert item.status == "candidate_rejected"
    assert item.candidate_payload["human_review"]["knowledge_value"] == "unworthy"
    assert item.candidate_payload["human_review"]["training_eligible"] == "是"
    assert item.error_code is None
    assert item.error_message is None
    db.commit.assert_called_once()


def test_batch_annotation_refuses_submitted_candidate() -> None:
    item = SimpleNamespace(
        id="ing-submitted",
        source_system="answer-hub",
        review_status="submitted",
        knowledge_id="K-001",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = item

    response = annotate_candidate_reviews(
        CandidateReviewBatchAnnotate(
            ingestion_ids=["ing-submitted"],
            knowledge_value="unworthy",
        ),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert response.updated == 0
    assert response.failed == 1
    assert response.results[0].error_code == "CANDIDATE_LOCKED"
    db.commit.assert_called_once()


def test_candidate_submission_content_preserves_case_media_blocks() -> None:
    content = _candidate_content(
        {
            "content": {
                "blocks": [
                    {"type": "text", "value": "核对案例中的实际现象。"},
                    {
                        "type": "image",
                        "external_url": "https://cdn.example.com/case.jpg",
                        "alt": "案例图片",
                        "caption": "来源案例图",
                    },
                    {
                        "type": "video",
                        "external_url": "https://cdn.example.com/case.mp4",
                        "alt": "案例视频",
                        "caption": "来源案例视频",
                    },
                ]
            },
            "recommended_reply": "请按案例步骤核对。",
        }
    )

    assert [block["type"] for block in content["blocks"]] == [
        "text",
        "image",
        "video",
    ]
    assert content["blocks"][1]["external_url"].endswith("case.jpg")
    assert content["blocks"][2]["external_url"].endswith("case.mp4")
    assert content["recommended_reply"] == "请按案例步骤核对。"
