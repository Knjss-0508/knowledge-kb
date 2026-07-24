from __future__ import annotations

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from answer_hub.cluster_annotation import (
    ClusterAnnotationStore,
    annotation_csv_bytes,
    annotation_export_rows,
    annotation_summary,
    annotation_validation_errors,
    load_cluster_payload,
    split_media_urls,
)


def _payload() -> dict[str, object]:
    return {
        "metadata": {"status": "complete"},
        "atomic_units": [
            {
                "unit_id": "S001-01",
                "sample_id": "S001",
                "normalized_issue": "屏幕亮线怎么判",
                "source_core_problem": "屏幕出现亮线",
                "source_conversation": "老师，这个屏幕有一条亮线怎么判？",
                "product_category": "手机",
                "requires_review": False,
            },
            {
                "unit_id": "S002-01",
                "sample_id": "S002",
                "normalized_issue": "屏幕亮线是否异常",
                "source_core_problem": "屏幕亮线是否异常",
                "source_conversation": "屏幕中间有亮线，算异常吗？",
                "product_category": "手机",
                "requires_review": False,
            },
        ],
        "clusters": [
            {
                "cluster_id": "T001",
                "theme_title": "屏幕亮线怎么判？",
                "member_atomic_ids": ["S001-01", "S002-01"],
                "product_category": "手机",
                "shared_knowledge_definition": "判断屏幕亮线是否属于显示异常。",
            }
        ],
    }


def test_load_cluster_payload_attaches_members(tmp_path: Path) -> None:
    path = tmp_path / "clusters.json"
    path.write_text(
        json.dumps(_payload(), ensure_ascii=False),
        encoding="utf-8",
    )

    payload = load_cluster_payload(path)

    assert payload["clusters"][0]["member_count"] == 2
    assert payload["clusters"][0]["members"][0]["unit_id"] == "S001-01"
    assert payload["unclustered_atomic_ids"] == []


def test_annotation_store_persists_and_summarizes(tmp_path: Path) -> None:
    store = ClusterAnnotationStore(tmp_path / "annotations.db")
    saved = store.save(
        cluster_id="T001",
        cluster_decision="正确",
        title_decision="错误",
        action="保留",
        outlier_atomic_ids=[],
        gold_topic_id="GOLD-001",
        gold_title="手机屏幕亮线判定",
        target_cluster_id="",
        notes="归簇正确，标题需要更具体。",
        reviewer="测试员",
    )

    assert saved.cluster_decision == "正确"
    assert store.get("T001").gold_topic_id == "GOLD-001"

    payload = _payload()
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    clusters = load_cluster_payload(payload_path)["clusters"]
    annotations = store.list_all()
    summary = annotation_summary(clusters, annotations)

    assert summary["completed_count"] == 1
    assert summary["cluster_accuracy"] == 1.0
    assert summary["title_accuracy"] == 0.0

    rows = annotation_export_rows(clusters, annotations)
    assert rows[0]["人工主题ID"] == "GOLD-001"
    assert annotation_csv_bytes(rows).startswith(b"\xef\xbb\xbf")


def test_split_media_urls_deduplicates_links() -> None:
    assert split_media_urls(
        "https://example.com/a.jpg\nhttps://example.com/a.jpg, https://example.com/b.mp4"
    ) == [
        "https://example.com/a.jpg",
        "https://example.com/b.mp4",
    ]


def test_annotation_validation_requires_correction_details() -> None:
    errors = annotation_validation_errors(
        cluster_decision="错误",
        title_decision="错误",
        action="合并",
        member_count=1,
        outlier_atomic_ids=[],
        gold_title="",
        target_cluster_id="",
    )

    assert "选择“合并”时，请选择目标主题簇。" in errors
    assert "标题判断为“错误”时，请填写正确的人工主题标题。" in errors


def test_annotation_validation_requires_split_members() -> None:
    errors = annotation_validation_errors(
        cluster_decision="错误",
        title_decision="正确",
        action="拆分",
        member_count=2,
        outlier_atomic_ids=[],
        gold_title="",
        target_cluster_id="",
    )

    assert errors == ["选择“拆分”时，请勾选需要移出或单独拆分的成员。"]


def test_cluster_annotation_workspace_renders(
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "clusters.json"
    payload_path.write_text(
        json.dumps(_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    database_path = tmp_path / "annotations.db"
    project_root = Path.cwd()
    script = f"""
from pathlib import Path
import sys

ROOT = Path({str(project_root)!r})
sys.path.insert(0, str(ROOT / "src"))

import answer_hub.cluster_annotation_ui as ui

ui.DEFAULT_CLUSTER_PAYLOAD = Path({str(payload_path)!r})
ui.DEFAULT_ANNOTATION_DB = Path({str(database_path)!r})
ui.render_cluster_annotation()
"""

    app = AppTest.from_string(script).run(timeout=30)

    assert not app.exception
    assert any(item.value == "完整聚类标注" for item in app.subheader)
    assert any(
        item.label == "主题簇" and item.value == "1"
        for item in app.metric
    )
    assert any(
        item.label == "选择主题簇" and item.value == "T001"
        for item in app.selectbox
    )
