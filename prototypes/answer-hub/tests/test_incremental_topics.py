from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

import answer_hub.topic_registry as topic_registry_module
from answer_hub.audit import AuditStore
from answer_hub.mimo import MimoLabelResult
from answer_hub.topic_registry import TopicRegistry
from answer_hub.workflow import build_topic_review_rows


def _row(
    work_order_id: str,
    *,
    product_type: str = "手机",
    business_line: str = "自营回收",
    subject: str = "手机外壳",
    phenomenon: str = "碎裂",
) -> dict[str, str]:
    return {
        "数据ID": work_order_id,
        "工单ID": work_order_id,
        "原始工单ID": work_order_id,
        "回收业务层级": business_line,
        "产品类型": product_type,
        "聊天内容": f"{subject}{phenomenon}应该怎么判定？",
        "核心问题": f"{subject}{phenomenon}如何判定",
        "模型主题一级分类": "外观问题",
        "模型主题二级分类": "外壳外观",
        "问题意图": "标准判定",
        "对象/部位": subject,
        "异常现象": phenomenon,
        "解题方式": "按外观质检口径判定",
        "_原子知识ID": f"{work_order_id}-U1",
    }


class _HighConfidenceDirectMimo:
    config = SimpleNamespace(model="incremental-topic-test")

    def analyze_cluster_units(
        self,
        row: dict[str, str],
    ) -> MimoLabelResult:
        return MimoLabelResult(
            candidate={
                "conversation_type": "single_topic",
                "reason": "会话只有一个清晰问题。",
                "topics": [
                    {
                        "normalized_issue": row["核心问题"],
                        "product_category": row["产品类型"],
                        "scope_type": "品类专用",
                        "platform": "通用",
                        "brand": "通用",
                        "model_scope": "通用",
                        "category_l1": row["模型主题一级分类"],
                        "category_l2": row["模型主题二级分类"],
                        "intent": row["问题意图"],
                        "subject": row["对象/部位"],
                        "phenomenon": row["异常现象"],
                        "judgment_target": row["核心问题"],
                        "resolution_mode": row["解题方式"],
                        "standard_path": row["核心问题"],
                        "threshold_or_exception": "无明确阈值",
                        "evidence_summary": row["聊天内容"],
                        "confidence": 0.95,
                        "requires_review": False,
                    }
                ],
            },
            request_audit={},
            response_audit={},
        )

    def cluster_atomic_units(self, _units):
        raise AssertionError("清晰单原子问题不应额外调用聚类模型")


def test_registry_appends_compatible_new_evidence_to_existing_topic(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    first = registry.integrate(
        proposed_topic_id="TOP-FIRST",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-001", phenomenon="碎裂")],
        run_id="run-1",
    )
    second = registry.integrate(
        proposed_topic_id="TOP-SECOND",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-002", phenomenon="掉漆")],
        run_id="run-2",
    )

    assert first.topic_id == "TOP-FIRST"
    assert second.topic_id == "TOP-FIRST"
    assert second.matched_existing is True
    assert second.added_member_count == 1
    assert second.evidence_version == 2
    assert {
        row["原始工单ID"]
        for row in second.rows
    } == {"WO-001", "WO-002"}


def test_registry_never_merges_different_objects_in_same_standard_family(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    rear_housing = registry.integrate(
        proposed_topic_id="TOP-REAR-HOUSING",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[
            _row(
                "WO-REAR-HOUSING",
                subject="手机后壳",
                phenomenon="磕碰",
            )
        ],
        run_id="run-rear-housing",
    )
    frame = registry.integrate(
        proposed_topic_id="TOP-FRAME",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-2"),
        rows=[
            _row(
                "WO-FRAME",
                subject="手机中框",
                phenomenon="划痕",
            )
        ],
        run_id="run-frame",
    )

    assert frame.topic_id != rear_housing.topic_id
    assert frame.matched_existing is False
    assert {
        row["原始工单ID"]
        for row in frame.rows
    } == {"WO-FRAME"}


def test_historical_similarity_rejects_object_mismatch_before_shared_target() -> None:
    base_signature = {
        "standard_families": [],
        "merge_policies": [],
        "phenomenon_values": [],
        "query_targets": ["phone_housing_appearance"],
        "detection_targets": [],
        "platforms": [],
        "brands": [],
        "model_scopes": [],
        "threshold_values": [],
        "topic_text": "手机外观问题",
    }
    current = {
        **base_signature,
        "object_keys": ["外壳"],
    }
    historical = {
        **base_signature,
        "standard_families": ["手机外壳外观标准"],
        "merge_policies": ["samestandardfamily"],
        "object_keys": ["后置摄像头"],
    }

    similarity, reason = topic_registry_module._signature_similarity(
        current,
        historical,
    )

    assert similarity == 0.0
    assert "对象不同" in reason


def test_registry_never_merges_different_product_categories(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    phone = registry.integrate(
        proposed_topic_id="TOP-PHONE",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-PHONE", product_type="手机")],
        run_id="run-phone",
    )
    tablet = registry.integrate(
        proposed_topic_id="TOP-TABLET",
        topic_key=("direct_mimo", "自营回收", "平板电脑", "cluster-1"),
        rows=[_row("WO-TABLET", product_type="平板电脑")],
        run_id="run-tablet",
    )

    assert phone.topic_id == "TOP-PHONE"
    assert tablet.topic_id == "TOP-TABLET"
    assert tablet.matched_existing is False


def test_registry_rejects_atomic_evidence_owned_by_another_topic(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    registry = TopicRegistry(audit)
    row = _row("WO-OWNED-EVIDENCE", product_type="手机")

    registry.integrate(
        proposed_topic_id="TOP-PHONE-OWNER",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[row],
        run_id="run-phone-owner",
    )
    conflicting_row = dict(row)
    conflicting_row["产品类型"] = "平板电脑"

    with pytest.raises(ValueError, match="已属于其他历史主题"):
        registry.integrate(
            proposed_topic_id="TOP-TABLET-CONFLICT",
            topic_key=("direct_mimo", "自营回收", "平板电脑", "cluster-1"),
            rows=[conflicting_row],
            run_id="run-tablet-conflict",
        )

    assert audit.list_registered_topics("自营回收", "平板电脑") == []


def test_registry_never_merges_different_business_lines(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    self_operated = registry.integrate(
        proposed_topic_id="TOP-SELF-OPERATED",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-SELF-OPERATED", business_line="自营回收")],
        run_id="run-self-operated",
    )
    aggregate = registry.integrate(
        proposed_topic_id="TOP-AGGREGATE",
        topic_key=("direct_mimo", "聚合回收", "手机", "cluster-1"),
        rows=[_row("WO-AGGREGATE", business_line="聚合回收")],
        run_id="run-aggregate",
    )

    assert self_operated.topic_id == "TOP-SELF-OPERATED"
    assert aggregate.topic_id == "TOP-AGGREGATE"
    assert aggregate.matched_existing is False


def test_registry_keeps_separate_by_phenomenon_rules_apart(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    color_spot = registry.integrate(
        proposed_topic_id="TOP-COLOR-SPOT",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[
            _row(
                "WO-COLOR-SPOT",
                subject="手机屏幕",
                phenomenon="色斑",
            )
        ],
        run_id="run-color-spot",
    )
    dead_pixel = registry.integrate(
        proposed_topic_id="TOP-DEAD-PIXEL",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-2"),
        rows=[
            _row(
                "WO-DEAD-PIXEL",
                subject="手机屏幕",
                phenomenon="坏点",
            )
        ],
        run_id="run-dead-pixel",
    )

    assert color_spot.topic_id == "TOP-COLOR-SPOT"
    assert dead_pixel.topic_id == "TOP-DEAD-PIXEL"
    assert dead_pixel.matched_existing is False


def test_registry_rejects_mixed_known_and_unknown_product_rows(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    with pytest.raises(ValueError, match="产品品类"):
        registry.integrate(
            proposed_topic_id="TOP-MIXED-PRODUCT",
            topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
            rows=[
                _row("WO-KNOWN", product_type="手机"),
                _row("WO-UNKNOWN", product_type="未知新品类"),
            ],
            run_id="run-mixed-product",
        )


def test_registry_rejects_internal_separate_by_phenomenon_conflict(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))

    with pytest.raises(ValueError, match="现象值"):
        registry.integrate(
            proposed_topic_id="TOP-MIXED-SCREEN",
            topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
            rows=[
                _row(
                    "WO-COLOR-SPOT-INTERNAL",
                    subject="手机屏幕",
                    phenomenon="色斑",
                ),
                _row(
                    "WO-DEAD-PIXEL-INTERNAL",
                    subject="手机屏幕",
                    phenomenon="坏点",
                ),
            ],
            run_id="run-mixed-screen",
        )


def test_registry_reimport_is_idempotent_for_the_same_atomic_evidence(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))
    row = _row("WO-RETRY", phenomenon="磨损")

    first = registry.integrate(
        proposed_topic_id="TOP-RETRY",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[row],
        run_id="run-first",
    )
    retried = registry.integrate(
        proposed_topic_id="TOP-RETRY-AGAIN",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[dict(row)],
        run_id="run-retry",
    )

    assert retried.topic_id == first.topic_id
    assert retried.added_member_count == 0
    assert retried.duplicate_member_count == 1
    assert retried.evidence_version == first.evidence_version
    assert len(retried.rows) == 1


def test_registry_does_not_remerge_different_atomics_from_same_work_order(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))
    first_row = _row("WO-MULTI", phenomenon="碎裂")
    first_row["_原子知识ID"] = "WO-MULTI-U1"
    second_row = _row("WO-MULTI", phenomenon="掉漆")
    second_row["_原子知识ID"] = "WO-MULTI-U2"

    first = registry.integrate(
        proposed_topic_id="TOP-MULTI-1",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[first_row],
        run_id="run-multi-1",
    )
    second = registry.integrate(
        proposed_topic_id="TOP-MULTI-2",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-2"),
        rows=[second_row],
        run_id="run-multi-2",
    )

    assert second.topic_id != first.topic_id
    assert second.matched_existing is False


def test_registry_respects_explicit_model_scope_differences(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))
    first_row = _row("WO-MODEL-15", phenomenon="碎裂")
    first_row["_原子机型范围"] = "iPhone 15"
    second_row = _row("WO-MODEL-16", phenomenon="掉漆")
    second_row["_原子机型范围"] = "iPhone 16"

    first = registry.integrate(
        proposed_topic_id="TOP-MODEL-15",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[first_row],
        run_id="run-model-15",
    )
    second = registry.integrate(
        proposed_topic_id="TOP-MODEL-16",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-2"),
        rows=[second_row],
        run_id="run-model-16",
    )

    assert second.topic_id != first.topic_id
    assert second.matched_existing is False


def test_registry_rejects_modified_original_work_order_id(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))
    row = _row("WO-ORIGINAL")
    row["工单ID"] = "WO-MODIFIED"

    with pytest.raises(ValueError, match="原始工单ID"):
        registry.integrate(
            proposed_topic_id="TOP-WORK-ORDER-MISMATCH",
            topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
            rows=[row],
            run_id="run-work-order-mismatch",
        )


def test_workflow_reuses_topic_id_and_accumulates_original_work_orders(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")

    first_topics, _first_mapping, _first_gaps, _first_pending = (
        build_topic_review_rows(
            [_row("WO-WORKFLOW-001", phenomenon="碎裂")],
            use_mimo=False,
            mimo_client=_HighConfidenceDirectMimo(),
            audit_store=audit,
            run_id="run-workflow-1",
            clustering_mode="direct_mimo",
            use_standard_references=False,
            enforce_cluster_admission=True,
        )
    )
    second_topics, second_mapping, _second_gaps, _second_pending = (
        build_topic_review_rows(
            [_row("WO-WORKFLOW-002", phenomenon="掉漆")],
            use_mimo=False,
            mimo_client=_HighConfidenceDirectMimo(),
            audit_store=audit,
            run_id="run-workflow-2",
            clustering_mode="direct_mimo",
            use_standard_references=False,
            enforce_cluster_admission=True,
        )
    )

    assert second_topics[0]["主题ID"] == first_topics[0]["主题ID"]
    assert second_topics[0]["主题样本数"] == 2
    assert set(second_topics[0]["主题工单ID"].splitlines()) == {
        "WO-WORKFLOW-001",
        "WO-WORKFLOW-002",
    }
    assert {
        row["原始工单ID"]
        for row in second_mapping
    } == {"WO-WORKFLOW-001", "WO-WORKFLOW-002"}


def test_workflow_does_not_write_history_when_admission_is_disabled(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")

    build_topic_review_rows(
        [_row("WO-NO-ADMISSION")],
        use_mimo=False,
        audit_store=audit,
        run_id="run-no-admission",
        clustering_mode="rule",
        use_standard_references=False,
        enforce_cluster_admission=False,
    )

    assert audit.list_registered_topics("自营回收", "手机") == []


def test_workflow_cluster_admission_rejects_mixed_unknown_product(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")

    topics, _mapping, _gaps, pending = build_topic_review_rows(
        [
            _row("WO-ADMISSION-KNOWN", product_type="手机"),
            _row("WO-ADMISSION-UNKNOWN", product_type="未知新品类"),
        ],
        use_mimo=False,
        audit_store=audit,
        run_id="run-admission-mixed-product",
        clustering_mode="rule",
        use_standard_references=False,
        enforce_cluster_admission=True,
    )

    assert topics == []
    assert len(pending) == 2
    unknown_pending = next(
        row
        for row in pending
        if row["原始工单ID"] == "WO-ADMISSION-UNKNOWN"
    )
    assert "产品品类" in unknown_pending["待聚合原因"]


def test_workflow_routes_clear_chat_and_human_core_conflict_to_review(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    conflicted_row = _row(
        "WO-STRUCTURED-CHAT-CONFLICT",
        subject="手机后壳",
        phenomenon="磕碰",
    )
    conflicted_row.update(
        {
            "聊天内容": "手机屏幕出现色斑应该怎么判定？",
            "核心问题": "手机后壳磕碰如何判定",
            "判定结论": "后壳磕碰按外观质检口径处理",
        }
    )

    topics, _mapping, _gaps, pending = build_topic_review_rows(
        [conflicted_row],
        use_mimo=False,
        mimo_client=_HighConfidenceDirectMimo(),
        audit_store=audit,
        run_id="run-structured-chat-conflict",
        clustering_mode="direct_mimo",
        use_standard_references=False,
        enforce_cluster_admission=True,
    )

    assert topics == []
    assert len(pending) == 1
    assert "人工校正核心问题与完整聊天" in pending[0]["待聚合原因"]
    assert audit.list_registered_topics("自营回收", "手机") == []


def test_workflow_ignores_transfer_metadata_when_checking_evidence_conflicts(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    row = _row(
        "WO-TRANSFER-METADATA",
        subject="手机后壳",
        phenomenon="磕碰",
    )
    row["聊天内容"] = (
        "问题描述：手机屏幕色斑怎么判定\n"
        "用户：手机后壳有磕碰应该怎么判定？"
    )

    topics, _mapping, _gaps, pending = build_topic_review_rows(
        [row],
        use_mimo=False,
        mimo_client=_HighConfidenceDirectMimo(),
        audit_store=audit,
        run_id="run-transfer-metadata",
        clustering_mode="direct_mimo",
        use_standard_references=False,
        enforce_cluster_admission=True,
    )

    assert len(topics) == 1
    assert all(
        "人工校正核心问题与完整聊天" not in row["待聚合原因"]
        for row in pending
    )
    assert audit.list_registered_topics("自营回收", "手机")


def test_registry_bootstraps_existing_audit_candidates_before_matching(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    audit.save_candidate(
        model_run_id="legacy-model-run",
        run_id="legacy-run",
        record_id="TOP-LEGACY",
        candidate={
            "主题ID": "TOP-LEGACY",
            "回收业务层级": "自营回收",
            "适用范围": "手机",
            "聚类准入状态": "已自动放行",
            "聚类准入置信度": 0.92,
            "主题问题意图": "标准判定",
            "主题对象/部位": "手机外壳",
            "主题异常现象": "碎裂",
            "主题解题方式": "按外观质检口径判定",
            "主题事实证据包": json.dumps(
                {
                    "facts": [
                        {
                            "source_record_id": "LEGACY-RECORD",
                            "work_order_id": "WO-LEGACY",
                            "atomic_question": "手机外壳碎裂如何判定",
                            "human_core_problem": "手机外壳碎裂如何判定",
                            "human_judgment_conclusion": "按外壳外观口径判定",
                            "conversation_excerpt": "手机外壳碎裂应该怎么判定？",
                            "historical_actual_reply": "",
                            "judgment_basis": "",
                            "image_urls": [],
                            "image_usable": False,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        },
    )

    resolved = TopicRegistry(audit).integrate(
        proposed_topic_id="TOP-NEW",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-NEW", phenomenon="掉漆")],
        run_id="run-new",
    )

    assert resolved.topic_id == "TOP-LEGACY"
    assert resolved.matched_existing is True
    assert {
        row["原始工单ID"]
        for row in resolved.rows
    } == {"WO-LEGACY", "WO-NEW"}


def test_registry_does_not_bootstrap_unverified_review_pending_candidate(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    audit.save_candidate(
        model_run_id="legacy-unverified-model-run",
        run_id="legacy-unverified-run",
        record_id="TOP-LEGACY-UNVERIFIED",
        candidate={
            "主题ID": "TOP-LEGACY-UNVERIFIED",
            "回收业务层级": "自营回收",
            "适用范围": "手机",
            "主题问题意图": "标准判定",
            "主题对象/部位": "手机外壳",
            "主题异常现象": "碎裂",
            "主题解题方式": "按外观质检口径判定",
            "主题事实证据包": json.dumps(
                {
                    "facts": [
                        {
                            "source_record_id": "LEGACY-UNVERIFIED",
                            "work_order_id": "WO-LEGACY-UNVERIFIED",
                            "atomic_question": "手机外壳碎裂如何判定",
                            "human_core_problem": "手机外壳碎裂如何判定",
                            "human_judgment_conclusion": "待人工确认",
                            "conversation_excerpt": "手机外壳碎裂应该怎么判定？",
                            "image_urls": [],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        },
    )

    resolved = TopicRegistry(audit).integrate(
        proposed_topic_id="TOP-VERIFIED-NEW",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-VERIFIED-NEW", phenomenon="掉漆")],
        run_id="run-verified-new",
    )

    assert resolved.topic_id == "TOP-VERIFIED-NEW"
    assert resolved.matched_existing is False


def test_registry_updates_signature_after_new_compatible_evidence(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    registry = TopicRegistry(audit)

    registry.integrate(
        proposed_topic_id="TOP-SIGNATURE",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[_row("WO-SIGNATURE-1", phenomenon="碎裂")],
        run_id="run-signature-1",
    )
    registry.integrate(
        proposed_topic_id="TOP-SIGNATURE-NEW",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-2"),
        rows=[_row("WO-SIGNATURE-2", phenomenon="掉漆")],
        run_id="run-signature-2",
    )

    stored = audit.list_registered_topics("自营回收", "手机")[0]
    assert set(stored["signature"]["phenomenon_values"]) == {
        "碎裂",
        "掉漆",
    }


def test_registry_serializes_concurrent_compatible_topic_creation(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    registries = [TopicRegistry(audit) for _index in range(8)]
    barrier = threading.Barrier(len(registries))

    def integrate(index: int) -> str:
        barrier.wait()
        resolution = registries[index].integrate(
            proposed_topic_id=f"TOP-CONCURRENT-{index}",
            topic_key=(
                "direct_mimo",
                "自营回收",
                "手机",
                f"cluster-{index}",
            ),
            rows=[
                _row(
                    f"WO-CONCURRENT-{index}",
                    phenomenon="碎裂" if index % 2 == 0 else "掉漆",
                )
            ],
            run_id=f"run-concurrent-{index}",
        )
        return resolution.topic_id

    with ThreadPoolExecutor(max_workers=len(registries)) as executor:
        topic_ids = list(executor.map(integrate, range(len(registries))))

    assert len(set(topic_ids)) == 1
    topics = audit.list_registered_topics("自营回收", "手机")
    assert len(topics) == 1
    assert topics[0]["member_count"] == len(registries)


def test_registry_does_not_merge_unrelated_topics_on_proposed_id_collision(
    tmp_path: Path,
) -> None:
    registry = TopicRegistry(AuditStore(tmp_path / "audit.db"))
    speaker = _row(
        "WO-SPEAKER",
        subject="扬声器",
        phenomenon="声音小",
    )
    bluetooth = _row(
        "WO-BLUETOOTH",
        subject="蓝牙",
        phenomenon="连接失败",
    )

    first = registry.integrate(
        proposed_topic_id="TOP-COLLISION",
        topic_key=("rule", "自营回收", "手机", "shared-key"),
        rows=[speaker],
        run_id="run-speaker",
    )
    second = registry.integrate(
        proposed_topic_id="TOP-COLLISION",
        topic_key=("rule", "自营回收", "手机", "shared-key"),
        rows=[bluetooth],
        run_id="run-bluetooth",
    )

    assert first.topic_id == "TOP-COLLISION"
    assert second.topic_id != first.topic_id
    assert second.matched_existing is False
    assert {
        row["原始工单ID"]
        for row in second.rows
    } == {"WO-BLUETOOTH"}


def test_registry_routes_uncertain_historical_match_to_manual_review(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    registry = TopicRegistry(audit)
    first_row = _row(
        "WO-SPEAKER-OLD",
        subject="扬声器",
        phenomenon="声音偏小",
    )
    first_row["核心问题"] = "扬声器声音偏小如何判断"
    first_row["聊天内容"] = "扬声器声音偏小如何判断？"
    first_row["模型主题一级分类"] = "功能问题"
    first_row["模型主题二级分类"] = "功能异常"
    first_row["问题意图"] = "检测核验"
    first_row["解题方式"] = "现场功能测试"
    second_row = _row(
        "WO-SPEAKER-NEW",
        subject="扬声器",
        phenomenon="声音很小",
    )
    second_row["核心问题"] = "扬声器声音很小怎么检测"
    second_row["聊天内容"] = "扬声器声音很小怎么检测？"
    second_row["模型主题一级分类"] = "功能问题"
    second_row["模型主题二级分类"] = "功能异常"
    second_row["问题意图"] = "检测核验"
    second_row["解题方式"] = "现场功能测试"

    registry.integrate(
        proposed_topic_id="TOP-SPEAKER-OLD",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-1"),
        rows=[first_row],
        run_id="run-speaker-old",
    )
    uncertain = registry.integrate(
        proposed_topic_id="TOP-SPEAKER-NEW",
        topic_key=("direct_mimo", "自营回收", "手机", "cluster-2"),
        rows=[second_row],
        run_id="run-speaker-new",
    )

    assert uncertain.requires_review is True
    assert uncertain.decision == "historical_topic_review_required"
    assert uncertain.added_member_count == 0
    assert len(
        audit.list_registered_topics("自营回收", "手机")
    ) == 1


def test_workflow_stops_uncertain_historical_match_before_transcription(
    tmp_path: Path,
) -> None:
    audit = AuditStore(tmp_path / "audit.db")
    old_row = _row(
        "WO-WORKFLOW-SPEAKER-OLD",
        subject="扬声器",
        phenomenon="声音偏小",
    )
    old_row.update(
        {
            "核心问题": "扬声器声音偏小如何判断",
            "聊天内容": "扬声器声音偏小如何判断？",
            "模型主题一级分类": "功能问题",
            "模型主题二级分类": "功能异常",
            "问题意图": "检测核验",
            "解题方式": "现场功能测试",
        }
    )
    new_row = _row(
        "WO-WORKFLOW-SPEAKER-NEW",
        subject="扬声器",
        phenomenon="声音很小",
    )
    new_row.update(
        {
            "核心问题": "扬声器声音很小怎么检测",
            "聊天内容": "扬声器声音很小怎么检测？",
            "模型主题一级分类": "功能问题",
            "模型主题二级分类": "功能异常",
            "问题意图": "检测核验",
            "解题方式": "现场功能测试",
        }
    )

    build_topic_review_rows(
        [old_row],
        use_mimo=False,
        mimo_client=_HighConfidenceDirectMimo(),
        audit_store=audit,
        run_id="run-workflow-speaker-old",
        clustering_mode="direct_mimo",
        use_standard_references=False,
        enforce_cluster_admission=True,
    )
    topics, _mapping, _gaps, pending = build_topic_review_rows(
        [new_row],
        use_mimo=False,
        mimo_client=_HighConfidenceDirectMimo(),
        audit_store=audit,
        run_id="run-workflow-speaker-new",
        clustering_mode="direct_mimo",
        use_standard_references=False,
        enforce_cluster_admission=True,
    )

    assert topics == []
    assert len(pending) == 1
    assert pending[0]["待聚合状态"] == "pending_historical_topic_review"
    assert "主题结构化文本相似度匹配" in pending[0]["待聚合原因"]
