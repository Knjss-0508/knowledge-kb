from __future__ import annotations

from scripts.build_full_cluster_units import (
    _build_units,
    _build_units_and_exclusions,
)


def test_build_units_preserves_source_and_fusion_fields() -> None:
    rows = [
        {
            "样本ID": "S001",
            "源记录键": "R1",
            "工单ID": "W1",
            "机型": "测试机型",
            "核心问题": "屏幕异常如何判定",
            "聊天内容": "屏幕有一条亮线",
            "图片链接": "image",
            "视频链接": "",
            "产品类型": "手机",
        }
    ]
    fusion_results = {
        "S001": {
            "status": "ok",
            "candidate": {
                "conversation_type": "single_topic",
                "reason": "单主题",
                "media_analysis": {
                    "image_summary": "屏幕存在亮线",
                    "video_summary": "无视频",
                    "media_relevance": "相关",
                    "used_for_topic_split": False,
                    "requires_review": False,
                },
                "topics": [
                    {
                        "normalized_issue": "手机｜屏幕｜亮线｜判断显示异常",
                        "product_category": "手机",
                        "scope_type": "品类专用",
                        "platform": "通用",
                        "brand": "通用",
                        "model_scope": "通用",
                        "category_l1": "显示问题",
                        "category_l2": "亮线",
                        "intent": "标准判定",
                        "subject": "屏幕",
                        "phenomenon": "亮线",
                        "judgment_target": "判断是否属于显示异常",
                        "resolution_mode": "按照显示异常标准判定",
                        "standard_path": "显示异常判定",
                        "threshold_or_exception": "无明确阈值",
                        "evidence_summary": "聊天和图片均显示亮线",
                        "confidence": 0.9,
                        "requires_review": False,
                    }
                ],
            },
        }
    }

    units = _build_units(rows, fusion_results)

    assert len(units) == 1
    assert units[0]["unit_id"] == "S001-01"
    assert units[0]["source_record_key"] == "R1"
    assert units[0]["normalized_issue"].startswith("手机")
    assert "显示异常判定" in units[0]["semantic_text"]


def test_build_units_excludes_missing_conversation_with_irrelevant_media() -> None:
    rows = [
        {
            "样本ID": "S306",
            "源记录键": "F306",
            "工单ID": "2077230486270251536",
            "机型": "待确认",
            "核心问题": "无法基于现有信息作出判定。",
            "判定结论": "无法基于现有信息作出判定。",
            "判定依据": "输入信息中仅包含工单元数据，缺失会话记录。",
            "产品类型": "笔记本",
        }
    ]
    fusion_results = {
        "S306": {
            "status": "ok",
            "candidate": {
                "conversation_type": "uncertain",
                "reason": "仅包含工单元数据，无法提取具体问题。",
                "media_analysis": {
                    "media_relevance": "不相关",
                    "requires_review": True,
                },
                "topics": [
                    {
                        "normalized_issue": "无法提取具体问题，信息缺失",
                        "product_category": "笔记本",
                        "scope_type": "待确认",
                        "category_l1": "其他待确认",
                        "intent": "其他待确认",
                        "subject": "待确认",
                        "phenomenon": "待确认",
                        "judgment_target": "待确认",
                        "resolution_mode": "待确认",
                        "standard_path": "待确认",
                        "confidence": 0.0,
                    }
                ],
            },
        }
    }

    units, excluded_rows = _build_units_and_exclusions(
        rows,
        fusion_results,
    )

    assert units == []
    assert excluded_rows[0]["source_record_key"] == "F306"
    assert "缺少有效答疑会话" in excluded_rows[0]["exclusion_reason"]
