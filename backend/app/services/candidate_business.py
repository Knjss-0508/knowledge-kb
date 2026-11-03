"""候选回收类型的确定性规则。

第二部分已有“回收业务层级”时以它为准，因为聚合回收的品类库本来就
包含手机等上门品类。唯一例外是：层级写成自营回收、但具体品类不在
上门 12 品类内时，纠正为聚合回收——自营只覆盖这 12 类。
只有层级为空时才按产品品类推断。
"""

import re
from typing import Any

from app.services.applicability import scope_keys


DOORSTEP_PRODUCTS = (
    ("phone", "手机", "智能手机"),
    ("tablet", "平板电脑", "平板"),
    ("watch", "智能手表", "手表"),
    ("headphones", "耳机/耳麦", "耳机", "耳麦", "蓝牙耳机"),
    ("laptop", "笔记本", "笔记本电脑", "Laptop"),
    ("game_console", "游戏机", "游戏主机", "掌机"),
    ("game_cartridge", "游戏卡带", "游戏卡", "Switch卡带", "实体游戏卡"),
    ("mirrorless_camera_body", "单电/微单机身", "单电机身", "微单机身", "单电", "微单"),
    ("dslr_camera_body", "单反机身", "单反"),
    ("camera_lens", "相机镜头", "镜头"),
    ("stylus", "手写笔", "触控笔"),
    ("learning_device", "学习机", "学习平板"),
)
_DOORSTEP_KEYS = scope_keys([value for group in DOORSTEP_PRODUCTS for value in group], "category")
_UNKNOWN_KEYS = scope_keys(["待确认", "其他", "不确定", "通用", "全品类"], "category")


def _product_names(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        values = [values]
    names = []
    for value in values:
        if isinstance(value, dict):
            value = next((value[key] for key in (
                "categoryName", "category_name", "name", "label", "title", "text",
            ) if value.get(key)), "")
        text = str(value or "").strip()
        text = re.sub(r"^(?:自营回收|聚合回收|self_operated|aggregated)[/／>]+", "", text)
        if "|" in text or "｜" in text:
            text = re.split(r"[|｜]", text, maxsplit=1)[0].strip()
        if (not text or text.isdigit()
                or re.fullmatch(r"[0-9a-fA-F-]{8,}", text)
                or re.match(r"^(?:cat|category)[-_]", text, re.I)):
            continue
        names.append(text)
    return names


def business_type_for_products(
    values: Any,
    fallback_values: Any = None,
) -> str | None:
    """按产品品类返回业务类型；没有可识别品类时返回 ``None``。

    ``fallback_values`` 用于传入 Manhattan 原始选项：选项同时带
    ``categoryId`` 和 ``categoryName`` 时，以名称判断 12 品类归属。
    """
    names = _product_names(values)
    identifiers = _identifiers(values)
    if identifiers:
        resolved = _resolved_categories(identifiers, fallback_values)
        if resolved is None:
            return None
        names.extend(_product_names(resolved))
    concrete = scope_keys(names, "category") - _UNKNOWN_KEYS
    if not concrete:
        return None
    return "aggregated" if concrete - _DOORSTEP_KEYS else "self_operated"


def reconcile_business_type(
    explicit: Any,
    derived: str | None,
) -> str | None:
    """合并源业务类型与品类推导结果。

    显式业务类型优先；只有“显式自营 + 品类不在上门 12 类”这一种矛盾
    才纠正为聚合回收。聚合回收的品类库包含手机等上门品类，因此显式
    聚合不能按品类改回自营。
    """
    explicit_text = str(explicit or "").strip()
    if explicit_text in {"aggregated", "聚合回收", "聚合"}:
        return "aggregated"
    if explicit_text in {"self_operated", "自营回收", "自营"}:
        return "aggregated" if derived == "aggregated" else "self_operated"
    return derived


def _identifiers(values: Any) -> set[str]:
    """提取无法从文本本身识别为品类的类目 ID。"""
    raw_values = values if isinstance(values, (list, tuple)) else [values]
    return {
        str(value).strip()
        for value in raw_values
        if isinstance(value, (str, int))
        and str(value).strip()
        and not _product_names([value])
    }


def _resolved_categories(
    identifiers: set[str],
    fallback_values: Any,
) -> list[dict] | None:
    """把类目 ID 还原成 Manhattan 选项；任一 ID 无法还原时返回 ``None``。"""
    if not isinstance(fallback_values, (list, tuple)):
        return None
    resolved: list[dict] = []
    seen: set[str] = set()
    for category in fallback_values:
        if not isinstance(category, dict):
            continue
        category_id = next(
            (
                str(category[key]).strip()
                for key in ("categoryId", "category_id", "id", "code", "value")
                if category.get(key) not in (None, "")
            ),
            "",
        )
        if category_id in identifiers and category_id not in seen:
            seen.add(category_id)
            resolved.append(category)
    return resolved if seen == identifiers else None


def categories_from_manhattan(values: Any, snapshot: dict | None) -> list[dict]:
    """把适用类目 ID 还原成 Manhattan 品类名称。

    前端提交的通常是类目 ID；如果只看 ID，无法判断是否属于上门 12 品类。
    """
    raw_values = values if isinstance(values, (list, tuple)) else [values]
    ids = {
        str(value).strip()
        for value in raw_values
        if isinstance(value, (str, int)) and str(value).strip()
    }
    if not ids or not isinstance(snapshot, dict):
        return []
    groups: list[dict] = []
    grouped = snapshot.get("options_by_business_type")
    if isinstance(grouped, dict):
        groups.extend(group for group in grouped.values() if isinstance(group, dict))
    groups.append(snapshot)

    categories: list[dict] = []
    for group in groups:
        for category in group.get("applicable_categories") or []:
            if not isinstance(category, dict):
                continue
            identifier = next(
                (
                    str(category[key]).strip()
                    for key in ("categoryId", "category_id", "id", "code", "value")
                    if category.get(key) not in (None, "")
                ),
                "",
            )
            if identifier not in ids:
                continue
            categories.append(category)
    return categories


def candidate_business_type(
    knowledge: dict,
    payload: dict,
    *,
    resolved_category_names: list[dict] | None = None,
) -> str:
    replay = (payload.get("selection") or {}).get("answer_hub_replay_input") or {}
    if not isinstance(replay, dict):
        replay = {}
    row = replay.get("row") or replay.get("topic_row") or replay
    if not isinstance(row, dict):
        row = {}
    derived = business_type_for_products(
        knowledge.get("applicable_categories") or [],
        resolved_category_names,
    )
    # 人工复核明确改过业务类型时优先，但仍只做“自营 + 非 12 类 → 聚合”。
    if knowledge.get("business_type_source") == "human":
        return (
            reconcile_business_type(knowledge.get("business_type"), derived)
            or knowledge.get("business_type")
            or "self_operated"
        )
    explicit_source = row.get("回收业务层级") or row.get("business_line")
    if explicit_source:
        return reconcile_business_type(explicit_source, derived) or "self_operated"
    # 聚合回收的品类库包含上门品类，源端已判定聚合时不能被品类改回自营。
    if knowledge.get("business_type") == "aggregated":
        return "aggregated"
    if derived:
        return derived
    source_product = next((
        row.get(key) for key in ("类目", "产品类型", "product_type", "product_category")
        if scope_keys(_product_names(row.get(key)), "category") - _UNKNOWN_KEYS
    ), None)
    source_derived = business_type_for_products(
        [source_product] if source_product else []
    )
    if source_derived:
        return (
            reconcile_business_type(knowledge.get("business_type"), source_derived)
            or source_derived
        )
    # 没有明确产品证据时沿用历史默认值，避免旧候选被静默改判为聚合回收。
    return knowledge.get("business_type") or "self_operated"
