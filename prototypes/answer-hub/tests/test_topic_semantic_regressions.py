from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer_hub import workflow as w
from answer_hub.draft_quality import assess_case_only_draft
from answer_hub.question_standardization import (
    standard_question_conflict_reason,
    standardize_atomic_question,
)


def test_component_split_rebuilds_inherited_composite_standard_question():
    split, applied = w._direct_local_split_notebook_brand_topics(
        {"产品类型": "笔记本", "核心问题": "内存和硬盘是否为品牌件"},
        [{
            "normalized_issue": "内存和硬盘品牌查询",
            "subject": "内存和硬盘",
            "phenomenon": "品牌属性",
            "judgment_target": "确认内存和硬盘是否为品牌件",
            "standard_question": "内存和硬盘是否为品牌件",
            "confidence": 0.9,
        }],
    )
    assert applied
    for topic in split:
        normalized = standardize_atomic_question(
            {"business_line": "自营回收", "product_category": "笔记本", **topic}
        )
        other = "硬盘" if topic["subject"] == "内存" else "内存"
        assert topic["subject"] in normalized["standard_question"]
        assert other not in normalized["standard_question"]


def test_standard_question_from_another_atomic_object_is_rebuilt():
    normalized = standardize_atomic_question({
        "business_line": "自营回收",
        "product_category": "笔记本",
        "subject": "BIOS锁",
        "phenomenon": "密码锁",
        "judgment_target": "如何处理BIOS锁",
        "resolution_mode": "核验密码锁",
        "standard_question": "更换硬盘导致容量异常如何处理",
    })
    assert "BIOS" in normalized["standard_question"]
    assert "硬盘" not in normalized["standard_question"]


def test_pre_split_component_atoms_do_not_keep_a_shared_parent_question():
    for subject, other in (("内存", "硬盘"), ("硬盘", "内存")):
        result = standardize_atomic_question({
            "business_line": "自营回收", "product_category": "笔记本",
            "subject": subject, "phenomenon": "品牌属性",
            "judgment_target": f"确认{subject}品牌",
            "standard_question": "内存和硬盘是否为品牌件",
        })
        assert other not in result["standard_question"]


def test_unknown_threshold_and_equivalent_rule_sentences_remain_comparable():
    def ready(threshold):
        return {
            "standardization_status": "ready",
            "threshold_or_exception": threshold,
        }
    assert not standard_question_conflict_reason([
        ready("胶条脱落大于10mm判屏幕脱胶"),
        ready("脱落长度>10毫米属于屏幕脱胶"),
    ])
    assert not standard_question_conflict_reason([
        ready("无明确阈值"), ready("胶条脱落大于10mm判屏幕脱胶"),
    ])
    assert standard_question_conflict_reason([ready(">10mm"), ready(">20mm")])


def test_same_display_target_is_recalled_across_missing_exception():
    base = {
        "business_line": "自营回收", "product_category": "笔记本",
        "subject": "屏幕", "phenomenon": "色斑",
        "judgment_target": "判断是否屏幕色斑",
        "resolution_mode": "按屏幕色斑核验", "standard_path": "屏幕显示",
    }
    left = {**base, "unit_id": "spot-a", "threshold_or_exception": "竖线不可复现时不加项"}
    right = {**base, "unit_id": "spot-b", "threshold_or_exception": "待确认"}
    assert w._direct_atomic_bucket_key(left) == w._direct_atomic_bucket_key(right)


def test_camera_metal_ring_and_frame_aliases_do_not_conflict():
    rows = [{
        "数据ID": str(index), "产品类型": "手机",
        "_原子知识ID": str(index) + "-U1",
        "核心问题": "后摄镜头框金属圈磕碰掉漆如何判定",
        "对象/部位": subject, "异常现象": "磕碰掉漆",
        "判定目标": "外壳磕碰掉漆程度值选取",
        "模型主题二级分类": category,
    } for index, (subject, category) in enumerate([
        ("后摄镜头金属圈", "镜头金属圈痕迹"),
        ("后摄镜头框边缘", "外壳磕碰掉漆"),
    ])]
    assert not w._direct_cluster_hard_conflict_reason(rows)
    different = {**rows[1], "对象/部位": "屏幕", "核心问题": "屏幕色斑如何判定",
                 "判定目标": "判断屏幕色斑", "异常现象": "色斑"}
    assert w._direct_cluster_hard_conflict_reason([rows[0], different])


def test_glue_queries_are_compared_without_merging_red_line_question():
    base = {"product_category": "手机", "business_line": "自营回收"}
    left = {**base, "unit_id": "glue-1", "subject": "屏幕侧面胶条",
            "phenomenon": "胶条脱落", "judgment_target": "判断是否屏幕脱胶"}
    right = {**base, "unit_id": "glue-2", "subject": "屏幕边缘胶条",
             "phenomenon": "磨损毛边疑似缺失", "judgment_target": "选择处理项"}
    red_line = {**base, "unit_id": "red-1", "subject": "屏幕边缘红线",
                "phenomenon": "红线缺失", "judgment_target": "是否拆修"}
    assert w._direct_atomic_bucket_key(left) == w._direct_atomic_bucket_key(right)
    assert w._direct_atomic_bucket_key(left) != w._direct_atomic_bucket_key(red_line)


def test_atomic_rule_draft_uses_bound_reply_not_multi_topic_source_summary():
    rows = [{
        "数据ID": "battery-source", "工单ID": "battery-source",
        "_原子知识ID": "battery-source-U1", "产品类型": "手机",
        "_原子问答配对状态": "paired",
        "_原子参考话术": "关于本机显示正品部件时选电池正品部件；一根线读取异常时加选电池工具读出异常。",
        "核心问题": "本机正品部件与电池工具读出异常如何勾选",
        "对象/部位": "电池", "异常现象": "工具读出异常",
        "判定目标": "选择电池选项", "问题意图": "标准判定",
        "判定结论": "电池按异常处理；镜头金属圈按磕碰处理。",
        "判定依据": "关键事实：回收师咨询电池和镜头金属圈。",
        "聊天内容": "请问电池怎么选？另外镜头金属圈怎么判？",
    }]
    candidate = w._topic_rule_draft("battery-topic", rows, [], use_standard_references=False)
    assert "电池" in candidate["content"]
    assert "镜头" not in candidate["content"]
    assert "关键事实" not in candidate["content"]


def test_verified_source_question_accepts_natural_wording_for_unique_answer():
    row = {
        "产品类型": "笔记本",
        "_原子知识ID": "fingerprint-U1",
        "_人工答复门禁状态": "answered",
        "核心问题": "指纹功能支持查询",
        "对象/部位": "指纹识别",
        "异常现象": "是否支持指纹",
        "_原子实际回复": "不支持指纹",
        "聊天内容": (
            "回收师：这个机器支持指纹吗？\n"
            "答疑：不支持"
        ),
    }

    assert w._verified_atomic_source_question(row, "不支持指纹") == (
        "这个机器支持指纹吗？"
    )


def test_atomic_boundary_ignores_other_question_in_full_conversation():
    row = {
        "产品类型": "笔记本",
        "_原子知识ID": "source-U2",
        "_原子核心问题": "深色表面白色细小痕迹是否为磕点",
        "核心问题": "深色表面白色细小痕迹是否为磕点",
        "对象/部位": "深色表面",
        "异常现象": "白色细小痕迹",
        "判定目标": "判断是否外观磕点",
        "问题意图": "标准判定",
        "聊天内容": "触控板有第三方维修标签，是否更换零部件？另外深色表面白点是不是磕碰？",
        "原始核心问题": "第三方维修标签；深色表面白点",
        "原始判定结论": "维修标签按零部件更换；白点需要确认",
    }
    boundary = w._topic_boundary_key(row)
    assert boundary is None or "零部件更换" not in str(boundary)


def test_unpaired_atomic_draft_does_not_reuse_sibling_judgment():
    rows = [{
        "数据ID": "glue-redline", "_原子知识ID": "glue-redline-U1",
        "_原子问答配对状态": "topic_order_mismatch", "产品类型": "手机",
        "核心问题": "屏幕胶条磨损如何判定", "对象/部位": "屏幕边缘胶条",
        "判定结论": "胶条脱落超过10mm判屏幕脱胶；针对红线按显示边界异常处理。",
        "聊天内容": "请问胶条是否缺失？另外屏幕红线如何判定？",
    }]
    candidate = w._topic_rule_draft("glue-topic", rows, [], use_standard_references=False)
    assert candidate["source_answer_missing"] is True
    assert candidate["content"] == ""


def test_bound_atomic_model_evidence_does_not_inherit_sibling_judgment():
    row = {
        "数据ID": "battery", "_原子知识ID": "battery-U1",
        "_原子问答配对状态": "paired", "产品类型": "手机",
        "核心问题": "电池读取异常如何勾选", "对象/部位": "电池",
        "_原子参考话术": "电池读取异常时勾选电池工具读出异常。",
        "原始判定结论": "电池异常需勾选；针对镜头金属圈痕迹按磕碰处理。",
        "判定依据": "关键事实：电池与镜头金属圈分别咨询。",
        "语义标注依据": "电池异常与镜头金属圈磕碰。",
    }
    fact = w._topic_source_fact(row, 1)
    assert "镜头" not in str(fact)
    assert fact["human_judgment_conclusion"] == row["_原子参考话术"]
    assert fact["fact_type"] == "second_part_reference_reply"


def test_single_question_without_ordered_pairs_retains_original_judgment():
    row = {
        "数据ID": "single", "_原子知识ID": "single-U1",
        "_原子问答配对状态": "", "_原子来源问题数": 1,
        "产品类型": "手机", "核心问题": "机型如何查询", "对象/部位": "机型",
        "判定结论": "按 STD-FAKE 查看关于本机的型号。",
    }
    assert "STD-FAKE" in w._topic_source_fact(row, 1)["human_judgment_conclusion"]


def test_untranscribed_candidate_does_not_expose_audit_note_as_knowledge():
    candidate = w._untranscribed_topic_candidate_row(
        "pending", ("pending",), [{
            "数据ID": "pending", "产品类型": "手机", "核心问题": "电池异常如何勾选",
            "判定结论": "需要确认电池选项",
        }], {"knowledge_value": "待确认"},
    )
    assert candidate["知识内容"] == ""
    assert candidate["推荐回复"] == ""
    assert "未形成" in candidate["模型初标原因"]


def test_knowledge_body_keeps_all_bound_reply_conditions():
    content = w._template_style_knowledge_content(
        "\n".join(["核验部件。"] * 5 + ["其他机型不得套用。"])
    )
    assert "其他机型不得套用。" in content.splitlines()[-1]


def test_same_standard_family_policy_does_not_reject_phenomenon_variants():
    units = [{
        "标准问题标准化状态": "ready",
        "_聚类标准族": "A面外观",
        "_聚类合并策略": "same_standard_family",
        "_聚类现象值": phenomenon,
        "_原子阈值例外": "无明确阈值",
    } for phenomenon in ("磕碰", "磨损")]
    assert standard_question_conflict_reason(units) == ""
    units[1]["_聚类合并策略"] = "separate_by_phenomenon"
    assert "聚类现象值" in standard_question_conflict_reason(units)


@pytest.mark.parametrize("content", [
    "回收师反馈关于本机显示正品部件、一根线读取电池异常并询问选项；"
    "仅覆盖上述来源事实明确记录的对象、条件和结论；"
    "来源未说明的其他情形不得直接套用，需要补充对应事实后再判断。",
    "适用情形：一根线读取电池异常该勾选什么？\n"
    "核验要点：电池 / 一根线读取异常\n"
    "适用边界：仅覆盖上述来源事实明确记录的对象、条件和结论。",
])
def test_question_summary_with_audit_boilerplate_is_not_finished_knowledge(content):
    assessment = assess_case_only_draft(
        content=content, source_values=["一根线读取电池异常该勾选什么？"]
    )
    assert assessment.decision == "manual_review"


def test_explicit_processing_conclusion_survives_audit_boundary():
    assessment = assess_case_only_draft(
        content=(
            "处理结论：一根线读取电池异常时勾选电池异常。"
            "仅覆盖上述来源事实。"
        ),
        source_values=["一根线读取电池异常时勾选电池异常。"],
    )
    assert assessment.decision == "pass"


def test_audit_cleanup_preserves_original_business_punctuation():
    from answer_hub.draft_quality import strip_case_audit_boilerplate

    assert strip_case_audit_boilerplate("条件A选A；条件B选B。") == "条件A选A；条件B选B。"


def test_key_fact_summary_is_not_treated_as_a_business_answer():
    assessment = assess_case_only_draft(
        content="关键事实：屏幕色斑属于显示异常。仅覆盖上述来源事实。",
        source_values=["屏幕色斑如何判定？"],
    )
    assert assessment.decision == "manual_review"


def test_empty_subtitles_are_preserved():
    assert w._finalize_topic_subtitles([], "屏幕色斑", "来源结论", "具体判定") == ""


def test_reply_keeps_trailing_model_restriction():
    reply = "屏幕色斑需要确认显示条件。" * 25 + "仅限本条来源所述机型，其他机型不得套用。"
    assert w._compact_recommended_reply(reply).endswith("其他机型不得套用。")


def test_model_cannot_supply_a_determined_answer_without_source_reply():
    selected = w._select_topic_recommended_reply(
        [{"产品类型": "手机", "核心问题": "电池异常该勾选什么？"}],
        title="电池异常如何选择", content="电池异常如何选择",
        model_reply="电池异常应勾选电池损坏。",
        evidence_status="available", use_standard_references=False,
    )
    assert selected["source_type"] != "deepseek_transcription"
    assert any("没有明确答复" in issue for issue in selected["issues"])


def test_atomic_reply_contract_does_not_reuse_full_source_actual_reply():
    selected = w._select_topic_recommended_reply(
        [{
            "数据ID": "multi-reply", "_原子知识ID": "multi-reply-U1",
            "_原子问答配对状态": "count_mismatch", "_原子参考话术": "",
            "产品类型": "笔记本", "对象/部位": "内存",
            "实际回复": "内存选正常；硬盘按第三方处理。",
        }],
        title="内存品牌如何判断", content="内存品牌需要核验",
        model_reply="", evidence_status="available", use_standard_references=False,
    )
    assert selected["source_type"] != "work_order_actual_reply"
    assert "硬盘按第三方" not in selected["reply"]


def test_mismatched_question_reply_pairing_is_repaired_by_unique_objects():
    bound, status, _ = w._bind_ordered_question_reply_pairs(
        {
            "AI结果问答配对状态": "single",
            "_AI结果有序问答对": [
                {"question_index": 1, "core_problem": "内存如何判断", "reference_reply": "内存答复"},
                {"question_index": 2, "core_problem": "硬盘如何判断", "reference_reply": "硬盘答复"},
                {"question_index": 3, "core_problem": "序列号真伪如何判断", "reference_reply": "序列号答复"},
            ],
        },
        [
            {"source_question_index": 1, "normalized_issue": "内存品牌如何判断"},
            {"source_question_index": 2, "normalized_issue": "序列号真伪如何判断"},
            {"source_question_index": 3, "normalized_issue": "硬盘品牌如何判断"},
        ],
    )
    assert status == "semantic_paired"
    assert "内存" in bound[0]["_source_reference_reply"]
    assert "序列号" in bound[1]["_source_reference_reply"]
    assert "硬盘" in bound[2]["_source_reference_reply"]


def test_risk_admission_preserves_confirmed_members_without_complete_equal_boundaries(monkeypatch):
    rows = [{
        "数据ID": source,
        "工单ID": source,
        "产品类型": "手机",
        "聊天内容": "胶条脱落大于10mm按屏幕脱胶处理，磨损毛边除外。",
        "实际回复": "胶条脱落大于10mm按屏幕脱胶处理，磨损毛边除外。",
        "核心问题": "胶条脱落如何判定",
        "对象/部位": "胶条",
        "异常现象": "脱落",
        "判定目标": "胶条脱胶判定",
        "_原子知识ID": source + "-U1",
        "_原子需要复核": True,
        "_聚类决策": "单例二次裁决确认合并",
        "_聚类裁决提供方": "mimo-direct-reconcile",
        "_聚类裁决置信度": 0.92,
    } for source in ("glue-a", "glue-b")]
    # The upstream semantic decision is frozen. Exercise actual admission,
    # provisional grouping and export, without making any model request.
    monkeypatch.setattr(w, "_direct_mimo_topic_groups", lambda *a, **k: (
        [(("reconciled", "glue-a-U1", "glue-b-U1"), rows)],
        {"effective_mode": "direct_mimo", "provider": "mimo-direct"},
    ))
    monkeypatch.setattr(w, "_provisional_topic_boundary_key", lambda row: None)
    topics, mappings, _, _ = w.build_topic_review_rows(
        rows, mimo_client=SimpleNamespace(config=SimpleNamespace(model="test")),
        clustering_mode="direct_mimo", use_mimo=False,
        use_standard_references=False, enforce_cluster_admission=True,
    )
    assert len(topics) == 1
    assert topics[0]["主题样本数"] == 2
    assert len(mappings) == 2
    assert topics[0]["是否重点复核"] == "是"
