import pytest

from answer_hub.topic_transcription import audit_topic_draft


FULL = "三星W23电量大于50%时折叠后自动关机，按自动关机判定。"
WIDE = "手机折叠后自动关机，按自动关机判定。"


@pytest.mark.parametrize("content,reply,missing", [
    (WIDE, WIDE, ("知识内容", "推荐回复")),
    (FULL, WIDE, ("推荐回复",)),
    (WIDE, FULL, ("知识内容",)),
    (FULL, FULL, ()),
    (FULL.replace("大于", ">"), FULL.replace("大于", "＞"), ()),
    (FULL.replace("大于", "不大于"), FULL.replace("大于", "不大于"),
     ("知识内容", "推荐回复")),
    (FULL.replace("大于", "不小于"), FULL, ("知识内容",)),
])
def test_single_verified_source_condition_must_survive_in_each_business_field(
    content, reply, missing,
):
    result = audit_topic_draft(
        content=content, recommended_reply=reply,
        evidence_package={"facts": [{
            "fact_id": "F01", "fact_type": "work_order_actual_reply",
            "human_core_problem": "三星W23电量大于50%时折叠后自动关机如何判定",
            "historical_actual_reply": "判自动关机",
            "source_supported_threshold_or_exception": "电量大于50%",
            "condition": "电量大于50%",
        }]},
        use_standard_references=False,
    )
    assert len(result.scope_expansion) == len(missing)
    for field in missing:
        assert any(field in issue and "来源条件遗漏" in issue for issue in result.scope_expansion)
    if missing:
        assert result.blocking and result.retryable


def test_unverified_model_condition_does_not_become_a_required_business_fact():
    result = audit_topic_draft(
        content=WIDE, recommended_reply=WIDE,
        evidence_package={"facts": [{
            "fact_id": "F01", "fact_type": "work_order_actual_reply",
            "historical_actual_reply": "判自动关机", "condition": "电量大于50%",
        }]},
        use_standard_references=False,
    )
    assert not result.scope_expansion


def test_active_standard_can_support_a_rule_broader_than_one_case():
    result = audit_topic_draft(
        content=WIDE, recommended_reply=WIDE,
        evidence_package={"facts": [{
            "fact_id": "F01", "fact_type": "work_order_actual_reply",
            "historical_actual_reply": "判自动关机",
            "source_supported_threshold_or_exception": "电量大于50%",
        }]},
        use_standard_references=True, active_standard_refs="TEST-STANDARD",
    )
    assert not result.scope_expansion
