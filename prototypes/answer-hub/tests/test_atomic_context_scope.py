from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from answer_hub import auto_review, workflow
from answer_hub.mimo import MimoLabelResult


@pytest.fixture
def build_atomic_context(monkeypatch):
    monkeypatch.setattr(
        auto_review.AutoReviewPolicy, "from_env",
        classmethod(lambda cls: cls()),
    )

    def build(chat, *, model="三星W23", threshold="电量大于50%", expect_answered=True):
        row = {
            "数据ID": "ATOMIC-CONTEXT", "工单ID": "700002", "产品类型": "手机",
            "_原子知识ID": "ATOMIC-CONTEXT-U1",
            "核心问题": "手机｜开机情况｜折叠后自动关机",
            "原始核心问题": "三星W23电量大于50%时，折叠后自动关机如何判定？",
            "对象/部位": "开机情况", "异常现象": "折叠后自动关机",
            "机型": model, "_原子机型范围": model,
            "品牌": "三星", "_原子品牌": "三星",
            "_原子阈值例外": threshold,
            "判定结论": "判自动关机", "_原子实际回复": "判自动关机",
            "聊天内容": chat,
        }
        original = deepcopy(row)
        captured = {"stage": [], "label": []}

        def stage(payload):
            captured["stage"].append(payload)
            return MimoLabelResult(candidate={
                "topic_stage": "质检标准", "knowledge_value": "值得沉淀",
                "stage_reason": "人工明确答复了关机判定。",
                "value_reason": "保留来源问题的条件和机型。",
                "reusable_knowledge": "折叠后自动关机按自动关机判定。",
                "confidence": 0.95, "needs_human_review": True,
            }, request_audit={}, response_audit={})

        def label(payload, matches, **kwargs):
            captured["label"].append(payload)
            return MimoLabelResult(candidate={
                "title": "手机折叠后自动关机如何判定", "subtitles": [],
                "content": "手机出现折叠后自动关机的现象时，开机情况按自动关机判定。",
                "recommended_reply": "判自动关机",
                "knowledge_form": "具体判定", "content_type": "定义型",
                "applicable_scope": "手机", "applicable_brands": [],
                "applicable_models": [], "standard_refs": [], "confidence": 0.95,
            }, request_audit={}, response_audit={})

        topics, mapping, gaps, pending = workflow.build_topic_review_rows(
            [row], use_mimo=True,
            mimo_client=SimpleNamespace(
                config=SimpleNamespace(model="offline-test"),
                classify_topic_stage=stage, label_topic=label,
            ),
            clustering_mode="rule", use_standard_references=False,
            transcribe_all_admitted_topics=True,
        )
        assert {key: row[key] for key in original} == original
        if not expect_answered:
            return topics, mapping, gaps, pending, captured
        assert not gaps and not pending
        assert len(topics) == len(mapping) == 1
        assert captured["stage"] and captured["label"]
        return topics[0], mapping[0], captured

    return build


def test_current_question_restores_omitted_model_and_condition_in_source_facts(
    build_atomic_context,
):
    question = "三星W23电量大于50%时，折叠后自动关机如何判定？"
    topic, mapping, captured = build_atomic_context(
        f"回收师：{question}\n答疑：判自动关机",
    )

    assert topic["适用机型"] == "三星W23"
    assert topic["适用品牌"] == "三星"
    assert "三星W23" in topic["主标题"]
    assert "电量大于50%" in topic["主标题"]
    for payload in [*captured["stage"], *captured["label"]]:
        fact = payload["evidence_package"]["facts"][0]
        assert fact["source_question_excerpt"] == question
        assert question in fact["conversation_excerpt"]
        assert fact["human_core_problem"] == "手机｜开机情况｜折叠后自动关机"
        assert fact["atomic_question"] == fact["human_core_problem"]
        assert fact["condition"] == "电量大于50%"
        assert fact["source_supported_threshold_or_exception"] == "电量大于50%"
        assert fact["threshold_source_supported"] is True
        assert fact["historical_actual_reply"] == "判自动关机"
    assert question in mapping["聊天内容"]
    assert topic["模型质量状态"] == "failed"
    assert topic["知识草稿状态"] == "blocked"
    assert "来源条件遗漏" in topic["转写质量审计"]


def test_multiple_source_questions_keep_only_the_current_answered_context(
    build_atomic_context,
):
    question = "三星W23电量大于50%时，折叠后自动关机如何判定？"
    chat = (
        "回收师：iPhone 15电量小于10%时，折叠后自动关机如何判定？\n"
        "答疑：先确认屏幕损坏情况\n"
        f"回收师：{question}\n答疑：判自动关机"
    )
    topic, mapping, captured = build_atomic_context(chat)

    assert topic["适用机型"] == "三星W23"
    for payload in [*captured["stage"], *captured["label"]]:
        facts = payload["evidence_package"]["facts"]
        assert facts[0]["source_question_excerpt"] == question
        assert facts[0]["condition"] == "电量大于50%"
        assert "iPhone" not in json.dumps(facts, ensure_ascii=False)
        assert "10%" not in json.dumps(facts, ensure_ascii=False)
    assert "iPhone 15" in mapping["聊天内容"]


@pytest.mark.parametrize("chat", [
    (
        "回收师：三星W23电量大于50%时，折叠后自动关机如何判定？\n"
        "答疑：判自动关机\n"
        "回收师：三星W24电量小于20%时，折叠后自动关机如何判定？\n"
        "答疑：判自动关机"
    ),
    (
        "回收师：三星W23电量大于50%时，屏幕闪烁如何判定？\n"
        "答疑：判自动关机"
    ),
])
def test_unproven_question_context_cannot_restore_model_or_condition(
    build_atomic_context, chat,
):
    topic, _mapping, captured = build_atomic_context(chat)

    assert not topic["适用机型"]
    assert not topic["适用品牌"]
    for payload in [*captured["stage"], *captured["label"]]:
        fact = payload["evidence_package"]["facts"][0]
        assert not fact["source_question_excerpt"]
        assert not fact["condition"]
        assert not fact["source_supported_threshold_or_exception"]
        assert "W23" not in fact["conversation_excerpt"]
        assert "50%" not in fact["conversation_excerpt"]


def test_robot_reply_cannot_admit_source_context_or_invoke_models(
    build_atomic_context,
):
    topics, mapping, gaps, _pending, captured = build_atomic_context(
        "回收师：三星W23电量大于50%时，折叠后自动关机如何判定？\n"
        "机器人：判自动关机",
        expect_answered=False,
    )

    assert not topics and not mapping
    assert gaps
    assert not captured["stage"] and not captured["label"]


def test_sibling_condition_cannot_support_current_threshold(
    build_atomic_context,
):
    question = "三星W23折叠后自动关机如何判定？"
    topic, _mapping, captured = build_atomic_context(
        "回收师：iPhone 15电量大于50%时，屏幕闪烁如何判定？\n"
        "答疑：按屏幕闪烁处理\n"
        f"回收师：{question}\n答疑：判自动关机",
    )

    assert topic["适用机型"] == "三星W23"
    for payload in [*captured["stage"], *captured["label"]]:
        fact = payload["evidence_package"]["facts"][0]
        assert fact["source_question_excerpt"] == question
        assert not fact["condition"]
        assert not fact["threshold_source_supported"]
        assert "50%" not in fact["conversation_excerpt"]
