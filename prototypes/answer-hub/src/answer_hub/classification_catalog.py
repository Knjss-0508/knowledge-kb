from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import json
import re

from .product_taxonomy import canonical_product_name


GENERIC_CLASS_TERMS = frozenset(
    {
        "",
        "/",
        "有",
        "无",
        "正常",
        "其他",
        "未知",
        "不支持",
        "无以上问题",
        "0",
        "1",
    }
)

_QUERY_SYNONYM_GROUPS = (
    ("SN", "SN码", "序列号"),
    ("IMEI", "IMEI码", "设备识别码"),
)

_PRODUCT_TERMS = frozenset(
    {
        "平板",
        "平板电脑",
        "手机",
        "笔记本",
        "游戏机",
        "游戏卡带",
        "相机机身",
        "相机镜头",
        "耳机",
        "耳机耳麦",
        "手表",
        "智能手表",
        "手写笔",
        "学习机",
    }
)

_LOW_INFORMATION_TERMS = frozenset(
    {
        "屏幕",
        "外观",
        "显示",
        "功能",
        "情况",
        "异常",
        "基本情况",
        "设备功能情况",
        "屏幕外观情况",
        "拆修及浸液情况",
        "机身外观",
        "配件状况",
    }
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", "", _clean(value).casefold())


def _as_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = ()
    return tuple(dict.fromkeys(_clean(item) for item in values if _clean(item)))


def _split_search_terms(value: Any) -> tuple[str, ...]:
    """Split path/leaf labels into useful searchable phrases."""
    text = _clean(value)
    if not text:
        return ()
    parts = re.split(
        r"[\s>/／、，,;；:：|｜()（）\[\]【】]+",
        text,
    )
    return tuple(
        dict.fromkeys(
            part.strip()
            for part in parts
            if part.strip() and not _is_generic_term(part)
        )
    )


def _is_generic_term(value: str) -> bool:
    normalized = _normalize(value)
    return normalized in {_normalize(item) for item in GENERIC_CLASS_TERMS} or len(
        normalized
    ) < 2


@dataclass(frozen=True)
class ClassificationCatalogItem:
    class_id: str
    category: str
    category_id: str
    path: tuple[str, ...]
    path_str: str
    leaf_level: int
    leaf: str
    upper: tuple[str, ...]
    aliases: tuple[str, ...]
    keywords: tuple[str, ...]
    degree: str
    definition: str
    detection_method: str
    is_negative: bool
    search_text: str
    source_file: str = ""
    source_row: int = 0

    @property
    def parent_path(self) -> str:
        if self.upper:
            return " > ".join((self.category, *self.upper))
        return " > ".join(self.path[:-1] or self.path)

    def specific_terms(self) -> tuple[str, ...]:
        terms = [
            *self.aliases,
            *self.keywords,
            *self.upper,
            *self.path,
            *self.path_str.split(">"),
            self.leaf,
        ]
        expanded_terms = [
            fragment
            for term in terms
            for fragment in _split_search_terms(term)
        ]
        return tuple(
            dict.fromkeys(
                term
                for term in expanded_terms
                if not _is_generic_term(term)
                and len(_normalize(term)) >= 2
            )
        )

    def path_terms(self) -> tuple[str, ...]:
        terms = [
            *self.aliases,
            *self.upper,
            *self.path,
            *self.path_str.split(">"),
            self.leaf,
        ]
        category = _normalize(self.category)
        fragments = [
            fragment
            for term in terms
            for fragment in _split_search_terms(term)
            if _normalize(fragment) != category
        ]
        return tuple(
            dict.fromkeys(
                fragment
                for fragment in fragments
                if not _is_generic_term(fragment)
                and len(_normalize(fragment)) >= 2
            )
        )

    def searchable_terms(self) -> tuple[str, ...]:
        """Terms that describe the object/target, excluding product-only words."""
        return tuple(
            term
            for term in self.path_terms()
            if _normalize(term) not in _PRODUCT_TERMS
            and _normalize(term) not in _LOW_INFORMATION_TERMS
        )


@dataclass(frozen=True)
class ClassificationCatalogMatch:
    item: ClassificationCatalogItem
    score: float
    matched_terms: tuple[str, ...]

    @property
    def class_id(self) -> str:
        return self.item.class_id

    @property
    def path_candidate(self) -> str:
        return self.item.parent_path


def _normalize_row(data: dict[str, Any]) -> ClassificationCatalogItem:
    raw_path = data.get("path")
    path = _as_strings(raw_path)
    path_str = _clean(data.get("path_str")) or " > ".join(path)
    try:
        leaf_level = int(data.get("leaf_level") or 0)
    except (TypeError, ValueError):
        leaf_level = 0
    source = data.get("source")
    if not isinstance(source, dict):
        source = {}
    try:
        source_row = int(source.get("row") or 0)
    except (TypeError, ValueError):
        source_row = 0
    return ClassificationCatalogItem(
        class_id=_clean(data.get("class_id")),
        category=canonical_product_name(
            _clean(data.get("category")),
            unknown=_clean(data.get("category")),
        ),
        category_id=_clean(data.get("category_id")),
        path=path,
        path_str=path_str,
        leaf_level=leaf_level,
        leaf=_clean(data.get("leaf")),
        upper=_as_strings(data.get("upper")),
        aliases=_as_strings(data.get("aliases")),
        keywords=_as_strings(data.get("keywords")),
        degree=_clean(data.get("degree")),
        definition=_clean(data.get("definition")),
        detection_method=_clean(data.get("detection_method")),
        is_negative=bool(data.get("is_negative")),
        search_text=_clean(data.get("search_text")),
        source_file=_clean(source.get("file")),
        source_row=source_row,
    )


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.casefold() == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("classes") or payload.get("items") or payload.get("rows")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("分类库 JSON 必须包含 classes、items 或 rows 数组")
    return [dict(row) for row in rows if isinstance(row, dict)]


def load_classification_catalog(
    path: str | Path | None,
) -> tuple[ClassificationCatalogItem, ...]:
    if not path:
        return ()
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"分类库文件不存在: {file_path}")
    rows = _read_rows(file_path)
    items = tuple(
        item
        for item in (_normalize_row(row) for row in rows)
        if item.class_id and item.category and item.path_str
    )
    if not items:
        raise ValueError(f"分类库没有有效分类记录: {file_path}")
    return items


def _query_fragments(query: str) -> tuple[str, ...]:
    text = _normalize(query)
    if not text:
        return ()
    fragments: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        # Short n-grams let a query such as “序列号在哪里查看” match a
        # keyword such as “SN码即产品序列号”, without treating every
        # single-character word as a positive signal.
        for size in range(2, min(8, len(chunk)) + 1):
            fragments.extend(
                chunk[index : index + size]
                for index in range(0, len(chunk) - size + 1)
            )
    return tuple(dict.fromkeys(fragments))


def _query_variants(query: str) -> tuple[str, ...]:
    normalized = _normalize(query)
    variants = [normalized]
    for group in _QUERY_SYNONYM_GROUPS:
        normalized_group = tuple(_normalize(item) for item in group)
        if any(item in normalized for item in normalized_group):
            variants.extend(normalized_group)
    return tuple(dict.fromkeys(variant for variant in variants if variant))


def _term_variants(term: str) -> tuple[str, ...]:
    normalized = _normalize(term)
    if not normalized:
        return ()
    variants = [normalized]
    simplified = normalized.replace("有", "").replace("的", "")
    if simplified and simplified != normalized:
        variants.append(simplified)
    for group in _QUERY_SYNONYM_GROUPS:
        normalized_group = tuple(_normalize(item) for item in group)
        if normalized in normalized_group:
            variants.extend(normalized_group)
    return tuple(
        dict.fromkeys(
            variant
            for variant in variants
            if not _is_generic_term(variant)
            and variant not in _PRODUCT_TERMS
            and variant not in _LOW_INFORMATION_TERMS
        )
    )


def retrieve_classification_matches(
    query: str,
    *,
    product_category: str,
    catalog: Iterable[ClassificationCatalogItem],
    top_k: int = 5,
    ambiguity_margin: float = 1.5,
) -> tuple[tuple[ClassificationCatalogMatch, ...], str]:
    """Retrieve classification candidates without deciding cluster merges."""
    product = canonical_product_name(product_category, unknown=_clean(product_category))
    normalized_query = _normalize(query)
    query_variants = _query_variants(query)
    query_fragments = set(_query_fragments(query))
    scored: list[ClassificationCatalogMatch] = []
    for item in catalog:
        if canonical_product_name(item.category, unknown=item.category) != product:
            continue
        matched_terms: list[str] = []
        score = 0.0
        for term in item.searchable_terms():
            for variant in _term_variants(term):
                if variant in query_variants:
                    score += 4.0 + min(5.0, len(variant) * 0.5)
                    matched_terms.append(term)
                    break
                if len(variant) >= 2 and variant in normalized_query:
                    score += 3.0 + min(3.0, len(variant) * 0.35)
                    matched_terms.append(variant)
                    break
        for keyword in item.keywords:
            normalized_keyword = _normalize(keyword)
            if not normalized_keyword:
                continue
            if normalized_keyword in query_variants:
                score += min(8.0, 2.0 + len(normalized_keyword) * 0.2)
                matched_terms.append(keyword)
                continue
            overlap = sorted(
                (
                    fragment
                    for fragment in query_fragments
                    if len(fragment) >= 4
                    and fragment not in _PRODUCT_TERMS
                    and fragment in normalized_keyword
                ),
                key=len,
                reverse=True,
            )
            if overlap:
                score += min(2.5, 0.5 + len(overlap[0]) * 0.2)
                matched_terms.append(overlap[0])
        if not matched_terms or score < 3.0:
            continue
        if item.is_negative:
            score -= 2.0
        if item.upper and any(
            _normalize(part) in normalized_query
            for part in item.upper
            if not _is_generic_term(part)
            and _normalize(part) not in {
                _normalize("基本情况"),
                _normalize("设备功能情况"),
                _normalize("屏幕外观情况"),
                _normalize("拆修及浸液情况"),
                _normalize("机身外观"),
                _normalize("配件状况"),
            }
        ):
            score += 2.0
        scored.append(
            ClassificationCatalogMatch(
                item=item,
                score=round(score, 4),
                matched_terms=tuple(dict.fromkeys(matched_terms)),
            )
        )
    scored.sort(
        key=lambda match: (
            -match.score,
            -len(match.item.parent_path),
            match.item.class_id,
        )
    )
    positive = [match for match in scored if not match.item.is_negative]
    selected = positive[: max(1, int(top_k))]
    if not selected:
        return (), "classification_not_matched"
    best_parent_path = selected[0].path_candidate
    parent_paths = {match.path_candidate for match in selected}
    status = "classification_matched"
    if len(parent_paths) > 1:
        best_score = selected[0].score
        next_path_score = max(
            (
                match.score
                for match in selected
                if match.path_candidate != selected[0].path_candidate
            ),
            default=-1.0,
        )
        if next_path_score >= best_score - ambiguity_margin:
            status = "classification_ambiguous"
    if status == "classification_matched":
        selected = [
            match
            for match in selected
            if match.path_candidate == best_parent_path
        ]
    return tuple(selected), status
