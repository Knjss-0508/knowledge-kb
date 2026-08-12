from __future__ import annotations

from scripts.eval_cluster_fixed_pairs import evaluate_fixed_pairs


def _result_with_units(units: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pairs": [
            {
                "pair_id": "P001",
                "left_id": "S999",
                "right_id": "S998",
                "new_prediction": "不同主题",
            }
        ],
        "schemes": {"new": {"units": units}},
    }


def test_fixed_pair_eval_aligns_by_sample_ids_not_pair_ids() -> None:
    result = _result_with_units(
        [
            {
                "unit_id": "S001-01",
                "sample_id": "S001",
                "conversation_type": "single_topic",
                "cluster_id": "C001",
            },
            {
                "unit_id": "S002-01",
                "sample_id": "S002",
                "conversation_type": "single_topic",
                "cluster_id": "C001",
            },
        ]
    )
    sample_rows = [
        {"样本ID": "S001", "聊天内容": ""},
        {"样本ID": "S002", "聊天内容": ""},
    ]
    gold_rows = [
        {
            "配对ID": "P001",
            "会话A_样本ID": "S001",
            "会话B_样本ID": "S002",
            "人工判断": "同一主题",
        }
    ]

    summary = evaluate_fixed_pairs(result, sample_rows, gold_rows)

    assert summary["overall"] == {"correct": 1, "total": 1, "accuracy": 1.0}
    assert summary["wrong"] == []


def test_fixed_pair_eval_uses_local_multi_topic_rescue() -> None:
    result = _result_with_units(
        [
            {
                "unit_id": "S038-01",
                "sample_id": "S038",
                "conversation_type": "single_topic",
                "cluster_id": "C041",
            },
            {
                "unit_id": "S050-01",
                "sample_id": "S050",
                "conversation_type": "single_topic",
                "cluster_id": "C041",
            },
        ]
    )
    sample_rows = [
        {
            "样本ID": "S038",
            "核心问题": "底部标签被撕，需要核对具体型号",
            "聊天内容": (
                "怎么看第几款\n"
                "怎么核对\n"
                "有没有什么问题\n"
                "后壳标签人为去除的话要勾选后壳无序列号\n"
                "显卡硬盘这些有问题吗\n"
                "内存硬盘都是品牌的"
            ),
        },
        {"样本ID": "S050", "聊天内容": "老师小型号没对上"},
    ]
    gold_rows = [
        {
            "配对ID": "P005",
            "会话A_样本ID": "S038",
            "会话B_样本ID": "S050",
            "人工判断": "多主题需拆分",
        }
    ]

    summary = evaluate_fixed_pairs(result, sample_rows, gold_rows)

    assert summary["overall"] == {"correct": 1, "total": 1, "accuracy": 1.0}
    assert summary["local_multi_topic_rescued_samples"] == ["S038"]
