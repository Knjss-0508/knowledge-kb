from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3

from openpyxl import Workbook
import pytest

from answer_hub.audit import AuditStore
from answer_hub.catalog import StandardCatalogItem, load_standard_catalog
from answer_hub.images import ImageEvidence
from answer_hub.mimo import (
    MimoClient,
    MimoConfig,
    MimoError,
    MimoLabelResult,
    _topic_signal_source_payload,
    _validate_topic_review,
)
from answer_hub.workflow import (
    build_cluster_validation_rows,
    build_topic_review_rows,
    cluster_validation_from_workbook,
    evaluate_cluster_validation_rows,
    generate_phone_candidate_rows,
    initial_label_rows,
    preprocess_source_rows,
)


def _source_rows() -> list[dict[str, object]]:
    return preprocess_source_rows(
        [
            {
                "序号": 1,
                "工单ID": "PHONE-001",
                "聊天内容": "屏幕有色斑，麻烦确认是否属于显示问题。",
                "图片链接": "https://example.com/phone.jpg",
                "视频链接": "https://example.com/phone.mp4",
                "核心问题": "手机屏幕色斑如何判定",
                "判定结论": "判定为色斑",
                "判定依据": "色斑属于显示问题",
                "产品类型": "手机",
                "一级分类": "显示问题",
                "二级分类": "色斑",
                "参考话术": "请拍摄清晰屏幕图片",
            }
        ]
    )


def test_case_only_topic_signal_payload_treats_historical_reply_as_primary_evidence() -> None:
    payload = _topic_signal_source_payload(_source_rows()[0])

    assert payload["primary_evidence"]["historical_actual_reply"] == "请拍摄清晰屏幕图片"
    assert "legacy_script" not in payload["legacy_reference_only"]


def test_case_only_topic_generation_rejects_model_standard_references() -> None:
    features, _ = generate_phone_candidate_rows(
        _source_rows(),
        [],
        use_mimo=False,
        image_downloader=_ReadyImageDownloader(),
        use_standard_references=False,
    )
    topics, _mapping, gaps, pending = build_topic_review_rows(
        features,
        [],
        mimo_client=_FakeMimo(),
        clustering_mode="rule",
        use_standard_references=False,
    )

    assert len(topics) == 1
    assert not gaps
    assert not pending
    assert topics[0]["模型阶段状态"] == "topic_model_failed"
    assert "标准引用" in topics[0]["校验备注"]
    assert "质检标准" not in topics[0]["知识内容"]
    assert topics[0]["关联标准项"] == ""


def test_case_only_review_ignores_model_request_for_standard_reference() -> None:
    class StandardDependentReviewMimo(_FakeMimo):
        def label_topic(self, _topic, _matches, use_standard_references=False):
            assert use_standard_references is False
            return MimoLabelResult(
                candidate={
                    "title": "屏幕显示异常如何通过图片核验",
                    "subtitles": [],
                    "content": (
                        "核验流程：\n"
                        "1. 确认异常出现的画面和位置。\n"
                        "2. 补充白屏全景与异常位置近景。\n"
                        "3. 排除反光、贴膜和环境光干扰。\n"
                        "4. 信息不足时补充后再处理。"
                    ),
                    "category_l1": "显示问题",
                    "category_l2": "色斑",
                    "layer": "L2",
                    "knowledge_form": "流程方法",
                    "standard_refs": [],
                    "applicable_scope": "手机-通用",
                    "recommended_reply": "请补充白屏全景和异常位置近景，并排除反光或贴膜影响；信息不足时补充后再处理。",
                    "confidence": 0.82,
                    "reasoning_summary": "依据完整会话和案例图片形成核验流程。",
                    "needs_human_review": False,
                    "image_evidence_summary": "案例包含可用图片。",
                    "requires_images": True,
                    "image_usage_instruction": "保留脱敏案例图说明异常位置。",
                },
                request_audit={},
                response_audit={},
            )

        def review_topic(
            self,
            _topic,
            _draft,
            _matches,
            use_standard_references=False,
        ):
            assert use_standard_references is False
            return MimoLabelResult(
                candidate={
                    "decision": "证据不足待补充",
                    "knowledge_value": "待确认",
                    "error_type": "标准未覆盖/标准召回不足",
                    "reason": "缺少标准引用。",
                    "standard_consistency": "无可信标准",
                    "evidence_sufficiency": "不足",
                    "confidence": 0.9,
                    "priority_review": True,
                },
                request_audit={},
                response_audit={},
            )

    features, _ = generate_phone_candidate_rows(
        _source_rows(),
        [],
        use_mimo=False,
        image_downloader=_ReadyImageDownloader(),
        use_standard_references=False,
    )
    topics, _mapping, gaps, pending = build_topic_review_rows(
        features,
        [],
        mimo_client=StandardDependentReviewMimo(),
        clustering_mode="rule",
        use_standard_references=False,
    )

    assert len(topics) == 1
    assert not gaps
    assert not pending
    assert topics[0]["模型初标错误类型"] == ""
    assert topics[0]["模型初标结论"] != "证据不足待补充"
    assert "已忽略模型提出的标准补充要求" in topics[0]["模型初标原因"]


def _standards() -> list[StandardCatalogItem]:
    return [
        StandardCatalogItem(
            standard_id="PHONE-DISPLAY-001",
            title="手机屏幕色斑判定",
            category_l1="显示问题",
            category_l2="色斑",
            knowledge_type="场景判定",
            standard_path="【显示问题】-【色斑】",
            keywords=["手机", "屏幕", "色斑"],
            scope="适用于手机屏幕色斑异常",
            response_snippet="按色斑标准核验",
            status="published",
            version="v2026.07",
        )
    ]


class _ReadyImageDownloader:
    def fetch(self, _links: str) -> list[ImageEvidence]:
        return [
            ImageEvidence(
                url="https://example.com/phone.jpg",
                status="ready",
                mime_type="image/jpeg",
                byte_size=8,
                data_url="data:image/jpeg;base64,AA==",
            )
        ]


class _FailedImageDownloader:
    def fetch(self, _links: str) -> list[ImageEvidence]:
        return [ImageEvidence(url="https://example.com/phone.jpg", status="failed", error="timeout")]


class _FakeMimo:
    config = SimpleNamespace(model="mimo-v2.5-test")

    def analyze_topic_signal(self, _source, _matches, _images):
        return MimoLabelResult(
            candidate={
                "intent": "标准判定",
                "subject": "屏幕",
                "phenomenon": "色斑",
                "resolution_mode": "对照标准判定",
                "category_l1": "显示问题",
                "category_l2": "色斑",
                "topic_tags": ["意图:标准判定", "对象:屏幕", "现象:色斑", "处理:对照标准判定"],
                "standard_refs": ["PHONE-DISPLAY-001"],
                "requires_images": True,
                "image_evidence_summary": "图片已接收，仍需人工确认细节。",
                "reasoning_summary": "完整会话在询问屏幕色斑的标准判定。",
                "confidence": 0.91,
                "needs_human_review": False,
            },
            request_audit={"topic_signal": "test"},
            response_audit={"choices": []},
        )

    def label(self, _source, _matches, _images):
        return MimoLabelResult(
            candidate={
                "title": "手机屏幕色斑判定",
                "subtitles": ["屏幕有色斑怎么判"],
                "content": "按色斑标准核验；证据不足时待人工确认。",
                "category_l1": "显示问题",
                "category_l2": "色斑",
                "layer": "L2",
                "knowledge_form": "具体判定",
                "standard_refs": ["PHONE-DISPLAY-001"],
                "applicable_scope": "适用于手机屏幕色斑异常",
                "confidence": 0.91,
                "reasoning_summary": "会话与检索标准的显示问题/色斑一致。",
                "needs_human_review": False,
                "image_evidence_summary": "图片已接收，仍需人工确认细节。",
            },
            request_audit={"source": "test"},
            response_audit={"choices": []},
        )

    def label_topic(self, _topic, _matches):
        return MimoLabelResult(
            candidate={
                "title": "手机屏幕色斑如何通过图片核验",
                "subtitles": ["显示异常", "屏幕 / 显示异常"],
                "content": "核验流程：\n1. 明确异常所在屏幕区域。\n2. 补充清晰近景、全景及不同角度图片。\n3. 对照当前有效显示质检标准。\n4. 证据不足时重点复核并转人工。",
                "category_l1": "显示问题",
                "category_l2": "色斑",
                "layer": "L2",
                "knowledge_form": "流程方法",
                "standard_refs": [],
                "applicable_scope": "手机",
                "confidence": 0.65,
                "reasoning_summary": "显示问题通常需要结合现场图片和生效标准沉淀核验流程。",
                "needs_human_review": True,
                "image_evidence_summary": "聚合案例包含可用图片。",
            },
            request_audit={"topic": "test"},
            response_audit={"choices": []},
        )

    def review_topic(self, _topic, _draft, _matches):
        return MimoLabelResult(
            candidate={
                "decision": "通过",
                "knowledge_value": "值得沉淀",
                "error_type": "",
                "reason": "转写草稿已沉淀为流程型知识，标准引用和证据链可追溯。",
                "standard_consistency": "一致",
                "evidence_sufficiency": "充分",
                "confidence": 0.88,
                "priority_review": False,
            },
            request_audit={"review": "test"},
            response_audit={"choices": []},
        )

    def review_cluster_pair(self, _left, _right, similarity, threshold):
        decision = "同一主题" if similarity >= threshold else "不同主题"
        return MimoLabelResult(
            candidate={
                "decision": decision,
                "topic_label": "测试主题",
                "reason": "根据两条记录的意图、对象和处理目标进行判断。",
                "key_difference": "" if decision == "同一主题" else "处理目标不同",
                "confidence": 0.9,
            },
            request_audit={"cluster_pair": "test"},
            response_audit={"choices": []},
        )


class _FakeEmbedding:
    config = SimpleNamespace(model="semantic-cluster-test")

    def embed_texts(self, texts):
        assert len(texts) == 3
        return [
            [1.0, 0.0],
            [0.99, 0.01],
            [0.0, 1.0],
        ]


def test_mimo_cluster_units_keeps_clear_secondary_topics() -> None:
    client = MimoClient(
        MimoConfig(
            api_key="test",
            base_url="https://example.com/v1",
            model="mimo-v2.5-test",
        )
    )
    client._post = lambda _payload: {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "conversation_type": "multi_topic",
                            "reason": "会话同时咨询前摄像头状态和按键触发闪屏，需要不同知识回答。",
                            "topics": [
                                {
                                    "normalized_issue": "前摄像头疑似异物或更换的核验",
                                    "product_category": "手机",
                                    "scope_type": "品类专用",
                                    "platform": "通用",
                                    "brand": "通用",
                                    "model_scope": "通用",
                                    "category_l1": "功能问题",
                                    "category_l2": "摄像头功能",
                                    "intent": "检测核验",
                                    "subject": "前摄像头",
                                    "phenomenon": "疑似异物或更换",
                                    "judgment_target": "核验前摄像头是否存在异物或更换",
                                    "resolution_mode": "结合外观证据转人工核验",
                                    "standard_path": "摄像头功能核验",
                                    "threshold_or_exception": "待确认",
                                    "evidence_summary": "聊天明确询问前摄像头是否正常，但没有形成最终结论。",
                                    "confidence": 0.78,
                                    "requires_review": True,
                                },
                                {
                                    "normalized_issue": "按开机键触发屏幕闪烁的判定",
                                    "product_category": "手机",
                                    "scope_type": "品类专用",
                                    "platform": "通用",
                                    "brand": "通用",
                                    "model_scope": "通用",
                                    "category_l1": "显示问题",
                                    "category_l2": "屏幕闪烁",
                                    "intent": "标准判定",
                                    "subject": "屏幕",
                                    "phenomenon": "按开机键时闪烁",
                                    "judgment_target": "判断按键触发的屏幕闪烁是否属于显示异常",
                                    "resolution_mode": "对照闪屏标准判定",
                                    "standard_path": "屏幕显示异常判定",
                                    "threshold_or_exception": "仅按开机键时触发",
                                    "evidence_summary": "聊天和上游视频分析均确认按开机键时出现闪烁。",
                                    "confidence": 0.94,
                                    "requires_review": False,
                                },
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    result = client.analyze_cluster_units(
        {
            "工单ID": "PHONE-MULTI-001",
            "产品类型": "手机",
            "聊天内容": "前摄像头正常吗？另外只有按开机键时屏幕才闪。",
            "核心问题": "前摄像头与屏幕闪烁问题",
            "判定结论": "屏幕闪烁按显示问题处理",
            "上游媒体分析摘要": "视频确认按开机键时屏幕闪烁。",
        }
    )

    assert result.candidate["conversation_type"] == "multi_topic"
    assert len(result.candidate["topics"]) == 2
    assert result.candidate["topics"][0]["subject"] == "前摄像头"
    assert result.candidate["topics"][1]["subject"] == "屏幕"


def test_single_record_only_extracts_features_and_topic_model_saves_audit(tmp_path: Path) -> None:
    audit = AuditStore(tmp_path / "phone_mvp.db")
    features, run_id = generate_phone_candidate_rows(
        _source_rows(),
        _standards(),
        mimo_client=_FakeMimo(),
        audit_store=audit,
        image_downloader=_ReadyImageDownloader(),
    )

    feature = features[0]
    assert run_id
    assert feature["模型阶段状态"] == "topic_signal_labeled"
    assert feature["问题意图"] == "标准判定"
    assert feature["对象/部位"] == "屏幕"
    assert "意图:标准判定" in feature["主题标签"]
    assert not feature.get("模型知识内容")

    second = dict(feature)
    second["数据ID"] = "PHONE-002"
    second["工单ID"] = "PHONE-002"
    topics, mapping, gaps, pending = build_topic_review_rows(
        [feature, second],
        _standards(),
        mimo_client=_FakeMimo(),
        audit_store=audit,
        run_id=run_id,
    )
    assert len(topics) == 1
    assert len(mapping) == 2
    assert not gaps
    assert not pending
    assert topics[0]["主题模型提供方"] == "mimo"
    assert topics[0]["知识分类"] == "检测方法"
    assert topics[0]["是否重点复核"] == "是"
    assert topics[0]["模型初标提供方"] == "mimo"
    assert topics[0]["模型初标结论"] == "通过"
    assert topics[0]["模型初标是否值得沉淀"] == "值得沉淀"

    connection = sqlite3.connect(audit.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_records").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2
    finally:
        connection.close()


def test_topic_review_validation_requires_consistent_deposition_value() -> None:
    review = _validate_topic_review(
        {
            "decision": "通过",
            "knowledge_value": "值得沉淀",
            "error_type": "",
            "reason": "问题清楚、处理方式可复用。",
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "充分",
            "content_consistency": "一致",
            "image_necessity": "不需要",
            "title_quality": "清晰",
            "confidence": 0.9,
            "priority_review": False,
        }
    )

    assert review["knowledge_value"] == "值得沉淀"


def test_topic_review_validation_rejects_unworthy_pass_decision() -> None:
    with pytest.raises(MimoError, match="decision 必须为驳回"):
        _validate_topic_review(
            {
                "decision": "通过",
                "knowledge_value": "不值得沉淀",
                "error_type": "",
                "reason": "纯个案。",
                "standard_consistency": "无可信标准",
                "evidence_sufficiency": "充分",
                "content_consistency": "一致",
                "image_necessity": "不需要",
                "title_quality": "清晰",
                "confidence": 0.9,
                "priority_review": False,
            }
        )


def test_topic_signal_uses_conversation_over_legacy_question_and_categories() -> None:
    class ConversationFirstMimo(_FakeMimo):
        def analyze_topic_signal(self, _source, _matches, _images):
            return MimoLabelResult(
                candidate={
                    "intent": "信息查询",
                    "subject": "设备机型",
                    "phenomenon": "机型查询",
                    "resolution_mode": "信息查询与实物核对",
                    "category_l1": "基本情况",
                    "category_l2": "机型",
                    "topic_tags": ["意图:信息查询", "对象:设备机型", "现象:机型查询", "处理:信息查询与实物核对"],
                    "standard_refs": [],
                    "requires_images": False,
                    "image_evidence_summary": "无需依赖图片。",
                    "reasoning_summary": "完整会话在询问设备机型查询方法。",
                    "confidence": 0.9,
                    "needs_human_review": True,
                },
                request_audit={"topic_signal": "conversation-first"},
                response_audit={"choices": []},
            )

    rows = _source_rows()
    rows[0].update(
        {
            "聊天内容": "这台设备的机型怎么查？",
            "核心问题": "手机屏幕色斑如何判定",
            "一级分类": "显示问题",
            "二级分类": "色斑",
        }
    )
    features, _ = generate_phone_candidate_rows(
        rows,
        _standards(),
        mimo_client=ConversationFirstMimo(),
        image_downloader=_ReadyImageDownloader(),
    )

    feature = features[0]
    assert feature["问题意图"] == "信息查询"
    assert feature["对象/部位"] == "设备机型"
    assert feature["模型主题一级分类"] == "基本情况"
    assert feature["核心问题"] == "手机屏幕色斑如何判定"


def test_semantic_clustering_merges_similar_rows_and_keeps_singleton_topic() -> None:
    features, _ = generate_phone_candidate_rows(
        _source_rows(),
        _standards(),
        use_mimo=False,
        image_downloader=_ReadyImageDownloader(),
    )
    first = features[0]
    second = dict(first)
    second["数据ID"] = "PHONE-002"
    second["工单ID"] = "PHONE-002"
    third = dict(first)
    third["数据ID"] = "PHONE-003"
    third["工单ID"] = "PHONE-003"
    third["核心问题"] = "设备机型如何查询"
    third["问题意图"] = "信息查询"
    third["对象/部位"] = "机型"

    clustering_meta = {}
    topics, mapping, gaps, pending = build_topic_review_rows(
        [first, second, third],
        _standards(),
        use_mimo=False,
        clustering_mode="semantic",
        semantic_threshold=0.8,
        embedding_client=_FakeEmbedding(),
        clustering_meta=clustering_meta,
    )

    assert len(topics) == 2
    assert len(mapping) == 3
    assert not gaps
    assert not pending
    singleton = next(topic for topic in topics if topic["主题样本数"] == 1)
    assert singleton["主题来源记录ID"] == "PHONE-003"
    assert singleton["是否重点复核"] == "是"
    assert clustering_meta["effective_mode"] == "semantic"
    assert clustering_meta["model"] == "semantic-cluster-test"


def test_mimo_cluster_gate_rejects_false_merge_and_merges_confirmed_topic() -> None:
    class ClusterGateEmbedding:
        config = SimpleNamespace(model="semantic-cluster-test")

        def embed_texts(self, texts):
            assert len(texts) == 3
            return [
                [1.0, 0.0],
                [0.87, 0.49],
                [0.86, 0.51],
            ]

    class ClusterGateMimo:
        config = SimpleNamespace(model="mimo-cluster-gate-test")

        def review_cluster_pair(self, left, right, _similarity, _threshold):
            pair = {left["数据ID"], right["数据ID"]}
            decision = "同一主题" if pair == {"B", "C"} else "不同主题"
            return MimoLabelResult(
                candidate={
                    "decision": decision,
                    "topic_label": "测试主题",
                    "reason": "根据判定对象、问题意图和处理目标进行判断。",
                    "key_difference": "标准对象不同" if decision == "不同主题" else "",
                    "confidence": 0.9,
                },
                request_audit={"cluster_gate": "test"},
                response_audit={"choices": []},
            )

    rows = [
        {
            "数据ID": "A",
            "工单ID": "A",
            "聊天内容": "主板内部标签异常如何判定",
            "核心问题": "主板内部标签是否属于拆修痕迹",
            "判定依据": "需要结合主板内部痕迹核验",
            "产品类型": "手机",
            "一级分类": "拆修问题",
            "二级分类": "主板拆修",
            "问题意图": "痕迹核验",
            "对象/部位": "主板",
            "异常现象": "内部标签",
            "解题方式": "对照拆修标准",
            "主标准路径": "【拆修问题】-【主板拆修】",
        },
        {
            "数据ID": "B",
            "工单ID": "B",
            "聊天内容": "外壳防水标签变红是否需要判定",
            "核心问题": "外壳防水标签变红是否需要处理",
            "判定依据": "外壳标签不作为浸液判定依据",
            "产品类型": "手机",
            "一级分类": "浸液问题",
            "二级分类": "防水标",
            "问题意图": "异常核验",
            "对象/部位": "外壳",
            "异常现象": "防水标签变红",
            "解题方式": "对照浸液标准",
            "主标准路径": "【浸液问题】-【防水标】",
        },
        {
            "数据ID": "C",
            "工单ID": "C",
            "聊天内容": "手机外壳防水标发红怎么处理",
            "核心问题": "外壳防水标签变红是否需要处理",
            "判定依据": "外壳标签不作为浸液判定依据",
            "产品类型": "手机",
            "一级分类": "浸液问题",
            "二级分类": "防水标",
            "问题意图": "异常核验",
            "对象/部位": "外壳",
            "异常现象": "防水标签变红",
            "解题方式": "对照浸液标准",
            "主标准路径": "【浸液问题】-【防水标】",
        },
    ]
    clustering_meta: dict[str, object] = {}

    topics, mapping, gaps, pending = build_topic_review_rows(
        rows,
        use_mimo=False,
        mimo_client=ClusterGateMimo(),
        clustering_mode="semantic_mimo",
        semantic_threshold=0.84,
        cluster_review_floor=0.75,
        cluster_auto_merge_threshold=0.9999,
        cluster_review_limit=10,
        embedding_client=ClusterGateEmbedding(),
        clustering_meta=clustering_meta,
    )

    assert len(topics) == 2
    assert {row["来源记录ID"] for row in mapping} == {"A", "B", "C"}
    mapping_by_id = {row["来源记录ID"]: row for row in mapping}
    assert mapping_by_id["B"]["聚类决策"] == "业务硬规则冲突后新建主题"
    assert mapping_by_id["B"]["聚类裁决提供方"] == "business-rule"
    assert mapping_by_id["C"]["聚类决策"] == "大模型确认合并"
    assert mapping_by_id["C"]["聚类裁决提供方"] == "mimo"
    assert not gaps
    assert not pending
    assert clustering_meta["effective_mode"] == "semantic_mimo"
    assert clustering_meta["mimo_review_calls"] == 1
    assert clustering_meta["mimo_review_approved"] == 1
    assert clustering_meta["mimo_review_rejected"] == 0
    assert clustering_meta["mimo_hard_rule_rejected"] == 1


def test_direct_mimo_clusters_one_to_many_without_embedding() -> None:
    class DirectMimo:
        config = SimpleNamespace(model="mimo-direct-test")

        def analyze_cluster_units(self, row):
            subject = "屏幕" if row["数据ID"] in {"A", "B"} else "摄像头"
            category_l1 = "显示问题" if subject == "屏幕" else "功能问题"
            return MimoLabelResult(
                candidate={
                    "conversation_type": "single_topic",
                    "reason": "会话包含一个清晰问题。",
                    "topics": [
                        {
                            "normalized_issue": f"手机｜{subject}｜异常｜核验",
                            "product_category": "手机",
                            "scope_type": "品类专用",
                            "platform": "通用",
                            "brand": "通用",
                            "model_scope": "通用",
                            "category_l1": category_l1,
                            "category_l2": f"{subject}异常",
                            "intent": "检测核验",
                            "subject": subject,
                            "phenomenon": "异常",
                            "judgment_target": f"判断{subject}是否异常",
                            "resolution_mode": "对照标准核验",
                            "standard_path": f"对照{subject}标准核验",
                            "threshold_or_exception": "无明确阈值",
                            "evidence_summary": "完整聊天支持该问题。",
                            "confidence": 0.9,
                            "requires_review": False,
                        }
                    ],
                },
                request_audit={},
                response_audit={},
            )

        def cluster_atomic_units(self, units):
            member_ids = [unit["unit_id"] for unit in units]
            return MimoLabelResult(
                candidate={
                    "clusters": [
                        {
                            "cluster_id": "C001",
                            "theme_name": "同一主题",
                            "member_atomic_ids": member_ids,
                            "merge_basis": "适用范围、对象、目标、标准路径和阈值一致。",
                        }
                    ],
                    "split_requests": [],
                    "review_requests": [],
                },
                request_audit={},
                response_audit={},
            )

    rows = [
        {
            "数据ID": record_id,
            "工单ID": record_id,
            "聊天内容": conversation,
            "核心问题": conversation,
            "产品类型": "手机",
            "问题意图": "检测核验",
            "对象/部位": "待确认",
            "异常现象": "异常",
            "解题方式": "对照标准核验",
        }
        for record_id, conversation in (
            ("A", "屏幕异常怎么核验"),
            ("B", "屏幕显示异常如何确认"),
            ("C", "摄像头功能异常如何核验"),
        )
    ]
    clustering_meta: dict[str, object] = {}

    topics, mapping, gaps, pending = build_topic_review_rows(
        rows,
        use_mimo=False,
        mimo_client=DirectMimo(),
        clustering_mode="direct_mimo",
        clustering_meta=clustering_meta,
    )

    assert len(topics) == 2
    assert sorted(topic["主题样本数"] for topic in topics) == [1, 2]
    assert len(mapping) == 3
    assert not gaps
    assert not pending
    assert clustering_meta["effective_mode"] == "direct_mimo"
    assert clustering_meta["atomic_unit_count"] == 3
    assert clustering_meta["direct_cluster_calls"] == 1


def test_cluster_validation_compares_clustering_model_and_human_labels() -> None:
    features, _ = generate_phone_candidate_rows(
        _source_rows(),
        _standards(),
        use_mimo=False,
        image_downloader=_ReadyImageDownloader(),
    )
    first = features[0]
    second = dict(first)
    second["数据ID"] = "PHONE-002"
    second["工单ID"] = "PHONE-002"
    third = dict(first)
    third["数据ID"] = "PHONE-003"
    third["工单ID"] = "PHONE-003"
    third["核心问题"] = "设备机型如何查询"
    third["问题意图"] = "信息查询"
    third["对象/部位"] = "机型"

    rows, summary = build_cluster_validation_rows(
        [first, second, third],
        semantic_threshold=0.8,
        max_pairs=3,
        embedding_client=_FakeEmbedding(),
        mimo_client=_FakeMimo(),
    )

    assert len(rows) == 3
    assert summary["validation_pairs"] == 3
    assert {row["聚类预测"] for row in rows} == {"同一主题", "不同主题"}
    assert all(row["大模型状态"] == "已标注" for row in rows)
    assert all("人工错误类型" in row for row in rows)
    for row in rows:
        row["人工判断"] = row["聚类预测"]
    evaluation = evaluate_cluster_validation_rows(rows)
    assert evaluation["clustering_accuracy"] == 1.0
    assert evaluation["large_model_accuracy"] == 1.0


def test_cluster_validation_reuses_mimo_media_signals_from_workbook(tmp_path: Path) -> None:
    class CapturingMimo(_FakeMimo):
        def __init__(self) -> None:
            self.signal_sources: list[dict[str, object]] = []
            self.pair_payloads: list[tuple[dict[str, str], dict[str, str]]] = []

        def analyze_topic_signal(self, source, matches, images):
            self.signal_sources.append(dict(source))
            return super().analyze_topic_signal(source, matches, images)

        def review_cluster_pair(self, left, right, similarity, threshold):
            self.pair_payloads.append((dict(left), dict(right)))
            return super().review_cluster_pair(left, right, similarity, threshold)

    source_path = tmp_path / "source.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "方向二"
    headers = [
        "工单ID",
        "聊天内容",
        "图片链接",
        "视频链接",
        "核心问题",
        "判定结论",
        "判定依据",
        "产品类型",
        "一级分类",
        "二级分类",
        "参考话术",
    ]
    worksheet.append(headers)
    for index in range(3):
        worksheet.append(
            [
                f"PHONE-{index + 1:03d}",
                "屏幕有色斑，请结合现场图片确认。",
                "https://example.com/phone.jpg",
                "https://example.com/phone.mp4",
                "手机屏幕色斑如何判定",
                "待确认",
                "需要结合现场证据",
                "手机",
                "显示问题",
                "色斑",
                "请补充清晰图片",
            ]
        )
    workbook.save(source_path)

    mimo = CapturingMimo()
    rows, summary = cluster_validation_from_workbook(
        source_path,
        product_type="手机",
        semantic_threshold=0.8,
        max_pairs=3,
        embedding_client=_FakeEmbedding(),
        mimo_client=mimo,
        image_downloader=_ReadyImageDownloader(),
    )

    assert len(mimo.signal_sources) == 3
    assert mimo.pair_payloads
    assert summary["conversation_signal_model_enabled"] is True
    assert all(row["记录A_图片证据摘要"] == "图片已接收，仍需人工确认细节。" for row in rows)
    assert all(row["记录A_主题标签"] for row in rows)
    assert all(row["记录A_语义标注依据"] for row in rows)
    assert all(row["记录A_视频链接"] == "https://example.com/phone.mp4" for row in rows)
    assert all(
        row["记录A_视频处理状态"] == "存在视频，当前未解析视频内容（1个）"
        for row in rows
    )
    left_payload, _right_payload = mimo.pair_payloads[0]
    assert left_payload["图片证据摘要"] == "图片已接收，仍需人工确认细节。"
    assert left_payload["视频处理状态"] == "存在视频，当前未解析视频内容（1个）"


def test_cluster_validation_evaluation_tracks_annotation_risks() -> None:
    rows = [
        {
            "聚类预测": "同一主题",
            "大模型判断": "同一主题",
            "人工判断": "不同主题",
        },
        {
            "聚类预测": "不同主题",
            "大模型判断": "同一主题",
            "人工判断": "同一主题",
        },
        {
            "聚类预测": "不同主题",
            "大模型判断": "不同主题",
            "人工判断": "不确定",
        },
        {
            "聚类预测": "不同主题",
            "大模型判断": "不同主题",
            "人工判断": "",
        },
    ]

    evaluation = evaluate_cluster_validation_rows(rows)

    assert evaluation["reviewed_pairs"] == 3
    assert evaluation["pending_pairs"] == 1
    assert evaluation["uncertain_pairs"] == 1
    assert evaluation["decisive_pairs"] == 2
    assert evaluation["clustering_accuracy"] == 0.0
    assert evaluation["large_model_accuracy"] == 0.5
    assert evaluation["false_merge_pairs"] == 1
    assert evaluation["false_merge_rate"] == 1.0
    assert evaluation["false_split_pairs"] == 1
    assert evaluation["false_split_rate"] == 1.0


def test_cluster_validation_scales_to_hundreds_without_materializing_all_pairs() -> None:
    class BulkEmbedding:
        config = SimpleNamespace(model="bulk-semantic-test")

        def embed_texts(self, texts, progress_callback=None):
            vectors = []
            theme_vectors = (
                [1.0, 0.0],
                [0.75, 0.6614378],
                [0.75, -0.6614378],
            )
            for index, _text in enumerate(texts):
                vectors.append(theme_vectors[index % len(theme_vectors)])
            if progress_callback:
                progress_callback(len(texts), len(texts))
            return vectors

    rows = [
        {
            "数据ID": f"BULK-{index:04d}",
            "工单ID": f"BULK-{index:04d}",
            "聊天内容": f"第 {index} 条脱敏测试会话",
            "核心问题": f"测试主题 {index % 3}",
            "产品类型": "手机",
            "一级分类": "批量测试",
            "二级分类": f"主题 {index % 3}",
        }
        for index in range(500)
    ]
    progress_events: list[tuple[str, int, int]] = []

    validation_rows, summary = build_cluster_validation_rows(
        rows,
        semantic_threshold=0.8,
        max_pairs=20,
        embedding_client=BulkEmbedding(),
        use_mimo=False,
        progress_callback=lambda stage, completed, total: progress_events.append(
            (stage, completed, total)
        ),
    )

    assert len(validation_rows) == 20
    assert summary["eligible_rows"] == 500
    assert summary["candidate_pairs"] == 124750
    assert {row["聚类预测"] for row in validation_rows} == {"同一主题", "不同主题"}
    assert any(stage == "embedding" for stage, _completed, _total in progress_events)
    assert any(stage == "pair_sampling" for stage, _completed, _total in progress_events)


def test_unavailable_image_without_chat_goes_to_evidence_gap() -> None:
    rows = _source_rows()
    rows[0]["聊天内容"] = ""
    features, _ = generate_phone_candidate_rows(
        rows,
        _standards(),
        mimo_client=_FakeMimo(),
        image_downloader=_FailedImageDownloader(),
    )
    topics, _mapping, gaps, pending = build_topic_review_rows(features, _standards(), use_mimo=False)
    assert not topics
    assert not pending
    assert len(gaps) == 1
    assert "不可用:1" in gaps[0]["图片处理状态"]


def test_mimo_client_retries_invalid_json_once() -> None:
    client = MimoClient(MimoConfig(api_key="test", base_url="https://example.com/v1", model="mimo-v2.5-test"))
    responses = iter(
        [
            {"choices": [{"message": {"content": "not-json"}}]},
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "手机屏幕色斑判定",
                                    "subtitles": ["色斑"],
                                    "content": "按标准核验。",
                                    "category_l1": "显示问题",
                                    "category_l2": "色斑",
                                    "layer": "L2",
                                    "knowledge_form": "具体判定",
                                    "standard_refs": ["PHONE-DISPLAY-001"],
                                    "applicable_scope": "手机屏幕",
                                    "confidence": 0.9,
                                    "reasoning_summary": "匹配色斑标准。",
                                    "needs_human_review": False,
                                    "image_evidence_summary": "无图片。",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        ]
    )
    client._post = lambda _payload: next(responses)  # type: ignore[method-assign]

    result = client.label(_source_rows()[0], [( _standards()[0], 7.5)], [])
    assert result.request_audit["attempt"] == 2
    assert result.candidate["standard_refs"] == ["PHONE-DISPLAY-001"]


def test_cz_rag_master_schema_is_read_as_standard_content(tmp_path: Path) -> None:
    path = tmp_path / "cz-phone-master.json"
    path.write_text(
        json.dumps(
            [
                {
                    "主标题": "设备机型是什么意思",
                    "知识内容": "按实物特征确认设备机型。",
                    "知识分类": "标准定义",
                    "关联标准项": "【基本情况】-【机型】",
                    "适用范围": "通用",
                    "生效状态": "生效中",
                    "来源版本": "SJ-HSYJBZ-2026009",
                    "检索关键词": "设备机型 | 机型怎么确认",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = load_standard_catalog(path)[0]
    assert item.standard_path == "【基本情况】-【机型】"
    assert item.knowledge_type == "标准定义"
    assert item.response_snippet == "按实物特征确认设备机型。"
    assert item.version == "SJ-HSYJBZ-2026009"


def test_ambiguous_repair_trace_becomes_process_not_irrelevant_answer() -> None:
    rows = preprocess_source_rows(
        [
            {
                "工单ID": "REPAIR-001",
                "聊天内容": "这个位置是胶吗？没有看出什么。",
                "核心问题": "设备某个位置疑似胶状物，不确定是否为维修痕迹。",
                "判定结论": "现有图片未识别出明显异常。",
                "判定依据": "证据不足，需结合清晰图片和质检标准确认。",
                "产品类型": "手机",
                "一级分类": "拆修问题",
                "二级分类": "屏幕拆修",
            }
        ]
    )
    weak_standard = StandardCatalogItem(
        standard_id="STD-SCREEN",
        title="屏幕检测方法",
        category_l1="",
        category_l2="",
        knowledge_type="检测方法",
        standard_path="【屏幕】",
        keywords=["屏幕"],
        scope="通用",
        response_snippet="检查屏幕。",
        status="published",
        version="v1",
    )
    candidate = initial_label_rows(rows, [weak_standard])[0]
    assert candidate["模型知识形态"] == "流程方法"
    assert "核验" in candidate["模型主标题"]
    assert candidate["模型关联标准"] == ""
    assert candidate["标准检索状态"] == "未搜索到相关知识（待人工补充）"
    assert candidate["是否重点复核"] == "是"


def test_mimo_cannot_override_uncertainty_process_guardrail() -> None:
    rows = preprocess_source_rows(
        [
            {
                "工单ID": "UNCERTAIN-001",
                "聊天内容": "屏幕上疑似有色斑，不确定是否符合标准。",
                "核心问题": "手机屏幕疑似色斑如何确认",
                "判定结论": "现有图片暂无法确认。",
                "判定依据": "证据不足，需要补拍白屏图片。",
                "产品类型": "手机",
                "一级分类": "显示问题",
                "二级分类": "色斑",
                "参考话术": "请补拍清晰图片。",
            }
        ]
    )
    features, _ = generate_phone_candidate_rows(
        rows,
        _standards(),
        mimo_client=_FakeMimo(),
        image_downloader=_ReadyImageDownloader(),
    )
    second = dict(features[0])
    second["数据ID"] = "UNCERTAIN-002"
    second["工单ID"] = "UNCERTAIN-002"
    topics, _mapping, _gaps, _pending = build_topic_review_rows(
        [features[0], second],
        _standards(),
        mimo_client=_FakeMimo(),
    )
    candidate = topics[0]
    assert candidate["主题模型提供方"] == "mimo"
    assert candidate["知识分类"] == "检测方法"
    assert candidate["是否重点复核"] == "是"
    assert "强制降级为流程方法" in candidate["校验备注"]
