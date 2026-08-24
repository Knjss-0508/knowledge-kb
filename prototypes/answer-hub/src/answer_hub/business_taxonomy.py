from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import re

from .product_taxonomy import is_concrete_unconfigured_product


SELF_OPERATED_BUSINESS_LINE_CODE = "self_operated"
SELF_OPERATED_BUSINESS_LINE_NAME = "自营回收"
AGGREGATE_BUSINESS_LINE_CODE = "aggregate"
AGGREGATE_BUSINESS_LINE_NAME = "聚合回收"
UNKNOWN_BUSINESS_LINE_NAME = "待确认"
DEFAULT_BUSINESS_TAXONOMY_PATH = Path(__file__).with_name("business_lines.json")


@dataclass(frozen=True)
class BusinessLine:
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    active: bool = True
    product_categories_configured: bool = False
    cz_category_path: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return re.sub(r"[\s_\-/／]+", "", _text(value).lower())


def business_taxonomy_path(path: str | Path | None = None) -> Path:
    configured = _text(path) or _text(
        os.getenv("ANSWER_HUB_BUSINESS_TAXONOMY_PATH")
    )
    return Path(configured) if configured else DEFAULT_BUSINESS_TAXONOMY_PATH


@lru_cache(maxsize=8)
def _resolved_taxonomy_path(path_text: str) -> str:
    return str(Path(path_text).resolve())


def _taxonomy_cache_key(path: str | Path | None = None) -> str:
    return _resolved_taxonomy_path(str(business_taxonomy_path(path)))


@lru_cache(maxsize=8)
def _load_business_taxonomy(
    path_text: str,
) -> tuple[tuple[BusinessLine, ...], str, str]:
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(f"回收业务层级配置不存在：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("回收业务层级配置必须是 JSON 对象")
    rows = payload.get("business_lines")
    if not isinstance(rows, list):
        raise ValueError("回收业务层级配置必须包含 business_lines 数组")

    lines: list[BusinessLine] = []
    seen_codes: set[str] = set()
    seen_names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("回收业务层级配置项必须是 JSON 对象")
        code = _text(row.get("code"))
        name = _text(row.get("name"))
        if not code or not name:
            raise ValueError("回收业务层级配置项缺少 code 或 name")
        if code in seen_codes or name in seen_names:
            raise ValueError(f"回收业务层级编码或名称重复：{code} / {name}")
        aliases = tuple(
            dict.fromkeys(
                alias
                for alias in (
                    _text(value)
                    for value in row.get("aliases") or []
                )
                if alias and alias != name
            )
        )
        lines.append(
            BusinessLine(
                code=code,
                name=name,
                aliases=aliases,
                active=bool(row.get("active", True)),
                product_categories_configured=bool(
                    row.get("product_categories_configured", False)
                ),
                cz_category_path=_text(row.get("cz_category_path")) or name,
            )
        )
        seen_codes.add(code)
        seen_names.add(name)

    default_code = _text(payload.get("default_code"))
    if not lines or default_code not in seen_codes:
        raise ValueError("回收业务层级配置缺少有效的 default_code")
    return tuple(lines), default_code, _text(payload.get("version"))


def load_business_lines(
    path: str | Path | None = None,
    *,
    active_only: bool = True,
) -> tuple[BusinessLine, ...]:
    lines, _default_code, _version = _load_business_taxonomy(
        _taxonomy_cache_key(path)
    )
    return tuple(line for line in lines if line.active) if active_only else lines


def configured_business_line_names(
    path: str | Path | None = None,
) -> tuple[str, ...]:
    return tuple(line.name for line in load_business_lines(path))


def resolve_business_line(
    value: Any,
    path: str | Path | None = None,
) -> BusinessLine | None:
    candidate = _key(value)
    if not candidate:
        return None
    for line in load_business_lines(path):
        values = (line.code, line.name, *line.aliases)
        if candidate in {_key(item) for item in values}:
            return line
    return None


def default_business_line(
    path: str | Path | None = None,
) -> BusinessLine:
    lines, default_code, _version = _load_business_taxonomy(
        _taxonomy_cache_key(path)
    )
    return next(line for line in lines if line.code == default_code)


def business_line_from_record(
    record: dict[str, Any],
    path: str | Path | None = None,
) -> BusinessLine | None:
    explicit = next(
        (
            _text(record.get(field))
            for field in (
                "回收业务层级编码",
                "回收业务层级",
                "回收业务",
                "业务线",
                "business_line",
            )
            if _text(record.get(field))
        ),
        "",
    )
    if explicit:
        return resolve_business_line(explicit, path)
    product_marker = resolve_business_line(record.get("产品类型"), path)
    if product_marker:
        return product_marker
    if is_concrete_unconfigured_product(record.get("产品类型")):
        return next(
            line
            for line in load_business_lines(path)
            if line.code == AGGREGATE_BUSINESS_LINE_CODE
        )
    return default_business_line(path)


def canonical_business_line_name(
    value: Any,
    path: str | Path | None = None,
    *,
    unknown: str = UNKNOWN_BUSINESS_LINE_NAME,
) -> str:
    line = resolve_business_line(value, path)
    return line.name if line else unknown


def canonical_business_line_code(
    value: Any,
    path: str | Path | None = None,
    *,
    unknown: str = "",
) -> str:
    line = resolve_business_line(value, path)
    return line.code if line else unknown


def cz_applicable_category_path(
    business_line: Any,
    product_category: Any = "",
    path: str | Path | None = None,
) -> str:
    line = resolve_business_line(business_line, path)
    root = line.cz_category_path if line else _text(business_line)
    product = _text(product_category)
    if not root:
        return product
    return f"{root}/{product}" if product else root


def business_line_metadata(
    path: str | Path | None = None,
) -> dict[str, Any]:
    resolved_path = Path(_taxonomy_cache_key(path))
    lines, default_code, version = _load_business_taxonomy(
        str(resolved_path)
    )
    try:
        digest = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    return {
        "version": version,
        "default_code": default_code,
        "path": str(resolved_path),
        "digest": digest,
        "lines": {
            line.code: {
                "name": line.name,
                "product_categories_configured": (
                    line.product_categories_configured
                ),
                "cz_category_path": line.cz_category_path,
            }
            for line in lines
        },
    }
