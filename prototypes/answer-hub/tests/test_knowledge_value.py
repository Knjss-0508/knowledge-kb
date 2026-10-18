import pytest

from answer_hub.knowledge_value import (
    has_draftable_source_rule,
    has_explicit_reusable_knowledge,
)


@pytest.mark.parametrize("answer", [
    "日版 Switch Lite 限定款可以回收，包装盒缺失会按配件缺失扣减。",
    "查找功能已绑定账户，按不回收类型处理。",
    "折叠后关机按自动关机勾选。",
    "判自动关机",
])
def test_concrete_answer_identifies_a_reusable_disposition(answer):
    assert has_explicit_reusable_knowledge(
        source_values=[answer], threshold_values=[],
    )


@pytest.mark.parametrize("answer", [
    "按实际情况处理。", "按具体情况处理。", "按标准处理。",
    "按来源处理。", "按现场情况处理。", "如何按配件缺失扣减？",
    "按正常处理。", "按异常处理。",
    "按照正常处理。", "按照异常处理。",
    "按当前标准处理。", "按照相关标准处理。",
    "判正常", "判异常", "判实际情况", "判待确认",
    "判待核实", "判待补充", "判不能确认",
])
def test_vague_or_question_only_disposition_is_not_a_rule(answer):
    assert not has_explicit_reusable_knowledge(
        source_values=[answer], threshold_values=[],
    )


@pytest.mark.parametrize("recognize", [
    has_explicit_reusable_knowledge, has_draftable_source_rule,
])
@pytest.mark.parametrize("answer", [
    "非新品无三包不用\n备用机要判",
    "铭文甲不用\n铭文乙要登记",
    "标记乙要复核，标记甲不用",
    "刻甲无需登记；刻乙需要登记",
    "刻甲不必登记\n刻乙需登记",
    "“标签 甲”不用；“标签乙”要判",
])
def test_named_conditions_with_opposite_dispositions_are_a_reusable_rule(
    recognize, answer,
):
    assert recognize(
        source_values=[answer], threshold_values=[],
    )


@pytest.mark.parametrize("recognize", [
    has_explicit_reusable_knowledge, has_draftable_source_rule,
])
@pytest.mark.parametrize("answers", [
    ["正常不用\n异常要判"],
    ["正常情况不用\n异常情况要判"],
    ["这个不用\n那个要判"],
    ["这种情况不用\n那种情况要判"],
    ["某个不用\n另一个要判"],
    ["刻甲不用\n刻乙要判吗？"],
    ["刻甲不用？\n刻乙要判？"],
    ["备用机要判"],
    ["刻甲不用\n刻乙要"],
    ["刻甲要判\n刻乙要判"],
    ["刻甲不用\n刻乙不用判"],
    ["“刻甲”不用\n刻甲要判"],
    ["刻甲不用登记\n刻乙要上报"],
    ["刻甲不用", "刻乙要判"],
])
def test_named_condition_rule_requires_distinct_named_nonquestion_branches(
    recognize, answers,
):
    assert not recognize(
        source_values=answers, threshold_values=[],
    )


def test_real_named_condition_rule_keeps_worthy_value_without_releasing_body():
    from types import SimpleNamespace

    from answer_hub.mimo import MimoLabelResult
    from answer_hub.workflow import build_topic_review_rows

    actual = "非新品无三包不用\n备用机要判"
    question = "手机｜后盖｜有“非新品 无三包”“备用机”等业务标识刻字"
    body = (
        "1. 手机后盖刻有“非新品 无三包”这类业务标识时，不用判定。\n"
        "2. 手机后盖刻有“备用机”这类业务标识时，要判定。"
    )
    seen_answers = []

    def stage(topic):
        seen_answers.extend(topic["historical_replies"])
        return MimoLabelResult(candidate={
            "topic_stage": "质检标准", "knowledge_value": "值得沉淀",
            "stage_reason": "按刻字内容区分判定。",
            "value_reason": "实答给出两个有名条件及相反处置。",
            "reusable_knowledge": actual, "confidence": 0.9,
            "needs_human_review": True,
        }, request_audit={}, response_audit={})

    def label(*args, **kwargs):
        return MimoLabelResult(candidate={
            "title": "手机后盖刻字如何判定", "subtitles": [],
            "content": body, "recommended_reply": actual,
            "knowledge_form": "具体判定", "content_type": "定义型",
            "confidence": 0.95, "standard_refs": [],
            "applicable_scope": "手机", "applicable_brands": [],
            "applicable_models": [],
        }, request_audit={}, response_audit={})

    topics, _, gaps, _ = build_topic_review_rows(
        [{
            "数据ID": "NAMED-CONDITIONS", "_原子知识ID": "NAMED-CONDITIONS-U1",
            "产品类型": "手机", "核心问题": question,
            "对象/部位": "后盖", "异常现象": "后盖有两种刻字",
            "聊天内容": f"回收师：{question}\n答疑：非新品无三包不用\n答疑：备用机要判",
        }],
        mimo_client=SimpleNamespace(
            config=SimpleNamespace(model="test"), classify_topic_stage=stage,
            label_topic=label,
        ),
        use_mimo=True, clustering_mode="rule", use_standard_references=False,
        transcribe_all_admitted_topics=True,
    )

    assert not gaps
    assert seen_answers == [actual]
    assert len(topics) == 1
    assert topics[0]["主题沉淀价值"] == "值得沉淀"
    assert "当前主题来源缺少明确阈值" not in topics[0]["主题价值原因"]
    assert topics[0]["主题无来源内容"]
    assert topics[0]["知识草稿状态"] == "blocked"
