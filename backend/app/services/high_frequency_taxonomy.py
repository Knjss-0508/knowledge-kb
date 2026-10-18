from __future__ import annotations

import re
from typing import Any


_CATEGORY_ALIASES = {
    "电脑": "笔记本",
    "笔记本电脑": "笔记本",
    "平板": "平板电脑",
    "耳麦": "耳机",
    "耳机/耳麦": "耳机",
}


def normalize_product_category(value: Any) -> str:
    """Return the stable product boundary used by high-frequency grouping."""

    category = re.sub(
        r"\s+",
        " ",
        str(value or "").replace("\u3000", " "),
    ).strip()
    return _CATEGORY_ALIASES.get(category, category or "未分类")
