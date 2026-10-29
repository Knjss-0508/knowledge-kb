"""摘要卡片必须统计「全部候选」，不能随状态筛选缩小。"""

from unittest.mock import MagicMock, patch

from app.routes import integration


def _call_list(monkeypatch, review_status: str, rows):
    monkeypatch.setattr(
        integration,
        "_candidate_review_light_rows",
        lambda _db, **kwargs: rows,
    )
    monkeypatch.setattr(
        integration, "_candidate_review_full_items", lambda _db, ids: []
    )
    return integration.list_candidate_reviews(
        keyword="",
        review_status=review_status,
        priority_only=False,
        deduplication_required=False,
        product_category="",
        annotation_status="",
        model_knowledge_value="",
        updated_from=None,
        updated_to=None,
        limit=100,
        offset=0,
        db=MagicMock(),
        _=None,
    )


def test_summary_uses_sql_status_counts_when_filtering(monkeypatch) -> None:
    """带状态筛选时，摘要仍来自全量计数而不是筛选后的行。"""

    monkeypatch.setattr(
        integration,
        "_candidate_review_status_counts",
        lambda _db: {
            "total": 100,
            "pending": 60,
            "ready": 5,
            "rejected": 20,
            "submitted": 10,
            "pending_model_annotation": 3,
            "draft_incomplete": 1,
            "hold_for_evidence": 0,
            "discard_review": 0,
            "revision_required": 1,
            "failed": 0,
        },
    )

    response = _call_list(monkeypatch, "ready", [])

    assert response.summary["total"] == 100
    assert response.summary["human_pending"] == 60
    assert response.summary["ready"] == 5
    assert response.total == 0


def test_summary_falls_back_to_rows_for_stubbed_sessions(monkeypatch) -> None:
    """单测替身没有真实数据库时，退回按行统计，语义保持一致。"""

    monkeypatch.setattr(
        integration, "_candidate_review_status_counts", lambda _db: {}
    )
    rows = [MagicMock(), MagicMock()]
    monkeypatch.setattr(
        integration,
        "_candidate_review_light_item",
        lambda row: row,
    )
    for row in rows:
        row.review_status = "pending"
        row.priority_review = False
        row.deduplication = None
        row.deduplication_confirmed = False
        row.applicable_categories = []

    response = _call_list(monkeypatch, "", rows)

    assert response.summary["total"] == 2
    assert response.summary["human_pending"] == 2
