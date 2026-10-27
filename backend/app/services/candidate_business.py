"""候选回收类型的确定性规则；具体品类优先于旧的自营默认值。"""

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


def candidate_business_type(knowledge: dict, payload: dict) -> str:
    replay = (payload.get("selection") or {}).get("answer_hub_replay_input") or {}
    if not isinstance(replay, dict):
        replay = {}
    row = replay.get("row") or replay.get("topic_row") or replay
    if not isinstance(row, dict):
        row = {}
    source_product = next((
        row.get(key) for key in ("类目", "产品类型", "product_type", "product_category")
        if scope_keys(_product_names(row.get(key)), "category") - _UNKNOWN_KEYS
    ), None)
    values = [source_product] if source_product else knowledge.get("applicable_categories") or []
    concrete = scope_keys(_product_names(values), "category") - _UNKNOWN_KEYS
    if concrete - _DOORSTEP_KEYS:
        return "aggregated"
    return knowledge.get("business_type") or ("self_operated" if concrete else "aggregated")
