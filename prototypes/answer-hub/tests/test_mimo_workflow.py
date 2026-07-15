from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3

from answer_hub.audit import AuditStore
from answer_hub.catalog import StandardCatalogItem, load_standard_catalog
from answer_hub.images import ImageEvidence
from answer_hub.mimo import MimoClient, MimoConfig, MimoLabelResult
from answer_hub.workflow import (
    build_topic_review_rows,
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


class _FakeEmbedding:
    config = SimpleNamespace(model="semantic-cluster-test")

    def embed_texts(self, texts):
        assert len(texts) == 3
        return [
            [1.0, 0.0],
            [0.99, 0.01],
            [0.0, 1.0],
        ]


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
    assert feature["模型阶段状态"] == "feature_extracted"
    assert feature["问题意图"] == "异常核验"
    assert feature["对象/部位"] == "屏幕"
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

    connection = sqlite3.connect(audit.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_records").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2
    finally:
        connection.close()


def test_semantic_clustering_merges_similar_rows_and_keeps_outlier_pending() -> None:
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

    assert len(topics) == 1
    assert len(mapping) == 2
    assert not gaps
    assert len(pending) == 1
    assert clustering_meta["effective_mode"] == "semantic"
    assert clustering_meta["model"] == "semantic-cluster-test"


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
