from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import hashlib
import inspect
import json
import re
import uuid

from .audit import AuditStore
from .catalog import StandardCatalogItem, is_active_standard, load_standard_catalog
from .embedding import EmbeddingClient, EmbeddingError
from .excel_io import read_workbook_rows, write_rows_to_workbook
from .images import ImageDownloader, ImageEvidence, split_image_urls
from .mimo import MimoClient, MimoError, PROMPT_VERSION


FLOW_STATUSES = [
    "raw",
    "preprocessed",
    "model_labeled",
    "review_pending",
    "review_approved",
    "review_rejected",
    "published",
    "deprecated",
]

# A single broad word such as “屏幕” must not make an unrelated standard look authoritative.
MIN_STANDARD_RELEVANCE_SCORE = 3.0
BROAD_RETRIEVAL_TERMS = {
    "手机",
    "设备",
    "屏幕",
    "问题",
    "检测",
    "情况",
    "异常",
    "功能",
}
UNCERTAINTY_MARKERS = [
    "疑似",
    "不确定",
    "证据不足",
    "无法判断",
    "未识别",
    "未发现",
    "没有看出",
    "怎么查",
    "如何查看",
]
EXPLICIT_BOUNDARY_CASES = (
    ("坏点", "漏液"),
    ("磕点", "划痕"),
)

REVIEW_DECISIONS = [
    "通过",
    "修改后通过",
    "驳回",
    "标记Bad Case",
]

MODEL_INITIAL_REVIEW_DECISIONS = [
    "通过",
    "需修改",
    "驳回",
    "证据不足待补充",
]

ERROR_TYPES = [
    "分类错",
    "标题不准",
    "标准项映射错",
    "场景理解错",
    "话术不合适",
    "证据不足",
    "图片判断失误",
    "标准未覆盖/标准召回不足",
    "标准过期或冲突",
    "需要拆分/合并知识",
]

SOURCE_COLUMNS = [
    "序号",
    "上传者",
    "分析时间",
    "工单ID",
    "回收单号",
    "聊天内容",
    "图片链接",
    "核心问题",
    "判定结论",
    "判定依据",
    "产品类型",
    "一级分类",
    "二级分类",
    "参考话术",
]

MODEL_COLUMNS = [
    "流程状态",
    "模型阶段状态",
    "数据ID",
    "模型知识层级",
    "模型知识形态",
    "模型主标题",
    "模型副标题",
    "模型知识内容",
    "模型一级分类",
    "模型二级分类",
    "模型关联标准",
    "模型适用范围",
    "模型置信度",
    "模型初标依据",
    "是否重点复核",
    "标准检索状态",
    "标准候选分数",
    "模型提供方",
    "模型名称",
    "Prompt版本",
    "模型运行ID",
    "图片处理状态",
    "图片证据摘要",
    "模型错误",
    "检索标准Top5",
    "标准版本",
]

REVIEW_COLUMNS = [
    "CZ复核结论",
    "CZ主标题",
    "CZ副标题",
    "CZ知识内容",
    "CZ一级分类",
    "CZ二级分类",
    "CZ关联标准",
    "CZ复核备注",
    "错误类型",
    "错误原因",
    "是否进入再训练样本",
    "审核人",
    "审核时间",
]

TOPIC_REVIEW_COLUMNS = [
    "审核结论",
    "错误类型",
    "错误原因",
    "审核备注",
    "是否进入训练集",
    "审核人",
    "审核时间",
]

TOPIC_MODEL_INITIAL_REVIEW_COLUMNS = [
    "模型初标结论",
    "模型初标错误类型",
    "模型初标原因",
    "模型初标标准一致性",
    "模型初标证据充分性",
    "模型初标内容一致性",
    "模型初标图片必要性",
    "模型初标标题质量",
    "模型初标置信度",
    "模型初标重点复核",
    "模型初标提供方",
    "模型初标模型名称",
    "模型初标Prompt版本",
    "模型初标运行ID",
    "模型初标状态",
]

TOPIC_FEATURE_COLUMNS = [
    "问题意图",
    "对象/部位",
    "异常现象",
    "解题方式",
    "证据等级",
    "标准关键词",
    "主标准路径",
    "图片处理状态",
    "图片证据摘要",
    "主题图片链接",
    "主题图片必要性",
    "主题图片说明",
]

KNOWLEDGE_MASTER_COLUMNS = [
    "主标题",
    "副标题",
    "知识内容",
    "知识分类",
    "知识来源",
    "关联标准项",
    "适用范围",
    "生效状态",
    "来源版本",
    "变更类型",
    "失效原因",
    "检索关键词",
    "校验备注",
]

TOPIC_CANDIDATE_COLUMNS = [
    "主题ID",
    "主题状态",
    "主题样本数",
    "主题来源记录ID",
    "主题工单ID",
    "主题聚类键",
    "主题问题意图",
    "主题对象/部位",
    "主题异常现象",
    "主题解题方式",
    "主题证据等级",
    "主题证据摘要",
    "主题检索标准Top5",
    "主题初标复核标准Top5",
    "主题标准版本",
    "主题置信度",
    "是否重点复核",
    "主题模型提供方",
    "主题模型名称",
    "主题Prompt版本",
    "主题模型运行ID",
    *TOPIC_MODEL_INITIAL_REVIEW_COLUMNS,
    *KNOWLEDGE_MASTER_COLUMNS,
]

TOPIC_SOURCE_MAPPING_COLUMNS = [
    "主题ID",
    "来源记录ID",
    "工单ID",
    "核心问题",
    "聊天内容",
    "产品类型",
    "一级分类",
    "二级分类",
    "主标准路径",
    "证据等级",
    "纳入主题原因",
    "问题意图",
    "对象/部位",
    "异常现象",
    "解题方式",
    "主标准路径",
    "关联标准项",
    "模型运行ID",
]

TOPIC_MODEL_DRAFT_COLUMNS = [
    "主题ID",
    "转写提供方",
    "转写模型名称",
    "转写Prompt版本",
    "转写模型运行ID",
    "转写置信度",
    "转写是否重点复核",
    *KNOWLEDGE_MASTER_COLUMNS,
]

CANDIDATE_COLUMNS = [
    "候选ID",
    "来源记录ID",
    *KNOWLEDGE_MASTER_COLUMNS,
    "候选知识形态",
    "模型置信度",
    "是否重点复核",
    "标准检索状态",
    "标准版本",
    "模型运行ID",
    "模型提供方",
    "模型名称",
    "图片处理状态",
    "图片证据摘要",
    "模型错误",
    "工单ID",
]

PUBLISHED_COLUMNS = [
    "知识ID",
    "来源记录ID",
    *KNOWLEDGE_MASTER_COLUMNS,
    "审核人",
    "审核时间",
]

FEEDBACK_COLUMNS = [
    "数据ID",
    "工单ID",
    "模型主标题",
    "CZ主标题",
    "模型一级分类",
    "CZ一级分类",
    "模型二级分类",
    "CZ二级分类",
    "模型关联标准",
    "CZ关联标准",
    "错误类型",
    "错误原因",
    "是否进入再训练样本",
    "审核人",
    "审核时间",
]

PREPROCESS_COLUMNS = [
    "预处理状态",
    "预处理备注",
    "缺失字段",
    "可进入模型初标",
    "原始问题清洗",
    "原始聊天清洗",
    "原始依据清洗",
    "原始话术清洗",
    "原始图片链接清洗",
]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _normalize_lines(value: Any) -> str:
    if value is None:
        return ""
    lines = []
    for line in str(value).splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _split_keywords(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    parts = re.split(r"[,\n，;；/|、\s]+", text)
    return [part for part in parts if part]


def _safe_join(parts: list[str], sep: str = " / ") -> str:
    return sep.join(part for part in parts if part)


def _record_id_for_row(row: dict[str, Any], index: int) -> str:
    return _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID")) or f"row-{index:05d}"


def _extract_reasoning_hint(core_problem: str, judgment: str, basis: str, reference_script: str) -> str:
    pieces = []
    for value in [core_problem, judgment, basis, reference_script]:
        text = _clean_text(value)
        if text:
            pieces.append(text[:120])
    return " | ".join(pieces)


def _guess_title(core_problem: str, standard: StandardCatalogItem | None = None) -> str:
    text = _clean_text(core_problem)
    if not text:
        return standard.title if standard and standard.title else ""
    text = re.sub(r"^(老师|您好|你好|麻烦|请问|请教|帮我|帮忙|想问下|问下|看看|看下)[,，\s]*", "", text)
    text = re.sub(r"^(这个|这种|这样|此|该)[,，\s]*", "", text)
    text = text.replace("问题描述：", "").replace("问题类型：质检问题", "")
    text = text.strip("：:。；;！？!?")
    replacements = {
        "怎么判": "如何判定",
        "怎么判断": "如何判定",
        "怎么处理": "如何处理",
        "是不是": "是否为",
        "能不能": "是否可以",
        "可不可以": "是否可以",
    }
    for source, target in replacements.items():
        if source in text:
            text = text.replace(source, target)
    if len(text) > 32:
        text = text[:32]
    return text or (standard.title if standard else "")


def _build_subtitles(core_problem: str, title: str, standard: StandardCatalogItem | None) -> list[str]:
    subtitles: list[str] = []
    question = _clean_text(core_problem)
    if question and question != title:
        subtitles.append(question[:48])
    if standard and standard.title and standard.title != title:
        subtitles.append(standard.title)
    for keyword in standard.keywords[:2] if standard else []:
        if keyword not in subtitles:
            subtitles.append(keyword)
    return subtitles[:3]


def _match_standard(
    row_text: str,
    category_l1: str,
    category_l2: str,
    standard_catalog: list[StandardCatalogItem],
    top_k: int = 3,
) -> tuple[StandardCatalogItem | None, list[tuple[StandardCatalogItem, float]], float]:
    scored: list[tuple[StandardCatalogItem, float]] = []
    row_lower = row_text.lower()
    for item in standard_catalog:
        if not is_active_standard(item.status):
            continue
        score = 0.0
        if category_l1 and item.category_l1 and category_l1 == item.category_l1:
            score += 3.0
        if category_l2 and item.category_l2 and category_l2 == item.category_l2:
            score += 4.0
        if item.title and item.title.lower() in row_lower:
            score += 4.0
        for keyword in item.keywords:
            if keyword and keyword.lower() in row_lower:
                score += 0.25 if keyword.lower() in BROAD_RETRIEVAL_TERMS else 2.0
        if item.scope and item.scope.lower() in row_lower:
            score += 1.0
        if item.standard_path and item.standard_path.lower() in row_lower:
            score += 3.0
        if item.knowledge_type and item.knowledge_type.lower() in row_lower:
            score += 0.5
        scored.append((item, score))
    knowledge_type_priority = {"场景判定": 3, "检测方法": 2, "标准定义": 1}
    scored.sort(
        key=lambda pair: (pair[1], knowledge_type_priority.get(pair[0].knowledge_type, 0)),
        reverse=True,
    )
    best = scored[0] if scored else (None, 0.0)
    return best[0], scored, best[1]


def _unique_standard_matches(
    matches: list[tuple[StandardCatalogItem, float]],
    top_k: int,
) -> list[tuple[StandardCatalogItem, float]]:
    unique: list[tuple[StandardCatalogItem, float]] = []
    seen: set[str] = set()
    for item, score in matches:
        if score < MIN_STANDARD_RELEVANCE_SCORE:
            continue
        key = item.standard_id or _primary_standard_path(item.standard_path) or item.title
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((item, score))
        if len(unique) >= top_k:
            break
    return unique


def _primary_standard_path(standard_path: str) -> str:
    """Keep the first topic path from a multi-line standard catalog cell."""
    for line in _normalize_lines(standard_path).splitlines():
        parts = re.findall(r"【([^】]+)】", line)
        if parts:
            return "-".join(f"【{part.strip()}】" for part in parts if part.strip())
    return _clean_text(standard_path).splitlines()[0].strip() if _clean_text(standard_path) else ""


def retrieve_standard_matches(
    source_row: dict[str, Any],
    standard_catalog: list[StandardCatalogItem],
    top_k: int = 5,
) -> list[tuple[StandardCatalogItem, float]]:
    searchable_text = " ".join(
        [
            _clean_text(source_row.get("核心问题")),
            _normalize_lines(source_row.get("聊天内容")),
            _clean_text(source_row.get("判定结论")),
            _normalize_lines(source_row.get("判定依据")),
            _normalize_lines(source_row.get("参考话术")),
            _clean_text(source_row.get("产品类型")),
            _clean_text(source_row.get("一级分类")),
            _clean_text(source_row.get("二级分类")),
        ]
    )
    _, matches, _ = _match_standard(
        searchable_text,
        _clean_text(source_row.get("一级分类")),
        _clean_text(source_row.get("二级分类")),
        standard_catalog,
        top_k=top_k,
    )
    return _unique_standard_matches(matches, top_k)


def _confidence_from_score(score: float, runner_up: float) -> float:
    if score <= 0:
        return 0.2
    confidence = 0.35 + min(score / 8.0, 0.55)
    if score - runner_up < 1.0:
        confidence -= 0.1
    return round(max(0.1, min(confidence, 0.98)), 3)


def _join_standard_refs(matches: list[tuple[StandardCatalogItem, float]]) -> str:
    refs = []
    for item, score in matches:
        ref = item.standard_id or item.standard_path or item.title
        if ref:
            refs.append(f"{ref}({round(score, 2)})")
    return "\n".join(refs)


def _build_model_content(
    core_problem: str,
    judgment: str,
    basis: str,
    reference_script: str,
    standard: StandardCatalogItem | None,
) -> str:
    sections = []
    title = standard.title if standard and standard.title else _guess_title(core_problem, standard)
    if title:
        sections.append(f"主问题：{title}")
    if basis:
        sections.append(f"判定依据：{_normalize_lines(basis)}")
    if judgment:
        sections.append(f"当前结论：{_normalize_lines(judgment)}")
    if reference_script:
        sections.append(f"参考话术：{_normalize_lines(reference_script)}")
    if standard and standard.scope:
        sections.append(f"适用范围：{standard.scope}")
    return "\n".join(sections)


def _process_kind(core_problem: str, source_l1: str, source_l2: str) -> str:
    text = " ".join([core_problem, source_l1, source_l2])
    if "机型" in text or "型号" in text:
        return "model_query"
    if any(marker in text for marker in ("拆修", "维修", "维修痕迹")):
        return "repair"
    if any(marker in text for marker in ("浸液", "防水标")):
        return "liquid"
    if any(marker in text for marker in ("摄像", "拍照", "充电", "闪光", "按键", "蓝牙", "WIFI", "WiFi", "功能")):
        return "function"
    if any(marker in text for marker in ("显示", "屏幕")):
        return "display"
    if any(marker in text for marker in ("外观", "中框", "外壳", "后盖", "镜头")):
        return "appearance"
    return ""


def _has_explicit_boundary_case(text: str) -> bool:
    return any(all(marker in text for marker in case) for case in EXPLICIT_BOUNDARY_CASES)


def _is_process_candidate(
    core_problem: str,
    judgment: str,
    basis: str,
    chat_content: str,
    standard: StandardCatalogItem | None,
    source_l1: str,
    source_l2: str,
) -> bool:
    """Prefer reusable verification/query methods for image-dependent question types."""
    text = " ".join([core_problem, judgment, basis, chat_content])
    if standard is None or any(marker in text for marker in UNCERTAINTY_MARKERS):
        return True
    if _has_explicit_boundary_case(text):
        return False
    return bool(_process_kind(core_problem, source_l1, source_l2))


def _process_standard_topic(standard: StandardCatalogItem | None) -> str:
    if not standard:
        return ""
    path_parts = re.findall(r"【([^】]+)】", _primary_standard_path(standard.standard_path))
    topic = path_parts[-1] if path_parts else ""
    return re.sub(r"[（(][^）)]*[）)]", "", topic).strip()


def _process_title(
    core_problem: str,
    source_l1: str,
    source_l2: str,
    standard: StandardCatalogItem | None = None,
) -> str:
    kind = _process_kind(core_problem, source_l1, source_l2)
    standard_topic = _process_standard_topic(standard)
    if kind == "model_query":
        return "设备机型如何查询与确认"
    if kind == "repair":
        return f"{standard_topic or '疑似拆修或维修痕迹'}如何核验"
    if kind == "liquid":
        return f"{standard_topic or '浸液风险'}如何核验"
    if kind == "function":
        return f"{standard_topic or source_l2 or '设备功能'}如何核验"
    if kind == "display":
        return f"{standard_topic or '屏幕显示异常'}如何通过图片核验"
    if kind == "appearance":
        return f"{standard_topic or '设备外观异常'}如何通过图片核验"
    category = _safe_join([source_l1, source_l2], " / ")
    return f"{category or _guess_title(core_problem)}如何核验"


def _build_process_content(
    core_problem: str,
    judgment: str,
    basis: str,
    source_l1: str,
    source_l2: str,
    standard: StandardCatalogItem | None = None,
) -> str:
    del judgment, basis
    kind = _process_kind(core_problem, source_l1, source_l2)
    scope = _safe_join([source_l1, source_l2], " / ") or "待人工确认分类"
    if kind == "model_query":
        sections = [
            "适用主题：设备机型或型号查询与确认。",
            "查询流程：\n1. 在设备设置中查看“关于本机/关于手机”中的型号名称或型号。\n2. 使用 IMEI、SN 或官方查询渠道核对出厂机型。\n3. 对照设备实物的外观样式、功能配置和关键部件特征。\n4. 查询结果与实物不一致时，补充截图和实物照片，按机况异常流程转人工确认。",
        ]
    elif kind == "repair":
        sections = [
            "适用主题：疑似拆修、维修痕迹或部件异常的图片核验。",
            "核验流程：\n1. 明确异常位于屏幕、后盖、中框、镜头或内部零部件，并记录对应部位。\n2. 补充异常部位近景、整机全景和不同角度照片；必要时补充拆机或工具检测结果。\n3. 核对原厂结构、胶痕、撬痕、部件标识和连接状态，并逐项对照拆修标准。\n4. 证据不能明确支持任一标准项时，保留“待确认”并转人工复核，不将疑似现象直接判为拆修。",
        ]
    elif kind == "display":
        sections = [
            "适用主题：屏幕显示、亮度、颜色、坏点或异常痕迹的图片核验。",
            "核验流程：\n1. 确认异常发生在亮屏、白屏、黑屏、息屏或特定测试画面中的哪一种状态。\n2. 使用标准测试画面拍摄屏幕正面全景和异常点近景，避免反光、贴膜或环境光干扰。\n3. 记录异常的颜色、位置、数量、直径或面积，并对照显示问题标准中的量化条件。\n4. 现象无法稳定复现、图片不清晰或无法与标准条件对应时，补充证据后转人工复核。",
        ]
    elif kind == "function":
        sections = [
            "适用主题：摄像头、充电、闪光灯、按键等设备功能的核验。",
            "功能核验流程：\n1. 明确待核验的功能、测试条件和所用配件；先排除电量、网络、权限、保护壳等外部影响。\n2. 按标准步骤连续执行功能测试，并记录测试画面、提示信息、声音、响应或结果照片/视频。\n3. 必要时更换已确认正常的配件或测试环境复验，并对照对应功能标准确认异常条件。\n4. 结果不稳定、无法复现或无法排除外部条件时，保留测试证据并转人工复核。",
        ]
    elif kind == "liquid":
        sections = [
            "适用主题：浸液、防水标或液体接触风险的核验。",
            "核验流程：\n1. 明确检查部位，包括防水标、卡槽、接口、屏幕边缘、后盖和内部零部件。\n2. 补充局部近景、全景和必要的拆机检测照片，记录变色、腐蚀、水渍或液体残留。\n3. 按当前标准核对防水标状态及浸液特征，不以单一模糊痕迹直接判定浸液。\n4. 证据不足、部位不可见或特征存在争议时，补充检测后转人工复核。",
        ]
    elif kind == "appearance":
        sections = [
            "适用主题：中框、外壳、后盖、镜头等外观异常的图片核验。",
            "核验流程：\n1. 确认异常部位、材质和现象类型，例如磕碰、划痕、磨损、掉漆、碎裂或脱胶。\n2. 拍摄整机全景、异常部位近景和侧视角度；涉及尺寸或数量时，补充量尺或可比对参照物。\n3. 记录异常的最大直径、长度、数量、是否有触感或材料缺损，并对照外观标准的边界条件。\n4. 无法区分现象类型或无法满足标准量化条件时，补充图片并转人工复核。",
        ]
    else:
        sections = [
            f"适用主题：{scope}相关问题的规则核验。",
            "核验流程：\n1. 明确待确认的对象、现象和对应标准项。\n2. 补充能支持判断的截图、照片、视频或查询结果。\n3. 对照当前有效标准逐项确认适用条件、边界条件和例外情况。\n4. 无法与标准明确对应时，保留证据并转人工复核。",
        ]
    sections.append(f"建议分类：{scope}")
    if standard and standard.response_snippet:
        sections.append(f"标准核验要点：\n{_normalize_lines(standard.response_snippet)[:900]}")
    sections.append("处理边界：本流程用于形成可复用的核验方法；没有充分证据时不得把单个工单的结论外推为通用判定。")
    return "\n".join(sections)


def _short_basis(basis: str, limit: int = 500) -> str:
    text = _normalize_lines(basis)
    if not text:
        return ""
    for marker in ("事实核查结果：", "采纳/排除逻辑："):
        text = text.split(marker, 1)[0]
    text = text.replace("平台标准依据：", "").strip()
    return text[:limit].rstrip("，,；;。")


def _candidate_title(row: dict[str, Any], standard: StandardCatalogItem | None) -> str:
    model_title = _clean_text(row.get("模型主标题"))
    if (
        model_title
        and len(model_title) <= 40
        and any(marker in model_title for marker in ("如何", "怎么", "是否", "什么", "哪些", "能否"))
        and not any(
            marker in model_title
            for marker in ("回收师", "缺乏相关知识", "希望获得", "问题，但", "判定为", "应被判定")
        )
    ):
        return model_title

    judgment = _clean_text(row.get("判定结论"))
    if judgment:
        title = re.split(r"[，,。；;]", judgment, maxsplit=1)[0].replace("应被判定为", "判定为")
        matched = re.match(r"^(.{2,32}?)(?:应|需|可)?判定为", title)
        if matched:
            return f"{matched.group(1).strip()}如何判定"
        title = re.sub(r"^(该问题|此问题|该情况)", "", title).strip()
        if 4 <= len(title) <= 40 and any(marker in title for marker in ("如何", "怎么", "是否", "什么", "哪些", "能否")):
            return title

    if standard and standard.title and any(marker in standard.title for marker in ("如何", "怎么", "是否", "什么")):
        return standard.title

    standard_topic = _process_standard_topic(standard)
    if standard_topic:
        return f"{standard_topic}如何判定"

    return _process_title(
        _clean_text(row.get("核心问题")),
        _clean_text(row.get("一级分类")),
        _clean_text(row.get("二级分类")),
        standard,
    )


def _candidate_subtitles(row: dict[str, Any], title: str, standard: StandardCatalogItem | None) -> str:
    values = [
        standard.title if standard else "",
        _clean_text(row.get("二级分类")),
        *(standard.keywords[:2] if standard else []),
    ]
    subtitles: list[str] = []
    for value in values:
        item = _clean_text(value)
        if (
            item
            and item != title
            and item not in subtitles
            and not any(marker in item for marker in ("回收师", "现场", "咨询", "希望获得", "问题描述"))
        ):
            subtitles.append(item[:80])
        if len(subtitles) >= 3:
            return "\n".join(subtitles)
    return "\n".join(subtitles)


def _candidate_content(
    row: dict[str, Any],
    standard: StandardCatalogItem | None,
    knowledge_form: str,
) -> str:
    core_problem = _clean_text(row.get("核心问题"))
    judgment = _clean_text(row.get("判定结论"))
    basis = _short_basis(_clean_text(row.get("判定依据")))
    reference_script = _normalize_lines(row.get("参考话术"))
    if knowledge_form == "流程方法":
        return _build_process_content(
            core_problem,
            judgment,
            basis,
            _clean_text(row.get("一级分类")),
            _clean_text(row.get("二级分类")),
            standard,
        )

    sections = []
    if standard and standard.response_snippet:
        sections.append(f"判定规则：\n{_normalize_lines(standard.response_snippet)[:900]}")
    elif standard and standard.standard_path:
        sections.append(f"关联标准：\n{standard.standard_path}")
    if judgment:
        sections.append(f"场景结论：\n{judgment}")
    if reference_script:
        sections.append(f"处理建议：\n{reference_script[:600]}")
    if basis:
        sections.append(f"核验依据摘要：\n{basis}")
    if not sections:
        sections.append("待人工补充判定规则、场景结论和处理建议。")
    return "\n\n".join(sections)


def _candidate_keywords(row: dict[str, Any], title: str, standard: StandardCatalogItem | None) -> str:
    values = [
        title,
        _clean_text(row.get("一级分类")),
        _clean_text(row.get("二级分类")),
        _primary_standard_path(standard.standard_path) if standard else "",
    ]
    keywords: list[str] = []
    for value in values:
        keyword = _clean_text(value)
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return " | ".join(keywords)


def _refresh_candidate_knowledge(
    row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
) -> None:
    standard = matches[0][0] if matches else None
    knowledge_form = _clean_text(row.get("模型知识形态")) or "流程方法"
    title = (
        _process_title(
            _clean_text(row.get("核心问题")),
            _clean_text(row.get("一级分类")),
            _clean_text(row.get("二级分类")),
            standard,
        )
        if knowledge_form == "流程方法"
        else _candidate_title(row, standard)
    )
    source_id = _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
    needs_review = _clean_text(row.get("是否重点复核")) == "是"
    notes = [
        f"来源数据ID：{source_id}",
        f"标准检索：{_clean_text(row.get('标准检索状态')) or '待检索'}",
    ]
    if not _normalize_lines(row.get("聊天内容")):
        notes.append("缺少原始聊天上下文")
    if needs_review:
        notes.append("需人工重点复核")
    if _clean_text(row.get("模型错误")):
        notes.append(_clean_text(row.get("模型错误")))

    row.update(
        {
            "候选ID": f"KC-{source_id}" if source_id else "",
            "来源记录ID": source_id,
            "主标题": title,
            "副标题": _candidate_subtitles(row, title, standard),
            "知识内容": _candidate_content(row, standard, knowledge_form),
            "知识分类": "检测方法" if knowledge_form == "流程方法" else "场景判定",
            "知识来源": "方向二会话候选",
            "关联标准项": _primary_standard_path(standard.standard_path) if standard else "",
            "适用范围": _safe_join([_clean_text(row.get("产品类型")), standard.scope if standard else ""], "；"),
            "生效状态": "待审核",
            "来源版本": standard.version if standard and standard.version else "待补充",
            "变更类型": "新增",
            "失效原因": "",
            "检索关键词": _candidate_keywords(row, title, standard),
            "校验备注": "；".join(note for note in notes if note),
            "候选知识形态": knowledge_form,
        }
    )


def _is_needs_review(
    confidence: float,
    matches: list[tuple[StandardCatalogItem, float]],
    source_categories: tuple[str, str],
    selected: StandardCatalogItem | None,
    threshold: float,
) -> bool:
    category_l1, category_l2 = source_categories
    if confidence < threshold:
        return True
    if len(matches) >= 2 and matches[0][1] - matches[1][1] < 0.9:
        return True
    if selected is None:
        return True
    if category_l1 and selected.category_l1 and category_l1 != selected.category_l1:
        return True
    if category_l2 and selected.category_l2 and category_l2 != selected.category_l2:
        return True
    return False


def _default_error_type(model_row: dict[str, Any], review_row: dict[str, Any]) -> str:
    fields = [
        ("模型主标题", "CZ主标题", "标题不准"),
        ("模型一级分类", "CZ一级分类", "分类错"),
        ("模型二级分类", "CZ二级分类", "分类错"),
        ("模型关联标准", "CZ关联标准", "标准项映射错"),
    ]
    for left_key, right_key, label in fields:
        left = _clean_text(model_row.get(left_key))
        right = _clean_text(review_row.get(right_key))
        if right and left and left != right:
            return label
    return ""


def _read_source_rows(path: str | Path) -> list[dict[str, Any]]:
    _, rows = read_workbook_rows(path)
    return rows


def filter_source_rows_by_product_type(
    source_rows: list[dict[str, Any]],
    product_type: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = _clean_text(product_type)
    if not target:
        return source_rows, []

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in source_rows:
        actual = _clean_text(row.get("产品类型"))
        if actual == target:
            selected.append(row)
            continue
        excluded_row = dict(row)
        excluded_row["排除原因"] = f"产品类型不匹配：期望 {target}，实际 {actual or '空'}"
        excluded.append(excluded_row)
    return selected, excluded


def _normalize_image_links(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    parts = [part.strip() for part in re.split(r"[\n,，;；\s]+", text) if part.strip()]
    return "\n".join(dict.fromkeys(parts))


def _missing_fields(row: dict[str, Any]) -> list[str]:
    critical_fields = [
        "核心问题",
        "判定结论",
        "判定依据",
        "一级分类",
        "二级分类",
    ]
    missing = []
    for field in critical_fields:
        if not _clean_text(row.get(field)):
            missing.append(field)
    return missing


def preprocess_source_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    processed_rows: list[dict[str, Any]] = []
    for index, source_row in enumerate(source_rows, start=1):
        row = dict(source_row)
        row["序号"] = _clean_text(row.get("序号") or index)
        row["上传者"] = _clean_text(row.get("上传者"))
        row["分析时间"] = _clean_text(row.get("分析时间"))
        row["工单ID"] = _clean_text(row.get("工单ID"))
        row["数据ID"] = _clean_text(row.get("数据ID")) or row["工单ID"] or f"row-{index:05d}"
        row["回收单号"] = _clean_text(row.get("回收单号"))
        row["聊天内容"] = _normalize_lines(row.get("聊天内容"))
        row["图片链接"] = _normalize_image_links(row.get("图片链接"))
        row["核心问题"] = _clean_text(row.get("核心问题"))
        row["判定结论"] = _clean_text(row.get("判定结论"))
        row["判定依据"] = _normalize_lines(row.get("判定依据"))
        row["产品类型"] = _clean_text(row.get("产品类型"))
        row["一级分类"] = _clean_text(row.get("一级分类"))
        row["二级分类"] = _clean_text(row.get("二级分类"))
        row["参考话术"] = _normalize_lines(row.get("参考话术"))

        missing = _missing_fields(row)
        notes = []
        if missing:
            notes.append(f"缺失字段: {', '.join(missing)}")
        if not row["聊天内容"]:
            notes.append("缺少原始聊天上下文；仅按结构化字段、图片和标准生成候选，强制重点复核")
        if row["聊天内容"] and len(row["聊天内容"]) > 8000:
            notes.append("聊天内容过长，已保留原文结构")
        if row["图片链接"] and "\n" in row["图片链接"]:
            notes.append("图片链接已去重")
        if not notes:
            notes.append("预处理完成")

        row["预处理状态"] = "preprocessed"
        row["预处理备注"] = "；".join(notes)
        row["缺失字段"] = "\n".join(missing)
        row["可进入模型初标"] = "是" if not missing else "否"
        row["原始问题清洗"] = row["核心问题"]
        row["原始聊天清洗"] = row["聊天内容"]
        row["原始依据清洗"] = row["判定依据"]
        row["原始话术清洗"] = row["参考话术"]
        row["原始图片链接清洗"] = row["图片链接"]
        processed_rows.append(row)
    return processed_rows


def filter_preprocessed_rows_for_model(
    preprocessed_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in preprocessed_rows:
        if _clean_text(row.get("可进入模型初标")) == "是":
            eligible.append(row)
            continue
        excluded_row = dict(row)
        missing = _clean_text(excluded_row.get("缺失字段"))
        excluded_row["排除原因"] = f"缺少模型初标必填字段：{missing or '未标明'}"
        excluded.append(excluded_row)
    return eligible, excluded


def _standard_payload(item: StandardCatalogItem | None) -> str:
    if not item:
        return ""
    parts = [item.standard_id, item.standard_path, item.title, item.knowledge_type, item.category_l1, item.category_l2]
    return _safe_join([part for part in parts if part], " | ")


def initial_label_rows(
    source_rows: list[dict[str, Any]],
    standard_catalog: list[StandardCatalogItem],
    min_confidence: float = 0.75,
) -> list[dict[str, Any]]:
    labeled_rows: list[dict[str, Any]] = []
    for index, source_row in enumerate(source_rows, start=1):
        serial_no = _clean_text(source_row.get("序号") or source_row.get("serial_no") or index)
        work_order_id = _clean_text(source_row.get("工单ID"))
        record_id = _record_id_for_row(source_row, index)
        core_problem = _clean_text(source_row.get("核心问题"))
        chat_content = _normalize_lines(source_row.get("聊天内容"))
        judgment = _clean_text(source_row.get("判定结论"))
        basis = _normalize_lines(source_row.get("判定依据"))
        reference_script = _normalize_lines(source_row.get("参考话术"))
        product_type = _clean_text(source_row.get("产品类型"))
        source_l1 = _clean_text(source_row.get("一级分类"))
        source_l2 = _clean_text(source_row.get("二级分类"))
        searchable_text = " ".join(
            [
                core_problem,
                chat_content,
                judgment,
                basis,
                reference_script,
                product_type,
                source_l1,
                source_l2,
            ]
        )
        _best_standard, raw_matches, _raw_score = _match_standard(searchable_text, source_l1, source_l2, standard_catalog)
        top_matches = _unique_standard_matches(raw_matches, top_k=5)
        standard = top_matches[0][0] if top_matches else None
        raw_score = top_matches[0][1] if top_matches else 0.0
        runner_up = top_matches[1][1] if len(top_matches) > 1 else 0.0
        confidence = _confidence_from_score(raw_score, runner_up)
        chosen_l1 = standard.category_l1 if standard and standard.category_l1 else source_l1
        chosen_l2 = standard.category_l2 if standard and standard.category_l2 else source_l2
        is_process = _is_process_candidate(
            core_problem,
            judgment,
            basis,
            chat_content,
            standard,
            source_l1,
            source_l2,
        )
        title = _process_title(core_problem, source_l1, source_l2, standard) if is_process else _guess_title(core_problem, standard)
        subtitles = _build_subtitles(core_problem, title, standard)
        content = (
            _build_process_content(core_problem, judgment, basis, source_l1, source_l2, standard)
            if is_process
            else _build_model_content(core_problem, judgment, basis, reference_script, standard)
        )
        needs_review = _is_needs_review(confidence, top_matches, (source_l1, source_l2), standard, min_confidence)
        if not chat_content:
            needs_review = True
        initial_note = _extract_reasoning_hint(core_problem, judgment, basis, reference_script)
        labeled_rows.append(
            {
                "序号": serial_no,
                "上传者": _clean_text(source_row.get("上传者")),
                "分析时间": _clean_text(source_row.get("分析时间")),
                "工单ID": work_order_id,
                "回收单号": _clean_text(source_row.get("回收单号")),
                "聊天内容": chat_content,
                "图片链接": _normalize_lines(source_row.get("图片链接")),
                "核心问题": core_problem,
                "判定结论": judgment,
                "判定依据": basis,
                "产品类型": product_type,
                "一级分类": source_l1,
                "二级分类": source_l2,
                "参考话术": reference_script,
                "预处理状态": _clean_text(source_row.get("预处理状态")) or "preprocessed",
                "预处理备注": _clean_text(source_row.get("预处理备注")),
                "缺失字段": _clean_text(source_row.get("缺失字段")),
                "可进入模型初标": _clean_text(source_row.get("可进入模型初标")) or "是",
                "原始问题清洗": _clean_text(source_row.get("原始问题清洗")),
                "原始聊天清洗": _normalize_lines(source_row.get("原始聊天清洗")),
                "原始依据清洗": _normalize_lines(source_row.get("原始依据清洗")),
                "原始话术清洗": _normalize_lines(source_row.get("原始话术清洗")),
                "原始图片链接清洗": _normalize_lines(source_row.get("原始图片链接清洗")),
                "流程状态": "review_pending",
                "模型阶段状态": "model_labeled",
                "数据ID": record_id,
                "模型知识层级": "L2",
                "模型知识形态": "流程方法" if is_process else "具体判定",
                "模型主标题": title,
                "模型副标题": subtitles,
                "模型知识内容": content,
                "模型一级分类": chosen_l1,
                "模型二级分类": chosen_l2,
                "模型关联标准": _join_standard_refs(top_matches),
                "模型适用范围": standard.scope if standard else "",
                "模型置信度": confidence,
                "模型初标依据": initial_note,
                "是否重点复核": "是" if needs_review else "否",
                "标准检索状态": "已命中相关知识" if top_matches else "未搜索到相关知识（待人工补充）",
                "标准候选分数": raw_score,
                "模型提供方": "rule-baseline",
                "模型名称": "standard-match-v1",
                "Prompt版本": "",
                "模型运行ID": "",
                "图片处理状态": "未处理",
                "图片证据摘要": "",
                "模型错误": "",
                "CZ复核结论": "",
                "CZ主标题": "",
                "CZ副标题": "",
                "CZ知识内容": "",
                "CZ一级分类": "",
                "CZ二级分类": "",
                "CZ关联标准": "",
                "CZ复核备注": "",
                "错误类型": "",
                "错误原因": "",
                "是否进入再训练样本": "",
                "审核人": "",
                "审核时间": "",
                "标准匹配摘要": _standard_payload(standard),
            }
        )
        _refresh_candidate_knowledge(labeled_rows[-1], top_matches)
    return labeled_rows


def _standard_reference(item: StandardCatalogItem) -> str:
    return item.standard_id or item.standard_path or item.title


def _retrieved_standard_rows(matches: list[tuple[StandardCatalogItem, float]]) -> list[dict[str, Any]]:
    return [
        {
            "standard_ref": _standard_reference(item),
            "standard_id": item.standard_id,
            "title": item.title,
            "category_l1": item.category_l1,
            "category_l2": item.category_l2,
            "knowledge_type": item.knowledge_type,
            "standard_path": item.standard_path,
            "keywords": item.keywords,
            "scope": item.scope,
            "response_snippet": item.response_snippet,
            "status": item.status,
            "version": item.version,
            "retrieval_score": round(score, 3),
        }
        for item, score in matches
    ]


def _format_model_refs(refs: list[str], matches: list[tuple[StandardCatalogItem, float]]) -> str:
    by_ref = {_standard_reference(item): item for item, _score in matches}
    result = []
    for ref in refs:
        item = by_ref.get(ref)
        if item:
            result.append(_safe_join([ref, item.title, f"版本:{item.version}"], " | "))
        elif ref:
            result.append(ref)
    return "\n".join(result)


def _format_retrieved_standards(matches: list[tuple[StandardCatalogItem, float]]) -> str:
    lines = []
    for item, score in matches:
        lines.append(
            _safe_join(
                [
                    _standard_reference(item),
                    item.title,
                    item.standard_path,
                    item.knowledge_type,
                    f"版本:{item.version}",
                    f"分数:{round(score, 2)}",
                ],
                " | ",
            )
        )
    return "\n".join(lines)


def _apply_process_guardrail(
    candidate: dict[str, Any],
    source_row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    reason: str,
) -> None:
    core_problem = _clean_text(source_row.get("核心问题"))
    judgment = _clean_text(source_row.get("判定结论"))
    basis = _normalize_lines(source_row.get("判定依据"))
    source_l1 = _clean_text(source_row.get("一级分类"))
    source_l2 = _clean_text(source_row.get("二级分类"))
    standard = matches[0][0] if matches else None
    title = _process_title(core_problem, source_l1, source_l2, standard)
    current_confidence = candidate.get("模型置信度", 0.45)
    try:
        confidence = min(float(current_confidence), 0.45)
    except (TypeError, ValueError):
        confidence = 0.45
    candidate.update(
        {
            "模型知识层级": "L2",
            "模型知识形态": "流程方法",
            "模型主标题": title,
            "模型副标题": _build_subtitles(core_problem, title, None),
            "模型知识内容": _build_process_content(core_problem, judgment, basis, source_l1, source_l2, standard),
            "模型一级分类": source_l1,
            "模型二级分类": source_l2,
            "模型关联标准": _join_standard_refs(matches),
            "模型适用范围": standard.scope if standard else "",
            "模型置信度": round(confidence, 3),
            "模型初标依据": _safe_join([_extract_reasoning_hint(core_problem, judgment, basis, ""), reason], " | "),
            "是否重点复核": "是",
            "模型错误": _safe_join([_clean_text(candidate.get("模型错误")), reason], "；"),
        }
    )


def _image_status(images: list[ImageEvidence], had_links: bool) -> tuple[str, bool]:
    if not had_links:
        return "无图片链接（文本初标）", False
    ready = [item for item in images if item.status == "ready"]
    failed = [item for item in images if item.status != "ready"]
    details = [f"可用:{len(ready)}"]
    if failed:
        details.append(f"不可用:{len(failed)}")
        details.extend(f"{item.status}:{item.error}" for item in failed[:2])
    return "；".join(details), bool(failed) or not ready


def _feature_text(row: dict[str, Any]) -> str:
    return " ".join(
        _clean_text(row.get(field))
        for field in ("核心问题", "聊天内容", "判定结论", "判定依据", "参考话术", "一级分类", "二级分类")
    )


def _feature_intent(row: dict[str, Any]) -> str:
    text = _feature_text(row)
    if any(word in text for word in ("机型", "型号", "如何查询", "怎么查", "查询")):
        return "信息查询"
    if _has_explicit_boundary_case(text) or any(word in text for word in ("区分", "还是", "界定")):
        return "边界判定"
    if any(word in text for word in ("拆修", "维修", "胶状", "胶", "进水", "防水标")):
        return "痕迹核验"
    if any(word in text for word in ("功能", "拍照", "充电", "按键", "蓝牙", "WiFi", "WIFI")):
        return "功能核验"
    return "异常核验"


def _feature_part(row: dict[str, Any]) -> str:
    text = _feature_text(row)
    candidates = [
        ("屏幕", ("屏幕", "显示", "坏点", "漏液", "色斑")),
        ("中框/外壳", ("中框", "外壳", "后盖", "划痕", "磕点", "掉漆")),
        ("摄像头", ("摄像头", "拍照", "录像", "对焦")),
        ("充电部件", ("充电", "尾插", "接口")),
        ("主板/内部", ("主板", "拆修", "维修", "胶状", "防水标")),
    ]
    for label, words in candidates:
        if any(word in text for word in words):
            return label
    return _clean_text(row.get("二级分类")) or "待人工确认对象"


def _feature_phenomenon(row: dict[str, Any]) -> str:
    text = _feature_text(row)
    candidates = [
        ("坏点/漏液边界", ("坏点", "漏液")),
        ("磕点/划痕边界", ("磕点", "划痕")),
        ("疑似拆修痕迹", ("拆修", "维修", "胶状", "胶", "防水标")),
        ("显示异常", ("显示", "屏幕", "色斑", "亮点")),
        ("外观异常", ("外观", "中框", "外壳", "后盖", "掉漆")),
        ("功能异常", ("拍照", "充电", "按键", "蓝牙", "WiFi", "WIFI")),
    ]
    for label, words in candidates:
        if any(word in text for word in words):
            return label
    return _clean_text(row.get("二级分类")) or "待人工确认现象"


def _feature_method(intent: str, row: dict[str, Any]) -> str:
    if intent == "信息查询":
        return "官方信息查询与实物核对"
    if intent == "边界判定":
        return "定义与边界条件对照"
    if intent == "功能核验":
        return "功能测试与标准对照"
    if intent == "痕迹核验":
        return "现场图片补充与拆修标准核验"
    return "现场图片/视频补充与有效标准核验"


def _standard_keywords(matches: list[tuple[StandardCatalogItem, float]], row: dict[str, Any]) -> str:
    values = [_clean_text(row.get("一级分类")), _clean_text(row.get("二级分类"))]
    if matches:
        standard = matches[0][0]
        values.extend([standard.category_l1, standard.category_l2, *standard.keywords[:5]])
    return _merge_unique_keywords(values)


def extract_topic_feature_rows(
    source_rows: list[dict[str, Any]],
    standard_catalog: list[StandardCatalogItem],
    raw_source_rows: list[dict[str, Any]] | None = None,
    audit_store: AuditStore | None = None,
    run_id: str | None = None,
    image_downloader: ImageDownloader | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Extract auditable topic features only; this stage never drafts knowledge."""
    active_run_id = run_id or uuid.uuid4().hex
    downloader = image_downloader or ImageDownloader()
    raw_rows = raw_source_rows if raw_source_rows and len(raw_source_rows) == len(source_rows) else source_rows
    feature_rows: list[dict[str, Any]] = []

    for index, source_row in enumerate(source_rows, start=1):
        row = dict(source_row)
        record_id = _record_id_for_row(row, index)
        matches = retrieve_standard_matches(row, standard_catalog, top_k=5)
        image_links = _normalize_lines(row.get("图片链接"))
        images = downloader.fetch(image_links) if image_links else []
        image_status, _image_requires_review = _image_status(images, bool(split_image_urls(image_links)))
        intent = _feature_intent(row)
        primary = matches[0][0] if matches else None
        row.update(
            {
                "流程状态": "topic_pending",
                "模型阶段状态": "feature_extracted",
                "数据ID": record_id,
                "问题意图": intent,
                "对象/部位": _feature_part(row),
                "异常现象": _feature_phenomenon(row),
                "解题方式": _feature_method(intent, row),
                "证据等级": "完整会话" if _normalize_lines(row.get("聊天内容")) else (
                    "图片证据" if not _image_requires_review and _has_usable_image_evidence({"图片处理状态": image_status}) else "结构化摘要"
                ),
                "标准关键词": _standard_keywords(matches, row),
                "主标准路径": _primary_standard_path(primary.standard_path) if primary else "",
                "图片处理状态": image_status,
                "图片证据摘要": (
                    "包含原始聊天上下文；图片可作为辅助证据。"
                    if _normalize_lines(row.get("聊天内容"))
                    else ("无聊天内容，但存在可用现场图片。" if "可用:" in image_status and "可用:0" not in image_status else "缺少原始聊天内容和可用图片。")
                ),
                "标准检索状态": "已命中相关知识" if matches else "未搜索到相关知识（待人工补充）",
                "检索标准Top5": _format_retrieved_standards(matches) if matches else "未搜索到相关知识（待人工补充）",
                "标准版本": "\n".join(
                    f"{_standard_reference(item)}:{item.version}" for item, _score in matches if _standard_reference(item)
                ),
                "标准候选分数": matches[0][1] if matches else 0.0,
                "模型提供方": "feature-rule",
                "模型名称": "topic-feature-v1",
                "Prompt版本": "",
                "模型运行ID": "",
                "模型错误": "",
            }
        )
        feature_rows.append(row)
        if audit_store:
            audit_store.record_ingestion(
                active_run_id,
                record_id,
                raw_rows[index - 1],
                row,
                [image.metadata() for image in images],
            )
    return feature_rows, active_run_id


def generate_phone_candidate_rows(
    source_rows: list[dict[str, Any]],
    standard_catalog: list[StandardCatalogItem],
    min_confidence: float = 0.75,
    raw_source_rows: list[dict[str, Any]] | None = None,
    use_mimo: bool = True,
    audit_store: AuditStore | None = None,
    run_id: str | None = None,
    image_downloader: ImageDownloader | None = None,
    mimo_client: MimoClient | None = None,
) -> tuple[list[dict[str, Any]], str]:
    del min_confidence, use_mimo, mimo_client
    return extract_topic_feature_rows(
        source_rows,
        standard_catalog,
        raw_source_rows=raw_source_rows,
        audit_store=audit_store,
        run_id=run_id,
        image_downloader=image_downloader,
    )


def _review_value(row: dict[str, Any], key: str, fallback: str = "") -> str:
    value = _clean_text(row.get(key))
    return value if value else fallback


def _subtitle_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    return [_clean_text(item) for item in str(value or "").splitlines() if _clean_text(item)]


def _row_to_published_record(row: dict[str, Any], review_time: str) -> dict[str, Any]:
    decision = _clean_text(row.get("CZ复核结论"))
    model_title = _clean_text(row.get("主标题")) or _clean_text(row.get("模型主标题"))
    cz_title = _clean_text(row.get("CZ主标题"))
    model_subtitles = _subtitle_list(row.get("副标题") or row.get("模型副标题"))
    cz_subtitles = _subtitle_list(row.get("CZ副标题"))
    model_content = _clean_text(row.get("知识内容")) or _clean_text(row.get("模型知识内容"))
    cz_content = _clean_text(row.get("CZ知识内容"))
    model_l1 = _clean_text(row.get("模型一级分类"))
    model_l2 = _clean_text(row.get("模型二级分类"))
    cz_l1 = _clean_text(row.get("CZ一级分类"))
    cz_l2 = _clean_text(row.get("CZ二级分类"))
    model_refs = _clean_text(row.get("关联标准项")) or _clean_text(row.get("模型关联标准"))
    cz_refs = _clean_text(row.get("CZ关联标准"))
    content = cz_content or model_content
    title = cz_title or model_title
    subtitles = cz_subtitles or model_subtitles
    category = _safe_join([cz_l1 or model_l1, cz_l2 or model_l2], "/") or _clean_text(row.get("知识分类"))
    refs = cz_refs or model_refs
    knowledge_id = (
        _clean_text(row.get("来源记录ID"))
        or _clean_text(row.get("数据ID"))
        or _clean_text(row.get("工单ID"))
    )
    status = "published" if decision in {"通过", "修改后通过"} else "deprecated"
    return {
        "知识ID": knowledge_id,
        "来源记录ID": _clean_text(row.get("来源记录ID")) or _clean_text(row.get("数据ID")),
        "主标题": title,
        "副标题": subtitles,
        "知识内容": content,
        "知识分类": category,
        "知识来源": _clean_text(row.get("知识来源")) or "方向二会话候选",
        "关联标准项": refs,
        "适用范围": _clean_text(row.get("适用范围")) or _clean_text(row.get("模型适用范围")),
        "生效状态": status,
        "来源版本": _clean_text(row.get("来源版本")) or _clean_text(row.get("标准版本")) or "v1",
        "变更类型": "新增" if decision in {"通过", "修改后通过"} else "停用",
        "失效原因": "" if decision in {"通过", "修改后通过"} else _clean_text(row.get("错误原因")),
        "检索关键词": _clean_text(row.get("检索关键词")) or "\n".join(
            [part for part in [title, model_l1, model_l2] if part]
        ),
        "校验备注": _safe_join(
            [_clean_text(row.get("校验备注")), _clean_text(row.get("CZ复核备注"))],
            "；",
        ),
        "审核人": _clean_text(row.get("审核人")),
        "审核时间": review_time,
    }


def build_feedback_event(row: dict[str, Any]) -> dict[str, Any]:
    decision = _clean_text(row.get("CZ复核结论"))
    model_title = _clean_text(row.get("主标题")) or _clean_text(row.get("模型主标题"))
    cz_title = _clean_text(row.get("CZ主标题"))
    model_l1 = _clean_text(row.get("模型一级分类"))
    cz_l1 = _clean_text(row.get("CZ一级分类"))
    model_l2 = _clean_text(row.get("模型二级分类"))
    cz_l2 = _clean_text(row.get("CZ二级分类"))
    model_refs = _clean_text(row.get("关联标准项")) or _clean_text(row.get("模型关联标准"))
    cz_refs = _clean_text(row.get("CZ关联标准"))
    error_type = _clean_text(row.get("错误类型")) or _default_error_type(row, row)
    return {
        "数据ID": _clean_text(row.get("来源记录ID")) or _clean_text(row.get("数据ID")),
        "工单ID": _clean_text(row.get("工单ID")),
        "模型主标题": model_title,
        "CZ主标题": cz_title,
        "模型一级分类": model_l1,
        "CZ一级分类": cz_l1,
        "模型二级分类": model_l2,
        "CZ二级分类": cz_l2,
        "模型关联标准": model_refs,
        "CZ关联标准": cz_refs,
        "错误类型": error_type,
        "错误原因": _clean_text(row.get("错误原因")),
        "是否进入再训练样本": _clean_text(row.get("是否进入再训练样本")),
        "审核人": _clean_text(row.get("审核人")),
        "审核时间": _clean_text(row.get("审核时间")),
    }


def _review_decision_allowed(decision: str) -> bool:
    return decision in REVIEW_DECISIONS


def finalize_review_rows(review_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    published_rows: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []
    for row in review_rows:
        decision = _clean_text(row.get("CZ复核结论"))
        if not decision:
            continue
        if not _review_decision_allowed(decision):
            raise ValueError(f"Unsupported review decision: {decision}")
        review_time = _clean_text(row.get("审核时间")) or datetime.now().isoformat(timespec="seconds")
        normalized_row = dict(row)
        normalized_row["审核时间"] = review_time
        feedback_rows.append(build_feedback_event(normalized_row))
        if decision in {"通过", "修改后通过"}:
            published_rows.append(_row_to_published_record(normalized_row, review_time))
    return published_rows, feedback_rows


def _topic_final_master_row(row: dict[str, Any], review_time: str) -> dict[str, Any]:
    return {
        "主标题": _clean_text(row.get("主标题")),
        "副标题": _clean_text(row.get("副标题")),
        "知识内容": _clean_text(row.get("知识内容")),
        "知识分类": _clean_text(row.get("知识分类")),
        "知识来源": _clean_text(row.get("知识来源")) or "方向二主题候选",
        "关联标准项": _clean_text(row.get("关联标准项")),
        "适用范围": _clean_text(row.get("适用范围")),
        # This local review only prepares a submission. The cz website owns
        # the formal review/published lifecycle.
        "生效状态": "待审核",
        "来源版本": _clean_text(row.get("来源版本")) or _clean_text(row.get("主题标准版本")) or "待补充",
        "变更类型": _clean_text(row.get("变更类型")) or "新增",
        "失效原因": "",
        "检索关键词": _clean_text(row.get("检索关键词")),
        "校验备注": _safe_join(
            [
                _clean_text(row.get("校验备注")),
                _clean_text(row.get("审核备注")),
                f"主题ID：{_clean_text(row.get('主题ID'))}" if _clean_text(row.get("主题ID")) else "",
                f"本地审核人：{_clean_text(row.get('审核人'))}" if _clean_text(row.get("审核人")) else "",
                f"本地审核时间：{review_time}",
            ],
            "；",
        ),
    }


def build_topic_feedback_event(row: dict[str, Any], review_time: str) -> dict[str, Any]:
    return {
        "主题ID": _clean_text(row.get("主题ID")),
        "审核结论": _clean_text(row.get("审核结论")),
        "主题样本数": _clean_text(row.get("主题样本数")),
        "主题来源记录ID": _clean_text(row.get("主题来源记录ID")),
        "主题证据等级": _clean_text(row.get("主题证据等级")),
        "主题标准版本": _clean_text(row.get("主题标准版本")),
        "最终主标题": _clean_text(row.get("主标题")),
        "最终知识分类": _clean_text(row.get("知识分类")),
        "最终关联标准项": _clean_text(row.get("关联标准项")),
        "错误类型": _clean_text(row.get("错误类型")),
        "错误原因": _clean_text(row.get("错误原因")),
        "是否进入训练集": _clean_text(row.get("是否进入训练集")),
        "审核人": _clean_text(row.get("审核人")),
        "审核时间": review_time,
    }


def finalize_topic_review_rows(
    topic_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return final master candidates, audit feedback and optional SFT examples."""
    final_rows: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []
    training_rows: list[dict[str, Any]] = []
    for row in topic_rows:
        decision = _clean_text(row.get("审核结论"))
        if not decision:
            continue
        if not _review_decision_allowed(decision):
            raise ValueError(f"Unsupported topic review decision: {decision}")
        review_time = _clean_text(row.get("审核时间")) or datetime.now().isoformat(timespec="seconds")
        normalized_row = dict(row)
        normalized_row["审核时间"] = review_time
        feedback = build_topic_feedback_event(normalized_row, review_time)
        feedback_rows.append(feedback)
        if decision not in {"通过", "修改后通过"}:
            continue
        final_row = _topic_final_master_row(normalized_row, review_time)
        final_rows.append(final_row)
        if _clean_text(row.get("是否进入训练集")) in {"是", "yes", "true", "1"}:
            training_rows.append(
                {
                    "task": "topic_knowledge_generation",
                    "topic_id": _clean_text(row.get("主题ID")),
                    "input": {
                        "sample_count": _clean_text(row.get("主题样本数")),
                        "source_record_ids": _clean_text(row.get("主题来源记录ID")),
                        "evidence_level": _clean_text(row.get("主题证据等级")),
                        "evidence_summary": _clean_text(row.get("主题证据摘要")),
                        "retrieved_standards": _clean_text(row.get("主题检索标准Top5")),
                        "standard_versions": _clean_text(row.get("主题标准版本")),
                        "model_candidate": {
                            field: _clean_text(row.get(field))
                            for field in KNOWLEDGE_MASTER_COLUMNS
                        },
                    },
                    "target": final_row,
                    "review": feedback,
                }
            )
    return final_rows, feedback_rows, training_rows


def export_topic_review_results(
    topic_rows: list[dict[str, Any]],
    output_dir: str | Path,
) -> dict[str, Any]:
    final_rows, feedback_rows, training_rows = finalize_topic_review_rows(topic_rows)
    output_path = _ensure_output_dir(output_dir)
    final_workbook = output_path / "candidate_knowledge_for_submission.xlsx"
    feedback_jsonl = output_path / "topic_feedback.jsonl"
    training_jsonl = output_path / "topic_training_samples.jsonl"
    write_rows_to_workbook({"候选知识": (KNOWLEDGE_MASTER_COLUMNS, final_rows)}, final_workbook)
    _write_jsonl(feedback_rows, feedback_jsonl)
    _write_jsonl(training_rows, training_jsonl)
    return {
        "candidate_rows": len(final_rows),
        "feedback_rows": len(feedback_rows),
        "training_rows": len(training_rows),
        "candidate_file": str(final_workbook),
        "feedback_file": str(feedback_jsonl),
        "training_file": str(training_jsonl),
    }


def finalize_topic_review_workbook(
    review_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    _, topic_rows = read_workbook_rows(review_path, sheet_name="topic_review_queue")
    summary = export_topic_review_results(topic_rows, output_dir)
    summary["review_file"] = str(Path(review_path))
    summary_path = _ensure_output_dir(output_dir) / "topic_review_summary.json"
    summary["summary_file"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _ensure_output_dir(output_dir: str | Path) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def _summary_for_labeled_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    review_counts: dict[str, int] = {}
    for row in rows:
        status = _clean_text(row.get("流程状态"))
        status_counts[status] = status_counts.get(status, 0) + 1
        review = _clean_text(row.get("是否重点复核"))
        review_counts[review] = review_counts.get(review, 0) + 1
    return {
        "total_rows": len(rows),
        "status_counts": status_counts,
        "review_counts": review_counts,
    }


def _summary_for_preprocessed_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_counts: dict[str, int] = {}
    for row in rows:
        missing = _clean_text(row.get("缺失字段"))
        if not missing:
            missing_counts["无缺失"] = missing_counts.get("无缺失", 0) + 1
            continue
        for field in missing.splitlines():
            field = field.strip()
            if not field:
                continue
            missing_counts[field] = missing_counts.get(field, 0) + 1
    return {
        "total_rows": len(rows),
        "missing_field_counts": missing_counts,
    }


def _summary_for_final_rows(published_rows: list[dict[str, Any]], feedback_rows: list[dict[str, Any]]) -> dict[str, Any]:
    decision_counts: dict[str, int] = {}
    retrain_count = 0
    for row in feedback_rows:
        error_type = _clean_text(row.get("错误类型"))
        decision_counts[error_type] = decision_counts.get(error_type, 0) + 1
        if _clean_text(row.get("是否进入再训练样本")) in {"是", "yes", "true", "1"}:
            retrain_count += 1
    return {
        "published_rows": len(published_rows),
        "feedback_rows": len(feedback_rows),
        "retrain_rows": retrain_count,
        "error_type_counts": decision_counts,
    }


def _standard_refs_from_cell(value: Any) -> set[str]:
    refs: set[str] = set()
    for raw_line in re.split(r"[\n\r;；]+", _clean_text(value)):
        line = raw_line.strip()
        if not line:
            continue
        ref = line.split("|", 1)[0].strip()
        ref = re.sub(r"\s*\([^()]*\)\s*$", "", ref).strip()
        if ref:
            refs.add(ref)
    return refs


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def evaluate_review_rows(review_rows: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [
        row
        for row in review_rows
        if _clean_text(row.get("CZ复核结论")) in REVIEW_DECISIONS
    ]
    standard_gold = [row for row in reviewed if _standard_refs_from_cell(row.get("CZ关联标准"))]
    top5_hits = sum(
        bool(
            _standard_refs_from_cell(row.get("CZ关联标准"))
            & _standard_refs_from_cell(row.get("检索标准Top5"))
        )
        for row in standard_gold
    )
    model_ref_matches = sum(
        _standard_refs_from_cell(row.get("模型关联标准"))
        == _standard_refs_from_cell(row.get("CZ关联标准"))
        for row in standard_gold
    )

    l1_gold = [row for row in reviewed if _clean_text(row.get("CZ一级分类"))]
    l2_gold = [row for row in reviewed if _clean_text(row.get("CZ二级分类"))]
    title_gold = [row for row in reviewed if _clean_text(row.get("CZ主标题"))]
    l1_matches = sum(
        _clean_text(row.get("模型一级分类")) == _clean_text(row.get("CZ一级分类"))
        for row in l1_gold
    )
    l2_matches = sum(
        _clean_text(row.get("模型二级分类")) == _clean_text(row.get("CZ二级分类"))
        for row in l2_gold
    )
    title_modified = sum(
        _clean_text(row.get("模型主标题")) != _clean_text(row.get("CZ主标题"))
        for row in title_gold
    )
    rejected_or_bad_case = sum(
        _clean_text(row.get("CZ复核结论")) in {"驳回", "标记Bad Case"}
        for row in reviewed
    )
    standard_uncovered = sum(
        "标准未覆盖/标准召回不足" in _clean_text(row.get("错误类型"))
        for row in reviewed
    )
    priority_review = sum(_clean_text(row.get("是否重点复核")) == "是" for row in reviewed)

    return {
        "reviewed_rows": len(reviewed),
        "standard_top5_hit_rate": _rate(top5_hits, len(standard_gold)),
        "model_standard_reference_match_rate": _rate(model_ref_matches, len(standard_gold)),
        "category_l1_match_rate": _rate(l1_matches, len(l1_gold)),
        "category_l2_match_rate": _rate(l2_matches, len(l2_gold)),
        "title_modification_rate": _rate(title_modified, len(title_gold)),
        "rejected_or_bad_case_rate": _rate(rejected_or_bad_case, len(reviewed)),
        "standard_uncovered_rate": _rate(standard_uncovered, len(reviewed)),
        "priority_review_rate": _rate(priority_review, len(reviewed)),
    }


def evaluate_review_workbook(
    review_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    _, review_rows = read_workbook_rows(review_path, sheet_name="review_queue")
    report = evaluate_review_rows(review_rows)
    output_path = _ensure_output_dir(output_dir)
    report_path = output_path / "quality_report.json"
    report.update(
        {
            "review_file": str(Path(review_path)),
            "report_file": str(report_path),
        }
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _attach_guide_sheet() -> tuple[list[str], list[dict[str, Any]]]:
    rows = []
    rows.append({"说明": "review_queue.xlsx 是候选知识主表；原始方向二数据位于 preprocessed_queue，cz 在 CZ* 列完成复核。"})
    rows.append({"说明": "候选字段按知识库主表组织：主标题、副标题、知识内容、知识分类、关联标准项、适用范围、来源版本等。"})
    rows.append({"说明": f"流程状态允许值：{', '.join(FLOW_STATUSES)}"})
    rows.append({"说明": f"复核结论允许值：{', '.join(REVIEW_DECISIONS)}"})
    rows.append({"说明": f"错误类型允许值：{', '.join(ERROR_TYPES)}"})
    rows.append({"说明": "审核通过或修改后通过的数据才会进入 published 结果。"})
    rows.append({"说明": "请把 CZ 复核结果、错误原因和是否进入再训练样本填写完整。"})
    return ["说明"], rows


def write_review_workbook(
    preprocessed_rows: list[dict[str, Any]],
    labeled_rows: list[dict[str, Any]],
    excluded_rows: list[dict[str, Any]],
    workbook_path: str | Path,
) -> None:
    preprocessed_columns = SOURCE_COLUMNS + PREPROCESS_COLUMNS
    columns = CANDIDATE_COLUMNS + REVIEW_COLUMNS
    guide_columns, guide_rows = _attach_guide_sheet()
    write_rows_to_workbook(
        {
            "preprocessed_queue": (preprocessed_columns, preprocessed_rows),
            "review_queue": (columns, labeled_rows),
            "excluded_rows": (SOURCE_COLUMNS + ["排除原因"], excluded_rows),
            "guide": (guide_columns, guide_rows),
        },
        workbook_path,
    )


def _merge_unique_text(values: Iterable[Any], separator: str = "\n") -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        for part in text.splitlines():
            part = _clean_text(part)
            if part and part not in seen:
                seen.add(part)
                merged.append(part)
    return separator.join(merged)


def _merge_unique_keywords(values: Iterable[Any]) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        for part in text.split("|"):
            keyword = _clean_text(part)
            if keyword and keyword not in seen:
                seen.add(keyword)
                merged.append(keyword)
    return " | ".join(merged)


def _strip_source_id_notes(note: str) -> str:
    parts = [part.strip() for part in _clean_text(note).split("；") if _clean_text(part)]
    keep = [
        part
        for part in parts
        if not part.startswith(("来源记录ID：", "来源数据ID：", "来源记录：", "数据ID："))
    ]
    return "；".join(keep)


def _has_usable_image_evidence(row: dict[str, Any]) -> bool:
    status = _clean_text(row.get("图片处理状态"))
    match = re.search(r"可用:(\d+)", status)
    return bool(match and int(match.group(1)) > 0)


def _topic_evidence(row: dict[str, Any]) -> tuple[str, bool, str]:
    """Classify source evidence before allowing a record into a topic candidate."""
    evidence_level = _clean_text(row.get("证据等级"))
    if evidence_level == "完整会话" or _normalize_lines(row.get("聊天内容")):
        return "完整会话", True, "包含原始聊天上下文"
    if evidence_level == "图片证据" or _has_usable_image_evidence(row):
        return "图片证据", True, "无聊天内容，但存在可用现场图片"
    return "结构化摘要", False, "缺少原始聊天内容和可用图片，仅用于覆盖分析与主题线索"


def _topic_group_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        _clean_text(row.get("产品类型")),
        _clean_text(row.get("一级分类")),
        _clean_text(row.get("二级分类")),
        _clean_text(row.get("主标准路径")),
        _clean_text(row.get("问题意图")),
        _clean_text(row.get("对象/部位")),
        _clean_text(row.get("异常现象")),
        _clean_text(row.get("解题方式")),
    )


def _topic_semantic_text(row: dict[str, Any]) -> str:
    fields = (
        ("核心问题", row.get("核心问题")),
        ("聊天内容", row.get("聊天内容")),
        ("判定结论", row.get("判定结论")),
        ("判定依据", row.get("判定依据")),
        ("问题意图", row.get("问题意图")),
        ("对象/部位", row.get("对象/部位")),
        ("异常现象", row.get("异常现象")),
        ("解题方式", row.get("解题方式")),
        ("一级分类", row.get("一级分类")),
        ("二级分类", row.get("二级分类")),
        ("主标准路径", row.get("主标准路径")),
    )
    return "\n".join(f"{label}：{_clean_text(value)}" for label, value in fields if _clean_text(value))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _semantic_topic_groups(
    rows: list[dict[str, Any]],
    embedding_client: EmbeddingClient,
    threshold: float,
) -> tuple[list[tuple[tuple[str, ...], list[dict[str, Any]]]], dict[str, Any]]:
    texts = [_topic_semantic_text(row) for row in rows]
    vectors = embedding_client.embed_texts(texts)
    if len(vectors) != len(rows):
        raise EmbeddingError("Embedding vector count does not match topic row count")

    grouped: list[dict[str, Any]] = []
    for row, vector in zip(rows, vectors):
        product_type = _clean_text(row.get("产品类型"))
        best_index = -1
        best_score = -1.0
        for index, cluster in enumerate(grouped):
            if cluster["product_type"] != product_type:
                continue
            score = _cosine_similarity(vector, cluster["centroid"])
            if score > best_score:
                best_index = index
                best_score = score
        if best_index >= 0 and best_score >= threshold:
            cluster = grouped[best_index]
            cluster["rows"].append(row)
            count = len(cluster["rows"])
            cluster["centroid"] = [
                (old * (count - 1) + new) / count
                for old, new in zip(cluster["centroid"], vector)
            ]
            cluster["min_similarity"] = min(cluster["min_similarity"], best_score)
        else:
            grouped.append(
                {
                    "product_type": product_type,
                    "rows": [row],
                    "centroid": vector,
                    "min_similarity": 1.0,
                }
            )

    result: list[tuple[tuple[str, ...], list[dict[str, Any]]]] = []
    min_similarity_values: list[float] = []
    for index, cluster in enumerate(grouped, start=1):
        cluster_rows = cluster["rows"]
        source_ids = sorted(
            {
                _clean_text(row.get("数据ID"))
                or _clean_text(row.get("来源记录ID"))
                or _clean_text(row.get("工单ID"))
                for row in cluster_rows
            }
            - {""}
        )
        key = (
            "semantic",
            cluster["product_type"],
            f"cluster-{index}",
            *source_ids,
        )
        result.append((key, cluster_rows))
        min_similarity_values.append(round(float(cluster["min_similarity"]), 4))
    return result, {
        "provider": "embedding",
        "model": embedding_client.config.model,
        "threshold": threshold,
        "cluster_count": len(result),
        "min_similarity": min(min_similarity_values) if min_similarity_values else None,
    }


def _topic_id(key: tuple[str, ...]) -> str:
    payload = "|".join(key).encode("utf-8")
    return f"TOP-{hashlib.sha1(payload).hexdigest()[:10].upper()}"


def _topic_confidence(rows: list[dict[str, Any]]) -> float:
    evidence_count = sum(1 for row in rows if _topic_evidence(row)[1])
    return round(min(0.85, 0.4 + evidence_count * 0.12), 3)


def _topic_evidence_summary(rows: list[dict[str, Any]]) -> str:
    parts = []
    for row in rows:
        evidence_level, _eligible, reason = _topic_evidence(row)
        source_id = _clean_text(row.get("来源记录ID")) or _clean_text(row.get("数据ID"))
        image_summary = _clean_text(row.get("图片证据摘要"))
        feature_summary = _safe_join(
            [
                _clean_text(row.get("问题意图")),
                _clean_text(row.get("对象/部位")),
                _clean_text(row.get("异常现象")),
                _clean_text(row.get("解题方式")),
            ],
            " / ",
        )
        detail = _safe_join(
            [
                source_id,
                evidence_level,
                reason,
                image_summary[:240] if image_summary else "",
                feature_summary[:240] if feature_summary else "",
            ],
            " | ",
        )
        if detail:
            parts.append(detail)
    return _merge_unique_text(parts, separator="\n")[:4000]


def _topic_image_links(rows: list[dict[str, Any]]) -> list[str]:
    links: list[str] = []
    for row in rows:
        links.extend(split_image_urls(_clean_text(row.get("图片链接"))))
    return list(dict.fromkeys(link for link in links if link))[:4]


def _topic_needs_images(rows: list[dict[str, Any]], candidate: dict[str, Any] | None = None) -> bool:
    if candidate and "requires_images" in candidate:
        return bool(candidate.get("requires_images"))
    text = " ".join(
        _clean_text(row.get(field))
        for row in rows
        for field in ("核心问题", "判定依据", "异常现象", "问题意图", "解题方式")
    )
    markers = ("图片", "照片", "外观", "显示", "坏点", "磕点", "划痕", "拆修", "胶", "颜色", "裂")
    return bool(_topic_image_links(rows) and any(marker in text for marker in markers))


def _topic_query(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = rows[0]
    questions = [_clean_text(row.get("核心问题")) for row in rows[:5]]
    return {
        "产品类型": _clean_text(base.get("产品类型")),
        "一级分类": _clean_text(base.get("一级分类")),
        "二级分类": _clean_text(base.get("二级分类")),
        "核心问题": "；".join(question for question in questions if question),
        "判定依据": _merge_unique_text([row.get("判定依据") for row in rows[:5]], separator="；"),
        "问题意图": _clean_text(base.get("问题意图")),
        "对象/部位": _clean_text(base.get("对象/部位")),
        "异常现象": _clean_text(base.get("异常现象")),
        "解题方式": _clean_text(base.get("解题方式")),
        "标准关键词": _merge_unique_keywords([row.get("标准关键词") for row in rows]),
    }


def _topic_rule_draft(
    topic_id: str,
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
) -> dict[str, Any]:
    query = _topic_query(rows)
    standard = matches[0][0] if matches else None
    text = " ".join(
        [
            _clean_text(query.get("核心问题")),
            _clean_text(query.get("判定依据")),
            _clean_text(query.get("异常现象")),
        ]
    )
    concrete_allowed = bool(matches) and _has_explicit_boundary_case(text) and not any(
        marker in text for marker in UNCERTAINTY_MARKERS
    )
    knowledge_form = "具体判定" if concrete_allowed else "流程方法"
    title = (
        _guess_title(_clean_text(query.get("核心问题")), standard)
        if concrete_allowed
        else _process_title(
            _clean_text(query.get("核心问题")),
            _clean_text(query.get("一级分类")),
            _clean_text(query.get("二级分类")),
            standard,
        )
    )
    content = (
        _build_model_content(
            _clean_text(query.get("核心问题")),
            "",
            _clean_text(query.get("判定依据")),
            "",
            standard,
        )
        if concrete_allowed
        else _build_process_content(
            _clean_text(query.get("核心问题")),
            "",
            _clean_text(query.get("判定依据")),
            _clean_text(query.get("一级分类")),
            _clean_text(query.get("二级分类")),
            standard,
        )
    )
    return {
        "title": title,
        "subtitles": [],
        "content": content,
        "category_l1": _clean_text(query.get("一级分类")),
        "category_l2": _clean_text(query.get("二级分类")),
        "layer": "L2",
        "knowledge_form": knowledge_form,
        "standard_refs": [_standard_reference(item) for item, _score in matches[:1]],
        "applicable_scope": _safe_join(
            [_clean_text(query.get("产品类型")), standard.scope if standard else ""], "；"
        ),
        "confidence": 0.72 if concrete_allowed else 0.45,
        "reasoning_summary": (
            "主题仅聚合明确边界问题，且命中可信标准。"
            if concrete_allowed
            else "按主题证据沉淀通用核验流程；不将单个工单的个案结论外推。"
        ),
        "needs_human_review": not concrete_allowed,
        "image_evidence_summary": _topic_evidence_summary(rows),
        "requires_images": _topic_needs_images(rows),
        "image_usage_instruction": (
            "保留现场图片，辅助说明需要核验的部位、现象和标准边界。"
            if _topic_needs_images(rows)
            else "文字已足以表达规则，不需要保留图片。"
        ),
        "topic_id": topic_id,
    }


def _topic_requires_process(
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
    candidate: dict[str, Any],
) -> bool:
    text = " ".join(
        _clean_text(row.get(field))
        for row in rows
        for field in ("核心问题", "判定结论", "判定依据", "聊天内容", "异常现象", "解题方式")
    )
    return (
        not matches
        or any(marker in text for marker in UNCERTAINTY_MARKERS)
        or not _has_explicit_boundary_case(text)
        or (
            candidate.get("knowledge_form") == "具体判定"
            and not candidate.get("standard_refs")
        )
    )


def _topic_candidate_row(
    topic_id: str,
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
    candidate: dict[str, Any],
    provider: str,
    model_name: str,
    prompt_version: str,
    model_run_id: str,
    model_error: str,
    stage_status: str,
    min_confidence: float,
) -> dict[str, Any]:
    query = _topic_query(rows)
    if _topic_requires_process(rows, matches, candidate):
        candidate = _topic_rule_draft(topic_id, rows, matches)
        candidate["needs_human_review"] = True
        candidate["confidence"] = min(float(candidate["confidence"]), 0.45)
        model_error = _safe_join([model_error, "主题缺少可外推的明确边界证据或可信标准，已强制降级为流程方法。"], "；")
    standard_refs = _format_model_refs(candidate.get("standard_refs", []), matches)
    standard = matches[0][0] if matches else None
    source_ids = list(
        dict.fromkeys(
            _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
            for row in rows
            if _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
        )
    )
    work_order_ids = list(dict.fromkeys(_clean_text(row.get("工单ID")) for row in rows if _clean_text(row.get("工单ID"))))
    image_links = _topic_image_links(rows)
    requires_images = _topic_needs_images(rows, candidate)
    image_note = _clean_text(candidate.get("image_usage_instruction")) or (
        "保留现场图片，作为部位、现象和标准边界的辅助说明。"
        if requires_images else "文字已足以说明处理方法，不需要保留图片。"
    )
    levels = list(dict.fromkeys(_topic_evidence(row)[0] for row in rows))
    confidence = float(candidate.get("confidence", 0.0))
    needs_review = (
        candidate.get("needs_human_review", False)
        or not matches
        or not candidate.get("standard_refs")
        or confidence < min_confidence
        or candidate.get("knowledge_form") != "具体判定"
    )
    return {
        "主题ID": topic_id,
        "主题状态": "review_pending",
        "主题样本数": len(rows),
        "主题来源记录ID": "\n".join(source_ids),
        "主题工单ID": "\n".join(work_order_ids),
        "主题聚类键": " | ".join(key),
        "主题问题意图": _clean_text(query.get("问题意图")),
        "主题对象/部位": _clean_text(query.get("对象/部位")),
        "主题异常现象": _clean_text(query.get("异常现象")),
        "主题解题方式": _clean_text(query.get("解题方式")),
        "主题证据等级": "、".join(levels),
        "主题证据摘要": _topic_evidence_summary(rows),
        "主题图片链接": "\n".join(image_links) if requires_images else "",
        "主题图片必要性": "需要保留" if requires_images else "不需要保留",
        "主题图片说明": image_note,
        "主题检索标准Top5": _format_retrieved_standards(matches) if matches else "未搜索到相关知识（待人工补充）",
        "主题标准版本": "\n".join(
            f"{_standard_reference(item)}:{item.version}" for item, _score in matches if _standard_reference(item)
        ),
        "主题置信度": round(confidence, 3),
        "是否重点复核": "是" if needs_review else "否",
        "主题模型提供方": provider,
        "主题模型名称": model_name,
        "主题Prompt版本": prompt_version,
        "主题模型运行ID": model_run_id,
        "主标题": _clean_text(candidate.get("title")),
        "副标题": "\n".join(candidate.get("subtitles", [])),
        "知识内容": _clean_text(candidate.get("content")),
        "知识分类": "检测方法" if candidate.get("knowledge_form") == "流程方法" else "场景判定",
        "知识来源": "方向二主题候选",
        "关联标准项": standard_refs,
        "适用范围": _clean_text(candidate.get("applicable_scope")) or _safe_join(
            [_clean_text(query.get("产品类型")), standard.scope if standard else ""], "；"
        ),
        "生效状态": "待审核",
        "来源版本": standard.version if standard and standard.version else "待补充",
        "变更类型": "新增",
        "失效原因": "",
        "检索关键词": _merge_unique_keywords(
            [
                _clean_text(query.get("问题意图")),
                _clean_text(query.get("对象/部位")),
                _clean_text(query.get("异常现象")),
                _clean_text(query.get("标准关键词")),
            ]
        ),
        "校验备注": _safe_join(
            [
                f"主题聚合样本数：{len(rows)}",
                f"主题知识形态：{candidate.get('knowledge_form', '流程方法')}",
                _clean_text(candidate.get("reasoning_summary")),
                "无可信标准，待人工补充。" if not matches else "",
                model_error,
            ],
            "；",
        ),
        **{field: "" for field in TOPIC_MODEL_INITIAL_REVIEW_COLUMNS},
        **{field: "" for field in TOPIC_REVIEW_COLUMNS},
        "流程状态": "review_pending",
        "模型阶段状态": stage_status,
    }


def _rule_topic_initial_review(
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
) -> dict[str, Any]:
    title = _clean_text(topic.get("主标题"))
    content = _clean_text(topic.get("知识内容"))
    refs = _clean_text(topic.get("关联标准项"))
    if "/" in title or "／" in title or title.count("、") >= 2:
        return {
            "decision": "需修改",
            "error_type": "标题不准",
            "reason": "主标题包含关键词堆砌或斜杠串词，不适合作为可直接使用的知识标题。",
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "需修改",
            "confidence": 0.92,
            "priority_review": True,
        }
    if not title or not content:
        return {
            "decision": "需修改",
            "error_type": "标题不准" if not title else "话术不合适",
            "reason": "转写草稿缺少可审核的标题或知识正文。",
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "不足",
            "content_consistency": "不一致",
            "title_quality": "需修改" if not title else "清晰",
            "confidence": 0.94,
            "priority_review": True,
        }
    if not matches:
        return {
            "decision": "证据不足待补充",
            "error_type": "标准未覆盖/标准召回不足",
            "reason": "未检索到可信生效标准，不能由模型初标放行。",
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.9,
            "priority_review": True,
        }
    if not refs:
        return {
            "decision": "需修改",
            "error_type": "标准项映射错",
            "reason": "已命中生效标准，但转写草稿未保留可追溯的标准引用。",
            "standard_consistency": "不一致",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.86,
            "priority_review": True,
        }
    if _clean_text(topic.get("主题图片必要性")) == "需要保留" and not _clean_text(topic.get("主题图片链接")):
        return {
            "decision": "证据不足待补充",
            "error_type": "图片判断失误",
            "reason": "草稿内容依赖视觉差异，但候选中没有保留可用图片。",
            "standard_consistency": "一致",
            "evidence_sufficiency": "不足",
            "content_consistency": "部分一致",
            "image_necessity": "图片不足",
            "title_quality": "清晰",
            "confidence": 0.91,
            "priority_review": True,
        }
    if len(content) < 60:
        return {
            "decision": "需修改",
            "error_type": "话术不合适",
            "reason": "知识正文过短，未能完整表达处理规则、步骤或限制条件。",
            "standard_consistency": "一致",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.84,
            "priority_review": True,
        }
    return {
        "decision": "通过",
        "error_type": "",
        "reason": "转写草稿具备主题级标题、正文、分类和可追溯标准引用，可进入人工复标。",
        "standard_consistency": "一致",
        "evidence_sufficiency": "充分" if _clean_text(topic.get("主题证据等级")) == "完整会话" else "部分充分",
        "confidence": 0.76,
        "priority_review": _clean_text(topic.get("是否重点复核")) == "是",
    }


def _apply_topic_initial_review_guard(
    review: dict[str, Any],
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
) -> dict[str, Any]:
    guarded = dict(review)
    if not matches:
        guarded.update(
            {
                "decision": "证据不足待补充",
                "error_type": "标准未覆盖/标准召回不足",
                "reason": _safe_join(
                    [_clean_text(guarded.get("reason")), "未检索到可信生效标准，模型初标不得标记通过。"],
                    "；",
                ),
                "standard_consistency": "无可信标准",
                "evidence_sufficiency": "不足",
                "priority_review": True,
            }
        )
    elif not _clean_text(topic.get("关联标准项")) and guarded.get("decision") == "通过":
        guarded.update(
            {
                "decision": "需修改",
                "error_type": "标准项映射错",
                "reason": _safe_join(
                    [_clean_text(guarded.get("reason")), "转写草稿缺少可追溯标准引用，不能通过初标。"],
                    "；",
                ),
                "standard_consistency": "不一致",
                "priority_review": True,
            }
        )
    return guarded


def _attach_topic_initial_review(
    topic: dict[str, Any],
    review: dict[str, Any],
    provider: str,
    model_name: str,
    prompt_version: str,
    model_run_id: str,
    status: str,
) -> None:
    topic.update(
        {
            "模型初标结论": _clean_text(review.get("decision")),
            "模型初标错误类型": _clean_text(review.get("error_type")),
            "模型初标原因": _clean_text(review.get("reason")),
            "模型初标标准一致性": _clean_text(review.get("standard_consistency")),
            "模型初标证据充分性": _clean_text(review.get("evidence_sufficiency")),
            "模型初标内容一致性": _clean_text(review.get("content_consistency"))
            or ("一致" if _clean_text(review.get("decision")) == "通过" else "部分一致"),
            "模型初标图片必要性": _clean_text(review.get("image_necessity"))
            or ("需要保留" if _clean_text(topic.get("主题图片必要性")) == "需要保留" else "不需要"),
            "模型初标标题质量": _clean_text(review.get("title_quality"))
            or ("清晰" if _clean_text(topic.get("主标题")) else "需修改"),
            "模型初标置信度": review.get("confidence", ""),
            "模型初标重点复核": "是" if review.get("priority_review") else "否",
            "模型初标提供方": provider,
            "模型初标模型名称": model_name,
            "模型初标Prompt版本": prompt_version,
            "模型初标运行ID": model_run_id,
            "模型初标状态": status,
        }
    )


def build_topic_review_rows(
    feature_rows: list[dict[str, Any]],
    standard_catalog: list[StandardCatalogItem] | None = None,
    min_confidence: float = 0.75,
    use_mimo: bool = True,
    mimo_client: MimoClient | None = None,
    audit_store: AuditStore | None = None,
    run_id: str | None = None,
    clustering_mode: str = "semantic",
    semantic_threshold: float = 0.84,
    embedding_client: EmbeddingClient | None = None,
    clustering_meta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Cluster record features, then generate one auditable draft per topic."""
    evidence_gap_rows: list[dict[str, Any]] = []
    eligible_topic_rows: list[dict[str, Any]] = []

    for row in feature_rows:
        evidence_level, eligible, reason = _topic_evidence(row)
        if not eligible:
            gap_row = dict(row)
            gap_row["证据缺口原因"] = reason
            gap_row["证据等级"] = evidence_level
            evidence_gap_rows.append(gap_row)
            continue
        eligible_topic_rows.append(row)

    normalized_mode = clustering_mode.strip().lower()
    if normalized_mode not in {"semantic", "rule"}:
        raise ValueError(f"Unsupported clustering mode: {clustering_mode}")
    threshold = max(0.0, min(float(semantic_threshold), 1.0))
    meta: dict[str, Any] = {
        "requested_mode": normalized_mode,
        "effective_mode": normalized_mode,
        "provider": "rule",
        "model": "",
        "threshold": threshold,
        "cluster_count": 0,
        "error": "",
    }
    topic_groups: list[tuple[tuple[str, ...], list[dict[str, Any]]]]
    if normalized_mode == "semantic" and eligible_topic_rows:
        semantic_client = embedding_client or EmbeddingClient.from_env()
        try:
            if semantic_client is None:
                raise EmbeddingError("EMBEDDING_BASE_URL or EMBEDDING_MODEL is not configured")
            topic_groups, semantic_meta = _semantic_topic_groups(
                eligible_topic_rows,
                semantic_client,
                threshold,
            )
            meta.update(semantic_meta)
        except EmbeddingError as exc:
            meta.update(
                {
                    "effective_mode": "rule",
                    "provider": "rule-fallback",
                    "error": str(exc),
                }
            )
            grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for row in eligible_topic_rows:
                grouped.setdefault(_topic_group_key(row), []).append(row)
            topic_groups = list(grouped.items())
    else:
        grouped = {}
        for row in eligible_topic_rows:
            grouped.setdefault(_topic_group_key(row), []).append(row)
        topic_groups = list(grouped.items())
        meta["cluster_count"] = len(topic_groups)
    if clustering_meta is not None:
        clustering_meta.clear()
        clustering_meta.update(meta)

    topic_rows: list[dict[str, Any]] = []
    source_mapping_rows: list[dict[str, Any]] = []
    pending_cluster_rows: list[dict[str, Any]] = []
    catalog = standard_catalog or []
    client = mimo_client if use_mimo else None
    if client is None and use_mimo:
        client = MimoClient.from_env()

    for key, rows in topic_groups:
        if len(rows) < 2:
            for row in rows:
                pending_row = dict(row)
                pending_row["待聚合原因"] = "同主题有效证据记录少于 2 条，暂不进入人工审核。"
                pending_row["主题聚类键"] = " | ".join(key)
                pending_cluster_rows.append(pending_row)
            continue
        topic_id = _topic_id(key)
        query = _topic_query(rows)
        matches = retrieve_standard_matches(query, catalog, top_k=5)
        fallback = _topic_rule_draft(topic_id, rows, matches)
        candidate = fallback
        provider = "topic-rule"
        model_name = "topic-rule-v1"
        prompt_version = ""
        stage_status = "topic_rule_labeled"
        model_error = ""
        model_run_id = uuid.uuid4().hex
        request_audit: dict[str, Any] = {
            "topic_id": topic_id,
            "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
            "topic_features": query,
            "evidence_summary": _topic_evidence_summary(rows),
            "retrieved_standards": _retrieved_standard_rows(matches),
        }
        response_audit: dict[str, Any] = {}
        if client and hasattr(client, "label_topic"):
            provider = "mimo"
            model_name = client.config.model
            prompt_version = PROMPT_VERSION
            try:
                result = client.label_topic(
                    {
                        "topic_id": topic_id,
                        "sample_count": len(rows),
                        "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
                        "features": query,
                        "evidence_summary": _topic_evidence_summary(rows),
                    },
                    matches,
                )
                candidate = result.candidate
                request_audit = result.request_audit
                response_audit = result.response_audit
                stage_status = "topic_model_labeled"
            except MimoError as exc:
                stage_status = "topic_model_failed"
                model_error = str(exc)
        else:
            model_error = "未配置 MiMo，使用主题级规则草稿。"

        topic = _topic_candidate_row(
            topic_id, key, rows, matches, candidate, provider, model_name, prompt_version, model_run_id,
            model_error, stage_status, min_confidence,
        )
        initial_review = _rule_topic_initial_review(topic, matches)
        initial_review_provider = "review-rule"
        initial_review_model = "topic-review-rule-v1"
        initial_review_prompt = ""
        initial_review_status = "topic_initial_reviewed_rule"
        initial_review_error = ""
        initial_review_run_id = uuid.uuid4().hex
        review_query = {
            **query,
            "核心问题": "；".join(
                [query.get("核心问题", ""), topic.get("主标题", ""), topic.get("知识内容", "")]
            ),
            "判定依据": f"{query.get('判定依据', '')}；{topic.get('关联标准项', '')}",
        }
        review_matches = retrieve_standard_matches(review_query, catalog, top_k=5)
        topic["主题初标复核标准Top5"] = (
            _format_retrieved_standards(review_matches)
            if review_matches else "未搜索到相关知识（待人工补充）"
        )
        review_request_audit: dict[str, Any] = {
            "topic_id": topic_id,
            "transcription_model_run_id": model_run_id,
            "draft": {field: _clean_text(topic.get(field)) for field in KNOWLEDGE_MASTER_COLUMNS},
            "transcription_retrieved_standards": _retrieved_standard_rows(matches),
            "review_retrieved_standards": _retrieved_standard_rows(review_matches),
        }
        review_response_audit: dict[str, Any] = {}
        if client and hasattr(client, "review_topic"):
            initial_review_provider = "mimo"
            initial_review_model = client.config.model
            initial_review_prompt = "phone-topic-initial-review-v1"
            try:
                review_topic = client.review_topic
                review_args = (
                    {
                        "topic_id": topic_id,
                        "sample_count": len(rows),
                        "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
                        "features": query,
                        "evidence_summary": _topic_evidence_summary(rows),
                    },
                    {field: _clean_text(topic.get(field)) for field in KNOWLEDGE_MASTER_COLUMNS},
                    review_matches,
                )
                if "transcription_matches" in inspect.signature(review_topic).parameters:
                    review_result = review_topic(
                        *review_args,
                        transcription_matches=matches,
                    )
                else:
                    review_result = review_topic(*review_args)
                initial_review = review_result.candidate
                review_request_audit = review_result.request_audit
                review_response_audit = review_result.response_audit
                initial_review_status = "topic_initial_reviewed_model"
            except MimoError as exc:
                initial_review_status = "topic_initial_review_failed"
                initial_review_error = str(exc)
        else:
            initial_review_error = "未配置支持主题初标的 MiMo，使用规则模型初标。"
        initial_review = _apply_topic_initial_review_guard(initial_review, topic, review_matches)
        _attach_topic_initial_review(
            topic,
            initial_review,
            initial_review_provider,
            initial_review_model,
            initial_review_prompt,
            initial_review_run_id,
            initial_review_status,
        )
        topic_rows.append(topic)
        if audit_store:
            audit_store.record_model_run(
                model_run_id=model_run_id,
                run_id=run_id or "",
                record_id=topic_id,
                provider=provider,
                model_name=model_name,
                prompt_version=prompt_version,
                status=stage_status,
                retrieved_standards=_retrieved_standard_rows(matches),
                request_audit=request_audit,
                response_audit=response_audit,
                error=model_error,
            )
            audit_store.save_candidate(model_run_id, run_id or "", topic_id, topic)
            audit_store.record_model_run(
                model_run_id=initial_review_run_id,
                run_id=run_id or "",
                record_id=topic_id,
                provider=initial_review_provider,
                model_name=initial_review_model,
                prompt_version=initial_review_prompt,
                status=initial_review_status,
                retrieved_standards=_retrieved_standard_rows(matches),
                request_audit=review_request_audit,
                response_audit=review_response_audit,
                error=initial_review_error,
            )
            audit_store.save_candidate(initial_review_run_id, run_id or "", topic_id, topic)

        for row in rows:
            evidence_level, _eligible, reason = _topic_evidence(row)
            source_mapping_rows.append(
                {
                    "主题ID": topic_id,
                    "来源记录ID": _clean_text(row.get("数据ID")),
                    "工单ID": _clean_text(row.get("工单ID")),
                    "核心问题": _clean_text(row.get("核心问题")),
                    "聊天内容": _clean_text(row.get("聊天内容")),
                    "产品类型": _clean_text(row.get("产品类型")),
                    "一级分类": _clean_text(row.get("一级分类")),
                    "二级分类": _clean_text(row.get("二级分类")),
                    "主标准路径": _clean_text(row.get("主标准路径")),
                    "证据等级": evidence_level,
                    "纳入主题原因": reason,
                    "问题意图": _clean_text(row.get("问题意图")),
                    "对象/部位": _clean_text(row.get("对象/部位")),
                    "异常现象": _clean_text(row.get("异常现象")),
                    "解题方式": _clean_text(row.get("解题方式")),
                    "主标准路径": _clean_text(row.get("主标准路径")),
                    "关联标准项": _clean_text(topic.get("关联标准项")),
                    "模型运行ID": model_run_id,
                }
            )
    return topic_rows, source_mapping_rows, evidence_gap_rows, pending_cluster_rows


def _topic_guide_sheet() -> tuple[list[str], list[dict[str, Any]]]:
    rows = [
        {"说明": "topic_review_queue 是主题级候选，不是一工单一条知识。"},
        {"说明": "仅完整会话或有可用现场图片的记录可形成主题候选；结构化摘要记录进入 evidence_gap_rows，单条主题进入 pending_cluster_rows。"},
        {"说明": "topic_model_drafts 保存主题级转写草稿；模型初标只审核该草稿，不修改 13 列内容。"},
        {"说明": "审核人直接编辑 13 列候选草稿；审核字段只记录人工复标结论、错误说明、训练标记和审核信息。"},
        {"说明": "审核结论为通过或修改后通过且标记进入训练集的主题，会导出为训练反馈样本。"},
        {"说明": "最终 13 列候选仍需提交至 cz 知识库网站进入正式审核和发布。"},
    ]
    return ["说明"], rows


def write_topic_review_workbook(
    preprocessed_rows: list[dict[str, Any]],
    feature_rows: list[dict[str, Any]],
    excluded_rows: list[dict[str, Any]],
    workbook_path: str | Path,
    standard_catalog: list[StandardCatalogItem] | None = None,
    min_confidence: float = 0.75,
    use_mimo: bool = True,
    mimo_client: MimoClient | None = None,
    audit_store: AuditStore | None = None,
    run_id: str | None = None,
    clustering_mode: str = "semantic",
    semantic_threshold: float = 0.84,
    embedding_client: EmbeddingClient | None = None,
) -> dict[str, Any]:
    clustering_meta: dict[str, Any] = {}
    topic_rows, mapping_rows, evidence_gap_rows, pending_cluster_rows = build_topic_review_rows(
        feature_rows,
        standard_catalog=standard_catalog,
        min_confidence=min_confidence,
        use_mimo=use_mimo,
        mimo_client=mimo_client,
        audit_store=audit_store,
        run_id=run_id,
        clustering_mode=clustering_mode,
        semantic_threshold=semantic_threshold,
        embedding_client=embedding_client,
        clustering_meta=clustering_meta,
    )
    model_draft_rows = [
        {
            "主题ID": _clean_text(topic.get("主题ID")),
            "转写提供方": _clean_text(topic.get("主题模型提供方")),
            "转写模型名称": _clean_text(topic.get("主题模型名称")),
            "转写Prompt版本": _clean_text(topic.get("主题Prompt版本")),
            "转写模型运行ID": _clean_text(topic.get("主题模型运行ID")),
            "转写置信度": _clean_text(topic.get("主题置信度")),
            "转写是否重点复核": _clean_text(topic.get("是否重点复核")),
            **{field: _clean_text(topic.get(field)) for field in KNOWLEDGE_MASTER_COLUMNS},
        }
        for topic in topic_rows
    ]
    guide_columns, guide_rows = _topic_guide_sheet()
    write_rows_to_workbook(
        {
            "topic_review_queue": (TOPIC_CANDIDATE_COLUMNS + TOPIC_REVIEW_COLUMNS, topic_rows),
            "topic_source_mapping": (TOPIC_SOURCE_MAPPING_COLUMNS, mapping_rows),
            "topic_model_drafts": (TOPIC_MODEL_DRAFT_COLUMNS, model_draft_rows),
            "evidence_gap_rows": (
                SOURCE_COLUMNS + PREPROCESS_COLUMNS + TOPIC_FEATURE_COLUMNS + ["证据缺口原因"],
                evidence_gap_rows,
            ),
            "pending_cluster_rows": (
                SOURCE_COLUMNS + PREPROCESS_COLUMNS + TOPIC_FEATURE_COLUMNS + ["主题聚类键", "待聚合原因"],
                pending_cluster_rows,
            ),
            "excluded_rows": (SOURCE_COLUMNS + ["排除原因"], excluded_rows),
            "guide": (guide_columns, guide_rows),
        },
        workbook_path,
    )
    return {
        "topic_rows": len(topic_rows),
        "topic_source_rows": len(mapping_rows),
        "evidence_gap_rows": len(evidence_gap_rows),
        "pending_cluster_rows": len(pending_cluster_rows),
        "clustering_requested_mode": clustering_meta.get("requested_mode", clustering_mode),
        "clustering_effective_mode": clustering_meta.get("effective_mode", clustering_mode),
        "clustering_provider": clustering_meta.get("provider", ""),
        "clustering_model": clustering_meta.get("model", ""),
        "clustering_threshold": clustering_meta.get("threshold", semantic_threshold),
        "clustering_error": clustering_meta.get("error", ""),
    }


def build_candidate_knowledge_rows(labeled_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, str, str]] = []
    for row in labeled_rows:
        key = (
            _clean_text(row.get("主标题")),
            _clean_text(row.get("知识分类")),
            _clean_text(row.get("知识来源")),
            _clean_text(row.get("关联标准项")),
        )
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    candidate_rows: list[dict[str, Any]] = []
    for key in order:
        rows = grouped[key]
        base = dict(rows[0])
        source_ids = [
            _clean_text(row.get("来源记录ID")) or _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
            for row in rows
        ]
        unique_source_ids = [item for item in dict.fromkeys(source_ids) if item]
        notes = [
            _strip_source_id_notes(row.get("校验备注"))
            for row in rows
            if _strip_source_id_notes(row.get("校验备注"))
        ]
        note_parts = [f"主题聚合样本数：{len(rows)}"]
        if unique_source_ids:
            sample_ids = "、".join(unique_source_ids[:5])
            if len(unique_source_ids) > 5:
                sample_ids = f"{sample_ids} 等"
            note_parts.append(f"来源记录ID：{sample_ids}")
        merged_note = _merge_unique_text(notes, separator="；")
        if merged_note:
            note_parts.append(merged_note)
        base["副标题"] = _merge_unique_text([row.get("副标题") for row in rows]) or base.get("副标题", "")
        base["检索关键词"] = _merge_unique_keywords([row.get("检索关键词") for row in rows]) or base.get("检索关键词", "")
        base["校验备注"] = "；".join(part for part in note_parts if part)
        candidate_rows.append({column: _clean_text(base.get(column)) for column in KNOWLEDGE_MASTER_COLUMNS})
    return candidate_rows


def write_candidate_knowledge_workbook(
    labeled_rows: list[dict[str, Any]],
    workbook_path: str | Path,
) -> None:
    """Export the candidate deliverable in the same shape as cz's knowledge master."""
    candidate_rows = build_candidate_knowledge_rows(labeled_rows)
    write_rows_to_workbook(
        {
            "候选知识": (
                KNOWLEDGE_MASTER_COLUMNS,
                candidate_rows,
            )
        },
        workbook_path,
    )


def write_topic_candidate_knowledge_workbook(
    topic_rows: list[dict[str, Any]],
    workbook_path: str | Path,
) -> None:
    """Export unreviewed topic candidates in the shared 13-column contract."""
    write_rows_to_workbook(
        {
            "候选知识": (
                KNOWLEDGE_MASTER_COLUMNS,
                [
                    {column: _clean_text(row.get(column)) for column in KNOWLEDGE_MASTER_COLUMNS}
                    for row in topic_rows
                ],
            )
        },
        workbook_path,
    )


def initial_label_from_workbook(
    source_path: str | Path,
    standards_path: str | Path | None,
    output_dir: str | Path,
    min_confidence: float = 0.75,
    product_type: str | None = None,
    use_mimo: bool = True,
    audit_db_path: str | Path | None = None,
    clustering_mode: str = "semantic",
    semantic_threshold: float = 0.84,
    embedding_client: EmbeddingClient | None = None,
) -> dict[str, Any]:
    standard_catalog = load_standard_catalog(standards_path)
    source_rows = _read_source_rows(source_path)
    selected_rows, excluded_rows = filter_source_rows_by_product_type(source_rows, product_type)
    preprocessed_rows = preprocess_source_rows(selected_rows)
    eligible_rows, validation_excluded_rows = filter_preprocessed_rows_for_model(preprocessed_rows)
    excluded_rows.extend(validation_excluded_rows)
    eligible_raw_rows = [
        source_row
        for source_row, preprocessed_row in zip(selected_rows, preprocessed_rows)
        if _clean_text(preprocessed_row.get("可进入模型初标")) == "是"
    ]
    audit_store = AuditStore.from_env(audit_db_path)
    active_run_id = uuid.uuid4().hex
    for index, row in enumerate(excluded_rows, start=1):
        audit_store.record_excluded(
            active_run_id,
            _record_id_for_row(row, index),
            row,
            _clean_text(row.get("排除原因")) or "未通过候选生成校验",
        )
    feature_rows, run_id = generate_phone_candidate_rows(
        eligible_rows,
        standard_catalog,
        min_confidence=min_confidence,
        raw_source_rows=eligible_raw_rows,
        use_mimo=use_mimo,
        audit_store=audit_store,
        run_id=active_run_id,
    )
    output_path = _ensure_output_dir(output_dir)
    workbook_path = output_path / "review_queue.xlsx"
    topic_workbook_path = output_path / "topic_review_queue.xlsx"
    candidate_workbook_path = output_path / "candidate_knowledge.xlsx"
    write_review_workbook(preprocessed_rows, feature_rows, excluded_rows, workbook_path)
    topic_summary = write_topic_review_workbook(
        preprocessed_rows,
        feature_rows,
        excluded_rows,
        topic_workbook_path,
        standard_catalog=standard_catalog,
        min_confidence=min_confidence,
        use_mimo=use_mimo,
        audit_store=audit_store,
        run_id=run_id,
        clustering_mode=clustering_mode,
        semantic_threshold=semantic_threshold,
        embedding_client=embedding_client,
    )
    _, topic_rows = read_workbook_rows(topic_workbook_path, sheet_name="topic_review_queue")
    write_topic_candidate_knowledge_workbook(topic_rows, candidate_workbook_path)
    summary = _summary_for_preprocessed_rows(preprocessed_rows)
    summary.update(_summary_for_labeled_rows(feature_rows))
    summary.update(
        {
            "source_file": str(Path(source_path)),
            "standard_file": str(Path(standards_path)) if standards_path else "",
            "output_file": str(workbook_path),
            "topic_review_file": str(topic_workbook_path),
            "candidate_output_file": str(candidate_workbook_path),
            "product_type": _clean_text(product_type),
            "source_total_rows": len(source_rows),
            "excluded_rows": len(excluded_rows),
            "eligible_rows": len(eligible_rows),
            "run_id": run_id,
            "audit_db": str(audit_store.path),
            "mimo_configured": bool(MimoClient.from_env()) if use_mimo else False,
        }
    )
    summary.update(topic_summary)
    (output_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def publish_rows(
    review_path: str | Path,
    output_dir: str | Path,
    audit_db_path: str | Path | None = None,
) -> dict[str, Any]:
    _, review_rows = read_workbook_rows(review_path, sheet_name="review_queue")
    published_rows, feedback_rows = finalize_review_rows(review_rows)
    output_path = _ensure_output_dir(output_dir)
    published_workbook = output_path / "published_knowledge.xlsx"
    published_jsonl = output_path / "published_knowledge.jsonl"
    feedback_jsonl = output_path / "feedback_events.jsonl"
    write_rows_to_workbook(
        {"published_knowledge": (PUBLISHED_COLUMNS, published_rows)},
        published_workbook,
    )
    _write_jsonl(published_rows, published_jsonl)
    _write_jsonl(feedback_rows, feedback_jsonl)
    audit_store = AuditStore.from_env(audit_db_path)
    published_by_id = {str(row.get("来源记录ID") or row.get("知识ID") or ""): row for row in published_rows}
    feedback_by_id = {str(row.get("数据ID") or ""): row for row in feedback_rows}
    for review_row in review_rows:
        decision = _clean_text(review_row.get("CZ复核结论"))
        model_run_id = _clean_text(review_row.get("模型运行ID"))
        record_id = _clean_text(review_row.get("来源记录ID")) or _clean_text(review_row.get("数据ID"))
        if not (decision and model_run_id and record_id):
            continue
        final_candidate = published_by_id.get(record_id) or {
            "审核结论": decision,
            "CZ主标题": _clean_text(review_row.get("CZ主标题")),
            "CZ知识内容": _clean_text(review_row.get("CZ知识内容")),
            "CZ一级分类": _clean_text(review_row.get("CZ一级分类")),
            "CZ二级分类": _clean_text(review_row.get("CZ二级分类")),
            "CZ关联标准": _clean_text(review_row.get("CZ关联标准")),
            "CZ复核备注": _clean_text(review_row.get("CZ复核备注")),
        }
        audit_store.save_review_outcome(
            model_run_id=model_run_id,
            record_id=record_id,
            decision=decision,
            final_candidate=final_candidate,
            feedback=feedback_by_id.get(record_id, build_feedback_event(review_row)),
        )
    summary = _summary_for_final_rows(published_rows, feedback_rows)
    summary.update(
        {
            "review_file": str(Path(review_path)),
            "published_file": str(published_workbook),
            "published_jsonl": str(published_jsonl),
            "feedback_file": str(feedback_jsonl),
            "audit_db": str(audit_store.path),
        }
    )
    (output_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    title: str = ""
    subtitles: list[str] | None = None
    content: str = ""
    category_l1: str = ""
    category_l2: str = ""
    standard_refs: str = ""
    note: str = ""
    error_type: str = ""
    error_reason: str = ""
    retrain: str = ""
    reviewer: str = ""
    reviewed_at: str = ""

    def as_row_updates(self) -> dict[str, Any]:
        return {
            "CZ复核结论": self.decision,
            "CZ主标题": self.title,
            "CZ副标题": self.subtitles or [],
            "CZ知识内容": self.content,
            "CZ一级分类": self.category_l1,
            "CZ二级分类": self.category_l2,
            "CZ关联标准": self.standard_refs,
            "CZ复核备注": self.note,
            "错误类型": self.error_type,
            "错误原因": self.error_reason,
            "是否进入再训练样本": self.retrain,
            "审核人": self.reviewer,
            "审核时间": self.reviewed_at,
        }
