from __future__ import annotations

import json
from types import SimpleNamespace

from answer_hub.topic_transcription import audit_topic_draft
from answer_hub.mimo import (
    MimoClient,
    MimoConfig,
    MimoError,
    MimoLabelResult,
)
from answer_hub.topic_transcription.evidence import build_topic_evidence
from answer_hub.catalog import StandardCatalogItem
from answer_hub import workflow as workflow_module
from answer_hub.workflow import (
    _rule_topic_initial_review,
    build_topic_review_rows,
)


def test_authoritative_standard_keeps_ai_facts_but_filters_standard_path_noise() -> None:
    standard = StandardCatalogItem(
        standard_id="STD-DISPLAY-LEAK-001",
        title="副屏漏液判定",
        category_l1="屏幕显示",
        category_l2="屏幕显示异常",
        knowledge_type="质检标准",
        standard_path="【手机】-【副屏屏幕显示情况】-【副屏-屏幕显示异常】-【屏幕有漏液】",
        keywords=["手机", "副屏", "漏液"],
        scope="手机-通用",
        response_snippet=(
            "勾选项：【手机】-【副屏屏幕显示情况】-【副屏-屏幕显示异常】-【屏幕有漏液】\n"
            "标准定义：1.漏液：屏幕显示出现在任意背景色都可以看到的色块，或＞0.5mm的黑点。\n"
            "检测方法：切换黑色、紫色、绿色、红色背景进行核验。"
        ),
        status="published",
        version="v2026.09",
    )
    rows = [
        {
            "数据ID": "CASE-LEAK-001",
            "工单ID": "WO-LEAK-001",
            "产品类型": "手机",
            "一级分类": "屏幕显示",
            "二级分类": "屏幕显示异常",
            "核心问题": "副屏无法显示时如何判断漏液",
            "聊天内容": "副屏无法显示，多种背景色都能看到异常色块。",
            "判定结论": "副屏无法显示，多种背景色可见异常色块。",
            "判定依据": "黑色、紫色、绿色、红色背景均可见。",
            "图片处理状态": "无图片",
        }
    ]
    candidate = {
        "title": "副屏漏液如何判定？",
        "subtitles": [],
        "content": (
            "匹配标准为【手机】-【副屏屏幕显示情况】-【副屏-屏幕显示异常】-【屏幕有漏液】。\n"
            "请在【副屏屏幕显示情况】中选择对应项。\n"
            "副屏无法显示，多种背景色都能看到异常色块。"
        ),
        "recommended_reply": (
            "请在【副屏屏幕显示情况】中选择对应项；副屏无法显示，多种背景色都能看到异常色块。"
        ),
        "content_type": "判定规则",
        "standard_refs": [standard.standard_id],
        "applicable_scope": "手机",
        "applicable_brands": [],
        "applicable_models": [],
        "confidence": 0.95,
        "needs_human_review": False,
        "knowledge_form": "具体判定",
        "image_evidence_summary": "",
        "requires_images": False,
        "image_usage_instruction": "",
    }

    topic = workflow_module._topic_candidate_row(
        "TOP-LEAK-001",
        ("手机", "屏幕显示", "屏幕显示异常", "副屏漏液"),
        rows,
        [(standard, 0.98)],
        candidate,
        "mimo",
        "mimo-test",
        "test",
        "run-1",
        "",
        "topic_model_labeled",
        0.0,
        use_standard_references=True,
    )

    content = topic["知识内容"]
    reply = topic["推荐回复"]
    assert "0.5mm" in content
    assert "多种背景色" in content
    assert "匹配标准为" not in content
    assert "请在【副屏屏幕显示情况】中选择" not in content
    assert "匹配标准为" not in reply
    assert "请在【副屏屏幕显示情况】中选择" not in reply


def _polarizer_evidence() -> dict[str, object]:
    return {
        "fact_count": 1,
        "representative_fact_ids": ["F01"],
        "facts": [
            {
                "fact_id": "F01",
                "source_record_id": "CASE-1",
                "human_core_problem": (
                    "iPad Pro（2022）11.0英寸使用1号和5号孔位检测"
                ),
                "human_judgment_conclusion": (
                    "一个符合原装标准，另一个不符合，"
                    "应选择屏幕偏光检测异常"
                ),
                "judgment_basis": "1号和5号孔位颜色不一致",
            }
        ],
    }


def test_mixed_polarity_cannot_be_rewritten_as_any_match_normal() -> None:
    audit = audit_topic_draft(
        content=(
            "1. 在适用的孔位中，只要有一个孔位的颜色与原装示例相符，"
            "即可判定为正常（屏幕偏光检测通过）。"
        ),
        recommended_reply="",
        evidence_package=_polarizer_evidence(),
        use_standard_references=False,
    )

    assert audit.status == "manual_review"
    assert any("量词关系错误" in item for item in audit.logic_conflicts)
    assert any("正常/异常极性冲突" in item for item in audit.logic_conflicts)
    assert audit.claim_fact_refs


def test_mismatch_branch_keeps_specific_condition_and_abnormal_polarity() -> None:
    audit = audit_topic_draft(
        content=(
            "1. 对iPad Pro（2022）11.0英寸同时检查1号和5号孔位。\n"
            "2. 当1号孔位与原装示例一致、5号孔位与原装示例不一致时，"
            "选择屏幕偏光检测异常。"
        ),
        recommended_reply="",
        evidence_package=_polarizer_evidence(),
        use_standard_references=False,
    )

    assert audit.status == "passed"
    assert audit.logic_conflicts == ()
    assert audit.unsupported_claims == ()


def test_absolute_scope_without_source_support_is_blocked() -> None:
    audit = audit_topic_draft(
        content="1. 所有设备都可以直接判定为正常。",
        recommended_reply="",
        evidence_package=_polarizer_evidence(),
        use_standard_references=False,
    )

    assert audit.status == "manual_review"
    assert any("适用范围扩大" in item for item in audit.scope_expansion)


def test_only_if_condition_cannot_be_weakened_to_sufficient_condition() -> None:
    evidence = _polarizer_evidence()
    evidence["facts"][0]["human_judgment_conclusion"] = (
        "只有1号和5号孔位均符合原装示例，才可判定正常"
    )
    audit = audit_topic_draft(
        content="1. 只要一个孔位颜色符合原装示例即可判定正常。",
        recommended_reply="",
        evidence_package=evidence,
        use_standard_references=False,
    )

    assert audit.status == "manual_review"
    assert any("条件方向错误" in item for item in audit.logic_conflicts)


def test_existing_standard_reference_is_marked_as_historical_hold() -> None:
    audit = audit_topic_draft(
        content="1. 先核对屏幕孔位颜色，再由人工确认处理项。",
        recommended_reply="",
        evidence_package=_polarizer_evidence(),
        use_standard_references=False,
        preserved_standard_refs="【显示问题】-【闪屏/花屏】",
    )

    assert audit.historical_standard_reference_status == "历史标准关联搁置"


def test_rule_review_cannot_pass_logic_gate_failure() -> None:
    topic = {
        "主标题": "iPad Pro 屏幕偏光检测如何判定？",
        "知识内容": (
            "1. 只要一个孔位颜色相符即可判定正常。"
        ),
        "推荐回复": "",
        "关联标准项": "",
        "主题图片必要性": "无案例图",
        "主题逻辑门禁状态": "failed",
        "转写逻辑门禁状态": "failed",
        "转写逻辑门禁原因": "量词关系错误：来源要求同时核验多个孔位。",
    }

    review = _rule_topic_initial_review(
        topic,
        [],
        use_standard_references=False,
    )

    assert review["decision"] == "需修改"
    assert review["error_type"] == "场景理解错"
    assert "量词关系错误" in review["reason"]


def test_quality_audit_is_json_serializable() -> None:
    audit = audit_topic_draft(
        content="1. 对1号和5号孔位进行核验。",
        recommended_reply="",
        evidence_package=_polarizer_evidence(),
        use_standard_references=False,
    )

    encoded = json.dumps(audit.to_dict(), ensure_ascii=False)
    assert "claim_fact_refs" in encoded


def test_topic_evidence_keeps_structured_slots_and_richer_representative_fields() -> None:
    evidence = build_topic_evidence(
        {
            "facts": [
                {
                    "fact_id": "F01",
                    "source_record_id": "CASE-1",
                    "human_core_problem": "孔位颜色如何判定",
                }
            ],
            "representative_facts": [
                {
                    "fact_id": "F01",
                    "source_record_id": "CASE-1",
                    "condition": "同时检查1号和5号孔位",
                    "observation": "5号孔位与原装示例不一致",
                    "result": "选择屏幕偏光检测异常",
                    "boundary": "仅适用于iPad Pro（2022）11.0英寸",
                    "semantic_basis": "人工结论明确",
                    "fact_type": "manual_judgment",
                    "image_processing_status": "可用:1",
                }
            ],
        }
    )

    assert len(evidence.facts) == 1
    fact = evidence.facts[0]
    assert fact.condition == "同时检查1号和5号孔位"
    assert fact.observation == "5号孔位与原装示例不一致"
    assert fact.result == "选择屏幕偏光检测异常"
    assert fact.boundary == "仅适用于iPad Pro（2022）11.0英寸"
    assert fact.fact_type == "manual_judgment"
    assert fact.image_state == "可用:1"
    assert "人工结论明确" in fact.text


def test_topic_quality_retries_at_most_twice_then_forces_manual_review() -> None:
    calls: list[str] = []

    def wrong_candidate() -> dict[str, object]:
        return {
            "title": "iPad Pro 屏幕偏光检测判定规则",
            "subtitles": [],
            "content": (
                "适用情形：iPad Pro（2022）11.0英寸屏幕偏光检测。\n"
                "1. 只要1号或5号孔位颜色与原装示例相符，"
                "即可判定正常。\n"
                "2. 记录检测结果并完成后续处理。"
            ),
            "category_l1": "功能问题",
            "category_l2": "屏幕显示",
            "layer": "L2",
            "knowledge_form": "判定规则",
            "standard_refs": [],
            "applicable_scope": "平板-iPad Pro（2022）11.0英寸",
            "applicable_brands": ["Apple"],
            "applicable_models": ["iPad Pro（2022）11.0英寸"],
            "recommended_reply": "只要一个孔位颜色相符即可判定正常。",
            "confidence": 0.9,
            "reasoning_summary": "依据来源案例生成。",
            "needs_human_review": False,
            "image_evidence_summary": "",
            "requires_images": False,
            "image_usage_instruction": "",
        }

    class AlwaysWrongLogicMimo:
        config = SimpleNamespace(model="mimo-topic-quality-retry-test")

        def classify_topic_stage(self, _topic):
            calls.append("classify")
            return MimoLabelResult(
                candidate={
                    "topic_stage": "质检流程",
                    "knowledge_value": "值得沉淀",
                    "stage_reason": "主题属于屏幕检测流程。",
                    "value_reason": "来源包含明确人工结论。",
                    "reusable_knowledge": "屏幕偏光检测判定规则。",
                    "confidence": 0.95,
                    "needs_human_review": False,
                },
                request_audit={},
                response_audit={},
            )

        def label_topic(self, _topic, _matches, retry_reason="", **_kwargs):
            calls.append("rewrite" if retry_reason else "transcribe")
            return MimoLabelResult(
                candidate=wrong_candidate(),
                request_audit={},
                response_audit={},
            )

        def review_topic(self, *_args, **_kwargs):
            calls.append("review")
            return MimoLabelResult(
                candidate={
                    "decision": "通过",
                    "knowledge_value": "值得沉淀",
                    "error_type": "",
                    "reason": "模型误判，确定性门禁应覆盖该结论。",
                    "standard_consistency": "无可信标准",
                    "evidence_sufficiency": "充分",
                    "content_consistency": "一致",
                    "image_necessity": "不需要",
                    "title_quality": "清晰",
                    "confidence": 0.99,
                    "priority_review": False,
                },
                request_audit={},
                response_audit={},
            )

    rows = [
        {
            "数据ID": "POLARIZER-RETRY-001",
            "工单ID": "POLARIZER-RETRY-001",
            "聊天内容": "iPad Pro（2022）11.0英寸使用1号和5号孔位检测。",
            "历史实际回复": "一个符合原装示例，另一个不符合，应选择屏幕偏光检测异常。",
            "核心问题": "iPad Pro（2022）11.0英寸屏幕偏光检测如何判定",
            "产品类型": "平板",
            "问题意图": "检测核验",
            "对象/部位": "屏幕偏光",
            "异常现象": "1号和5号孔位颜色一处符合、一处不符合",
            "解题方式": "核对孔位颜色与原装示例",
            "判定结论": "一个符合、另一个不符合，选择屏幕偏光检测异常",
            "判定依据": "1号和5号孔位颜色不一致",
            "语义标注依据": "人工结论明确。",
        }
    ]

    topics, _mapping, _gaps, _pending = build_topic_review_rows(
        rows,
        mimo_client=AlwaysWrongLogicMimo(),
        clustering_mode="rule",
        use_standard_references=False,
        topic_model_call_limit=8,
        transcribe_all_admitted_topics=True,
    )

    topic = topics[0]
    assert calls == ["classify", "transcribe", "rewrite", "rewrite", "review"]
    assert topic["转写重试次数"] == "2"
    assert topic["转写逻辑门禁状态"] == "failed"
    assert topic["模型初标结论"] != "通过"
    assert "量词关系错误" in topic["转写重试原因"]


def test_label_topic_single_attempt_error_does_not_claim_a_retry() -> None:
    client = MimoClient(
        MimoConfig(
            api_key="test-key",
            base_url="https://example.com/v1",
            model="mimo-topic-attempt-test",
        )
    )

    def invalid_post(_payload: dict[str, object]) -> dict[str, object]:
        return {
            "choices": [
                {"message": {"content": "{\"not_a_topic\": true}"}}
            ]
        }

    client._post = invalid_post  # type: ignore[method-assign]
    try:
        client.label_topic(
            {
                "topic_id": "TOP-ATTEMPT-001",
                "features": "孔位颜色判定",
                "evidence_package": {},
            },
            [],
            use_standard_references=False,
            max_attempts=1,
        )
    except MimoError as exc:
        assert "MiMo 主题 JSON 校验失败：" in str(exc)
        assert "已重试一次" not in str(exc)
    else:
        raise AssertionError("无效主题 JSON 应触发 MimoError")
