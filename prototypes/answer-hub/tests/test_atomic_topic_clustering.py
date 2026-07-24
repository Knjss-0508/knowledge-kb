from __future__ import annotations

import json
from pathlib import Path

import pytest

from answer_hub.mimo import (
    ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
    MimoClient,
    MimoConfig,
    MimoError,
    _atomic_unit_payload,
    _validate_atomic_topic_clusters,
)
from scripts.run_atomic_topic_clustering import (
    _analyze_bucket,
    _bucket_units,
    _build_output,
    _cluster_program_conflicts,
)


def _unit(
    atomic_id: str,
    *,
    product_category: str = "手机",
    scope_type: str = "品类专用",
    platform: str = "通用",
    brand: str = "通用",
    model_scope: str = "通用",
    category_l1: str = "显示问题",
    category_l2: str = "屏幕色斑",
    intent: str = "标准判定",
    subject: str = "屏幕",
    standard_path: str = "根据显示异常标准判断是否属于色斑",
    threshold_or_exception: str = "无明确阈值",
    requires_review: bool = False,
) -> dict[str, object]:
    return {
        "unit_id": atomic_id,
        "sample_id": atomic_id.split("-")[0],
        "normalized_issue": f"{subject}异常如何判定",
        "product_category": product_category,
        "scope_type": scope_type,
        "platform": platform,
        "brand": brand,
        "model_scope": model_scope,
        "category_l1": category_l1,
        "category_l2": category_l2,
        "intent": intent,
        "subject": subject,
        "phenomenon": f"{subject}出现异常",
        "judgment_target": f"判断{subject}异常是否符合标准",
        "resolution_mode": "按照对应标准给出判定结论",
        "standard_path": standard_path,
        "threshold_or_exception": threshold_or_exception,
        "requires_review": requires_review,
    }


def _valid_cluster_result(*atomic_ids: str) -> dict[str, object]:
    return {
        "clusters": [
            {
                "cluster_id": "C001",
                "theme_name": "屏幕颜色异常判定",
                "member_atomic_ids": list(atomic_ids),
                "scope_consistent": True,
                "object_consistent": True,
                "judgment_target_consistent": True,
                "standard_path_consistent": True,
                "threshold_exception_consistent": True,
                "shared_knowledge_definition": "判断屏幕颜色异常是否属于色斑。",
                "merge_basis": "适用范围、对象、目标、路径和阈值例外均一致。",
            }
        ],
        "split_requests": [],
        "review_requests": [],
    }


def test_atomic_cluster_validation_accepts_one_to_many_cluster() -> None:
    result = _validate_atomic_topic_clusters(
        _valid_cluster_result("A", "B", "C"),
        {"A", "B", "C"},
    )

    assert result["clusters"][0]["member_atomic_ids"] == ["A", "B", "C"]


def test_atomic_cluster_payload_uses_chat_and_ignores_transfer_description() -> None:
    unit = _unit("CHAT-PRIMARY")
    unit["source_conversation"] = (
        "26/07/15 18:05:00:00 问题类型：质检问题 "
        "问题描述：IMEI号全是0怎么判 "
        "转人工原因：问题复杂不确定怎么问\n"
        "26/07/15 18:05:51:51 屏幕边缘发红是不是算老化\n"
        "26/07/15 18:06:20:20 后壳有保护壳留下的印怎么判"
    )
    unit["evidence_summary"] = "真实聊天包含屏幕显示和后壳外观两个问题。"

    payload = _atomic_unit_payload(unit)

    assert "IMEI号全是0" not in payload["conversation_evidence_excerpt"]
    assert "屏幕边缘发红" in payload["conversation_evidence_excerpt"]
    assert "后壳有保护壳留下的印" in payload["conversation_evidence_excerpt"]
    assert payload["evidence_summary"] == "真实聊天包含屏幕显示和后壳外观两个问题。"


def test_atomic_cluster_validation_rejects_failed_hard_rule_flag() -> None:
    payload = _valid_cluster_result("A", "B")
    payload["clusters"][0]["standard_path_consistent"] = False

    with pytest.raises(MimoError, match="五项一致性"):
        _validate_atomic_topic_clusters(payload, {"A", "B"})


def test_atomic_cluster_validation_rejects_duplicate_or_missing_ids() -> None:
    payload = _valid_cluster_result("A")
    payload["review_requests"] = [
        {
            "atomic_id": "A",
            "review_type": "标准路径",
            "reason": "标准路径不清晰",
        }
    ]

    with pytest.raises(MimoError, match="重复分配"):
        _validate_atomic_topic_clusters(payload, {"A", "B"})


def test_hard_buckets_separate_platform_and_problem_type() -> None:
    ios = _unit(
        "IOS",
        scope_type="平台专用",
        platform="iOS",
    )
    android = _unit(
        "ANDROID",
        scope_type="平台专用",
        platform="Android",
    )
    appearance = _unit(
        "APPEARANCE",
        category_l1="外观问题",
        category_l2="划痕",
        subject="后盖",
    )

    buckets, review_units = _bucket_units([ios, android, appearance])

    assert not review_units
    assert len(buckets) == 3


def test_uncertain_units_go_to_review_instead_of_automatic_cluster() -> None:
    clear_a = _unit("A")
    clear_b = _unit("B")
    uncertain = _unit(
        "REVIEW",
        standard_path="待确认",
        requires_review=True,
    )
    units = [clear_a, clear_b, uncertain]
    buckets, review_units = _bucket_units(units)
    bucket = buckets[0]
    cache = {
        bucket["bucket_id"]: {
            "status": "ok",
            "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
            "atomic_ids": bucket["atomic_ids"],
            "candidate": _valid_cluster_result("A", "B"),
        }
    }

    output = _build_output(
        input_path=Path("input.json"),
        units=units,
        buckets=buckets,
        review_units=review_units,
        cache=cache,
    )

    assert output["metadata"]["status"] == "complete"
    assert output["clusters"][0]["member_atomic_ids"] == ["A", "B"]
    assert output["review_requests"][0]["atomic_id"] == "REVIEW"
    assert output["metadata"]["duplicate_assignment_count"] == 0


def test_validation_mode_can_cluster_review_flagged_unit_when_core_fields_are_clear() -> None:
    review_flagged = _unit(
        "REVIEW",
        standard_path="待确认",
        requires_review=True,
    )

    buckets, review_units = _bucket_units(
        [review_flagged],
        include_review_flagged=True,
    )

    assert len(buckets) == 1
    assert not review_units


def test_program_gate_rejects_different_secondary_categories() -> None:
    unit_by_id = {
        "A": _unit("A", category_l2="屏幕颜色异常"),
        "B": _unit("B", category_l2="屏生线"),
    }

    reasons = _cluster_program_conflicts(
        _valid_cluster_result("A", "B")["clusters"][0],
        unit_by_id,
    )

    assert "知识二级分类不同" in reasons


def test_program_gate_rejects_different_product_categories() -> None:
    unit_by_id = {
        "PHONE": _unit("PHONE", product_category="手机"),
        "TABLET": _unit("TABLET", product_category="平板"),
    }

    reasons = _cluster_program_conflicts(
        _valid_cluster_result("PHONE", "TABLET")["clusters"][0],
        unit_by_id,
    )

    assert "产品品类不同" in reasons


def test_program_gate_rejects_different_thresholds() -> None:
    unit_by_id = {
        "A": _unit("A", threshold_or_exception="无明确阈值"),
        "B": _unit("B", threshold_or_exception="直径大于0.5mm"),
    }

    reasons = _cluster_program_conflicts(
        _valid_cluster_result("A", "B")["clusters"][0],
        unit_by_id,
    )

    assert "阈值或例外条件不同" in reasons


def test_mimo_client_directly_clusters_complete_bucket() -> None:
    client = MimoClient(
        MimoConfig(
            api_key="test",
            base_url="https://example.com/v1",
            model="mimo-test",
        )
    )
    client._post = lambda _payload: {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        _valid_cluster_result("A", "B"),
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    result = client.cluster_atomic_units([_unit("A"), _unit("B")])

    assert result.candidate["clusters"][0]["member_atomic_ids"] == ["A", "B"]


def test_failed_large_bucket_falls_back_to_chunked_clustering() -> None:
    class FakeConfig:
        model = "mimo-test"

    class FakeResult:
        def __init__(self, atomic_ids: list[str]) -> None:
            self.candidate = _valid_cluster_result(*atomic_ids)
            self.request_audit = {}
            self.response_audit = {}

    class FakeClient:
        config = FakeConfig()

        def __init__(self) -> None:
            self.calls = 0

        def cluster_atomic_units(self, units):
            self.calls += 1
            if self.calls == 1:
                raise MimoError("整桶输出遗漏ID")
            return FakeResult([unit["unit_id"] for unit in units])

    units = [_unit(f"A{index:02d}") for index in range(12)]
    bucket = {
        "bucket_id": "B-TEST",
        "bucket_key": ["手机"],
        "atomic_ids": [unit["unit_id"] for unit in units],
        "units": units,
    }

    result = _analyze_bucket(FakeClient(), bucket)

    assert result["status"] == "ok"
    assert result["fallback_mode"] == "chunked_after_direct_failure"
    assert {
        atomic_id
        for cluster in result["candidate"]["clusters"]
        for atomic_id in cluster["member_atomic_ids"]
    } == set(bucket["atomic_ids"])
