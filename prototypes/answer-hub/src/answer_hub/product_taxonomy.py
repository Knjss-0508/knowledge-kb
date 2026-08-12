from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
import hashlib
import json
import os
import re


UNKNOWN_PRODUCT_NAME = "待确认"
UNKNOWN_PRODUCT_CODE = "pending"
DEFAULT_PRODUCT_TAXONOMY_PATH = Path(__file__).with_name("product_categories.json")
REQUIRED_PRODUCT_CATEGORIES = (
    ("phone", "手机"),
    ("tablet", "平板电脑"),
    ("watch", "智能手表"),
    ("headphones", "耳机/耳麦"),
    ("laptop", "笔记本"),
    ("game_console", "游戏机"),
    ("game_cartridge", "游戏卡带"),
    ("mirrorless_camera_body", "单电/微单机身"),
    ("dslr_camera_body", "单反机身"),
    ("camera_lens", "相机镜头"),
    ("stylus", "手写笔"),
    ("learning_device", "学习机"),
)
AMBIGUOUS_CAMERA_VALUES = frozenset({"相机", "相机机身", "camera_body"})
AMBIGUOUS_PRODUCT_VALUES = frozenset({"电脑"})


@dataclass(frozen=True)
class ProductCategory:
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    active: bool = True


def _text(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return re.sub(r"[\s_\-/／]+", "", _text(value).lower())


def product_taxonomy_path(path: str | Path | None = None) -> Path:
    configured = _text(path) or _text(os.getenv("ANSWER_HUB_PRODUCT_TAXONOMY_PATH"))
    return Path(configured) if configured else DEFAULT_PRODUCT_TAXONOMY_PATH


@lru_cache(maxsize=16)
def _resolved_product_taxonomy_path(path_text: str) -> str:
    return str(Path(path_text).resolve())


@lru_cache(maxsize=16)
def _load_product_categories(
    path_text: str,
    content_text: str,
) -> tuple[ProductCategory, ...]:
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(f"产品品类配置不存在：{path}")
    payload = json.loads(content_text)
    rows = payload.get("categories") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("产品品类配置必须包含 categories 数组")

    categories: list[ProductCategory] = []
    seen_codes: set[str] = set()
    seen_names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("产品品类配置项必须是 JSON 对象")
        code = _text(row.get("code"))
        name = _text(row.get("name"))
        if not code or not name:
            raise ValueError("产品品类配置项缺少 code 或 name")
        if code in seen_codes or name in seen_names:
            raise ValueError(f"产品品类编码或名称重复：{code} / {name}")
        aliases = tuple(
            dict.fromkeys(
                alias
                for alias in (_text(value) for value in row.get("aliases") or [])
                if alias and alias != name
            )
        )
        categories.append(
            ProductCategory(
                code=code,
                name=name,
                aliases=aliases,
                active=bool(row.get("active", True)),
            )
        )
        seen_codes.add(code)
        seen_names.add(name)
    if not categories:
        raise ValueError("产品品类配置不能为空")
    active_contract = tuple(
        (category.code, category.name)
        for category in categories
        if category.active
    )
    if active_contract != REQUIRED_PRODUCT_CATEGORIES:
        raise ValueError(
            "产品品类配置必须保持固定12项及稳定编码："
            + "、".join(name for _code, name in REQUIRED_PRODUCT_CATEGORIES)
        )
    ambiguous_aliases = sorted(
        {
            value
            for category in categories
            if category.active
            for value in category.aliases
            if value.casefold() in AMBIGUOUS_CAMERA_VALUES
        }
    )
    if ambiguous_aliases:
        raise ValueError(
            "产品品类配置不得包含模糊相机别名："
            + "、".join(ambiguous_aliases)
        )
    ambiguous_product_aliases = sorted(
        {
            value
            for category in categories
            if category.active
            for value in category.aliases
            if value.casefold() in AMBIGUOUS_PRODUCT_VALUES
        }
    )
    if ambiguous_product_aliases:
        raise ValueError(
            "产品品类配置不得包含模糊设备别名："
            + "、".join(ambiguous_product_aliases)
        )
    return tuple(categories)


@lru_cache(maxsize=1)
def _load_default_product_categories() -> tuple[ProductCategory, ...]:
    resolved = Path(
        _resolved_product_taxonomy_path(
            str(DEFAULT_PRODUCT_TAXONOMY_PATH)
        )
    )
    if not resolved.is_file():
        raise FileNotFoundError(f"产品品类配置不存在：{resolved}")
    return _load_product_categories(
        str(resolved),
        resolved.read_text(encoding="utf-8"),
    )


def load_product_categories(
    path: str | Path | None = None,
    *,
    active_only: bool = True,
) -> tuple[ProductCategory, ...]:
    configured = _text(path) or _text(
        os.getenv("ANSWER_HUB_PRODUCT_TAXONOMY_PATH")
    )
    if not configured:
        categories = _load_default_product_categories()
        return (
            tuple(category for category in categories if category.active)
            if active_only
            else categories
        )
    resolved = Path(
        _resolved_product_taxonomy_path(str(Path(configured)))
    )
    if not resolved.is_file():
        raise FileNotFoundError(f"产品品类配置不存在：{resolved}")
    content_text = resolved.read_text(encoding="utf-8")
    categories = _load_product_categories(
        str(resolved),
        content_text,
    )
    return tuple(category for category in categories if category.active) if active_only else categories


def product_taxonomy_metadata(
    path: str | Path | None = None,
) -> dict[str, Any]:
    resolved = Path(
        _resolved_product_taxonomy_path(str(product_taxonomy_path(path)))
    )
    raw = resolved.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    return {
        "loaded": True,
        "path": str(resolved),
        "version": _text(payload.get("version")) if isinstance(payload, dict) else "",
        "digest": hashlib.sha256(raw).hexdigest(),
        "product_categories": list(configured_product_names(resolved)),
    }


def configured_product_names(path: str | Path | None = None) -> tuple[str, ...]:
    return tuple(category.name for category in load_product_categories(path))


def configured_product_codes(path: str | Path | None = None) -> tuple[str, ...]:
    return tuple(category.code for category in load_product_categories(path))


def product_category_prompt(path: str | Path | None = None) -> str:
    return "、".join(configured_product_names(path))


def resolve_product_category(
    value: Any,
    path: str | Path | None = None,
) -> ProductCategory | None:
    candidate = _key(value)
    if not candidate:
        return None
    for category in load_product_categories(path):
        values = (category.code, category.name, *category.aliases)
        if candidate in {_key(item) for item in values}:
            return category
    return None


def infer_product_category(
    values: Iterable[Any],
    path: str | Path | None = None,
) -> ProductCategory | None:
    categories = load_product_categories(path)
    for value in values:
        text = _text(value)
        direct = resolve_product_category(text, path)
        if direct:
            return direct
        normalized = _key(text)
        if not normalized:
            continue
        matches = [
            category
            for category in categories
            if any(
                _key(alias) and _key(alias) in normalized
                for alias in (category.name, *category.aliases)
            )
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def canonical_product_name(
    value: Any,
    path: str | Path | None = None,
    *,
    unknown: str = UNKNOWN_PRODUCT_NAME,
) -> str:
    category = resolve_product_category(value, path)
    return category.name if category else unknown


def canonical_product_code(
    value: Any,
    path: str | Path | None = None,
    *,
    unknown: str = "",
) -> str:
    category = resolve_product_category(value, path)
    return category.code if category else unknown


def product_from_scope(
    scope: Any,
    path: str | Path | None = None,
) -> str:
    text = _text(scope)
    prefix = re.split(r"[-—–]", text, maxsplit=1)[0].strip()
    category = resolve_product_category(prefix, path)
    return category.name if category else prefix


def normalize_product_scope(
    product_type: Any,
    scope: Any = "",
    path: str | Path | None = None,
    *,
    default_suffix: str = "通用",
) -> str:
    category = resolve_product_category(product_type, path)
    return category.name if category else UNKNOWN_PRODUCT_NAME
