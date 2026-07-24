from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from answer_hub.workflow import _read_source_rows


MULTI_TOPIC_MARKERS = (
    "以及",
    "同时",
    "另外",
    "并询问",
    "分别",
    "两个问题",
    "多个问题",
    "多项",
    "还需要",
    "一是",
    "二是",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stable_rank(row: dict[str, Any]) -> str:
    value = "|".join(
        (
            _text(row.get("工单ID")),
            _text(row.get("核心问题")),
            _text(row.get("聊天内容"))[:500],
        )
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _source_sheet(value: Any) -> str:
    text = _text(value)
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            return f"{parsed.month}.{parsed.day}"
        except ValueError:
            continue
    return ""


def _media_summary(row: dict[str, Any]) -> str:
    basis = _text(row.get("判定依据"))
    match = re.search(
        r"事实核查结果[:：]\s*(.*?)(?:\n\s*\n|采纳/排除逻辑[:：]|$)",
        basis,
        flags=re.DOTALL,
    )
    return _text(match.group(1) if match else "")[:500]


def _looks_multi_topic(row: dict[str, Any]) -> bool:
    text = f"{_text(row.get('核心问题'))}\n{_text(row.get('聊天内容'))}"
    return any(marker in text for marker in MULTI_TOPIC_MARKERS)


def _allocate_quotas(counts: Counter[str], sample_size: int) -> dict[str, int]:
    products = sorted(counts)
    if sample_size <= 0:
        raise ValueError("sample_size 必须大于0")
    if sample_size > sum(counts.values()):
        raise ValueError("sample_size 不能大于源记录数")
    if sample_size < len(products):
        selected_products = sorted(products, key=lambda product: (-counts[product], product))[
            :sample_size
        ]
        return {product: int(product in selected_products) for product in products}

    quotas = {product: 1 for product in products}
    remaining = sample_size - len(products)
    total = sum(counts.values())
    remainders: list[tuple[float, str]] = []
    for product in products:
        exact = remaining * counts[product] / total
        extra = min(counts[product] - 1, math.floor(exact))
        quotas[product] += extra
        remainders.append((exact - math.floor(exact), product))

    while sum(quotas.values()) < sample_size:
        progressed = False
        for _remainder, product in sorted(remainders, key=lambda item: (-item[0], item[1])):
            if quotas[product] >= counts[product]:
                continue
            quotas[product] += 1
            progressed = True
            if sum(quotas.values()) == sample_size:
                break
        if not progressed:
            raise RuntimeError("无法完成品类抽样配额分配")
    return quotas


def build_sample(
    rows: list[dict[str, Any]],
    sample_size: int = 60,
    excluded_source_keys: set[str] | None = None,
    sample_id_prefix: str = "S",
    sample_id_start: int = 1,
) -> list[dict[str, Any]]:
    excluded_keys = {
        _text(value)
        for value in (excluded_source_keys or set())
        if _text(value)
    }
    indexed_rows = [
        (index, dict(row))
        for index, row in enumerate(rows, start=2)
        if (
            _text(row.get("工单ID")) or _stable_rank(row)[:16]
        ) not in excluded_keys
    ]
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for source_row, row in indexed_rows:
        product = _text(row.get("产品类型")) or "待确认"
        grouped[product].append((source_row, row))

    counts = Counter({product: len(members) for product, members in grouped.items()})
    quotas = _allocate_quotas(counts, sample_size)
    selected: list[tuple[int, dict[str, Any]]] = []
    for product in sorted(grouped):
        members = sorted(
            grouped[product],
            key=lambda item: (_stable_rank(item[1]), item[0]),
        )
        selected.extend(members[: quotas[product]])

    selected.sort(key=lambda item: (_text(item[1].get("产品类型")), _stable_rank(item[1])))
    samples: list[dict[str, Any]] = []
    for index, (source_row, row) in enumerate(selected, start=sample_id_start):
        ticket_id = _text(row.get("工单ID"))
        image_links = _text(row.get("图片链接"))
        video_links = _text(row.get("视频链接"))
        samples.append(
            {
                "样本ID": f"{sample_id_prefix}{index:03d}",
                "源记录键": ticket_id or _stable_rank(row)[:16],
                "源工作表": _source_sheet(row.get("分析时间")),
                "源行号": source_row,
                "工单ID": ticket_id,
                "回收单号": _text(row.get("回收单号")),
                "机型": _text(row.get("机型")),
                "聊天内容": _text(row.get("聊天内容")),
                "核心问题": _text(row.get("核心问题")),
                "判定结论": _text(row.get("判定结论")),
                "判定依据": _text(row.get("判定依据")),
                "上游媒体分析摘要": _media_summary(row),
                "图片链接": image_links,
                "视频链接": video_links,
                "产品类型": _text(row.get("产品类型")) or "待确认",
                "一级分类": _text(row.get("一级分类")),
                "二级分类": _text(row.get("二级分类")),
                "疑似多主题抽样": _looks_multi_topic(row),
                "含图片": bool(image_links),
                "含视频": bool(video_links),
            }
        )
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description="从质检答疑案例库稳定抽取聚类A/B测试样本。")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=60)
    parser.add_argument(
        "--exclude-json",
        type=Path,
        help="排除另一个样本JSON中已经使用的源记录键。",
    )
    parser.add_argument("--sample-id-prefix", default="S")
    parser.add_argument("--sample-id-start", type=int, default=1)
    args = parser.parse_args()

    rows = _read_source_rows(args.source)
    excluded_source_keys: set[str] = set()
    if args.exclude_json:
        excluded_rows = json.loads(args.exclude_json.read_text(encoding="utf-8"))
        if not isinstance(excluded_rows, list):
            raise ValueError("exclude-json 必须是样本数组")
        excluded_source_keys = {
            _text(row.get("源记录键"))
            for row in excluded_rows
            if isinstance(row, dict) and _text(row.get("源记录键"))
        }
    samples = build_sample(
        rows,
        sample_size=args.sample_size,
        excluded_source_keys=excluded_source_keys,
        sample_id_prefix=_text(args.sample_id_prefix) or "S",
        sample_id_start=max(1, args.sample_id_start),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "source": str(args.source),
                "source_rows": len(rows),
                "excluded_source_rows": len(excluded_source_keys),
                "sample_rows": len(samples),
                "products": dict(Counter(row["产品类型"] for row in samples)),
                "multi_topic_candidates": sum(
                    bool(row["疑似多主题抽样"])
                    for row in samples
                ),
                "with_images": sum(bool(row["含图片"]) for row in samples),
                "with_videos": sum(bool(row["含视频"]) for row in samples),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
