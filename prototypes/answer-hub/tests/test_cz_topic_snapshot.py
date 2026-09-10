from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

from answer_hub.cz_topic_snapshot import CZTopicSnapshot, boundary_hash


def _topic(topic_id: str, *, status: str = "published") -> dict[str, str]:
    return {
        "cz_topic_id": topic_id,
        "status": status,
        "title": "手机外壳碎裂判定",
        "business_line": "自营回收",
        "product_category": "手机",
        "standard_family": "外观",
        "merge_policy": "same_standard_family",
        "object_key": "手机外壳",
        "phenomenon_value": "碎裂",
        "query_target": "",
        "detection_target": "外观判定",
        "platform": "通用",
        "brand": "通用",
        "model_scope": "通用",
        "threshold_values": "",
    }


def test_snapshot_keeps_only_published_metadata_and_filters_scope(tmp_path):
    snapshot = CZTopicSnapshot(tmp_path / "cz-topics.db", ttl_seconds=3600)
    result = snapshot.sync(
        [_topic("CZ-1"), _topic("CZ-2", status="review_pending")],
        snapshot_version="v1",
        synced_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    assert result.upserted == 1
    assert result.skipped == 1
    rows = snapshot.list_published(
        business_line="自营回收", product_category="手机",
        now=datetime(2026, 9, 3, 0, 30, tzinfo=timezone.utc),
    )
    assert [row["cz_topic_id"] for row in rows] == ["CZ-1"]
    assert rows[0]["boundary_hash"] == boundary_hash(_topic("CZ-1"))


def test_snapshot_ttl_stops_stale_auto_reuse(tmp_path):
    snapshot = CZTopicSnapshot(tmp_path / "cz-topics.db", ttl_seconds=60)
    synced = datetime(2026, 9, 3, tzinfo=timezone.utc)
    snapshot.sync([_topic("CZ-1")], snapshot_version="v1", synced_at=synced)
    assert snapshot.is_fresh(synced + timedelta(seconds=30))
    assert not snapshot.is_fresh(synced + timedelta(seconds=61))
    assert snapshot.list_published(now=synced + timedelta(seconds=61)) == []


def test_snapshot_full_sync_retires_topics_missing_from_new_version(tmp_path):
    snapshot = CZTopicSnapshot(tmp_path / "cz-topics.db", ttl_seconds=3600)
    synced = datetime(2026, 9, 3, tzinfo=timezone.utc)
    snapshot.sync([_topic("CZ-1"), _topic("CZ-2")], snapshot_version="v1", synced_at=synced)
    result = snapshot.sync([_topic("CZ-1")], snapshot_version="v2", synced_at=synced)
    assert result.retired == 1
    assert [row["cz_topic_id"] for row in snapshot.list_published(now=synced)] == ["CZ-1"]


def test_snapshot_incremental_sync_uses_source_updated_at(tmp_path):
    snapshot = CZTopicSnapshot(tmp_path / "cz-topics.db", ttl_seconds=3600)
    synced = datetime(2026, 9, 3, tzinfo=timezone.utc)
    old = _topic("CZ-OLD")
    old["updated_at"] = "2026-09-01T00:00:00+00:00"
    new = _topic("CZ-NEW")
    new["updated_at"] = "2026-09-03T00:00:00+00:00"
    result = snapshot.sync(
        [old, new], snapshot_version="v2", synced_at=synced,
        updated_since="2026-09-02T00:00:00+00:00",
    )
    assert result.upserted == 1
    assert [row["cz_topic_id"] for row in snapshot.list_published(now=synced)] == ["CZ-NEW"]


def test_snapshot_imports_local_json_envelope_and_preserves_review_status(tmp_path):
    source = tmp_path / "local-cz-topic-snapshot.json"
    review = _topic("CZ-REVIEW", status="review")
    published = _topic("CZ-PUBLISHED")
    source.write_text(
        json.dumps(
            {
                "snapshot_version": "local-cz-20260903-001",
                "generated_at": "2026-09-03T10:00:00+08:00",
                "items": [published, review],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = CZTopicSnapshot(tmp_path / "cz-topics.db", ttl_seconds=3600)
    result = snapshot.sync_from_file(
        source,
        synced_at=datetime(2026, 9, 3, 2, tzinfo=timezone.utc),
    )

    assert result.snapshot_version == "local-cz-20260903-001"
    assert result.upserted == 2
    published_rows = snapshot.list_topics(
        business_line="自营回收",
        product_category="手机",
        statuses={"published"},
        now=datetime(2026, 9, 3, 2, 30, tzinfo=timezone.utc),
    )
    review_rows = snapshot.list_topics(
        statuses={"review"},
        now=datetime(2026, 9, 3, 2, 30, tzinfo=timezone.utc),
    )
    assert [row["cz_topic_id"] for row in published_rows] == ["CZ-PUBLISHED"]
    assert [row["cz_topic_id"] for row in review_rows] == ["CZ-REVIEW"]


def test_snapshot_rejects_local_file_without_version_or_items(tmp_path):
    source = tmp_path / "invalid-cz-topic-snapshot.json"
    source.write_text("{}", encoding="utf-8")
    snapshot = CZTopicSnapshot(tmp_path / "cz-topics.db")

    try:
        snapshot.sync_from_file(source)
    except ValueError as exc:
        assert "snapshot_version" in str(exc)
    else:
        raise AssertionError("缺少快照版本的本地文件必须被拒绝")
