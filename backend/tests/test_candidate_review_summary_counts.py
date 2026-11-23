"""摘要卡片必须统计「全部候选」，不能随状态筛选缩小。"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from weakref import ref

from app.routes import integration


def _call_list(monkeypatch, review_status: str, rows, *, page_ids=None, **filters):
    def load_page(_db, ids):
        if page_ids is not None:
            page_ids.extend(ids)
        return []

    monkeypatch.setattr(
        integration,
        "_candidate_review_light_rows",
        lambda _db, **kwargs: rows,
    )
    monkeypatch.setattr(integration, "_candidate_review_full_items", load_page)
    params = dict(
        keyword="", priority_only=False, deduplication_required=False,
        product_category="", annotation_status="", model_knowledge_value="",
        updated_from=None, updated_to=None, limit=100, offset=0,
    )
    params.update(filters)
    return integration.list_candidate_reviews(
        review_status=review_status, db=MagicMock(), _=None, **params,
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


def test_empty_queue_has_zero_summary_counts(monkeypatch) -> None:

    monkeypatch.setattr(
        integration, "_candidate_review_status_counts", lambda _db: {}
    )
    response = _call_list(monkeypatch, "", [])

    assert response.summary["total"] == 0
    assert response.summary["human_pending"] == 0


def test_all_status_scan_processes_each_row_before_reading_the_next(monkeypatch):
    class Row(SimpleNamespace):
        pass

    monkeypatch.setattr(
        integration, "_candidate_review_status_counts", lambda _db: {"total": 5, "pending": 5}
    )

    def rows():
        for index in range(5):
            row = Row(
                id=str(index),
                review_status="pending",
                priority_review=index != 1,
                deduplication=None,
                applicable_categories=["手机"] if index == 4 else [],
            )
            yield row
            assert parsed[-1]() is row
            # 前面的条目应可释放，即便已命中当前页。
            if index >= 2:
                assert parsed[index - 2]() is None

    parsed = []

    def parse(row):
        parsed.append(ref(row))
        return row

    monkeypatch.setattr(integration, "_candidate_review_light_item", parse)
    page_ids = []
    response = _call_list(monkeypatch, "", rows(), limit=2, offset=1, page_ids=page_ids)

    assert response.total == 5
    assert page_ids == ["1", "2"]
    assert response.summary["total"] == 5
    assert response.summary["model_priority"] == 4
    assert response.product_categories == [{"value": "手机", "label": "手机"}]


def test_streaming_keeps_summary_and_categories_before_filters(monkeypatch):
    defaults = dict(
        review_status="pending", title="iphone", event_id="event",
        source_conversation_id="conv", evidence_excerpt="",
        applicable_categories=[{"name": "手机", "id": "Mobile"}],
        updated_at=datetime(2026, 11, 23), priority_review=True,
        deduplication=SimpleNamespace(action="review_duplicate"),
        deduplication_confirmed=False, human_review={"decision": "approved"},
        model_review={"knowledge_value": "worthy"},
    )
    overrides = [
        {"title": "another"},
        {"applicable_categories": ["相机"]},
        {"priority_review": False},
        {"deduplication_confirmed": True},
        {"human_review": {}},
        {"model_review": {"knowledge_value": "pending"}},
        {"updated_at": datetime(2026, 11, 22)},
        {"updated_at": datetime(2026, 11, 24)},
        {},
        {},
    ]
    rows = (
        SimpleNamespace(id=str(index), **(defaults | override))
        for index, override in enumerate(overrides)
    )
    monkeypatch.setattr(integration, "_candidate_review_light_item", lambda row: row)
    monkeypatch.setattr(
        integration, "_candidate_review_status_counts", lambda _db: {"total": 10, "pending": 10}
    )
    page_ids = []
    response = _call_list(
        monkeypatch, "", rows, keyword="IPHONE", product_category="mobile",
        priority_only=True, deduplication_required=True,
        annotation_status="annotated", model_knowledge_value="worthy",
        updated_from=date(2026, 11, 23), updated_to=date(2026, 11, 23),
        limit=1, offset=1, page_ids=page_ids,
    )

    assert response.total == 2
    assert page_ids == ["9"]
    assert response.summary["total"] == 10
    assert response.summary["priority"] == response.summary["deduplication_required"] == 9
    assert {category["value"] for category in response.product_categories} == {"手机", "相机"}
