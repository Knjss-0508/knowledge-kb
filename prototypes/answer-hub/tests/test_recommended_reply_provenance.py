from __future__ import annotations

from answer_hub.workflow import _select_topic_recommended_reply


TITLE = "耳机查找功能异常是否影响回收？"
CONTENT = "1. 确认查找功能确实异常。\n2. 查找功能异常时可以正常回收。"


def test_verified_single_fact_reply_can_repeat_the_complete_knowledge_sentence():
    sentence = "2019款16寸MacBook Pro的耳机孔为3.5mm规格。"
    result = _select_topic_recommended_reply(
        [{
            "数据ID": "SPEC", "_人工答复门禁状态": "answered",
            "_原子实际回复": "3.5的", "产品类型": "笔记本",
            "对象/部位": "耳机孔", "异常现象": "接口规格查询",
        }],
        title="2019款16寸MacBook Pro的耳机孔是什么规格",
        content="1. " + sentence, model_reply="3.5的",
        evidence_status="available", use_standard_references=False,
    )
    assert result["reply"] == sentence
    assert result["model_rewritten"] is True
    assert "3.5的" in result["original"]


def _select(rows, model_reply: str = ""):
    return _select_topic_recommended_reply(
        rows,
        title=TITLE,
        content=CONTENT,
        model_reply=model_reply,
        evidence_status="available",
        use_standard_references=False,
    )


def test_second_part_reference_reply_has_first_priority_when_topic_aligned() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-1",
                "参考话术": "您好，查找功能异常时可以正常回收。",
                "答疑回复": "您好，请先重启后再确认。",
            }
        ]
    )

    assert result["source_type"] == "second_part_reference_reply"
    assert result["source_record_id"] == "SRC-1"
    assert result["reply"] == "您好，查找功能异常时可以正常回收。"
    assert result["model_rewritten"] is False


def test_more_complete_reference_reply_can_enrich_a_short_human_answer() -> None:
    result = _select_topic_recommended_reply(
        [
            {
                "来源记录ID": "SRC-ENRICH",
                "_人工答复门禁状态": "answered",
                "_原子实际回复": "多次恢复后仍有提示就不收。",
                "参考话术": (
                    "这个情况不能收：本地退ID不等于云端解绑，页面还提示“已为拥有者锁定iPhone”，"
                    "说明激活锁未关闭。若抹除恢复后仍出现该页面，按不回收处理。"
                ),
                "产品类型": "手机",
                "对象/部位": "账号状态",
                "异常现象": "抹除恢复后仍提示已为拥有者锁定iPhone",
                "核心问题": "账号状态如何判定",
            }
        ],
        title="手机抹除恢复后仍提示已为拥有者锁定iPhone如何判定",
        content="1. 抹除恢复后仍提示已为拥有者锁定iPhone时，按不回收处理。",
        model_reply="",
        evidence_status="available",
        use_standard_references=True,
    )

    assert result["source_type"] == "second_part_reference_reply"
    assert result["source_record_id"] == "SRC-ENRICH"
    assert "激活锁未关闭" in result["reply"]
    assert "多次恢复后仍有提示就不收" not in result["reply"]


def test_unrelated_reference_reply_falls_back_to_topic_aligned_actual_reply() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-2",
                "参考话术": "您好，请核对手机屏幕是否漏液。",
                "答疑回复": "您好，查找功能异常时可以正常回收。",
            }
        ]
    )

    assert result["source_type"] == "work_order_actual_reply"
    assert result["source_record_id"] == "SRC-2"
    assert "正常回收" in result["reply"]


def test_conflicting_source_replies_require_review_and_do_not_choose_one_side() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-3",
                "参考话术": "您好，查找功能异常时可以正常回收。",
            },
            {
                "来源记录ID": "SRC-4",
                "参考话术": "您好，查找功能异常时不回收。",
            },
        ],
        model_reply="您好，查找功能异常时不回收。",
    )

    assert result["source_type"] == "human_review_required"
    assert result["source_record_ids"] == ["SRC-3", "SRC-4"]
    assert result["source_record_id"] == "SRC-3\nSRC-4"
    assert len(result["source_candidates"]) == 2
    assert "SRC-3" in result["original"]
    assert "SRC-4" in result["original"]
    assert result["conflict"] == "来源回答存在相反的回收资格结论"
    assert any("必须人工裁决" in issue for issue in result["issues"])
    assert result["reply"]


def test_invalid_model_reply_still_falls_back_to_nonempty_content_reply() -> None:
    result = _select(
        [],
        model_reply="您好，关于手机漏液：请查看屏幕图片",
    )

    assert result["source_type"] == "knowledge_content_generation"
    assert result["model_rewritten"] is True
    assert "正常回收" in result["reply"]


def test_single_source_reply_with_opposite_conclusion_is_not_reused() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-5",
                "参考话术": "您好，查找功能异常时不回收。",
            }
        ]
    )

    assert result["source_type"] == "human_review_required"
    assert result["source_record_id"] == "SRC-5"
    assert result["source_record_ids"] == ["SRC-5"]
    assert "SRC-5" in result["original"]
    assert any("必须人工裁决" in issue for issue in result["issues"])
    assert "正常回收" in result["reply"]


def test_structured_topic_anchor_rejects_other_issue_from_multi_question_record() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-6",
                "产品类型": "耳机",
                "对象/部位": "查找功能",
                "异常现象": "功能异常",
                "参考话术": "您好，耳机包装盒内缺少充电线时请补齐配件。",
                "答疑回复": "您好，查找功能异常时可以正常回收。",
            }
        ]
    )

    assert result["source_type"] == "work_order_actual_reply"
    assert result["source_record_id"] == "SRC-6"
    assert "查找功能" in result["reply"]


def test_product_mismatch_cannot_reuse_reply_even_when_issue_words_match() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-7",
                "产品类型": "手机",
                "对象/部位": "查找功能",
                "异常现象": "功能异常",
                "参考话术": "您好，查找功能异常时可以正常回收。",
            }
        ]
    )

    assert result["source_type"] == "knowledge_content_generation"
    assert result["source_record_id"] == ""


def test_each_reply_must_match_its_own_atomic_source_row() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-WRONG",
                "产品类型": "耳机",
                "对象/部位": "包装配件",
                "异常现象": "充电线缺失",
                "参考话术": "您好，查找功能异常时可以正常回收。",
            },
            {
                "来源记录ID": "SRC-RIGHT",
                "产品类型": "耳机",
                "对象/部位": "查找功能",
                "异常现象": "功能异常",
                "答疑回复": "您好，查找功能异常时可以正常回收。",
            },
        ]
    )

    assert result["source_type"] == "work_order_actual_reply"
    assert result["source_record_id"] == "SRC-RIGHT"


def test_atomic_row_uses_only_its_ordered_reference_reply() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-ORDERED-U1",
                "_原子知识ID": "SRC-ORDERED-U1",
                "_原子参考话术": "您好，查找功能异常时可以正常回收。",
                "参考话术": (
                    "1. 您好，查找功能异常时可以正常回收。\n"
                    "2. 您好，包装盒缺少充电线时请补齐配件。"
                ),
            }
        ]
    )

    assert result["source_type"] == "second_part_reference_reply"
    assert result["reply"] == "您好，查找功能异常时可以正常回收。"
    assert "充电线" not in result["reply"]


def test_atomic_row_never_falls_back_to_full_record_reference_reply() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-UNPAIRED-U1",
                "_原子知识ID": "SRC-UNPAIRED-U1",
                "_原子问答配对状态": "count_mismatch",
                "参考话术": (
                    "1. 您好，查找功能异常时可以正常回收。\n"
                    "2. 您好，包装盒缺少充电线时请补齐配件。"
                ),
            }
        ]
    )

    assert result["source_type"] == "knowledge_content_generation"
    assert "充电线" not in result["reply"]


def test_atomic_reply_suppresses_legacy_full_reply_for_same_source() -> None:
    result = _select(
        [
            {
                "来源记录ID": "SRC-MIXED",
                "参考话术": (
                    "您好，查找功能异常时可以正常回收。"
                    "您好，查找功能异常时不影响回收。"
                ),
            },
            {
                "来源记录ID": "SRC-MIXED",
                "_原子知识ID": "SRC-MIXED-U1",
                "_原子问答配对状态": "paired",
                "_原子参考话术": "您好，查找功能异常时可以正常回收。",
                "参考话术": (
                    "您好，查找功能异常时可以正常回收。"
                    "您好，查找功能异常时不影响回收。"
                ),
            },
        ]
    )

    assert result["source_type"] == "second_part_reference_reply"
    assert result["source_record_id"] == "SRC-MIXED"
    assert result["reply"] == "您好，查找功能异常时可以正常回收。"
    assert "充电线" not in result["reply"]
