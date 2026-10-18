from __future__ import annotations

import importlib.util
from pathlib import Path

from openpyxl import Workbook


def test_offline_contract_executes_transcription_review_and_candidate_export(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "offline_standardization_replay.py"
    spec = importlib.util.spec_from_file_location("offline_standardization_replay", script)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    source = tmp_path / "fixture.xlsx"
    book = Workbook()
    book.active.append([
        "数据ID", "工单ID", "产品类型", "回收业务层级",
        "核心问题", "聊天内容", "判定结论", "参考话术",
        "一级分类", "二级分类", "人工标题",
    ])
    for index in range(2):
        book.active.append([
            f"fixture-{index}", f"fixture-{index}", "手机", "自营回收",
            "手机机型如何查询",
            "回收师：手机机型如何查询？答疑：请打开设置中的关于本机查看型号。",
            "手机机型可在设置中的关于本机查看。",
            "请打开设置中的关于本机查看型号。",
            "信息查询", "机型查询", "不得输入模型的人工标题",
        ])
    book.save(source)
    report = replay.run_replay(source, tmp_path / "output")
    calls = report["model_fixture_calls"]
    assert report["input_count"] == 2
    assert report["admitted_topics"] == 1
    assert calls["你是严谨的主题环节与知识沉淀价值标注员。"] > 0
    assert calls["你是严谨的多品类主题知识标注助手。"] > 0
    assert calls["你是严谨的多品类主题知识初审员。"] > 0
    assert report["transcription_statuses"] == {"topic_model_labeled": 1}
    assert report["reply_sources"] == {"deepseek_transcription": 1}
    assert report["review_decisions"] == {"需修改": 1}
    assert report["reply_nonempty"] == report["topic_count"] == 1
    assert report["candidate_artifact_exists"]
    assert report["formal_candidate_count"] == 0
    assert report["transcription_blocked_count"] == report["review_skipped_count"] == 0
    assert report["standardization_ready"] == 2
    assert report["atomic_failures"] == report["cluster_failures"] == 0


def test_offline_contract_records_standard_reference_block_without_bypassing_gate(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "offline_standardization_replay.py"
    spec = importlib.util.spec_from_file_location("offline_standardization_replay", script)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    source = tmp_path / "fixture.xlsx"
    book = Workbook()
    book.active.append([
        "数据ID", "工单ID", "产品类型", "回收业务层级", "核心问题",
        "聊天内容", "判定结论", "一级分类", "二级分类",
    ])
    for index in range(2):
        book.active.append([
            f"fixture-{index}", f"fixture-{index}", "手机", "自营回收", "手机机型如何查询",
            "回收师：手机机型如何查询？答疑：按 STD-FAKE 查看关于本机的型号。",
            "按 STD-FAKE 查看关于本机的型号。",
            "信息查询", "机型查询",
        ])
    book.save(source)
    report = replay.run_replay(source, tmp_path / "output")
    assert report["no_standard_reference_blocked_count"] == 1
    assert report["transcription_validation_blocked_count"] == 1
    assert report["transcription_schema_failures"] == 0
    assert report["review_skipped_count"] == 1
    assert report["formal_candidate_count"] == 0
