from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


PAIR_COLUMNS = [
    "配对ID",
    "会话A_样本ID",
    "会话A_产品",
    "会话A_机型",
    "会话A_核心问题",
    "会话A_聊天内容",
    "会话B_样本ID",
    "会话B_产品",
    "会话B_机型",
    "会话B_核心问题",
    "会话B_聊天内容",
    "人工判断",
    "人工关键差异/依据",
    "标注人",
    "标注时间",
    "系统预测（后续回填）",
    "是否正确",
]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def build_pair_annotation_rows(
    sample_rows: list[dict[str, Any]],
    result_payload: dict[str, Any],
    same_product_only: bool = False,
) -> list[dict[str, str]]:
    samples = {_text(row.get("样本ID")): row for row in sample_rows}
    rows: list[dict[str, str]] = []
    for pair in result_payload.get("pairs") or []:
        left_id = _text(pair.get("left_id"))
        right_id = _text(pair.get("right_id"))
        left = samples.get(left_id, {})
        right = samples.get(right_id, {})
        left_product = _text(left.get("产品类型"))
        right_product = _text(right.get("产品类型"))
        if same_product_only and left_product and right_product and left_product != right_product:
            continue
        rows.append(
            {
                "配对ID": _text(pair.get("pair_id")),
                "会话A_样本ID": left_id,
                "会话A_产品": left_product,
                "会话A_机型": _text(left.get("机型")),
                "会话A_核心问题": _text(left.get("核心问题")),
                "会话A_聊天内容": _text(left.get("聊天内容")),
                "会话B_样本ID": right_id,
                "会话B_产品": right_product,
                "会话B_机型": _text(right.get("机型")),
                "会话B_核心问题": _text(right.get("核心问题")),
                "会话B_聊天内容": _text(right.get("聊天内容")),
                "人工判断": "",
                "人工关键差异/依据": "",
                "标注人": "",
                "标注时间": "",
                "系统预测（后续回填）": _text(pair.get("new_prediction")),
                "是否正确": "",
            }
        )
    return rows


def write_pair_annotation_workbook(
    sample_rows: list[dict[str, Any]],
    result_payload: dict[str, Any],
    output_path: Path,
    same_product_only: bool = False,
) -> dict[str, Any]:
    rows = build_pair_annotation_rows(sample_rows, result_payload, same_product_only)
    guide_rows = [
        {"项目": "标注目标", "说明": "人工判断固定 A/B 会话对是否属于同一主题，用于新抽样 holdout 验证。"},
        {"项目": "同一主题", "说明": "A/B 可由同一条知识完整回答，适用范围、对象、判定目标、处理路径基本一致。"},
        {"项目": "不同主题", "说明": "A/B 需要不同知识正文、不同判定对象或不同处理路径。"},
        {"项目": "多主题需拆分", "说明": "任一会话本身包含多个独立主题，需要先拆成原子主题再判断。"},
        {"项目": "不确定", "说明": "证据不足、图片不可判断或人工无法确定时填写；计算准确率时默认跳过。"},
    ]
    stats_rows = [
        {"指标": "验证对数", "结果": len(rows)},
        {"指标": "已填写人工判断", "结果": 0},
        {"指标": "已回填模型预测数", "结果": sum(bool(row["系统预测（后续回填）"]) for row in rows)},
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(guide_rows).to_excel(writer, sheet_name="标注说明", index=False)
        pd.DataFrame(rows, columns=PAIR_COLUMNS).to_excel(
            writer,
            sheet_name="配对盲标",
            index=False,
        )
        pd.DataFrame(stats_rows).to_excel(writer, sheet_name="结果统计", index=False)
    return {
        "pair_count": len(rows),
        "same_product_only": same_product_only,
        "output_xlsx": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-json", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--output-xlsx", type=Path, required=True)
    parser.add_argument("--same-product-only", action="store_true")
    args = parser.parse_args()

    summary = write_pair_annotation_workbook(
        _read_json(args.sample_json),
        _read_json(args.result_json),
        args.output_xlsx,
        same_product_only=args.same_product_only,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
