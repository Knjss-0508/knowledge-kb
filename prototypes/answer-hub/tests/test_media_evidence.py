from __future__ import annotations

import pytest

from answer_hub.audit import AuditStore, media_review_decision
from answer_hub.workflow import _topic_case_videos, _topic_media_evidence


def _row() -> dict[str, str]:
    return {
        "来源记录ID": "SRC-1",
        "数据ID": "SRC-1",
        "_原子知识ID": "ATOM-1",
        "工单ID": "WO-1",
        "核心问题": "屏幕出现漏液如何判断",
        "对象/部位": "屏幕",
        "异常现象": "漏液",
        "图片链接": "https://cdn.example.com/screen.jpg",
        "视频链接": "https://cdn.example.com/screen.mp4",
        "图片处理状态": "可用:1",
        "视频处理状态": "存在视频，当前未解析视频内容（1个）",
        "语义标注状态": "topic_signal_labeled",
        "语义标注图片必要性": "需要",
    }


def test_media_evidence_binds_source_atomic_topic_and_hides_unparsed_video() -> None:
    items = _topic_media_evidence([_row()], "TOP-1")

    image = next(item for item in items if item["媒体类型"] == "image")
    video = next(item for item in items if item["媒体类型"] == "video")

    assert image["来源记录ID"] == "SRC-1"
    assert image["原子问题ID"] == "ATOM-1"
    assert image["主题ID"] == "TOP-1"
    assert image["相关性状态"] == "relevant"
    assert image["审核状态"] == "model_confirmed"
    assert image["审计风险"] == ""

    assert video["相关性状态"] == "unconfirmed"
    assert video["审核状态"] == "pending"
    assert "未解析视频" in video["审计风险"]


def test_media_evidence_id_is_scoped_to_topic() -> None:
    first = _topic_media_evidence([_row()], "TOP-1")
    second = _topic_media_evidence([_row()], "TOP-2")

    assert {item["媒体证据ID"] for item in first}.isdisjoint(
        {item["媒体证据ID"] for item in second}
    )


def test_human_confirmed_unparsed_video_stays_hidden() -> None:
    row = {
        **_row(),
        "video相关性状态": "人工确认相关",
    }

    assert _topic_case_videos([row]) == []


@pytest.mark.parametrize(
    (
        "decision",
        "parse_status",
        "media_type",
        "existing_risk",
        "expected",
    ),
    [
        (
            "关联当前主题",
            "已解析",
            "image",
            "无法读取",
            ("relevant", "human_confirmed", ""),
        ),
        (
            "关联当前主题",
            "未解析",
            "video",
            "",
            ("relevant", "human_confirmed", "未解析视频"),
        ),
        (
            "不相关",
            "已解析",
            "image",
            "",
            ("irrelevant", "human_confirmed", "不相关"),
        ),
        (
            "无法判断",
            "未解析",
            "image",
            "",
            ("unconfirmed", "human_pending", "无法判断"),
        ),
    ],
)
def test_media_review_decision_matrix(
    decision: str,
    parse_status: str,
    media_type: str,
    existing_risk: str,
    expected: tuple[str, str, str],
) -> None:
    result = media_review_decision(
        decision,
        parse_status=parse_status,
        media_type=media_type,
        existing_risk=existing_risk,
    )

    assert (
        result["relevance_status"],
        result["review_status"],
        result["audit_risk"],
    ) == expected


def test_media_audit_supports_read_only_risk_listing_and_three_way_review(tmp_path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    item = {
        "媒体证据ID": "ME-1",
        "媒体类型": "video",
        "媒体链接": "https://cdn.example.com/screen.mp4",
        "来源记录ID": "SRC-1",
        "原子问题ID": "ATOM-1",
        "主题ID": "TOP-1",
        "事实ID": "F01",
        "工单ID": "WO-1",
        "相关性状态": "unconfirmed",
        "可见事实": "屏幕漏液",
        "选择原因": "来源媒体待审计",
        "审核状态": "pending",
        "解析状态": "未解析",
        "审计风险": "未解析视频",
    }
    store.save_media_evidence("RUN-1", [item])

    assert store.list_media_evidence(risk_only=True)[0]["media_evidence_id"] == "ME-1"
    reviewed = store.review_media_evidence("ME-1", "不相关", "reviewer")

    assert reviewed["relevance_status"] == "irrelevant"
    assert reviewed["review_status"] == "human_confirmed"
    assert reviewed["audit_risk"] == "不相关"

    rerun_item = {
        **item,
        "主题ID": "TOP-1",
        "相关性状态": "relevant",
        "审核状态": "model_confirmed",
        "审计风险": "",
    }
    store.save_media_evidence("RUN-2", [rerun_item])
    preserved = store.list_media_evidence(topic_id="TOP-1")[0]

    assert preserved["relevance_status"] == "irrelevant"
    assert preserved["review_status"] == "human_confirmed"
    assert preserved["audit_risk"] == "不相关"
