from __future__ import annotations

from answer_hub.atomic_standard_mapper import (
    HumanAnnotation,
    map_atomic_units_to_standards,
)
from answer_hub.catalog import StandardCatalogItem


def _standard(
    standard_id: str,
    title: str,
    category_l1: str,
    category_l2: str,
    *,
    scope: str = "手机-通用",
) -> StandardCatalogItem:
    return StandardCatalogItem(
        standard_id=standard_id,
        title=title,
        category_l1=category_l1,
        category_l2=category_l2,
        knowledge_type="质检标准",
        standard_path=f"【{scope.split('-', 1)[0]}】-【{category_l1}】-【{category_l2}】-【{title}】",
        keywords=[title, category_l1, category_l2],
        scope=scope,
        response_snippet=f"检测并判定：{title}",
        status="published",
        version="v1",
    )


def _unit(**overrides):
    unit = {
        "unit_id": "S001-01",
        "sample_id": "S001",
        "normalized_issue": "手机｜屏幕｜按压有起伏｜判定是否为屏幕脱胶",
        "product_category": "手机",
        "scope_type": "品类专用",
        "platform": "通用",
        "brand": "通用",
        "model_scope": "通用",
        "category_l1": "外观问题",
        "category_l2": "屏幕外观",
        "subject": "屏幕",
        "phenomenon": "按压有起伏",
        "judgment_target": "判定是否为屏幕脱胶",
        "resolution_mode": "按屏幕脱胶标准判定",
        "standard_path": "屏幕按压有起伏判脱胶",
        "threshold_or_exception": "支架凸起>1mm",
        "requires_review": False,
    }
    unit.update(overrides)
    return unit


def test_mapping_filters_other_product_categories() -> None:
    standards = [
        _standard(
            "PHONE",
            "屏幕脱胶(盖板凸起/按压起伏)、支架凸起>1mm",
            "屏幕及正面外观",
            "屏幕外观其他现象",
        ),
        _standard(
            "WATCH",
            "屏幕脱胶",
            "屏幕外观",
            "其他外观",
            scope="手表-通用",
        ),
    ]

    result = map_atomic_units_to_standards([_unit()], standards, top_k=5)

    assert [candidate["standard_id"] for candidate in result["records"][0]["candidates"]] == [
        "PHONE"
    ]
    assert result["metadata"]["standard_count"] == 2


def test_mapping_keeps_camera_body_and_lens_standards_separate() -> None:
    standards = [
        _standard(
            "CAMERA-BODY",
            "机身卡口磨损",
            "外观问题",
            "卡口外观",
            scope="相机机身-通用",
        ),
        _standard(
            "CAMERA-LENS",
            "镜片霉斑",
            "外观问题",
            "镜片外观",
            scope="相机镜头-通用",
        ),
    ]
    unit = _unit(
        normalized_issue="相机镜头｜镜片｜疑似霉斑｜确认检测方法",
        product_category="相机镜头",
        subject="镜片",
        phenomenon="疑似霉斑",
        judgment_target="确认是否存在霉斑",
        resolution_mode="按镜片外观标准核验",
        standard_path="镜片外观检测",
        threshold_or_exception="无明确阈值",
    )

    result = map_atomic_units_to_standards([unit], standards, top_k=5)

    assert [candidate["standard_id"] for candidate in result["records"][0]["candidates"]] == [
        "CAMERA-LENS"
    ]


def test_threshold_and_subject_make_correct_standard_rank_first() -> None:
    standards = [
        _standard(
            "MATCH",
            "屏幕脱胶(盖板凸起/按压起伏)、支架凸起>1mm",
            "屏幕及正面外观",
            "屏幕外观其他现象",
        ),
        _standard(
            "OTHER",
            "后盖脱胶且缝隙>0.3mm",
            "中框及外壳外观",
            "外壳碎裂/刻字/脱胶",
        ),
    ]

    result = map_atomic_units_to_standards([_unit()], standards, top_k=2)

    assert result["records"][0]["candidates"][0]["standard_id"] == "MATCH"
    assert "阈值数字一致：1" in result["records"][0]["candidates"][0]["match_reasons"]


def test_old_coarse_category_is_only_a_soft_signal() -> None:
    standards = [
        _standard(
            "CORRECT",
            "屏幕闪屏",
            "屏幕显示情况",
            "屏幕显示异常",
        ),
        _standard(
            "WRONG",
            "屏幕轻微划痕",
            "屏幕及正面外观",
            "屏幕无触感划痕",
        ),
    ]
    unit = _unit(
        normalized_issue="手机｜屏幕｜开机时持续闪烁｜判定是否闪屏",
        category_l1="显示问题",
        category_l2="屏幕显示异常",
        phenomenon="开机时持续闪烁",
        judgment_target="判定是否属于闪屏",
        resolution_mode="按闪屏标准处理",
        standard_path="屏幕显示闪烁",
        threshold_or_exception="无明确阈值",
    )

    result = map_atomic_units_to_standards([unit], standards, top_k=2)

    assert result["records"][0]["candidates"][0]["standard_id"] == "CORRECT"


def test_human_legacy_category_correction_maps_to_official_category() -> None:
    standards = [
        _standard(
            "FUNCTION",
            "前摄坏点直径>0.5mm或数量>3",
            "设备功能情况",
            "前置摄像头功能",
        ),
        _standard(
            "REPAIR",
            "前摄像头焊接异常",
            "拆修及浸液情况",
            "Xray检测结果",
        ),
    ]
    unit = _unit(
        normalized_issue="手机｜前摄像头｜坏点｜判断属于什么问题",
        category_l1="显示问题",
        subject="前摄像头",
        phenomenon="坏点",
        judgment_target="判定摄像头坏点",
        standard_path="待确认",
    )
    annotation = HumanAnnotation(
        atomic_id="S001-01",
        source_sheet="待人工确认",
        decision="补充字段后参与聚类",
        note="摄像头坏点是功能问题",
    )

    result = map_atomic_units_to_standards(
        [unit],
        standards,
        {"S001-01": annotation},
        top_k=2,
    )

    record = result["records"][0]
    assert record["effective_unit"]["category_l1"] == "设备功能情况"
    assert record["candidates"][0]["standard_id"] == "FUNCTION"


def test_embedding_only_reranks_lexical_top_five() -> None:
    standards = [
        _standard(
            f"S{index}",
            f"屏幕脱胶候选{index}",
            "屏幕及正面外观",
            "屏幕外观其他现象",
        )
        for index in range(10)
    ]

    class FakeEmbedding:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def embed_texts(self, texts, progress_callback=None):
            self.calls.append(len(texts))
            if progress_callback:
                progress_callback(len(texts), len(texts))
            return [[1.0, 0.0] for _text in texts]

    embedding = FakeEmbedding()
    result = map_atomic_units_to_standards(
        [_unit()],
        standards,
        top_k=5,
        embedding_client=embedding,
    )

    assert embedding.calls == [1, 5]
    assert result["metadata"]["embedding_status"] == "used"
    assert result["metadata"]["embedded_candidate_standard_count"] == 5
