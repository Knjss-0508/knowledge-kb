from __future__ import annotations

from types import SimpleNamespace
import json

import pytest
from openpyxl import Workbook

from answer_hub.mimo import (
    MimoLabelResult,
    _atomic_unit_payload,
    _build_atomic_topic_cluster_prompt,
)
from answer_hub.question_standardization import (
    standard_question_conflict_reason,
    standard_question_sort_key,
    standardize_atomic_question,
)
from answer_hub.workflow import (
    _direct_atomic_bucket_key,
    _direct_mimo_topic_groups,
    filter_preprocessed_rows_for_model,
    preprocess_source_rows,
    filter_source_rows_by_product_type,
)


def _unit(**overrides: str) -> dict[str, str]:
    unit = {
        "unit_id": "A-01",
        "business_line": "自营回收",
        "product_category": "手机",
        "normalized_issue": "手机｜屏幕｜局部颜色异常｜如何判定",
        "subject": "屏幕",
        "phenomenon": "局部颜色异常",
        "judgment_target": "判断是否属于色斑",
        "resolution_mode": "按照显示异常口径判定",
        "standard_path": "屏幕显示异常判定",
        "threshold_or_exception": "无明确阈值",
    }
    unit.update(overrides)
    return unit


def test_standardization_merges_wording_and_punctuation_variants() -> None:
    left = standardize_atomic_question(_unit(judgment_target="应如何判定？"))
    right = standardize_atomic_question(_unit(judgment_target="怎么判定"))

    assert left["standard_question"] == right["standard_question"]
    assert left["standard_question_key"] == right["standard_question_key"]
    assert left["standardization_status"] == "ready"


def test_standardization_prefers_model_standard_question_for_synonyms() -> None:
    left = standardize_atomic_question(
        _unit(
            standard_question="屏幕局部颜色异常如何判定",
            phenomenon="屏幕局部偏色",
            judgment_target="确认是否属于色斑",
        )
    )
    right = standardize_atomic_question(
        _unit(
            standard_question="屏幕局部颜色异常如何判定",
            phenomenon="显示区域颜色不均",
            judgment_target="判断是否按色斑处理",
        )
    )

    assert left["standard_question"] == right["standard_question"]
    assert left["standard_question_key"] == right["standard_question_key"]


def test_standardization_keeps_hard_business_boundaries_separate() -> None:
    baseline = standardize_atomic_question(_unit())

    variants = [
        _unit(business_line="聚合回收"),
        _unit(product_category="平板电脑"),
        _unit(subject="后壳"),
        _unit(phenomenon="屏幕亮线"),
        _unit(judgment_target="查询异常出现的位置"),
        _unit(resolution_mode="指导用户执行检测步骤"),
        _unit(threshold_or_exception="大于1毫米按漏液判定"),
    ]

    for variant in variants:
        result = standardize_atomic_question(variant)
        assert result["standard_question_key"] != baseline["standard_question_key"]


def test_standardization_marks_uncertain_questions_for_review() -> None:
    result = standardize_atomic_question(
        _unit(
            subject="待确认",
            phenomenon="待确认",
            judgment_target="待确认",
            resolution_mode="待确认",
        )
    )

    assert result["standardization_status"] == "needs_review"
    assert result["standardization_review_reason"]


def test_standardization_does_not_replace_the_atomic_issue() -> None:
    unit = _unit(normalized_issue="保留这条原始原子问题")

    result = standardize_atomic_question(unit)

    assert unit["normalized_issue"] == "保留这条原始原子问题"
    assert result["standard_question"] != unit["normalized_issue"]


def test_standard_question_sort_keeps_related_questions_adjacent() -> None:
    units = [
        {
            "unit_id": "A-01",
            "standard_question_key": "手机｜屏幕｜色斑｜判定",
            "standardization_status": "ready",
        },
        {
            "unit_id": "B-01",
            "standard_question_key": "手机｜电池｜健康度｜查询",
            "standardization_status": "ready",
        },
        {
            "unit_id": "Z-99",
            "standard_question_key": "手机｜屏幕｜色斑｜判定",
            "standardization_status": "ready",
        },
        {
            "unit_id": "R-01",
            "standard_question_key": "",
            "standardization_status": "needs_review",
        },
    ]

    ordered = sorted(units, key=standard_question_sort_key)

    assert [unit["unit_id"] for unit in ordered] == [
        "A-01",
        "Z-99",
        "B-01",
        "R-01",
    ]


def test_standard_question_conflict_rejects_different_thresholds() -> None:
    left_unit = _unit(
        threshold_or_exception="大于1毫米",
    )
    right_unit = _unit(
        threshold_or_exception="大于2毫米",
    )
    left = {**left_unit, **standardize_atomic_question(left_unit)}
    right = {**right_unit, **standardize_atomic_question(right_unit)}

    assert standard_question_conflict_reason([left, right]) == (
        "标准问题结构化维度冲突：阈值或例外不同，不能自动聚类合并"
    )


def test_threshold_normalization_preserves_decimals_and_comparison_signs() -> None:
    for left_threshold, right_threshold in [
        ("大于1.5毫米", "大于15毫米"),
        ("<1毫米", ">1毫米"),
        ("小于等于1毫米", "小于1毫米"),
    ]:
        left_unit = _unit(threshold_or_exception=left_threshold)
        right_unit = _unit(threshold_or_exception=right_threshold)
        left = {**left_unit, **standardize_atomic_question(left_unit)}
        right = {**right_unit, **standardize_atomic_question(right_unit)}
        assert left["standard_question_key"] != right["standard_question_key"]
        assert "阈值或例外不同" in standard_question_conflict_reason([left, right])


def test_different_standard_question_wording_is_not_a_hard_boundary() -> None:
    left_unit = _unit(standard_question="屏幕局部颜色异常如何判定")
    right_unit = _unit(standard_question="屏幕局部偏色如何判断")
    left = {**left_unit, **standardize_atomic_question(left_unit)}
    right = {**right_unit, **standardize_atomic_question(right_unit)}

    assert standard_question_conflict_reason([left, right]) == ""
    assert _direct_atomic_bucket_key(left) == _direct_atomic_bucket_key(right)


def test_equivalent_threshold_wording_does_not_block_evidence_merge() -> None:
    for left_threshold, right_threshold in [
        ("大于 1 毫米", "超过1mm"),
        ("小于等于1.5毫米", "≤ 1.5 mm"),
        ("大于等于2毫米", ">=2mm"),
        ("大于1.0毫米", ">1mm"),
        ("至少1毫米", "大于等于1毫米"),
        ("不大于1毫米", "小于等于1毫米"),
        ("不小于1.50毫米", "≥1.5mm"),
    ]:
        left_unit = _unit(threshold_or_exception=left_threshold)
        right_unit = _unit(threshold_or_exception=right_threshold)
        left = {**left_unit, **standardize_atomic_question(left_unit)}
        right = {**right_unit, **standardize_atomic_question(right_unit)}
        assert left["standard_question_key"] == right["standard_question_key"]
        assert standard_question_conflict_reason([left, right]) == ""


def test_ready_model_standard_questions_use_one_comparison_bucket() -> None:
    left_unit = _unit(
        unit_id="A-01",
        standard_question="屏幕局部颜色异常如何判定",
        phenomenon="屏幕局部偏色",
        judgment_target="确认是否属于色斑",
    )
    right_unit = _unit(
        unit_id="B-01",
        standard_question="屏幕局部颜色异常如何判定",
        phenomenon="显示区域颜色不均",
        judgment_target="判断是否按色斑处理",
    )
    left = {**left_unit, **standardize_atomic_question(left_unit)}
    right = {**right_unit, **standardize_atomic_question(right_unit)}

    assert _direct_atomic_bucket_key(left) == _direct_atomic_bucket_key(right)


def test_cluster_payload_exposes_standard_question_as_auxiliary_evidence() -> None:
    unit = {**_unit(), **standardize_atomic_question(_unit())}

    payload = _atomic_unit_payload(unit)
    prompt = _build_atomic_topic_cluster_prompt([unit])

    assert payload["standard_question"] == unit["standard_question"]
    assert payload["standard_question_key"] == unit["standard_question_key"]
    assert payload["standardization_status"] == "ready"
    assert "标准问题相同只是值得比较的辅助信号" in prompt
    assert "不同产品品类" in prompt
    assert "不同对象、异常现象、判定诉求、处理路径或阈值例外" in prompt


class _WorkflowMimo:
    config = SimpleNamespace(model="question-standardization-test")

    def __init__(self, topics: dict[str, dict[str, object]], *, merge: bool = False):
        self._topics = topics
        self._merge = merge

    def analyze_cluster_units(self, row: dict[str, object]) -> MimoLabelResult:
        return MimoLabelResult(
            candidate={
                "conversation_type": "single_topic",
                "reason": "测试原子问题。",
                "topics": [self._topics[str(row["数据ID"])]],
            },
            request_audit={},
            response_audit={},
        )

    def cluster_atomic_units(
        self,
        units: list[dict[str, object]],
    ) -> MimoLabelResult:
        groups = [units] if self._merge else [[unit] for unit in units]
        return MimoLabelResult(
            candidate={
                "clusters": [
                    {
                        "cluster_id": f"C{index:03d}",
                        "theme_name": "测试主题",
                        "member_atomic_ids": [
                            str(unit["unit_id"]) for unit in group
                        ],
                        "shared_knowledge_definition": "测试知识定义。",
                        "merge_basis": "测试合并依据。",
                    }
                    for index, group in enumerate(groups, start=1)
                ],
                "split_requests": [],
                "review_requests": [],
            },
            request_audit={},
            response_audit={},
        )


def _workflow_topic(
    *,
    subject: str = "屏幕",
    phenomenon: str = "局部颜色异常",
    judgment_target: str = "判断是否属于色斑",
    resolution_mode: str = "按照显示异常口径判定",
    standard_question: str = "屏幕局部颜色异常如何判定",
) -> dict[str, object]:
    return {
        "normalized_issue": f"手机｜{subject}｜{phenomenon}｜{judgment_target}",
        "standard_question": standard_question,
        "product_category": "手机",
        "scope_type": "品类专用",
        "platform": "通用",
        "brand": "通用",
        "model_scope": "通用",
        "category_l1": "显示问题",
        "category_l2": "屏幕异常",
        "intent": "标准判定",
        "subject": subject,
        "phenomenon": phenomenon,
        "judgment_target": judgment_target,
        "resolution_mode": resolution_mode,
        "standard_path": "显示异常判定",
        "threshold_or_exception": "无明确阈值",
        "evidence_summary": "来自完整聊天和人工校正字段。",
        "confidence": 0.95,
        "requires_review": False,
    }


def test_workflow_attaches_standardization_fields_before_clustering() -> None:
    rows = preprocess_source_rows(
        [
            {
                "数据ID": "ROW-001",
                "工单ID": "ROW-001",
                "产品类型": "手机",
                "核心问题": "屏幕局部颜色异常怎么判定",
                "判定结论": "需要按显示异常口径判断是否属于色斑",
                "聊天内容": "屏幕这里颜色不一样，应该怎么判？",
            },
            {
                "数据ID": "ROW-002",
                "工单ID": "ROW-002",
                "产品类型": "手机",
                "核心问题": "屏幕出现局部颜色异常如何处理",
                "判定结论": "需要按显示异常口径判断是否属于色斑",
                "聊天内容": "屏幕局部颜色不一致，如何判定？",
            },
        ]
    )
    reviewer = _WorkflowMimo(
        {
            "ROW-001": _workflow_topic(),
            "ROW-002": _workflow_topic(),
        }
    )

    groups, meta = _direct_mimo_topic_groups(rows, reviewer)

    output_rows = [row for _key, group_rows in groups for row in group_rows]
    assert {row["标准问题标准化状态"] for row in output_rows} == {"ready"}
    assert len({row["标准问题"] for row in output_rows}) == 1
    assert len({row["标准问题归一键"] for row in output_rows}) == 1
    assert meta["question_standardization_ready"] == 2
    assert meta["question_standardization_needs_review"] == 0


def test_workflow_keeps_standardization_uncertainty_safe() -> None:
    rows = preprocess_source_rows(
        [
            {
                "数据ID": "ROW-001",
                "工单ID": "ROW-001",
                "产品类型": "手机",
                "核心问题": "屏幕异常怎么判定",
                "判定结论": "需要进一步确认",
                "聊天内容": "屏幕这里异常，怎么判？",
            }
        ]
    )
    reviewer = _WorkflowMimo(
        {
            "ROW-001": _workflow_topic(
                subject="待确认",
                phenomenon="待确认",
                judgment_target="待确认",
                resolution_mode="待确认",
                standard_question="",
            )
        }
    )

    groups, meta = _direct_mimo_topic_groups(rows, reviewer)

    output_row = next(row for _key, group_rows in groups for row in group_rows)
    assert output_row["标准问题标准化状态"] == "needs_review"
    assert output_row["_原子需要复核"] is True
    assert "标准问题缺少明确维度" in output_row["人工优先复核原因"]
    assert meta["question_standardization_needs_review"] == 1


def test_evidence_model_can_merge_different_standard_question_wording() -> None:
    rows = preprocess_source_rows([
        {
            "数据ID": record_id, "工单ID": record_id, "产品类型": "手机",
            "核心问题": "屏幕局部颜色异常如何判定",
            "聊天内容": "屏幕局部颜色异常如何判定？",
            "判定结论": "按显示异常口径复核",
        }
        for record_id in ("ROW-001", "ROW-002")
    ])
    reviewer = _WorkflowMimo({
        "ROW-001": _workflow_topic(standard_question="屏幕局部颜色异常如何判定"),
        "ROW-002": _workflow_topic(standard_question="屏幕局部偏色如何判断"),
    }, merge=True)
    groups, meta = _direct_mimo_topic_groups(rows, reviewer)
    assert len(groups) == 1
    assert len(groups[0][1]) == 2
    assert len({row["标准问题归一键"] for row in groups[0][1]}) == 2
    assert meta["direct_post_guard_split_clusters"] == 0


def test_raw_second_part_columns_are_mapped_and_manual_title_is_ignored() -> None:
    rows = preprocess_source_rows(
        [
            {
                "id": "record-1",
                "ticket_id": "ticket-1",
                "analyze_time": "2026-10-13 10:00:00",
                "category": "手机",
                "chat_content": "回收师询问屏幕局部偏色如何判定。",
                "chat_images": "https://example.invalid/screen.jpg",
                "chat_videos": "",
                "ai_result": {
                    "核心问题": "屏幕局部颜色异常如何判定",
                    "判定结论": "按显示异常口径复核",
                    "参考话术": "请补充屏幕近景图。",
                },
                "人工标题": "这个标题不能进入模型输入",
            }
        ]
    )

    eligible, excluded = filter_preprocessed_rows_for_model(rows)

    assert not excluded
    assert eligible[0]["数据ID"] == "record-1"
    assert eligible[0]["工单ID"] == "ticket-1"
    assert eligible[0]["分析时间"] == "2026-10-13 10:00:00"
    assert eligible[0]["产品类型"] == "手机"
    assert eligible[0]["聊天内容"] == "回收师询问屏幕局部偏色如何判定。"
    assert eligible[0]["图片链接"] == "https://example.invalid/screen.jpg"
    assert eligible[0]["核心问题"] == "屏幕局部颜色异常如何判定"
    assert "人工标题" not in eligible[0]["核心问题"]


def test_category_filter_accepts_raw_category_and_preserves_source() -> None:
    source = [
        {"id": "phone", "category": "手机", "ai_result": {"产品类型": "笔记本"}},
        {"id": "notebook", "category": "笔记本"},
    ]
    selected, excluded = filter_source_rows_by_product_type(source, "手机")
    assert [row["id"] for row in selected] == ["phone"]
    assert len(excluded) == 1
    assert "类目" not in source[0]


def test_raw_json_media_lists_normalize_without_fabricating_empty_media() -> None:
    rows = preprocess_source_rows([
        {"ticket_id": "1", "category": "手机", "chat_images": "[]"},
        {
            "ticket_id": "2", "category": "手机",
            "chat_images": '["https://example.invalid/a.jpg", "https://example.invalid/a.jpg"]',
            "chat_videos": ["https://example.invalid/a.mp4"],
        },
    ])
    eligible, excluded = filter_preprocessed_rows_for_model(rows)
    assert len(excluded) == 1
    assert len(eligible) == 1
    assert eligible[0]["图片链接"] == "https://example.invalid/a.jpg"
    assert eligible[0]["视频链接"] == "https://example.invalid/a.mp4"


def test_raw_numeric_ticket_id_still_uses_precision_guard() -> None:
    import pytest
    with pytest.raises(ValueError, match="精度"):
        preprocess_source_rows([{"ticket_id": 12345678901234567, "chat_content": "测试"}])


@pytest.mark.parametrize("stale_field", [
    "question_standardization_version", "atomic_prompt_version",
    "cluster_prompt_version", None,
])
def test_public_resume_rebuilds_only_stale_topic_versions(tmp_path, monkeypatch, stale_field):
    from unittest.mock import Mock
    from answer_hub import workflow

    monkeypatch.setenv("KB_BASE_URL", "")
    monkeypatch.setenv("KB_INTEGRATION_KEY", "")
    monkeypatch.setenv("AUTO_REVIEW_ENABLED", "false")
    monkeypatch.setattr("answer_hub.cz_integration.load_dotenv", lambda **_: None)
    monkeypatch.setattr("answer_hub.auto_review.load_dotenv", lambda **_: None)
    monkeypatch.setattr("answer_hub.audit.load_dotenv", lambda **_: None)
    source = tmp_path / "source.xlsx"
    book = Workbook()
    book.active.append(["工单ID", "产品类型", "核心问题", "聊天内容", "判定结论"])
    book.active.append(["fixture-1", "手机", "屏幕颜色异常如何判定", "屏幕局部偏色如何判定", "需核验显示异常"])
    book.save(source)
    kwargs = dict(
        source_path=source, standards_path=None, output_dir=tmp_path / "output",
        use_mimo=False, clustering_mode="rule", use_standard_references=False,
        audit_db_path=tmp_path / "audit.db", enforce_cluster_admission=False,
    )
    workflow.initial_label_from_workbook(**kwargs)
    checkpoint_path = tmp_path / "output" / "workflow_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if stale_field:
        checkpoint["topic_summary"][stale_field] = "legacy"
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
    writer = Mock(wraps=workflow.write_topic_review_workbook)
    monkeypatch.setattr(workflow, "write_topic_review_workbook", writer)
    summary = workflow.initial_label_from_workbook(**kwargs, resume=True)
    assert writer.call_count == (1 if stale_field else 0)
    assert summary["question_standardization_version"] == workflow.QUESTION_STANDARDIZATION_VERSION
    assert summary["atomic_prompt_version"] == workflow.CLUSTER_UNIT_PROMPT_VERSION
    assert summary["cluster_prompt_version"] == workflow.ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION
