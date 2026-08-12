from __future__ import annotations

from collections import Counter

import scripts.build_cluster_sample as cluster_sample


build_sample = cluster_sample.build_sample
_media_summary = cluster_sample._media_summary


def _row(index: int, product: str) -> dict[str, object]:
    return {
        "分析时间": "2026-07-22 10:30",
        "工单ID": f"T-{index:03d}",
        "回收单号": f"R-{index:03d}",
        "机型": f"{product}机型",
        "聊天内容": f"{product}问题{index}，请确认。",
        "核心问题": f"{product}问题{index}如何判定",
        "判定结论": "按标准处理",
        "判定依据": "事实核查结果：现场证据支持该结论。\n\n采纳/排除逻辑：采纳。",
        "产品类型": product,
        "一级分类": "功能问题",
        "二级分类": "测试分类",
        "图片链接": "https://example.com/a.jpg" if index % 2 == 0 else "",
        "视频链接": "",
    }


def _topic_row(index: int, product: str, category_l2: str) -> dict[str, object]:
    row = _row(index, product)
    row["二级分类"] = category_l2
    row["核心问题"] = f"{product}{category_l2}问题{index}如何判定"
    return row


def test_build_sample_is_deterministic_and_covers_products() -> None:
    rows = [
        *[_row(index, "手机") for index in range(1, 9)],
        *[_row(index, "笔记本") for index in range(9, 13)],
        _row(13, "相机"),
    ]

    first = build_sample(rows, sample_size=6)
    second = build_sample(rows, sample_size=6)

    assert first == second
    assert len(first) == 6
    assert {row["产品类型"] for row in first} == {"手机", "笔记本", "相机"}
    assert [row["样本ID"] for row in first] == [f"S{index:03d}" for index in range(1, 7)]
    assert all(row["源工作表"] == "7.22" for row in first)


def test_build_sample_prefers_pairs_from_dense_topic_groups() -> None:
    rows = [
        *[_topic_row(index, "手机", "屏幕外观") for index in range(1, 5)],
        _topic_row(5, "手机", "电池健康"),
        _topic_row(6, "手机", "存储容量"),
        *[_topic_row(index, "笔记本", "小型号") for index in range(7, 9)],
    ]

    sample = build_sample(rows, sample_size=6)

    phone_screen_rows = [
        row
        for row in sample
        if row["产品类型"] == "手机" and row["二级分类"] == "屏幕外观"
    ]
    computer_model_rows = [
        row
        for row in sample
        if row["产品类型"] == "笔记本" and row["二级分类"] == "小型号"
    ]
    assert len(phone_screen_rows) >= 2
    assert len(computer_model_rows) >= 2


def test_build_sample_prefers_dense_groups_even_when_hash_ranks_singletons_first(
    monkeypatch,
) -> None:
    rows = [
        *[_topic_row(index, "手机", "屏幕外观") for index in range(1, 5)],
        *[_topic_row(index, "手机", "小型号") for index in range(5, 9)],
        *[_topic_row(index, "手机", f"单例{index}") for index in range(9, 17)],
    ]

    def rank_singletons_first(row: dict[str, object]) -> str:
        category_l2 = str(row.get("二级分类") or "")
        prefix = "z" if category_l2 in {"屏幕外观", "小型号"} else "a"
        return f"{prefix}-{row.get('工单ID')}"

    monkeypatch.setattr(cluster_sample, "_stable_rank", rank_singletons_first)

    sample = build_sample(rows, sample_size=6)
    counts = Counter(row["二级分类"] for row in sample)

    assert counts["屏幕外观"] >= 2
    assert counts["小型号"] >= 2


def test_build_sample_excludes_existing_sources_and_supports_custom_ids() -> None:
    rows = [
        _row(1, "手机"),
        _row(2, "手机"),
    ]

    sample = build_sample(
        rows,
        sample_size=1,
        excluded_source_keys={"T-001"},
        sample_id_prefix="N",
        sample_id_start=61,
    )

    assert sample[0]["样本ID"] == "N061"
    assert sample[0]["源记录键"] == "T-002"


def test_media_summary_extracts_fact_section() -> None:
    row = {
        "判定依据": (
            "平台标准依据：测试。\n"
            "事实核查结果：图片和聊天均支持该结论。\n\n"
            "采纳/排除逻辑：采纳。"
        )
    }

    assert _media_summary(row) == "图片和聊天均支持该结论。"
