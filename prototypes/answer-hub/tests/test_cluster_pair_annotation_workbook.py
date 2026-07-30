from __future__ import annotations

from scripts.build_cluster_pair_annotation_workbook import build_pair_annotation_rows


def test_pair_annotation_rows_keep_fixed_pair_fields_for_human_labeling() -> None:
    rows = build_pair_annotation_rows(
        [
            {
                "样本ID": "H001",
                "产品类型": "手机",
                "机型": "iPhone",
                "核心问题": "全新机怎么判",
                "聊天内容": "可以按全新机",
            },
            {
                "样本ID": "H002",
                "产品类型": "手机",
                "机型": "iPhone Pro",
                "核心问题": "塑封全新机怎么判",
                "聊天内容": "有塑封可以按全新机",
            },
        ],
        {
            "pairs": [
                {
                    "pair_id": "P001",
                    "left_id": "H001",
                    "right_id": "H002",
                    "new_prediction": "同一主题",
                }
            ]
        },
    )

    assert rows == [
        {
            "配对ID": "P001",
            "会话A_样本ID": "H001",
            "会话A_产品": "手机",
            "会话A_机型": "iPhone",
            "会话A_核心问题": "全新机怎么判",
            "会话A_聊天内容": "可以按全新机",
            "会话B_样本ID": "H002",
            "会话B_产品": "手机",
            "会话B_机型": "iPhone Pro",
            "会话B_核心问题": "塑封全新机怎么判",
            "会话B_聊天内容": "有塑封可以按全新机",
            "人工判断": "",
            "人工关键差异/依据": "",
            "标注人": "",
            "标注时间": "",
            "系统预测（后续回填）": "同一主题",
            "是否正确": "",
        }
    ]


def test_pair_annotation_rows_can_filter_cross_product_pairs() -> None:
    rows = build_pair_annotation_rows(
        [
            {
                "样本ID": "H001",
                "产品类型": "电脑",
                "机型": "ThinkPad",
                "核心问题": "硬盘怎么判",
                "聊天内容": "硬盘正常",
            },
            {
                "样本ID": "H002",
                "产品类型": "耳机",
                "机型": "AirPods",
                "核心问题": "耳机缺失怎么判",
                "聊天内容": "单只缺失",
            },
        ],
        {
            "pairs": [
                {
                    "pair_id": "P001",
                    "left_id": "H001",
                    "right_id": "H002",
                    "new_prediction": "同一主题",
                }
            ]
        },
        same_product_only=True,
    )

    assert rows == []
