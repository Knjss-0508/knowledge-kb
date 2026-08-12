from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from openpyxl import load_workbook

from answer_hub.business_cluster_validation import (
    run_category_cluster_jobs,
    write_cluster_validation_workbook,
)
from answer_hub.mimo import MimoLabelResult
from answer_hub.mimo import MimoError
from answer_hub.workflow import _direct_mimo_topic_groups, preprocess_source_rows


class _CategoryMimo:
    config = SimpleNamespace(model="mimo-category-test")

    def __init__(self, cluster_calls: list[set[str]]) -> None:
        self._cluster_calls = cluster_calls

    def analyze_cluster_units(self, row: dict[str, str]) -> MimoLabelResult:
        product_category = row["产品类型"]
        return MimoLabelResult(
            candidate={
                "topics": [
                    {
                        "normalized_issue": row["核心问题"],
                        "product_category": product_category,
                        "scope_type": "品类专用",
                        "platform": "通用",
                        "brand": "通用",
                        "model_scope": "通用",
                        "category_l1": "模型分类",
                        "category_l2": "模型子分类",
                        "intent": "异常核验",
                        "subject": "测试对象",
                        "phenomenon": "测试现象",
                        "judgment_target": "测试判定",
                        "resolution_mode": "测试处理",
                        "standard_path": "待确认",
                        "threshold_or_exception": "无明确阈值",
                        "evidence_summary": "测试会话证据。",
                        "confidence": 0.9,
                        "requires_review": False,
                    }
                ]
            },
            request_audit={},
            response_audit={},
        )

    def cluster_atomic_units(self, units: list[dict[str, str]]) -> MimoLabelResult:
        self._cluster_calls.append(
            {str(unit["product_category"]) for unit in units}
        )
        return MimoLabelResult(
            candidate={
                "clusters": [
                    {
                        "cluster_id": "C001",
                        "theme_name": "同品类测试主题",
                        "member_atomic_ids": [
                            str(unit["unit_id"]) for unit in units
                        ],
                        "shared_knowledge_definition": "同一主题定义。",
                        "merge_basis": "同一品类、同一对象和处理方式。",
                    }
                ],
                "split_requests": [],
                "review_requests": [],
            },
            request_audit={},
            response_audit={},
        )


def test_category_jobs_cluster_independently_and_write_combined_workbook(
    tmp_path: Path,
) -> None:
    rows = preprocess_source_rows(
        [
            {
                "数据ID": "PHONE-001",
                "产品类型": "手机",
                "一级分类": "显示问题",
                "聊天内容": "手机屏幕有色斑怎么确认",
                "核心问题": "手机屏幕色斑确认",
            },
            {
                "数据ID": "PHONE-002",
                "产品类型": "手机",
                "一级分类": "外观问题",
                "聊天内容": "手机屏幕有色斑如何处理",
                "核心问题": "手机屏幕色斑确认",
            },
            {
                "数据ID": "TABLET-001",
                "产品类型": "平板",
                "一级分类": "显示问题",
                "聊天内容": "平板屏幕有色斑怎么确认",
                "核心问题": "平板屏幕色斑确认",
            },
        ]
    )
    cluster_calls: list[set[str]] = []
    completed_categories: list[str] = []

    results = run_category_cluster_jobs(
        rows,
        reviewer_factory=lambda: _CategoryMimo(cluster_calls),
        category_workers=2,
        on_category_completed=lambda result: completed_categories.append(
            str(result["product_category"])
        ),
    )

    assert {result["product_category"] for result in results} == {"手机", "平板电脑"}
    assert set(completed_categories) == {"手机", "平板电脑"}
    assert cluster_calls == [{"手机"}]

    workbook_path = tmp_path / "主题聚类人工审核表.xlsx"
    write_cluster_validation_workbook(
        workbook_path,
        results,
        source_row_count=len(rows),
        excluded_rows=[],
    )

    workbook = load_workbook(workbook_path)
    review_rows = list(workbook["主题簇审核总表"].values)
    summary_rows = list(workbook["品类运行汇总"].values)
    assert len(review_rows) == 4
    assert len(summary_rows) == 4
    assert {row[1] for row in review_rows[1:]} == {"手机", "平板电脑"}
    assert {row[0] for row in summary_rows[2:]} == {"手机", "平板电脑"}


def test_direct_cluster_retries_smaller_batches_before_using_singletons() -> None:
    class RetryMimo(_CategoryMimo):
        def __init__(self) -> None:
            super().__init__([])
            self.batch_sizes: list[int] = []

        def cluster_atomic_units(self, units: list[dict[str, str]]) -> MimoLabelResult:
            self.batch_sizes.append(len(units))
            if len(units) == 4:
                raise MimoError("模拟大批输出校验失败")
            return MimoLabelResult(
                candidate={
                    "clusters": [
                        {
                            "member_atomic_ids": [str(unit["unit_id"]) for unit in units],
                            "theme_name": "拆批重试后的同主题",
                            "shared_knowledge_definition": "同一主题定义。",
                            "merge_basis": "同一品类、同一对象和处理方式。",
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
            "数据ID": f"PHONE-{index}",
            "产品类型": "手机",
            "一级分类": "显示问题",
            "聊天内容": "手机屏幕有色斑怎么确认",
            "核心问题": "手机屏幕色斑确认",
        }
        for index in range(1, 5)
    ]
    reviewer = RetryMimo()

    groups, meta = _direct_mimo_topic_groups(rows, reviewer, batch_size=4)

    assert reviewer.batch_sizes == [4, 2, 2]
    assert [len(member_rows) for _key, member_rows in groups] == [2, 2]
    assert meta["direct_cluster_failed"] == 1
    assert meta["direct_cluster_retry_splits"] == 1
    assert meta["direct_cluster_retry_succeeded"] == 2
    assert meta["direct_cluster_retry_exhausted_batches"] == 0
    assert all(
        row["_聚类决策"] == "纯大模型1-N聚类（拆批重试）"
        for _key, member_rows in groups
        for row in member_rows
    )
