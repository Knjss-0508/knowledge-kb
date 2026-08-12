import json

from openpyxl import Workbook

from scripts import replay_case4_cluster_cache as replay_script
from scripts.replay_case4_cluster_cache import (
    _resolve_atomic_cache_entry,
)


def test_resolve_atomic_cache_entry_accepts_legacy_key_without_business_line() -> None:
    cached_result = {
        "topics": [{"normalized_issue": "手机｜屏幕｜色斑｜判定"}],
        "failed": False,
    }
    atomic_results = {
        "7e10ebbc97d89259fe15d4f92ea91908047cf087b6ad3351b1030a09db58644a": (
            cached_result
        )
    }

    result, cache_key, cache_mode = _resolve_atomic_cache_entry(
        1,
        {
            "工单ID": "WO-001",
            "回收单号": "RO-001",
            "回收业务层级": "自营回收",
            "产品类型": "手机",
            "聊天内容": "屏幕色斑怎么判",
            "图片链接": "",
            "视频链接": "",
        },
        atomic_results,
    )

    assert result == cached_result
    assert (
        cache_key
        == "7e10ebbc97d89259fe15d4f92ea91908047cf087b6ad3351b1030a09db58644a"
    )
    assert cache_mode == "legacy_without_business_line"


def test_resolve_atomic_cache_entry_reports_missing_cache() -> None:
    result, cache_key, cache_mode = _resolve_atomic_cache_entry(
        1,
        {
            "工单ID": "WO-001",
            "回收业务层级": "自营回收",
            "产品类型": "手机",
            "聊天内容": "屏幕色斑怎么判",
        },
        {},
    )

    assert result is None
    assert cache_key == ""
    assert cache_mode == "missing"


def test_replay_injects_human_corrected_evidence_into_atomic_units(
    tmp_path,
    monkeypatch,
) -> None:
    source_path = tmp_path / "source.xlsx"
    source_workbook = Workbook()
    source_sheet = source_workbook.active
    source_sheet.append(
        [
            "工单ID",
            "聊天内容",
            "核心问题",
            "原始核心问题",
            "判定结论",
            "原始判定结论",
            "回收业务层级",
            "产品类型",
        ]
    )
    source_sheet.append(
        [
            "WO-REPLAY-001",
            "请结合现场图片确认屏幕上的点",
            "人工确认这是屏幕色斑还是灰尘",
            "旧模型认为是屏幕坏点",
            "人工看图后确认是屏幕色斑",
            "旧模型判断为屏幕坏点",
            "自营回收",
            "手机",
        ]
    )
    source_workbook.save(source_path)

    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "atomic_results": {},
                "cluster_results": {},
                "atomic_signature": {"model": "offline-test"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    model_path = tmp_path / "model.xlsx"
    model_workbook = Workbook()
    model_sheet = model_workbook.active
    model_sheet.title = "聚类结果"
    model_sheet.append(
        ["聚类主题ID", "产品类型", "主题来源记录ID"]
    )
    model_sheet.append(["TOP-001", "手机", "WO-REPLAY-001"])
    model_workbook.save(model_path)

    cached = {
        "topics": [
            {
                "normalized_issue": "手机｜屏幕｜色斑｜判定",
                "product_category": "手机",
                "subject": "屏幕",
                "phenomenon": "色斑",
                "judgment_target": "确认显示异常类型",
                "confidence": 0.95,
            }
        ],
        "failed": False,
        "rescue_reason": "",
    }
    monkeypatch.setattr(
        replay_script,
        "_resolve_atomic_cache_entry",
        lambda *_args, **_kwargs: (cached, "cache-key", "current"),
    )

    captured_units = []

    def capture_rule_match(unit):
        if unit.get("unit_id"):
            captured_units.append(dict(unit))
        return None

    monkeypatch.setattr(
        replay_script.workflow,
        "_direct_clustering_rule_match",
        capture_rule_match,
    )

    summary = replay_script.replay(
        source_path,
        cache_path,
        model_path,
        tmp_path / "output",
    )

    assert summary["real_model_calls"] == 0
    assert captured_units
    assert captured_units[0]["source_core_problem"] == (
        "人工确认这是屏幕色斑还是灰尘"
    )
    assert captured_units[0]["source_judgment_conclusion"] == (
        "人工看图后确认是屏幕色斑"
    )
    assert captured_units[0]["business_line"] == "自营回收"
