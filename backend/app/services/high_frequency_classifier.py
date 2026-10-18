from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.high_frequency_taxonomy import normalize_product_category

try:
    from app.core.config import settings
except ModuleNotFoundError:  # Allows pure classifier tests without backend extras.
    class _Settings:
        HIGH_FREQUENCY_CLASSIFICATION_ENABLED = True
        HIGH_FREQUENCY_CLASSIFICATION_BATCH_SIZE = 5
        HIGH_FREQUENCY_CLASSIFICATION_TIMEOUT_SECONDS = 120.0
        HIGH_FREQUENCY_CLASSIFICATION_MAX_COMPLETION_TOKENS = 2048
        HIGH_FREQUENCY_CLASSIFICATION_CACHE_PATH = "high_frequency_classifications.json"
        DEEPSEEK_BASE_URL = ""
        DEEPSEEK_API_KEY = ""
        GROUP_LLM_BASE_URL = ""
        GROUP_LLM_API_KEY = ""
        HIGH_FREQUENCY_CLASSIFICATION_MODEL = ""

    settings = _Settings()

try:
    from app.services.model_annotation import ModelAnnotationError
except ModuleNotFoundError:
    class ModelAnnotationError(RuntimeError):
        def __init__(self, message: str, error_code: str = "MODEL_ERROR"):
            super().__init__(message)
            self.message = message


_CACHE_LOCK = Lock()
_CLASSIFIER_CACHE_VERSION = "standard-question-v10-semantic-rules"
_APPROVED_QUESTION_ANCHORS = {
    "笔记本": [
        "硬盘是否品牌、原装或第三方",
        "内存是否品牌、原装或第三方",
        "准确型号或小型号怎么核实",
        "是否配备指纹，录入异常怎么判定",
        "转轴异响或机械异常怎么判定",
    ],
    "手机": [
        "关于本机显示电池正品，但验机工具报异常怎么判定",
        "防水标签变红是否按浸液判定",
        "屏幕偏光检测异常怎么判定",
        "iPhone 型号代码对应哪个销售版本",
    ],
    "耳机": [
        "定位或查找功能怎么检测、解绑",
        "购买渠道无法确认时怎么核验",
    ],
    "平板电脑": ["外观刻字怎么判定"],
}
_CATEGORY_ANCHORS = {
    "笔记本": {"笔记本", "笔记本电脑", "电脑"},
    "手机": {"手机"},
    "耳机": {"耳机", "耳机/耳麦"},
    "平板电脑": {"平板电脑", "平板"},
}


def _reference_catalog(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    categories = {str(item.get("category", "")).strip() for item in items}
    return [
        {"category": category, "approved_standard_questions": titles}
        for canonical, titles in _APPROVED_QUESTION_ANCHORS.items()
        for category in sorted(categories & _CATEGORY_ANCHORS[canonical])
    ]


def _reference_section(items: list[dict[str, str]]) -> str:
    catalog = _reference_catalog(items)
    if not catalog:
        return ""
    return (
        "以下是用户在测试样本中确认准确的标准问题参考。只有当对象、事实状态和处理诉求都一致时才复用标题；"
        "这是可扩展参考，不是封闭分类，不得把新问题强行套入。相似主题但对象或诉求不同必须分开。"
        "已确认的关键区分：硬盘品牌问题与内存品牌问题不同；准确机型核实与 iPhone 销售版本核实不同；"
        "耳机定位/解绑与购买渠道核验不同；电池工具检测异常与浸液判定不同。\n"
        + json.dumps(catalog, ensure_ascii=False)
        + "\n"
    )


_CONSOLIDATION_NGRAM_IGNORES = {
    "相机",
    "如何",
    "怎么",
    "处理",
    "判定",
    "是否",
    "问题",
    "异常",
    "确认",
    "需要",
    "影响",
    "出现",
    "显示",
    "无法",
    "找到",
    "原装",
    "机身",
    "情况",
    "如",
    "何",
}


def _model_connection_config() -> dict[str, Any]:
    try:
        from app.services.model_annotation import resolve_call_config

        return resolve_call_config()
    except (ImportError, AttributeError):
        return {}


def _classifier_model_id() -> str:
    override = str(
        getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_MODEL", "")
    ).strip()
    resolved = _model_connection_config()
    return str(
        override
        or resolved.get("model")
        or getattr(settings, "DEEPSEEK_MODEL", "")
        or "deepseek-flash"
    ).strip()


def _split_cache_path() -> Path:
    path = _cache_path()
    return path.with_name(f"{path.stem}.splits{path.suffix}")


def _split_cache_key(item: dict[str, str]) -> str:
    payload = "\x1f".join(
        (
            "atomic-question-split-v2",
            _classifier_model_id(),
            str(item.get("category", "")).strip(),
            str(item.get("level1", "")).strip(),
            str(item.get("level2", "")).strip(),
            str(item.get("core_problem", "")).strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_split_cache() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_split_cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        questions = value.get("questions")
        if not isinstance(questions, list) or any(not str(q).strip() for q in questions):
            continue
        result[str(key)] = {
            "questions": [str(q).strip() for q in questions],
            "needs_review": bool(value.get("needs_review", False)),
        }
    return result


def _write_split_cache(cache: dict[str, dict[str, Any]]) -> None:
    path = _split_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError:
        return


def get_cached_atomic_splits(
    items: list[dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], int]:
    with _CACHE_LOCK:
        cache = _read_split_cache()
    output: dict[str, dict[str, Any]] = {}
    uncached = 0
    for item in items:
        cached = cache.get(_split_cache_key(item))
        if cached is None:
            uncached += 1
        else:
            output[str(item.get("id", ""))] = cached
    return output, uncached


def _split_prompt(items: list[dict[str, str]]) -> str:
    return (
        "你是答疑问题拆分器。仅根据每条 AI 核心问题原文，识别可以分别回答、分别判定的独立问题单元。"
        "一个对象的背景、证据、追问和同一判定诉求必须留在同一问题里；不能把同一问题拆成多个碎片。"
        "若原文明确包含多个部件、功能或互不相同的判定诉求，才拆为多个问题；不得补造事实、结论或输入中没有的内容。"
        "如果确认原文其实只有一个问题，返回一个问题并设置 needs_review=false；"
        "不确定问题边界或无法确认是否有多个独立问题时必须设置 needs_review=true；不要为了拆分而拆分。"
        "问题尽量保留原文中的对象、事实状态和咨询诉求；不要把判定结论、聊天内容或参考话术添加进问题。"
        "只返回 JSON 对象，格式为 "
        + '{"items":[{"id":"输入id","questions":["问题单元"],"needs_review":false}]}。'
        + "每个输入 id 必须且只能返回一次。\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


def split_ambiguous_questions(
    items: list[dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Use DeepSeek only for ambiguous multi-intent cores; never send chat or judgments."""
    if not items:
        return {}, None
    if not getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_ENABLED", True):
        return {}, "DeepSeek 高频问题归类已禁用，含多意图的来源暂不计入高频统计。"
    try:
        from app.services.model_annotation import _call_deepseek_flash
    except ModuleNotFoundError as exc:
        return {}, f"DeepSeek 问题拆分依赖未安装：{exc}"
    model_config = _model_connection_config()
    if not (
        str(model_config.get("base_url") or "").strip()
        and str(model_config.get("api_key") or "").strip()
    ):
        return {}, "DeepSeek 问题拆分接口未配置，含多意图的来源暂不计入高频统计。"

    with _CACHE_LOCK:
        cache = _read_split_cache()
    output: dict[str, dict[str, Any]] = {}
    pending_by_category: dict[str, list[dict[str, str]]] = {}
    items_by_cache_key: dict[str, list[dict[str, str]]] = {}
    for item in items:
        cache_key = _split_cache_key(item)
        if cache_key in cache:
            output[str(item.get("id", ""))] = cache[cache_key]
        else:
            matching_items = items_by_cache_key.setdefault(cache_key, [])
            matching_items.append(item)
            if len(matching_items) == 1:
                pending_by_category.setdefault(
                    str(item.get("category", "")), []
                ).append({**item, "cache_key": cache_key})

    batch_size = max(1, min(int(getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_BATCH_SIZE", 8)), 20))
    timeout_seconds = max(1.0, float(getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_TIMEOUT_SECONDS", 120.0)))
    max_completion_tokens = max(512, int(getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_MAX_COMPLETION_TOKENS", 4096)))
    model_override = _classifier_model_id()
    try:
        for category_items in pending_by_category.values():
            for offset in range(0, len(category_items), batch_size):
                batch = category_items[offset : offset + batch_size]
                model_batch = [
                    {
                        "id": item["cache_key"],
                        "category": item.get("category", ""),
                        "core_problem": item.get("core_problem", ""),
                    }
                    for item in batch
                ]
                result = None
                for attempt in range(2):
                    try:
                        result = _call_deepseek_flash(
                            _split_prompt(model_batch),
                            purpose="高频问题多意图拆分",
                            model_override=model_override,
                            timeout_override=timeout_seconds,
                            max_completion_tokens_override=max_completion_tokens,
                        )
                        break
                    except ModelAnnotationError as exc:
                        if attempt == 0 and getattr(exc, "retryable", False):
                            time.sleep(1.0)
                            continue
                        raise
                rows = result.get("items") if isinstance(result, dict) else None
                expected = {item["cache_key"] for item in batch}
                response_ids = [str(row.get("id", "")) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                if (
                    len(response_ids) != len(expected)
                    or len(response_ids) != len(set(response_ids))
                    or set(response_ids) != expected
                ):
                    raise ModelAnnotationError(
                        f"DeepSeek 问题拆分返回不完整，预期 {len(expected)} 条。",
                        "INCOMPLETE_SPLIT",
                    )
                by_id = {str(row["id"]): row for row in rows if isinstance(row, dict)}
                for item in batch:
                    row = by_id[item["cache_key"]]
                    raw_questions = row.get("questions")
                    needs_review = row.get("needs_review") is True
                    questions = [
                        re.sub(r"\s+", " ", str(value or "")).strip()
                        for value in raw_questions
                        if isinstance(value, str)
                    ] if isinstance(raw_questions, list) else []
                    questions = list(dict.fromkeys(q for q in questions if q))
                    if not questions or len(questions) > 6:
                        needs_review = True
                        questions = []
                    cache[item["cache_key"]] = {
                        "questions": questions,
                        "needs_review": needs_review,
                    }
                    for source_item in items_by_cache_key[item["cache_key"]]:
                        output[str(source_item.get("id", ""))] = cache[
                            item["cache_key"]
                        ]
                with _CACHE_LOCK:
                    _write_split_cache(cache)
        return output, None
    except ModelAnnotationError as exc:
        return output, str(exc)


def _cache_key(category: str, issue: str, level1: str = "", level2: str = "") -> str:
    model = _classifier_model_id()
    payload = "\x1f".join(
        (
            _CLASSIFIER_CACHE_VERSION,
            model,
            normalize_product_category(category),
            level1.strip(),
            level2.strip(),
            issue.strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deterministic_classification(
    item: dict[str, str],
) -> dict[str, str] | None:
    """Canonicalize confirmed component and appearance verification vocabulary."""

    category = normalize_product_category(item.get("category", ""))
    issue = str(item.get("issue", "")).strip()
    if not issue:
        return None
    compact_issue = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", issue)
    if category in _CATEGORY_ANCHORS["平板电脑"] and any(
        marker in issue for marker in ("磕碰", "掉漆", "破损")
    ):
        return {
            "business_theme": "平板电脑外观质检",
            "standard_question": "外壳磕碰/掉漆如何判定",
        }
    if (
        category in _CATEGORY_ANCHORS["手机"]
        and "电池" in issue
        and any(marker in issue for marker in ("正品", "官方配件"))
        and any(marker in issue for marker in ("一根线", "工具"))
        and "异常" in issue
    ):
        return {
            "business_theme": "电池检测",
            "standard_question": "本机显示正品部件但工具读出电池异常如何判定",
        }
    if (
        category == "手机"
        and "电池" in issue
        and any(marker in issue for marker in ("健康值", "健康度"))
        and "评估中" in issue
    ):
        return {
            "business_theme": "电池检测",
            "standard_question": "电池健康度显示评估中如何判定",
        }
    if (
        category == "手机"
        and "防水标签" in issue
        and "变红" in issue
    ):
        return {
            "business_theme": "浸液检测",
            "standard_question": "防水标签变红是否判定浸液",
        }
    if (
        category == "手机"
        and "偏光" in issue
        and any(marker in issue for marker in ("异常", "判定", "拆修"))
    ):
        return {
            "business_theme": "屏幕检测",
            "standard_question": "偏光检测异常如何判定",
        }
    if category == "平板电脑" and "刻字" in issue:
        return {
            "business_theme": "平板电脑外观质检",
            "standard_question": "平板机身外观有刻字如何判定",
        }
    earphone_location_subject = any(
        marker in compact_issue
        for marker in ("定位功能", "查找功能", "定位查找", "查找定位")
    )
    earphone_location_state = any(
        marker in issue
        for marker in (
            "正常",
            "异常",
            "状态",
            "判定",
            "检测",
            "查看",
            "显示",
            "离线",
            "精确查找",
            "必须使用",
        )
    )
    earphone_explicit_state = any(
        marker in issue
        for marker in (
            "定位正常",
            "查找正常",
            "定位异常",
            "查找异常",
            "是否正常",
            "显示状态",
            "显示离线",
            "功能状态",
        )
    )
    earphone_other_intent = any(
        marker in issue
        for marker in ("解绑", "小型号", "真伪", "正品", "购买渠道")
    )
    if (
        category == "耳机"
        and earphone_location_subject
        and earphone_location_state
        and not (earphone_other_intent and not earphone_explicit_state)
    ):
        return {
            "business_theme": "定位检测",
            "standard_question": "耳机定位查找功能正常如何判定",
        }
    if category not in _CATEGORY_ANCHORS["笔记本"]:
        return None
    component_model = bool(
        re.search(
            r"(?:显卡|硬盘|内存|电池|充电器|处理器|CPU).{0,8}(?:型号|机型)",
            issue,
            re.IGNORECASE,
        )
    )
    serial_number_disposition = (
        any(marker in issue.upper() for marker in ("序列号", "SN", "INVALID"))
        and any(marker in issue for marker in ("无效", "异常", "能收", "回收"))
    )
    non_model_disposition = any(
        marker in issue
        for marker in (
            "未就型号",
            "判换壳",
            "更换过外壳",
            "后盖材质",
        )
    )
    whole_device_model = any(
        marker in issue
        for marker in (
            "请求确认型号",
            "请求确认机型",
            "确认设备型号",
            "确认机器型号",
            "确认该机机型",
            "确认回收机型",
            "确认现场设备型号",
            "核实设备型号",
            "核实笔记本型号",
            "核实笔记本具体型号",
            "核对设备型号",
            "型号确认",
            "机型确认",
            "型号是否正确",
            "机型是否正确",
            "型号是否为",
            "机型是否为",
            "型号是否选错",
            "无法确认型号",
            "无法确认机型",
            "无法确定型号",
            "无法确定机型",
            "无法查询型号",
            "无法查询机型",
            "具体型号",
            "具体机型",
            "准确型号",
            "正确型号",
            "所选型号",
            "录入型号",
            "小型号",
            "实际机型与工单",
        )
    )
    if (
        whole_device_model
        and not component_model
        and not serial_number_disposition
        and not non_model_disposition
    ):
        return {
            "business_theme": "机型核验",
            "standard_question": "笔记本具体型号如何核实",
        }
    if "指纹" in issue and any(
        marker in issue for marker in ("支持", "查看", "确认", "识别")
    ):
        return {
            "business_theme": "指纹检测",
            "standard_question": "笔记本是否支持指纹识别如何确认",
        }
    upper_issue = re.sub(r"\s+", "", issue.upper())
    bios_entry_operation = (
        bool(re.search(r"进入.{0,12}BIOS", upper_issue))
        and any(
            marker in issue
            for marker in ("按哪个", "按下哪个", "F键", "操作指导", "如何进入")
        )
    )
    if bios_entry_operation:
        return {
            "business_theme": "BIOS操作",
            "standard_question": "笔记本进入BIOS需按哪个按键",
        }
    if (
        (
            "BIOS锁" in upper_issue
            or (
                "BIOS" in upper_issue
                and any(
                    marker in issue
                    for marker in ("上锁", "无锁", "有锁", "锁状态", "密码")
                )
            )
        )
    ):
        return {
            "business_theme": "BIOS检测",
            "standard_question": "笔记本BIOS锁状态如何确认",
        }
    if (
        "转轴" in issue
        and "缝隙" in issue
        and any(
            marker in issue
            for marker in ("没有异响", "无异响", "无异常异响")
        )
    ):
        return {
            "business_theme": "转轴检测",
            "standard_question": "笔记本转轴缝隙如何判定",
        }
    if (
        "转轴" in issue
        and not any(
            marker in issue
            for marker in ("没有异响", "无异响", "无异常异响")
        )
        and any(marker in issue for marker in ("异响", "吱吱声", "声音", "声响"))
    ):
        return {
            "business_theme": "转轴检测",
            "standard_question": "笔记本转轴异响如何判定",
        }
    if (
        "转轴异常" in issue
        and not any(
            marker in issue
            for marker in ("缝隙", "松动", "开裂", "无法开合", "不能固定")
        )
    ):
        return {
            "business_theme": "转轴检测",
            "standard_question": "笔记本转轴异响如何判定",
        }
    if not any(marker in issue for marker in ("品牌", "原装", "第三方", "非原装")):
        return None
    if "硬盘" in issue and "内存" not in issue:
        return {
            "business_theme": "笔记本配件品牌核验",
            "standard_question": "硬盘是否品牌、原装或第三方",
        }
    if "内存" in issue and "硬盘" not in issue:
        return {
            "business_theme": "笔记本配件品牌核验",
            "standard_question": "内存是否品牌、原装或第三方",
        }
    return None


def _cache_path() -> Path:
    return Path(settings.HIGH_FREQUENCY_CLASSIFICATION_CACHE_PATH)


def _read_cache() -> dict[str, dict[str, str]]:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return {
        str(key): {
            "business_theme": str(value.get("business_theme", "")).strip(),
            "standard_question": str(value.get("standard_question", "")).strip(),
        }
        for key, value in payload.items()
        if (
            str(key).strip()
            and isinstance(value, dict)
            and str(value.get("business_theme", "")).strip()
            and str(value.get("standard_question", "")).strip()
        )
    } if isinstance(payload, dict) else {}


def _write_cache(cache: dict[str, dict[str, str]]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError:
        # Classification remains usable without persistence; the next request
        # will simply call the model again.
        return


def get_cached_classifications(
    items: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], int]:
    """Return cached model classifications without making a network request."""

    with _CACHE_LOCK:
        cache = _read_cache()
    output: dict[str, dict[str, str]] = {}
    uncached_count = 0
    for item in items:
        key = _cache_key(
            item.get("category", ""),
            item.get("issue", ""),
            item.get("level1", ""),
            item.get("level2", ""),
        )
        cached = _deterministic_classification(item) or cache.get(key)
        if cached:
            output[key] = cached
        else:
            uncached_count += 1
    return output, uncached_count


def _prompt(items: list[dict[str, str]]) -> str:
    return (
        "你是答疑讲师问题标准化器。为每条原子问题生成业务主题和可统计的标准问题。"
        "标准问题只表达产品部件/对象 + 核心咨询或判定诉求，删除偶然机型、品牌、聊天过程和工单动作；"
        "不得删除区分问题所需的部件、事实状态或处理动作，不得将多个独立问题拼成一个标题。"
        "只能在同一产品品类内比较；不同部件、故障、事实状态或判定诉求必须分开。"
        "业务主题只是上层标签，相同主题不意味着标准问题可以合并。不要将分类、判定结论、依据或质检路径当成问题主体。"
        "不要补造输入没有提供的事实。\n"
        + _reference_section(items)
        + "只返回 JSON 对象，格式为 "
        + '{"items":[{"id":"输入id","business_theme":"业务主题","standard_question":"标准问题"}]}'
        + "。每个输入 id 必须只出现一次。\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


def _consolidation_prompt(items: list[dict[str, str]]) -> str:
    return (
        "你是答疑讲师标准问题归并器。只在同品类中，把对象、事实状态和处理口径完全相同、仅措辞不同的问题统一为同一短标题。"
        "业务主题相同不能作为合并依据；不同部件、不同异常现象、不同核验对象或不同处理动作必须保持分开。"
        "不确定是否相同就不要合并；不得补造输入没有的事实。\n"
        + _reference_section(items)
        + "只返回 JSON 对象，格式为 "
        + '{"items":[{"id":"输入id","business_theme":"业务主题","standard_question":"统一后的标准问题"}]}'
        + "。每个输入 id 必须只出现一次。\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


def _subject_ngrams(title: str) -> set[str]:
    """Return non-generic character ngrams used only to batch model context."""

    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(title or ""))
    result: set[str] = set()
    for width in range(2, min(5, len(text)) + 1):
        for offset in range(0, len(text) - width + 1):
            token = text[offset : offset + width]
            if not any(
                ignored in token
                for ignored in _CONSOLIDATION_NGRAM_IGNORES
            ):
                result.add(token)
    return result


def _consolidation_batches(
    items: list[dict[str, str]],
    batch_size: int,
) -> list[list[dict[str, str]]]:
    """Keep related short titles in one model comparison batch.

    This function never merges questions. It only ensures that titles sharing a
    non-generic subject phrase (for example, ``序列号``) are compared by the
    model in the same request even when their source records are far apart.
    """

    if not items:
        return []

    short_batch_size = max(1, batch_size)
    related_batch_size = min(20, max(short_batch_size, short_batch_size * 4))
    by_category: dict[str, list[dict[str, str]]] = {}
    for item in items:
        by_category.setdefault(str(item.get("category", "")), []).append(item)

    batches: list[list[dict[str, str]]] = []
    for category_items in by_category.values():
        ordered_items = sorted(
            category_items,
            key=lambda item: (
                str(item.get("standard_question", "")),
                str(item.get("id", "")),
            ),
        )
        ngram_counts: dict[str, int] = {}
        item_ngrams: list[set[str]] = []
        for item in ordered_items:
            ngrams = _subject_ngrams(item.get("standard_question", ""))
            item_ngrams.append(ngrams)
            for ngram in ngrams:
                ngram_counts[ngram] = ngram_counts.get(ngram, 0) + 1

        parent = list(range(len(ordered_items)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        first_by_ngram: dict[str, int] = {}
        for index, ngrams in enumerate(item_ngrams):
            for ngram in ngrams:
                if ngram_counts[ngram] < 2:
                    continue
                previous = first_by_ngram.get(ngram)
                if previous is None:
                    first_by_ngram[ngram] = index
                else:
                    union(previous, index)

        components: dict[int, list[dict[str, str]]] = {}
        for index, item in enumerate(ordered_items):
            components.setdefault(find(index), []).append(item)

        for component in components.values():
            size = (
                related_batch_size if len(component) > 1 else short_batch_size
            )
            for offset in range(0, len(component), size):
                batches.append(component[offset : offset + size])
    return batches


def classify_questions(
    items: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], str | None]:
    """Classify issue representatives and return key -> theme/title."""

    if not items:
        return {}, None
    if not getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_ENABLED", True):
        return {}, "DeepSeek 高频问题归类已禁用，当前只保留原子问题统计。"
    try:
        from app.services.model_annotation import _call_deepseek_flash
    except ModuleNotFoundError as exc:
        return {}, f"DeepSeek 高频问题归类依赖未安装：{exc}"
    model_config = _model_connection_config()
    if not (
        str(model_config.get("base_url") or "").strip()
        and str(model_config.get("api_key") or "").strip()
    ):
        return {}, "DeepSeek 高频问题归类接口未配置，已保留原子问题统计。"

    with _CACHE_LOCK:
        cache = _read_cache()
    output: dict[str, dict[str, str]] = {}
    items_by_key: dict[str, dict[str, str]] = {}
    pending: list[dict[str, str]] = []
    for item in items:
        key = _cache_key(
            item.get("category", ""),
            item.get("issue", ""),
            item.get("level1", ""),
            item.get("level2", ""),
        )
        items_by_key[key] = item
        deterministic = _deterministic_classification(item)
        cached = deterministic or cache.get(key)
        if cached:
            output[key] = cached
            if deterministic:
                cache[key] = deterministic
        else:
            pending.append({**item, "id": key})

    batch_size = max(
        1,
        min(
            int(getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_BATCH_SIZE", 20)),
            50,
        ),
    )
    timeout_seconds = max(
        1.0,
        float(
            getattr(
                settings,
                "HIGH_FREQUENCY_CLASSIFICATION_TIMEOUT_SECONDS",
                120.0,
            )
        ),
    )
    max_completion_tokens = max(
        256,
        int(
            getattr(
                settings,
                "HIGH_FREQUENCY_CLASSIFICATION_MAX_COMPLETION_TOKENS",
                2048,
            )
        ),
    )
    model_override = _classifier_model_id()

    def complete_batch(
        batch: list[dict[str, str]],
        prompt_builder,
        purpose: str,
        error_code: str,
    ) -> dict[str, dict[str, str]]:
        """Return a complete id-to-classification mapping, retrying omissions."""

        for attempt in range(2):
            # Cache keys are SHA-256 values. Keep them internal: models are much
            # more reliable at copying a short, sequential response identifier.
            aliases = {
                f"q-{index}": item["id"]
                for index, item in enumerate(batch, 1)
            }
            aliased_batch = [
                {**item, "id": alias}
                for alias, item_id in aliases.items()
                for item in batch
                if item["id"] == item_id
            ]
            try:
                result = _call_deepseek_flash(
                    prompt_builder(aliased_batch),
                    purpose=purpose,
                    model_override=model_override,
                    timeout_override=timeout_seconds,
                    max_completion_tokens_override=max_completion_tokens,
                )
            except ModelAnnotationError as exc:
                if attempt == 0 and getattr(exc, "retryable", False):
                    time.sleep(1.0)
                    continue
                raise
            rows = result.get("items")
            expected_ids = set(aliases)
            response_ids = [
                str(row.get("id", ""))
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ]
            if (
                any(response_id not in expected_ids for response_id in response_ids)
                or len(response_ids) != len(set(response_ids))
            ):
                raise ModelAnnotationError(
                    f"{purpose}返回了重复或未知的输入 id。",
                    error_code,
                )
            by_id = {
                aliases[str(row.get("id"))]: {
                    "business_theme": str(
                        row.get("business_theme", "")
                    ).strip(),
                    "standard_question": str(
                        row.get("standard_question", "")
                    ).strip(),
                }
                for row in (rows if isinstance(rows, list) else [])
                if (
                    isinstance(row, dict)
                    and str(row.get("id", "")) in aliases
                    and str(row.get("business_theme", "")).strip()
                    and str(row.get("standard_question", "")).strip()
                )
            }
            complete = {
                item["id"]: by_id[item["id"]]
                for item in batch
                if item["id"] in by_id
            }
            missing_items = [
                item for item in batch if item["id"] not in complete
            ]
            if not missing_items:
                return complete
            # A partial JSON list is a model formatting failure, not a data
            # failure. Re-ask only omitted rows so valid rows retain their
            # first response and the model receives a smaller prompt.
            if len(missing_items) < len(batch):
                complete.update(
                    complete_batch(
                        missing_items,
                        prompt_builder,
                        purpose,
                        error_code,
                    )
                )
                return complete
            if attempt == 1:
                raise ModelAnnotationError(
                    f"{purpose}返回不完整，缺少 {len(missing_items)} 条标题。",
                    error_code,
                )
        raise ModelAnnotationError(f"{purpose}未返回可用结果。", error_code)

    try:
        pending_by_category: dict[str, list[dict[str, str]]] = {}
        for item in pending:
            pending_by_category.setdefault(item.get("category", ""), []).append(item)
        for category_items in pending_by_category.values():
            for offset in range(0, len(category_items), batch_size):
                batch = category_items[offset : offset + batch_size]
                by_id = complete_batch(
                    batch,
                    _prompt,
                    "高频问题标准化归类",
                    "INCOMPLETE_CLASSIFICATION",
                )
                for item in batch:
                    key = item["id"]
                    output[key] = by_id[key]
                    cache[key] = by_id[key]

        # The gateway is reliable for short JSON batches. Related titles are
        # compared together, while unrelated titles retain the smaller batch
        # size so a whole category cannot produce an oversized JSON response.
        consolidation_items = [
            {
                "id": key,
                "category": items_by_key[key].get("category", ""),
                "business_theme": classification["business_theme"],
                "standard_question": classification["standard_question"],
            }
            for key, classification in output.items()
            if key in items_by_key
        ]
        if pending and len(consolidation_items) > 1:
            for batch in _consolidation_batches(
                consolidation_items,
                batch_size,
            ):
                by_id = complete_batch(
                    batch,
                    _consolidation_prompt,
                    "高频问题标准标题归并",
                    "INCOMPLETE_CONSOLIDATION",
                )
                for item in batch:
                    output[item["id"]] = by_id[item["id"]]
                    cache[item["id"]] = by_id[item["id"]]
        with _CACHE_LOCK:
            _write_cache(cache)
        return output, None
    except ModelAnnotationError as exc:
        # Never mix partially clustered rows with raw rows: the displayed
        # frequency must always reflect one consistent grouping rule.
        return {}, str(exc)


def classify_incremental_question(
    item: dict[str, str],
    existing_standard_questions: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str | None]:
    """Classify one new atomic question without re-sending historic rows.

    The database is the durable source of historic standard questions.  The
    bounded candidate list lets the model reuse a genuinely equivalent title
    while ensuring a new record never reopens an already completed batch.
    """

    deterministic = _deterministic_classification(item)
    if deterministic:
        return {**deterministic, "_mapping_source": "deterministic"}, None
    if not getattr(settings, "HIGH_FREQUENCY_CLASSIFICATION_ENABLED", True):
        return None, "DeepSeek 高频问题归类已禁用。"
    try:
        from app.services.model_annotation import _call_deepseek_flash
    except ModuleNotFoundError as exc:
        return None, f"DeepSeek 高频问题归类依赖未安装：{exc}"
    model_config = _model_connection_config()
    if not (
        str(model_config.get("base_url") or "").strip()
        and str(model_config.get("api_key") or "").strip()
    ):
        return None, "DeepSeek 高频问题归类接口未配置。"

    candidates = [
        {
            "business_theme": str(candidate.get("business_theme", "")).strip(),
            "standard_question": str(candidate.get("standard_question", "")).strip(),
        }
        for candidate in existing_standard_questions[:50]
        if str(candidate.get("standard_question", "")).strip()
    ]
    payload = {
        "id": "q-1",
        "category": normalize_product_category(item.get("category", "")),
        "issue": str(item.get("issue", "")).strip(),
        "level1": str(item.get("level1", "")).strip(),
        "level2": str(item.get("level2", "")).strip(),
        "existing_standard_questions": candidates,
    }
    prompt = (
        "你是答疑讲师问题标准化器。为一个新的原子问题给出业务主题和标准问题。"
        "只能在相同产品品类内比较；如果现有标准问题中有对象、事实状态和处理诉求完全相同、仅措辞不同的一项，"
        "必须逐字复用它的 standard_question。否则创建一个新的短标题。"
        "相同业务主题不等于同一标准问题；不同部件、异常现象、核验对象或处理动作不得合并。"
        "标准问题用于跨工单统计，优先控制在 8 到 28 个汉字左右，只保留“对象或部件 + 事实状态 + 核验/判定诉求”。"
        "不得包含具体品牌、机型、年份、容量、序列号、工具名称、回收师、现场、图片、工单过程、答疑过程或最终判定结论，"
        "除非该信息本身就是区分不同问题所必需的事实。不得原样复述长问题或写成完整会话句。"
        "例如：电池正品提示与工具异常如何判定、蜂窝网络可联网但不能通话如何判定、镜头适配卡口如何确认。"
        "business_theme 必须是 2 到 10 个字的上层业务主题，且必须反映当前原子问题的对象；"
        "网络通信、屏幕显示、镜头参数、外观问题等不同对象不得互相错标。"
        "不得更改、重命名或重新归并任何现有问题，也不得补造输入中没有的事实。"
        "只返回 JSON 对象，格式为 "
        '{"items":[{"id":"q-1","business_theme":"业务主题","standard_question":"标准问题"}]}。\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    for attempt in range(2):
        try:
            result = _call_deepseek_flash(
                prompt,
                purpose="高频问题增量标准化归类",
                model_override=_classifier_model_id(),
                timeout_override=max(
                    1.0,
                    float(
                        getattr(
                            settings,
                            "HIGH_FREQUENCY_CLASSIFICATION_TIMEOUT_SECONDS",
                            120.0,
                        )
                    ),
                ),
                max_completion_tokens_override=max(
                    256,
                    int(
                        getattr(
                            settings,
                            "HIGH_FREQUENCY_CLASSIFICATION_MAX_COMPLETION_TOKENS",
                            2048,
                        )
                    ),
                ),
            )
        except ModelAnnotationError as exc:
            if attempt == 0 and getattr(exc, "retryable", False):
                time.sleep(1.0)
                continue
            return None, str(exc)
        rows = result.get("items") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            if attempt == 0:
                continue
            return None, "DeepSeek 高频问题增量归类返回不完整。"
        row = rows[0]
        if str(row.get("id", "")).strip() != "q-1":
            if attempt == 0:
                continue
            return None, "DeepSeek 高频问题增量归类返回了未知输入 id。"
        business_theme = str(row.get("business_theme", "")).strip()
        standard_question = str(row.get("standard_question", "")).strip()
        if not business_theme or not standard_question:
            if attempt == 0:
                continue
            return None, "DeepSeek 高频问题增量归类缺少标准问题或业务主题。"
        return {
            "business_theme": business_theme,
            "standard_question": standard_question,
            "_mapping_source": "model",
        }, None
    return None, "DeepSeek 高频问题增量归类返回不完整。"
