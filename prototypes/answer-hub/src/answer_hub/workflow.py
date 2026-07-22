from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from heapq import heappop, heappush
from pathlib import Path
from typing import Any, Callable, Iterable
import hashlib
import inspect
import json
import re
import uuid

import numpy as np

from .auto_review import (
    AutoReviewPolicy,
    UNWORTHY_VALUES,
    WORTHY_VALUES,
    apply_auto_review_annotation,
)
from .audit import AuditStore
from .catalog import StandardCatalogItem, is_active_standard, load_standard_catalog
from .embedding import EmbeddingClient, EmbeddingError
from .excel_io import read_workbook_rows, write_rows_to_workbook
from .images import ImageDownloader, ImageEvidence, split_image_urls
from .mimo import (
    ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
    CLUSTER_UNIT_PROMPT_VERSION,
    CLUSTER_PAIR_REVIEW_PROMPT_VERSION,
    MimoClient,
    MimoError,
    PROMPT_VERSION,
    TOPIC_REVIEW_PROMPT_VERSION,
    TOPIC_SIGNAL_PROMPT_VERSION,
)
from .product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    canonical_product_code,
    canonical_product_name,
    configured_product_names,
    normalize_product_scope,
    resolve_product_category,
)


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
DEFAULT_CLUSTER_REVIEW_FLOOR = 0.75
DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD = 0.92
DEFAULT_CLUSTER_REVIEW_LIMIT = 100
MAX_CLUSTER_REVIEW_CANDIDATES = 3
BROAD_RETRIEVAL_TERMS = {
    "设备",
    "屏幕",
    "问题",
    "检测",
    "情况",
    "异常",
    "功能",
    *configured_product_names(),
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
    "视频链接",
    "核心问题",
    "判定结论",
    "判定依据",
    "产品类型",
    "产品类型编码",
    "一级分类",
    "二级分类",
    "参考话术",
    "历史实际回复",
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
    "模型初标是否值得沉淀",
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
    "自动审核状态",
    "自动审核原因",
    "自动审核策略版本",
]

CLUSTER_VALIDATION_COLUMNS = [
    "验证对ID",
    "样本类型",
    "聚类预测",
    "语义相似度",
    "聚类阈值",
    "记录A_ID",
    "记录A_工单ID",
    "记录A_核心问题",
    "记录A_聊天内容",
    "记录A_图片链接",
    "记录A_视频链接",
    "记录A_图片处理状态",
    "记录A_图片证据摘要",
    "记录A_视频处理状态",
    "记录A_图片必要性",
    "记录A_主题标签",
    "记录A_语义标注依据",
    "记录A_一级分类",
    "记录A_二级分类",
    "记录B_ID",
    "记录B_工单ID",
    "记录B_核心问题",
    "记录B_聊天内容",
    "记录B_图片链接",
    "记录B_视频链接",
    "记录B_图片处理状态",
    "记录B_图片证据摘要",
    "记录B_视频处理状态",
    "记录B_图片必要性",
    "记录B_主题标签",
    "记录B_语义标注依据",
    "记录B_一级分类",
    "记录B_二级分类",
    "大模型判断",
    "大模型主题",
    "大模型原因",
    "大模型关键差异",
    "大模型置信度",
    "大模型名称",
    "大模型Prompt版本",
    "大模型状态",
    "人工判断",
    "人工错误类型",
    "人工备注",
    "审核人",
    "审核时间",
]

TOPIC_FEATURE_COLUMNS = [
    "问题意图",
    "对象/部位",
    "异常现象",
    "解题方式",
    "模型主题一级分类",
    "模型主题二级分类",
    "主题标签",
    "标签聚类键",
    "语义标注依据",
    "语义标注置信度",
    "语义标注图片必要性",
    "语义标注提供方",
    "语义标注模型",
    "语义标注Prompt版本",
    "语义标注状态",
    "语义标注错误",
    "证据等级",
    "标准关键词",
    "主标准路径",
    "图片处理状态",
    "图片证据摘要",
    "视频处理状态",
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

CASE_KNOWLEDGE_COLUMNS = [
    "知识ID",
    "主标题",
    "副标题",
    "知识内容",
    "图例",
    "推荐回复",
    "知识分类",
    "关联标准项",
    "适用范围",
    "关键词",
]

KNOWLEDGE_REVIEW_EXTENSION_COLUMNS = [
    "推荐回复",
    "是否值得沉淀",
    "是否可用",
    "如何修改",
    "问题反馈",
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
    "知识ID",
    "图例",
    "关键词",
    *KNOWLEDGE_MASTER_COLUMNS,
    *KNOWLEDGE_REVIEW_EXTENSION_COLUMNS,
]

TOPIC_SOURCE_MAPPING_COLUMNS = [
    "主题ID",
    "来源记录ID",
    "工单ID",
    "核心问题",
    "聊天内容",
    "历史实际回复",
    "图片链接",
    "视频链接",
    "图片处理状态",
    "视频处理状态",
    "产品类型",
    "一级分类",
    "二级分类",
    "模型主题一级分类",
    "模型主题二级分类",
    "主题标签",
    "标签聚类键",
    "语义标注依据",
    "语义标注置信度",
    "语义标注图片必要性",
    "语义标注提供方",
    "语义标注模型",
    "语义标注Prompt版本",
    "语义标注状态",
    "语义标注错误",
    "主标准路径",
    "证据等级",
    "纳入主题原因",
    "聚类决策",
    "聚类候选相似度",
    "聚类裁决提供方",
    "聚类裁决原因",
    "聚类裁决置信度",
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
    "知识ID",
    "图例",
    "关键词",
    *KNOWLEDGE_MASTER_COLUMNS,
    "推荐回复",
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
    "原始视频链接清洗",
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


def _historical_actual_reply(row: dict[str, Any]) -> str:
    for field in (
        "历史实际回复",
        "实际回复",
        "答疑回复",
        "回复内容",
        "客服回复",
        "参考话术",
    ):
        value = _normalize_lines(row.get(field))
        if value:
            return value
    return ""


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
    # Existing approved knowledge wins ties so the pipeline reuses it before
    # drafting a new item, while a materially better raw-standard match still
    # remains authoritative.
    knowledge_type_priority = {"已有知识": 4, "场景判定": 3, "检测方法": 2, "标准定义": 1}
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


def retrieve_topic_signal_matches(
    source_row: dict[str, Any],
    standard_catalog: list[StandardCatalogItem],
    top_k: int = 5,
) -> list[tuple[StandardCatalogItem, float]]:
    """Retrieve standards from the real conversation before legacy metadata."""
    conversation = _normalize_lines(source_row.get("聊天内容"))
    searchable_text = " ".join(
        value
        for value in (
            conversation,
            _clean_text(source_row.get("产品类型")),
            _clean_text(source_row.get("核心问题")) if not conversation else "",
        )
        if value
    )
    _, matches, _ = _match_standard(searchable_text, "", "", standard_catalog, top_k=top_k)
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
    if re.fullmatch(r"第?\d+行", topic):
        topic = standard.category_l2 or standard.category_l1
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
    use_standard_references: bool = True,
) -> str:
    del judgment, basis
    kind = _process_kind(core_problem, source_l1, source_l2)
    scope = _safe_join([source_l1, source_l2], " / ") or "待人工确认分类"
    if kind == "model_query":
        points = [
            "查询流程：",
            "1. 在设备设置的“关于本机/关于手机”中查看型号。",
            "2. 使用 IMEI、SN 或官方渠道核对出厂机型。",
            "3. 对照实物外观、功能配置和关键部件特征。",
            "4. 查询与实物不一致时，补充截图和实物照片后再判定。",
        ]
    elif kind == "repair":
        points = [
            "核验流程：",
            "1. 明确疑似拆修或维修痕迹的具体部位。",
            "2. 补充局部近景、整机全景和多角度照片。",
            "3. 核对原厂结构、胶痕、撬痕、部件标识和连接状态。",
            "4. 逐项对照当前拆修标准；证据不足时补充证据后再判定。",
        ]
    elif kind == "display":
        points = [
            "核验流程：",
            "1. 确认异常出现于亮屏、白屏、黑屏、息屏或特定测试画面。",
            "2. 拍摄屏幕正面全景和异常点近景，排除反光、贴膜和环境光干扰。",
            "3. 记录颜色、位置、数量、直径或面积并对照显示标准。",
            "4. 现象无法复现或图片不清晰时，补充证据后再判定。",
        ]
    elif kind == "function":
        points = [
            "功能核验流程：",
            "1. 明确待核验功能、测试条件和所用配件。",
            "2. 排除电量、网络、权限、保护壳等外部影响。",
            "3. 按标准步骤复测，并记录画面、提示、声音或响应结果。",
            "4. 结果不稳定或无法复现时，补充测试证据后再判定。",
        ]
    elif kind == "liquid":
        points = [
            "核验流程：",
            "1. 检查防水标、卡槽、接口、屏幕边缘、后盖及内部部件。",
            "2. 补充局部近景、全景和必要的拆机检测照片。",
            "3. 记录变色、腐蚀、水渍或液体残留并对照浸液标准。",
            "4. 不以单一模糊痕迹直接判定；证据不足时补充证据后再判定。",
        ]
    elif kind == "appearance":
        points = [
            "核验流程：",
            "1. 确认异常部位、材质及磕碰、划痕、磨损、掉漆、碎裂或脱胶类型。",
            "2. 拍摄整机全景、异常近景和侧视角度。",
            "3. 涉及尺寸或数量时补充量尺，记录直径、长度、数量及材料缺损。",
            "4. 对照外观标准边界；无法量化时补充图片后再判定。",
        ]
    else:
        points = [
            "核验流程：",
            "1. 明确待确认的对象、现象和对应标准项。",
            "2. 补充支持判断的截图、照片、视频或查询结果。",
            "3. 对照当前有效标准确认适用条件、边界和例外。",
            "4. 无法与标准明确对应时，补充证据后再判定。",
        ]
    sections = [f"适用主题：{scope}", *points]
    if standard and standard.response_snippet:
        standard_point = _normalize_lines(standard.response_snippet).splitlines()[0][:180]
        if standard_point and standard_point not in "\n".join(sections):
            sections.append(f"标准要点：{standard_point}")
    content = "\n".join(sections)
    if not use_standard_references:
        replacements = {
            "逐项对照当前拆修标准": "逐项核对原厂结构、胶痕、撬痕、部件标识和连接状态",
            "并对照显示标准": "并记录可复现的显示现象",
            "按标准步骤复测": "使用一致的测试条件复测",
            "并对照浸液标准": "并核对多处浸液迹象是否一致",
            "对照外观标准边界": "结合案例证据核对外观边界",
            "对应标准项": "对应问题",
            "对照当前有效标准确认适用条件、边界和例外": "结合案例证据确认适用条件、边界和例外",
            "无法与标准明确对应时": "无法明确判断时",
        }
        for source, target in replacements.items():
            content = content.replace(source, target)
    return content


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
    target = canonical_product_name(product_type, unknown=_clean_text(product_type))
    if not target:
        return source_rows, []

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in source_rows:
        actual_value = row.get("产品类型编码") or row.get("产品类型")
        actual = canonical_product_name(actual_value, unknown=_clean_text(row.get("产品类型")))
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
    if _normalize_lines(row.get("聊天内容")) or _normalize_image_links(row.get("图片链接")):
        return []
    return ["聊天内容或图片链接"]


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
        row["视频链接"] = _normalize_image_links(row.get("视频链接"))
        row["核心问题"] = _clean_text(row.get("核心问题"))
        row["判定结论"] = _clean_text(row.get("判定结论"))
        row["判定依据"] = _normalize_lines(row.get("判定依据"))
        raw_product_type = _clean_text(row.get("产品类型"))
        raw_product_code = _clean_text(row.get("产品类型编码"))
        product_category = resolve_product_category(raw_product_code or raw_product_type)
        row["产品类型原值"] = raw_product_type
        row["产品类型"] = product_category.name if product_category else UNKNOWN_PRODUCT_NAME
        row["产品类型编码"] = product_category.code if product_category else ""
        row["一级分类"] = _clean_text(row.get("一级分类"))
        row["二级分类"] = _clean_text(row.get("二级分类"))
        row["参考话术"] = _normalize_lines(row.get("参考话术"))
        row["历史实际回复"] = _historical_actual_reply(row)

        missing = _missing_fields(row)
        notes = []
        if missing:
            notes.append(f"缺失主证据: {', '.join(missing)}")
        if not row["聊天内容"]:
            notes.append("缺少原始聊天上下文；仅按结构化字段和图片生成候选，强制重点复核")
        if row["聊天内容"] and len(row["聊天内容"]) > 8000:
            notes.append("聊天内容过长，已保留原文结构")
        if row["图片链接"] and "\n" in row["图片链接"]:
            notes.append("图片链接已去重")
        if row["视频链接"] and "\n" in row["视频链接"]:
            notes.append("视频链接已去重")
        if product_category is None:
            notes.append(
                f"产品类型未在当前品类配置中识别：{raw_product_type or raw_product_code or '空'}；进入人工确认"
            )
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
        row["原始视频链接清洗"] = row["视频链接"]
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
        product_value = source_row.get("产品类型编码") or source_row.get("产品类型")
        product_type = canonical_product_name(product_value)
        product_type_code = canonical_product_code(product_value)
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
                "产品类型编码": product_type_code,
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


def _video_status(video_links: str) -> str:
    count = len(split_image_urls(video_links))
    if not count:
        return "无视频链接"
    return f"存在视频，当前未解析视频内容（{count}个）"


def _feature_text(row: dict[str, Any]) -> str:
    conversation = _normalize_lines(row.get("聊天内容"))
    if conversation:
        return conversation
    return " ".join(
        _clean_text(row.get(field))
        for field in ("核心问题", "判定结论", "判定依据", "参考话术")
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


def _feature_method(
    intent: str,
    row: dict[str, Any],
    use_standard_references: bool = True,
) -> str:
    if intent == "信息查询":
        return "官方信息查询与实物核对"
    if intent == "边界判定":
        return "定义与边界条件对照"
    if intent == "功能核验":
        return "功能测试与结果核对" if not use_standard_references else "功能测试与标准对照"
    if intent == "痕迹核验":
        return (
            "现场图片补充与痕迹核验"
            if not use_standard_references
            else "现场图片补充与拆修标准核验"
        )
    return (
        "现场图片/视频补充与案例证据核验"
        if not use_standard_references
        else "现场图片/视频补充与有效标准核验"
    )


def _standard_keywords(matches: list[tuple[StandardCatalogItem, float]], row: dict[str, Any]) -> str:
    values = [
        _clean_text(row.get("模型主题一级分类")),
        _clean_text(row.get("模型主题二级分类")),
    ]
    if matches:
        standard = matches[0][0]
        values.extend([standard.category_l1, standard.category_l2, *standard.keywords[:5]])
    return _merge_unique_keywords(values)


def _signal_primary_standard(
    matches: list[tuple[StandardCatalogItem, float]],
    refs: list[str],
) -> StandardCatalogItem | None:
    by_ref = {_standard_reference(item): item for item, _score in matches}
    for ref in refs:
        if ref in by_ref:
            return by_ref[ref]
    return matches[0][0] if matches else None


def _signal_topic_tags(
    intent: str,
    subject: str,
    phenomenon: str,
    resolution_mode: str,
    refs: list[str],
    extra_tags: list[str] | None = None,
) -> list[str]:
    tags = [
        f"意图:{intent}",
        f"对象:{subject}",
        f"现象:{phenomenon}",
        f"处理:{resolution_mode}",
    ]
    if refs:
        tags.append(f"标准:{refs[0]}")
    tags.extend(extra_tags or [])
    return list(dict.fromkeys(_clean_text(tag) for tag in tags if _clean_text(tag)))[:6]


def _topic_tag_cluster_key(row: dict[str, Any]) -> str:
    current = _clean_text(row.get("标签聚类键"))
    if current:
        return current
    values = [
        _clean_text(row.get("产品类型")),
        _clean_text(row.get("问题意图")),
        _clean_text(row.get("对象/部位")),
        _clean_text(row.get("异常现象")),
        _clean_text(row.get("解题方式")),
        _clean_text(row.get("主标准路径")),
    ]
    return " | ".join(value for value in values if value)


def _fallback_topic_signal(
    row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    image_status: str,
    use_standard_references: bool = True,
) -> dict[str, Any]:
    intent = _feature_intent(row)
    subject = _feature_part(row)
    phenomenon = _feature_phenomenon(row)
    resolution_mode = _feature_method(
        intent,
        row,
        use_standard_references=use_standard_references,
    )
    primary = matches[0][0] if matches else None
    refs = [_standard_reference(primary)] if primary and _standard_reference(primary) else []
    text = _feature_text(row)
    requires_images = bool(
        split_image_urls(_clean_text(row.get("图片链接")))
        and any(marker in text for marker in ("图片", "照片", "外观", "显示", "颜色", "划痕", "裂", "拆修", "胶"))
    )
    return {
        "intent": intent,
        "subject": subject,
        "phenomenon": phenomenon,
        "resolution_mode": resolution_mode,
        "category_l1": (
            primary.category_l1
            if primary and primary.category_l1
            else _clean_text(row.get("一级分类"))
            if not use_standard_references
            else "待确认"
        ),
        "category_l2": (
            primary.category_l2
            if primary and primary.category_l2
            else _clean_text(row.get("二级分类"))
            if not use_standard_references
            else "待确认"
        ),
        "topic_tags": _signal_topic_tags(intent, subject, phenomenon, resolution_mode, refs),
        "standard_refs": refs,
        "requires_images": requires_images,
        "image_evidence_summary": (
            "规则回退；图片下载状态：" + image_status
            if requires_images
            else "规则回退；当前会话文本可作为主要证据。"
        ),
        "reasoning_summary": "未完成模型会话语义标注，当前使用基于原始会话的规则特征，需人工复核。",
        "confidence": 0.45,
        "needs_human_review": True,
    }


def extract_topic_feature_rows(
    source_rows: list[dict[str, Any]],
    standard_catalog: list[StandardCatalogItem],
    raw_source_rows: list[dict[str, Any]] | None = None,
    use_mimo: bool = True,
    mimo_client: MimoClient | None = None,
    audit_store: AuditStore | None = None,
    run_id: str | None = None,
    image_downloader: ImageDownloader | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    use_standard_references: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """Extract auditable topic features only; this stage never drafts knowledge."""
    active_run_id = run_id or uuid.uuid4().hex
    downloader = image_downloader or ImageDownloader()
    raw_rows = raw_source_rows if raw_source_rows and len(raw_source_rows) == len(source_rows) else source_rows
    feature_rows: list[dict[str, Any]] = []
    client = mimo_client if use_mimo else None
    if client is None and use_mimo:
        client = MimoClient.from_env()

    if progress_callback:
        progress_callback("semantic_labeling", 0, len(source_rows))
    for index, source_row in enumerate(source_rows, start=1):
        row = dict(source_row)
        record_id = _record_id_for_row(row, index)
        matches = (
            retrieve_topic_signal_matches(row, standard_catalog, top_k=5)
            if use_standard_references
            else []
        )
        image_links = _normalize_lines(row.get("图片链接"))
        video_links = _normalize_lines(row.get("视频链接"))
        images = downloader.fetch(image_links) if image_links else []
        image_status, _image_requires_review = _image_status(images, bool(split_image_urls(image_links)))
        video_status = _video_status(video_links)
        row["视频处理状态"] = video_status
        signal = _fallback_topic_signal(
            row,
            matches,
            image_status,
            use_standard_references=use_standard_references,
        )
        signal_provider = "topic-signal-rule"
        signal_model = "topic-signal-rule-v2"
        signal_prompt_version = ""
        signal_status = "rule_fallback"
        signal_error = ""
        model_run_id = ""
        if client and hasattr(client, "analyze_topic_signal"):
            signal_provider = "mimo"
            signal_model = client.config.model
            signal_prompt_version = TOPIC_SIGNAL_PROMPT_VERSION
            try:
                analyze_topic_signal = client.analyze_topic_signal
                if "use_standard_references" in inspect.signature(analyze_topic_signal).parameters:
                    result = analyze_topic_signal(
                        row,
                        matches,
                        images,
                        use_standard_references=use_standard_references,
                    )
                else:
                    result = analyze_topic_signal(row, matches, images)
                signal = result.candidate
                signal_status = "topic_signal_labeled"
                model_run_id = uuid.uuid4().hex
            except MimoError as exc:
                signal_status = "topic_signal_rule_fallback"
                signal_error = str(exc)
        elif use_mimo:
            signal_status = "topic_signal_rule_fallback"
            signal_error = "MiMo 未配置或客户端未提供会话语义标注能力"

        intent = _clean_text(signal.get("intent")) or "其他待确认"
        subject = _clean_text(signal.get("subject")) or "待确认"
        phenomenon = _clean_text(signal.get("phenomenon")) or "待确认"
        resolution_mode = _clean_text(signal.get("resolution_mode")) or "补充证据后再判定"
        refs = (
            [
                _clean_text(ref)
                for ref in signal.get("standard_refs", [])
                if _clean_text(ref)
            ]
            if use_standard_references
            else []
        )
        primary = _signal_primary_standard(matches, refs)
        tags = _signal_topic_tags(
            intent,
            subject,
            phenomenon,
            resolution_mode,
            refs,
            signal.get("topic_tags") if isinstance(signal.get("topic_tags"), list) else [],
        )
        tag_cluster_key = " | ".join(
            [
                _clean_text(row.get("产品类型")),
                intent,
                subject,
                phenomenon,
                resolution_mode,
                refs[0] if refs else _primary_standard_path(primary.standard_path) if primary else "",
            ]
        )
        row.update(
            {
                "流程状态": "topic_pending",
                "模型阶段状态": signal_status,
                "数据ID": record_id,
                "问题意图": intent,
                "对象/部位": subject,
                "异常现象": phenomenon,
                "解题方式": resolution_mode,
                "模型主题一级分类": _clean_text(signal.get("category_l1")) or "待确认",
                "模型主题二级分类": _clean_text(signal.get("category_l2")) or "待确认",
                "主题标签": " | ".join(tags),
                "标签聚类键": tag_cluster_key,
                "语义标注依据": _clean_text(signal.get("reasoning_summary")),
                "语义标注置信度": signal.get("confidence", ""),
                "语义标注图片必要性": "需要" if signal.get("requires_images") else "不需要",
                "语义标注提供方": signal_provider,
                "语义标注模型": signal_model,
                "语义标注Prompt版本": signal_prompt_version,
                "语义标注状态": signal_status,
                "语义标注错误": signal_error,
                "证据等级": "完整会话" if _normalize_lines(row.get("聊天内容")) else (
                    "图片证据" if not _image_requires_review and _has_usable_image_evidence({"图片处理状态": image_status}) else "结构化摘要"
                ),
                "标准关键词": _standard_keywords(matches, row) if use_standard_references else "",
                "主标准路径": (
                    _primary_standard_path(primary.standard_path)
                    if use_standard_references and primary
                    else ""
                ),
                "图片处理状态": image_status,
                "视频处理状态": video_status,
                "图片证据摘要": (
                    _clean_text(signal.get("image_evidence_summary"))
                    if _normalize_lines(row.get("聊天内容"))
                    else _safe_join(
                        [
                            _clean_text(signal.get("image_evidence_summary")),
                            "无聊天内容，但存在可用现场图片。" if "可用:" in image_status and "可用:0" not in image_status else "缺少原始聊天内容和可用图片。",
                        ],
                        "；",
                    )
                ),
                "标准检索状态": (
                    "已命中相关知识"
                    if matches
                    else "未搜索到相关知识（待人工补充）"
                    if use_standard_references
                    else "未启用标准引用"
                ),
                "检索标准Top5": (
                    _format_retrieved_standards(matches)
                    if matches
                    else "未搜索到相关知识（待人工补充）"
                    if use_standard_references
                    else ""
                ),
                "标准版本": "\n".join(
                    f"{_standard_reference(item)}:{item.version}" for item, _score in matches if _standard_reference(item)
                ),
                "标准候选分数": matches[0][1] if matches else 0.0,
                "模型提供方": signal_provider,
                "模型名称": signal_model,
                "Prompt版本": signal_prompt_version,
                "模型运行ID": model_run_id,
                "模型错误": signal_error,
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
        if progress_callback:
            progress_callback("semantic_labeling", index, len(source_rows))
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
    progress_callback: Callable[[str, int, int], None] | None = None,
    use_standard_references: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    del min_confidence
    return extract_topic_feature_rows(
        source_rows,
        standard_catalog,
        raw_source_rows=raw_source_rows,
        use_mimo=use_mimo,
        mimo_client=mimo_client,
        audit_store=audit_store,
        run_id=run_id,
        image_downloader=image_downloader,
        progress_callback=progress_callback,
        use_standard_references=use_standard_references,
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
        "知识ID": _clean_text(row.get("知识ID")) or _clean_text(row.get("主题ID")),
        "主标题": _clean_text(row.get("主标题")),
        "副标题": _clean_text(row.get("副标题")),
        "知识内容": _clean_text(row.get("知识内容")),
        "图例": _clean_text(row.get("图例")) or _clean_text(row.get("主题图片链接")),
        "推荐回复": _clean_text(row.get("推荐回复")),
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
        "关键词": _clean_text(row.get("关键词")) or _clean_text(row.get("检索关键词")),
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
        "审核结论": _clean_text(row.get("审核结论")) or _simple_review_decision(row),
        "主题样本数": _clean_text(row.get("主题样本数")),
        "主题来源记录ID": _clean_text(row.get("主题来源记录ID")),
        "主题证据等级": _clean_text(row.get("主题证据等级")),
        "主题标准版本": _clean_text(row.get("主题标准版本")),
        "最终主标题": _clean_text(row.get("主标题")),
        "最终知识分类": _clean_text(row.get("知识分类")),
        "最终关联标准项": _clean_text(row.get("关联标准项")),
        "模型初标是否值得沉淀": _clean_text(row.get("模型初标是否值得沉淀")),
        "是否值得沉淀": _clean_text(row.get("是否值得沉淀")),
        "错误类型": _clean_text(row.get("错误类型")),
        "错误原因": _safe_join(
            [
                _clean_text(row.get("错误原因")),
                _clean_text(row.get("问题反馈")),
                _clean_text(row.get("如何修改")),
            ],
            "；",
        ),
        "是否进入训练集": _clean_text(row.get("是否进入训练集")),
        "审核人": _clean_text(row.get("审核人")),
        "审核时间": review_time,
    }


def _simple_review_decision(row: dict[str, Any]) -> str:
    knowledge_value = _clean_text(row.get("是否值得沉淀")).lower()
    if knowledge_value in UNWORTHY_VALUES:
        return "驳回"
    if knowledge_value not in WORTHY_VALUES:
        return ""
    value = _clean_text(row.get("是否可用")).lower()
    if value in {"是", "可用", "通过", "yes", "true", "1"}:
        return "通过"
    if value in {"否", "不可用", "驳回", "no", "false", "0"}:
        return "驳回"
    return ""


def finalize_topic_review_rows(
    topic_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return final master candidates, audit feedback and optional SFT examples."""
    final_rows: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []
    training_rows: list[dict[str, Any]] = []
    for row in topic_rows:
        decision = _clean_text(row.get("审核结论")) or _simple_review_decision(row)
        knowledge_value = _clean_text(row.get("是否值得沉淀")).lower()
        if knowledge_value in UNWORTHY_VALUES:
            decision = "驳回"
        if (
            decision in {"通过", "修改后通过"}
            and _clean_text(row.get("自动审核状态")) != "auto_approved"
            and knowledge_value not in WORTHY_VALUES
        ):
            continue
        if not decision:
            continue
        if not _review_decision_allowed(decision):
            raise ValueError(f"Unsupported topic review decision: {decision}")
        review_time = _clean_text(row.get("审核时间")) or datetime.now().isoformat(timespec="seconds")
        normalized_row = dict(row)
        normalized_row["审核结论"] = decision
        normalized_row["审核备注"] = _safe_join(
            [
                _clean_text(normalized_row.get("审核备注")),
                _clean_text(normalized_row.get("如何修改")),
                _clean_text(normalized_row.get("问题反馈")),
            ],
            "；",
        )
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
    case_only = any(
        _clean_text(row.get("知识来源")) == "方向二案例沉淀"
        for row in topic_rows
    )
    export_columns = CASE_KNOWLEDGE_COLUMNS if case_only else KNOWLEDGE_MASTER_COLUMNS
    export_rows = build_case_knowledge_rows(final_rows) if case_only else final_rows
    write_rows_to_workbook(
        {"候选知识": (export_columns, export_rows)},
        final_workbook,
    )
    _write_jsonl(feedback_rows, feedback_jsonl)
    _write_jsonl(training_rows, training_jsonl)
    return {
        "candidate_rows": len(final_rows),
        "feedback_rows": len(feedback_rows),
        "training_rows": len(training_rows),
        "candidate_file": str(final_workbook),
        "feedback_file": str(feedback_jsonl),
        "training_file": str(training_jsonl),
        "case_only": case_only,
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
        _clean_text(row.get("模型主题一级分类")) or _clean_text(row.get("一级分类")),
        _clean_text(row.get("模型主题二级分类")) or _clean_text(row.get("二级分类")),
        _clean_text(row.get("主标准路径")),
        _clean_text(row.get("问题意图")),
        _clean_text(row.get("对象/部位")),
        _clean_text(row.get("异常现象")),
        _topic_tag_cluster_key(row),
    )


def _semantic_excerpt(value: Any, max_chars: int = 800) -> str:
    text = _clean_text(value)
    if len(text) <= max_chars:
        return text
    head_size = int(max_chars * 0.7)
    tail_size = max_chars - head_size
    return f"{text[:head_size]}\n[...]\n{text[-tail_size:]}"


def _topic_semantic_text(row: dict[str, Any]) -> str:
    fields = (
        ("产品类型", row.get("产品类型")),
        ("机型", row.get("机型")),
        ("主题标签", row.get("主题标签")),
        ("标签聚类键", _topic_tag_cluster_key(row)),
        ("问题意图", row.get("问题意图")),
        ("对象/部位", row.get("对象/部位")),
        ("异常现象", row.get("异常现象")),
        ("解题方式", row.get("解题方式")),
        ("模型一级分类", row.get("模型主题一级分类")),
        ("模型二级分类", row.get("模型主题二级分类")),
        ("主标准路径", row.get("主标准路径")),
    )
    return "\n".join(f"{label}：{_clean_text(value)}" for label, value in fields if _clean_text(value))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) == 0 or len(right) == 0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _semantic_topic_groups_from_vectors(
    rows: list[dict[str, Any]],
    vectors: list[list[float]],
    threshold: float,
) -> tuple[
    list[tuple[tuple[str, ...], list[dict[str, Any]]]],
    dict[str, Any],
    dict[int, int],
]:
    if len(vectors) != len(rows):
        raise EmbeddingError("Embedding vector count does not match topic row count")
    if not rows:
        return [], {
            "threshold": threshold,
            "cluster_count": 0,
            "min_similarity": None,
        }, {}

    vector_matrix = np.asarray(vectors, dtype=np.float32)
    if vector_matrix.ndim != 2 or vector_matrix.shape[0] != len(rows):
        raise EmbeddingError("Embedding vectors must form a two-dimensional matrix")

    grouped: list[dict[str, Any]] = []
    assignments: dict[int, int] = {}
    for row, vector in zip(rows, vector_matrix):
        product_type = _clean_text(row.get("产品类型"))
        best_index = -1
        best_score = -1.0
        matching_indices = [
            index
            for index, cluster in enumerate(grouped)
            if cluster["product_type"] == product_type
            and not _cluster_has_topic_merge_conflict(row, cluster["rows"])
        ]
        if matching_indices:
            centroids = np.stack([grouped[index]["centroid"] for index in matching_indices])
            vector_norm = float(np.linalg.norm(vector))
            centroid_norms = np.linalg.norm(centroids, axis=1)
            denominators = centroid_norms * vector_norm
            scores = np.divide(
                centroids @ vector,
                denominators,
                out=np.zeros_like(centroid_norms),
                where=denominators > 0,
            )
            local_index = int(np.argmax(scores))
            best_index = matching_indices[local_index]
            best_score = float(scores[local_index])
        if best_index >= 0 and best_score >= threshold:
            cluster = grouped[best_index]
            cluster["rows"].append(row)
            count = len(cluster["rows"])
            cluster["centroid"] = (
                cluster["centroid"] * (count - 1) + vector
            ) / count
            cluster["min_similarity"] = min(cluster["min_similarity"], best_score)
            assignments[id(row)] = best_index + 1
        else:
            grouped.append(
                {
                    "product_type": product_type,
                    "rows": [row],
                    "centroid": vector,
                    "min_similarity": 1.0,
                }
            )
            assignments[id(row)] = len(grouped)

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
        "threshold": threshold,
        "cluster_count": len(result),
        "min_similarity": min(min_similarity_values) if min_similarity_values else None,
    }, assignments


def _semantic_topic_groups(
    rows: list[dict[str, Any]],
    embedding_client: EmbeddingClient,
    threshold: float,
) -> tuple[list[tuple[tuple[str, ...], list[dict[str, Any]]]], dict[str, Any]]:
    texts = [_topic_semantic_text(row) for row in rows]
    vectors = embedding_client.embed_texts(texts)
    result, meta, _assignments = _semantic_topic_groups_from_vectors(rows, vectors, threshold)
    meta.update(
        {
            "provider": "embedding",
            "model": embedding_client.config.model,
        }
    )
    return result, meta


def _direct_atomic_fallback(row: dict[str, Any]) -> dict[str, Any]:
    category_l1 = _clean_text(row.get("模型主题一级分类")) or _clean_text(row.get("一级分类"))
    allowed_categories = {
        "基本情况",
        "成色与回收标准",
        "外观问题",
        "显示问题",
        "功能问题",
        "拆修问题",
        "信息查询",
        "流程操作",
        "其他待确认",
    }
    if category_l1 not in allowed_categories:
        category_l1 = "其他待确认"
    product_type = _clean_text(row.get("产品类型")) or "待确认"
    text = " ".join(
        _clean_text(row.get(field))
        for field in ("聊天内容", "核心问题", "主题标签", "主标准路径")
    ).lower()
    platform = (
        "iOS"
        if any(marker in text for marker in ("苹果", "iphone", "ipad", "ios", "watchos"))
        else "Android"
        if any(marker in text for marker in ("安卓", "android"))
        else "HarmonyOS"
        if any(marker in text for marker in ("鸿蒙", "harmony"))
        else "通用"
    )
    return {
        "normalized_issue": _safe_join(
            [
                _clean_text(row.get("对象/部位")),
                _clean_text(row.get("异常现象")),
                _clean_text(row.get("解题方式")),
            ],
            "｜",
        )
        or _clean_text(row.get("核心问题"))
        or "待确认问题",
        "product_category": product_type,
        "scope_type": "平台专用" if platform != "通用" else "品类专用",
        "platform": platform,
        "brand": "通用",
        "model_scope": "通用",
        "category_l1": category_l1,
        "category_l2": _clean_text(row.get("模型主题二级分类"))
        or _clean_text(row.get("二级分类"))
        or "待确认",
        "intent": _clean_text(row.get("问题意图")) or "其他待确认",
        "subject": _clean_text(row.get("对象/部位")) or "待确认",
        "phenomenon": _clean_text(row.get("异常现象")) or "待确认",
        "judgment_target": _clean_text(row.get("核心问题")) or "待确认",
        "resolution_mode": _clean_text(row.get("解题方式")) or "补充证据后再判定",
        "standard_path": _clean_text(row.get("主标准路径")) or "待确认",
        "threshold_or_exception": "无明确阈值",
        "evidence_summary": _clean_text(row.get("语义标注依据"))
        or _semantic_excerpt(row.get("聊天内容"), 300),
        "confidence": row.get("语义标注置信度", 0.45),
        "requires_review": True,
    }


def _direct_atomic_bucket_key(unit: dict[str, Any]) -> tuple[str, ...]:
    scope_type = _clean_text(unit.get("scope_type"))
    platform = _clean_text(unit.get("platform")) if scope_type in {"平台专用", "品牌专用", "机型专用"} else "通用"
    brand = _clean_text(unit.get("brand")) if scope_type in {"品牌专用", "机型专用"} else "通用"
    model_scope = _clean_text(unit.get("model_scope")) if scope_type == "机型专用" else "通用"
    return (
        _clean_text(unit.get("product_category")),
        scope_type,
        platform,
        brand,
        model_scope,
        _clean_text(unit.get("category_l1")),
        _clean_text(unit.get("intent")),
    )


def _direct_mimo_topic_groups(
    rows: list[dict[str, Any]],
    reviewer: MimoClient,
    batch_size: int = 40,
) -> tuple[list[tuple[tuple[str, ...], list[dict[str, Any]]]], dict[str, Any]]:
    atomic_units: list[dict[str, Any]] = []
    row_by_atomic_id: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {
        "provider": "mimo-atomic-extraction+direct-topic-clustering",
        "model": reviewer.config.model,
        "atomic_prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
        "cluster_prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
        "atomic_extraction_calls": 0,
        "atomic_extraction_failed": 0,
        "atomic_unit_count": 0,
        "direct_cluster_calls": 0,
        "direct_cluster_failed": 0,
        "direct_review_singletons": 0,
        "direct_batch_size": batch_size,
    }

    for source_index, source_row in enumerate(rows, start=1):
        base_id = (
            _clean_text(source_row.get("数据ID"))
            or _clean_text(source_row.get("工单ID"))
            or f"ROW-{source_index:05d}"
        )
        topics: list[dict[str, Any]]
        meta["atomic_extraction_calls"] += 1
        try:
            result = reviewer.analyze_cluster_units(source_row)
            topics = list(result.candidate.get("topics") or [])
        except (AttributeError, MimoError):
            meta["atomic_extraction_failed"] += 1
            topics = [_direct_atomic_fallback(source_row)]
        if not topics:
            topics = [_direct_atomic_fallback(source_row)]
        for topic_index, topic in enumerate(topics, start=1):
            atomic_id = f"{base_id}-U{topic_index}"
            unit = {
                "unit_id": atomic_id,
                "sample_id": base_id,
                **topic,
            }
            atomic_units.append(unit)
            atomic_row = dict(source_row)
            atomic_row.update(
                {
                    "_原子知识ID": atomic_id,
                    "_原子适用范围类型": _clean_text(topic.get("scope_type")),
                    "_原子平台": _clean_text(topic.get("platform")),
                    "_原子品牌": _clean_text(topic.get("brand")),
                    "_原子机型范围": _clean_text(topic.get("model_scope")),
                    "_原子阈值例外": _clean_text(topic.get("threshold_or_exception")),
                    "核心问题": _clean_text(topic.get("normalized_issue"))
                    or _clean_text(source_row.get("核心问题")),
                    "产品类型": _clean_text(topic.get("product_category"))
                    or _clean_text(source_row.get("产品类型")),
                    "模型主题一级分类": _clean_text(topic.get("category_l1")),
                    "模型主题二级分类": _clean_text(topic.get("category_l2")),
                    "问题意图": _clean_text(topic.get("intent")),
                    "对象/部位": _clean_text(topic.get("subject")),
                    "异常现象": _clean_text(topic.get("phenomenon")),
                    "解题方式": _clean_text(topic.get("resolution_mode")),
                    "主标准路径": _clean_text(topic.get("standard_path")),
                    "语义标注依据": _clean_text(topic.get("evidence_summary")),
                    "语义标注置信度": topic.get("confidence", ""),
                    "语义标注状态": "atomic_unit_labeled",
                    "主题标签": _safe_join(
                        [
                            f"意图:{_clean_text(topic.get('intent'))}",
                            f"对象:{_clean_text(topic.get('subject'))}",
                            f"现象:{_clean_text(topic.get('phenomenon'))}",
                            f"处理:{_clean_text(topic.get('resolution_mode'))}",
                        ],
                        " | ",
                    ),
                }
            )
            row_by_atomic_id[atomic_id] = atomic_row

    meta["atomic_unit_count"] = len(atomic_units)
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for unit in atomic_units:
        buckets.setdefault(_direct_atomic_bucket_key(unit), []).append(unit)

    topic_groups: list[tuple[tuple[str, ...], list[dict[str, Any]]]] = []
    cluster_index = 0
    for bucket_key, bucket_units in sorted(buckets.items(), key=lambda item: item[0]):
        ordered = sorted(bucket_units, key=lambda unit: _clean_text(unit.get("unit_id")))
        for batch_index in range(0, len(ordered), max(1, batch_size)):
            batch = ordered[batch_index : batch_index + max(1, batch_size)]
            if len(batch) == 1:
                candidate = {
                    "clusters": [{"member_atomic_ids": [batch[0]["unit_id"]]}],
                    "split_requests": [],
                    "review_requests": [],
                }
            else:
                meta["direct_cluster_calls"] += 1
                try:
                    candidate = reviewer.cluster_atomic_units(batch).candidate
                except (AttributeError, MimoError):
                    meta["direct_cluster_failed"] += 1
                    candidate = {
                        "clusters": [
                            {"member_atomic_ids": [unit["unit_id"]]}
                            for unit in batch
                        ],
                        "split_requests": [],
                        "review_requests": [],
                    }
            assigned: set[str] = set()
            for cluster in candidate.get("clusters", []):
                member_ids = [
                    _clean_text(atomic_id)
                    for atomic_id in cluster.get("member_atomic_ids", [])
                    if _clean_text(atomic_id) in row_by_atomic_id
                ]
                if not member_ids:
                    continue
                assigned.update(member_ids)
                cluster_index += 1
                member_rows = [row_by_atomic_id[atomic_id] for atomic_id in member_ids]
                for member_row in member_rows:
                    member_row.update(
                        {
                            "_聚类决策": "纯大模型1-N聚类",
                            "_聚类候选相似度": "",
                            "_聚类裁决提供方": "mimo-direct",
                            "_聚类裁决原因": _clean_text(cluster.get("merge_basis"))
                            or "原子问题满足适用范围、对象、目标、标准路径和阈值例外一致性。",
                            "_聚类裁决置信度": "",
                        }
                    )
                topic_groups.append(
                    (
                        (
                            "direct_mimo",
                            *bucket_key,
                            f"batch-{batch_index // max(1, batch_size) + 1}",
                            f"cluster-{cluster_index}",
                            *member_ids,
                        ),
                        member_rows,
                    )
                )
            unresolved_ids = {
                _clean_text(request.get("atomic_id"))
                for request in [
                    *candidate.get("split_requests", []),
                    *candidate.get("review_requests", []),
                ]
                if _clean_text(request.get("atomic_id")) in row_by_atomic_id
            }
            unresolved_ids.update(
                _clean_text(unit.get("unit_id"))
                for unit in batch
                if _clean_text(unit.get("unit_id")) not in assigned
            )
            for atomic_id in sorted(unresolved_ids):
                meta["direct_review_singletons"] += 1
                cluster_index += 1
                member_row = row_by_atomic_id[atomic_id]
                member_row.update(
                    {
                        "_聚类决策": "待复核原子问题独立成簇",
                        "_聚类候选相似度": "",
                        "_聚类裁决提供方": "mimo-direct-guard",
                        "_聚类裁决原因": "原子问题需要拆分或字段待确认，保守地保留为单成员主题。",
                        "_聚类裁决置信度": "",
                    }
                )
                topic_groups.append(
                    (
                        ("direct_mimo", *bucket_key, f"review-{cluster_index}", atomic_id),
                        [member_row],
                    )
                )

    meta["cluster_count"] = len(topic_groups)
    return topic_groups, meta


def _rank_semantic_cluster_candidates(
    vector: np.ndarray,
    grouped: list[dict[str, Any]],
    product_type: str,
) -> list[tuple[int, float]]:
    matching_indices = [
        index
        for index, cluster in enumerate(grouped)
        if cluster["product_type"] == product_type
    ]
    if not matching_indices:
        return []
    centroids = np.stack([grouped[index]["centroid"] for index in matching_indices])
    vector_norm = float(np.linalg.norm(vector))
    centroid_norms = np.linalg.norm(centroids, axis=1)
    denominators = centroid_norms * vector_norm
    scores = np.divide(
        centroids @ vector,
        denominators,
        out=np.zeros_like(centroid_norms),
        where=denominators > 0,
    )
    return sorted(
        (
            (cluster_index, float(score))
            for cluster_index, score in zip(matching_indices, scores)
        ),
        key=lambda item: item[1],
        reverse=True,
    )


def _has_topic_merge_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    unknown_values = {"", "待确认", "未知", "通用", "不限"}
    for field in (
        "产品类型",
        "模型主题一级分类",
        "模型主题二级分类",
        "主标准路径",
    ):
        left_value = _clean_text(left.get(field))
        right_value = _clean_text(right.get(field))
        if (
            left_value not in unknown_values
            and right_value not in unknown_values
            and left_value != right_value
        ):
            return True

    mismatches = 0
    for field in ("问题意图", "对象/部位", "解题方式"):
        left_value = _clean_text(left.get(field))
        right_value = _clean_text(right.get(field))
        if left_value and right_value and left_value != right_value:
            mismatches += 1
    return mismatches >= 2


def _cluster_has_topic_merge_conflict(
    candidate: dict[str, Any],
    cluster_rows: list[dict[str, Any]],
) -> bool:
    return any(
        _has_topic_merge_conflict(candidate, member)
        for member in cluster_rows
    )


def _has_high_confidence_topic_signal(row: dict[str, Any]) -> bool:
    if _clean_text(row.get("语义标注状态")) != "topic_signal_labeled":
        return False
    try:
        return float(row.get("语义标注置信度", 0.0)) >= 0.8
    except (TypeError, ValueError):
        return False


def _append_to_semantic_cluster(
    cluster: dict[str, Any],
    row: dict[str, Any],
    vector: np.ndarray,
    similarity: float,
) -> None:
    cluster["rows"].append(row)
    count = len(cluster["rows"])
    cluster["centroid"] = (cluster["centroid"] * (count - 1) + vector) / count
    cluster["min_similarity"] = min(cluster["min_similarity"], similarity)
    representative_vector = cluster["representative_vector"]
    if _cosine_similarity(vector, cluster["centroid"]) >= _cosine_similarity(
        representative_vector,
        cluster["centroid"],
    ):
        cluster["representative_row"] = row
        cluster["representative_vector"] = vector


def _semantic_mimo_topic_groups(
    rows: list[dict[str, Any]],
    embedding_client: EmbeddingClient,
    reviewer: MimoClient,
    threshold: float,
    review_floor: float = DEFAULT_CLUSTER_REVIEW_FLOOR,
    auto_merge_threshold: float = DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    review_limit: int = DEFAULT_CLUSTER_REVIEW_LIMIT,
) -> tuple[list[tuple[tuple[str, ...], list[dict[str, Any]]]], dict[str, Any]]:
    """Use model tags first, embeddings only for candidate recall, and MiMo for final merges."""
    texts = [_topic_semantic_text(row) for row in rows]
    vectors = embedding_client.embed_texts(texts)
    if len(vectors) != len(rows):
        raise EmbeddingError("Embedding vector count does not match topic row count")

    vector_matrix = np.asarray(vectors, dtype=np.float32)
    if vector_matrix.ndim != 2 or vector_matrix.shape[0] != len(rows):
        raise EmbeddingError("Embedding vectors must form a two-dimensional matrix")

    floor = max(0.0, min(float(review_floor), 1.0))
    auto_threshold = max(floor, min(float(auto_merge_threshold), 1.0))
    limit = max(0, int(review_limit))
    grouped: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "threshold": threshold,
        "review_floor": floor,
        "auto_merge_threshold": auto_threshold,
        "review_limit": limit,
        "mimo_review_model": reviewer.config.model,
        "mimo_review_calls": 0,
        "mimo_review_approved": 0,
        "mimo_review_rejected": 0,
        "mimo_review_uncertain": 0,
        "mimo_review_failed": 0,
        "mimo_hard_rule_rejected": 0,
        "mimo_auto_merged": 0,
        "mimo_tag_auto_merged": 0,
        "mimo_review_limit_reached": 0,
    }

    for row, vector in zip(rows, vector_matrix):
        product_type = _clean_text(row.get("产品类型"))
        merged = False
        final_decision = "新建主题"
        final_provider = "embedding"
        final_reason = "未找到达到大模型裁决下限的候选主题。"
        final_confidence: Any = ""
        final_similarity = 0.0

        tag_key = _topic_tag_cluster_key(row)
        tag_candidates = [
            cluster
            for cluster in grouped
            if cluster["product_type"] == product_type
            and tag_key
            and tag_key == _topic_tag_cluster_key(cluster["representative_row"])
        ]
        for cluster in tag_candidates:
            representative = cluster["representative_row"]
            if _cluster_has_topic_merge_conflict(row, cluster["rows"]):
                continue
            if not (_has_high_confidence_topic_signal(row) and _has_high_confidence_topic_signal(representative)):
                continue
            similarity = _cosine_similarity(vector.tolist(), cluster["centroid"].tolist())
            _append_to_semantic_cluster(cluster, row, vector, similarity)
            meta["mimo_tag_auto_merged"] += 1
            row.update(
                {
                    "_聚类决策": "模型标签一致合并",
                    "_聚类候选相似度": round(similarity, 4),
                    "_聚类裁决提供方": "mimo-topic-signal",
                    "_聚类裁决原因": "两条会话由模型独立标注为相同规范标签，且标签置信度均不低于 0.8。",
                    "_聚类裁决置信度": min(
                        float(row.get("语义标注置信度", 0.0)),
                        float(representative.get("语义标注置信度", 0.0)),
                    ),
                }
            )
            merged = True
            break
        if merged:
            continue

        ranked_candidates = _rank_semantic_cluster_candidates(vector, grouped, product_type)
        for cluster_index, similarity in ranked_candidates[:MAX_CLUSTER_REVIEW_CANDIDATES]:
            if similarity < floor:
                break
            final_similarity = similarity
            cluster = grouped[cluster_index]
            representative = cluster["representative_row"]
            conflict = _cluster_has_topic_merge_conflict(row, cluster["rows"])
            if conflict:
                meta["mimo_hard_rule_rejected"] += 1
                final_decision = "业务硬规则冲突后新建主题"
                final_provider = "business-rule"
                final_reason = (
                    "候选与主题簇成员在品类、知识分类、标准路径，"
                    "或核心处理目标上存在硬冲突，禁止交给相似度强行合并。"
                )
                continue
            if (
                similarity >= auto_threshold
                and tag_key
                and tag_key == _topic_tag_cluster_key(representative)
            ):
                _append_to_semantic_cluster(cluster, row, vector, similarity)
                meta["mimo_auto_merged"] += 1
                row.update(
                    {
                        "_聚类决策": "高置信自动合并",
                        "_聚类候选相似度": round(similarity, 4),
                        "_聚类裁决提供方": "embedding",
                        "_聚类裁决原因": "标签聚类键一致、相似度达到自动合并阈值，且模型特征无明显冲突。",
                        "_聚类裁决置信度": "",
                    }
                )
                merged = True
                break
            if meta["mimo_review_calls"] >= limit:
                meta["mimo_review_limit_reached"] += 1
                final_decision = "裁决上限后新建主题"
                final_provider = "mimo-limit"
                final_reason = "本次大模型聚类裁决已达到调用上限，保守地不合并。"
                break
            meta["mimo_review_calls"] += 1
            try:
                if hasattr(reviewer, "review_cluster_membership"):
                    review = reviewer.review_cluster_membership(
                        _cluster_validation_payload(row),
                        [
                            _cluster_membership_member_payload(member)
                            for member in cluster["rows"]
                        ],
                        similarity,
                        threshold,
                    ).candidate
                else:
                    review = reviewer.review_cluster_pair(
                        _cluster_validation_payload(row),
                        _cluster_validation_payload(representative),
                        similarity,
                        threshold,
                    ).candidate
            except MimoError:
                meta["mimo_review_failed"] += 1
                final_decision = "模型失败后新建主题"
                final_provider = "mimo"
                final_reason = "大模型聚类裁决调用失败，保守地不合并。"
                continue
            decision = _clean_text(review.get("decision"))
            if decision == "同一主题":
                _append_to_semantic_cluster(cluster, row, vector, similarity)
                meta["mimo_review_approved"] += 1
                row.update(
                    {
                        "_聚类决策": "大模型确认合并",
                        "_聚类候选相似度": round(similarity, 4),
                        "_聚类裁决提供方": "mimo",
                        "_聚类裁决原因": _clean_text(review.get("reason")),
                        "_聚类裁决置信度": review.get("confidence", ""),
                    }
                )
                merged = True
                break
            if decision == "不同主题":
                meta["mimo_review_rejected"] += 1
                final_decision = "大模型拒绝后新建主题"
                final_provider = "mimo"
                final_reason = _clean_text(review.get("reason")) or "大模型判断候选主题不同。"
                final_confidence = review.get("confidence", "")
            else:
                meta["mimo_review_uncertain"] += 1
                final_decision = "大模型不确定后新建主题"
                final_provider = "mimo"
                final_reason = _clean_text(review.get("reason")) or "大模型无法确认是否属于同一主题。"
                final_confidence = review.get("confidence", "")
        if merged:
            continue
        row.update(
            {
                "_聚类决策": final_decision,
                "_聚类候选相似度": round(final_similarity, 4) if final_similarity else "",
                "_聚类裁决提供方": final_provider,
                "_聚类裁决原因": final_reason,
                "_聚类裁决置信度": final_confidence,
            }
        )
        grouped.append(
            {
                "product_type": product_type,
                "rows": [row],
                "centroid": vector,
                "representative_row": row,
                "representative_vector": vector,
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
        result.append(
            (
                ("semantic_mimo", cluster["product_type"], f"cluster-{index}", *source_ids),
                cluster_rows,
            )
        )
        min_similarity_values.append(round(float(cluster["min_similarity"]), 4))
    meta.update(
        {
            "provider": "mimo-topic-signal+embedding-recall+mimo-cluster-gate",
            "model": embedding_client.config.model,
            "cluster_count": len(result),
            "min_similarity": min(min_similarity_values) if min_similarity_values else None,
        }
    )
    return result, meta


def _cluster_validation_payload(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: _clean_text(row.get(field))
        for field in (
            "数据ID",
            "工单ID",
            "核心问题",
            "聊天内容",
            "图片链接",
            "视频链接",
            "图片处理状态",
            "图片证据摘要",
            "视频处理状态",
            "判定结论",
            "判定依据",
            "产品类型",
            "一级分类",
            "二级分类",
            "模型主题一级分类",
            "模型主题二级分类",
            "主题标签",
            "标签聚类键",
            "语义标注依据",
            "语义标注置信度",
            "语义标注图片必要性",
            "问题意图",
            "对象/部位",
            "异常现象",
            "解题方式",
            "主标准路径",
        )
        if _clean_text(row.get(field))
    }


def _cluster_membership_member_payload(row: dict[str, Any]) -> dict[str, str]:
    """Keep every cluster member auditable without repeating full conversation transcripts."""
    return {
        field: _clean_text(row.get(field))
        for field in (
            "数据ID",
            "工单ID",
            "产品类型",
            "机型",
            "核心问题",
            "模型主题一级分类",
            "模型主题二级分类",
            "问题意图",
            "对象/部位",
            "异常现象",
            "解题方式",
            "主标准路径",
            "主题标签",
            "语义标注依据",
            "图片证据摘要",
            "视频处理状态",
        )
        if _clean_text(row.get(field))
    }


def _cluster_validation_record_fields(
    prefix: str,
    row: dict[str, Any],
    record_id: str,
) -> dict[str, Any]:
    return {
        f"{prefix}_ID": record_id,
        f"{prefix}_工单ID": _clean_text(row.get("工单ID")),
        f"{prefix}_核心问题": _clean_text(row.get("核心问题")),
        f"{prefix}_聊天内容": _clean_text(row.get("聊天内容")),
        f"{prefix}_图片链接": _clean_text(row.get("图片链接")),
        f"{prefix}_视频链接": _clean_text(row.get("视频链接")),
        f"{prefix}_图片处理状态": _clean_text(row.get("图片处理状态")),
        f"{prefix}_图片证据摘要": _clean_text(row.get("图片证据摘要")),
        f"{prefix}_视频处理状态": _clean_text(row.get("视频处理状态")),
        f"{prefix}_图片必要性": _clean_text(row.get("语义标注图片必要性")),
        f"{prefix}_主题标签": _clean_text(row.get("主题标签")),
        f"{prefix}_语义标注依据": _clean_text(row.get("语义标注依据")),
        f"{prefix}_一级分类": _clean_text(row.get("一级分类")),
        f"{prefix}_二级分类": _clean_text(row.get("二级分类")),
    }


def _cluster_validation_record_id(row: dict[str, Any], fallback: int) -> str:
    return (
        _clean_text(row.get("数据ID"))
        or _clean_text(row.get("来源记录ID"))
        or _clean_text(row.get("工单ID"))
        or f"ROW-{fallback:04d}"
    )


def _push_bounded_candidate(
    heap: list[tuple[Any, ...]],
    item: tuple[Any, ...],
    limit: int,
) -> None:
    heappush(heap, item)
    if len(heap) > limit:
        heappop(heap)


def _select_cluster_validation_pairs(
    rows: list[dict[str, Any]],
    vectors: list[list[float]],
    assignments: dict[int, int],
    threshold: float,
    pair_limit: int,
    boundary_margin: float,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    row_count = len(rows)
    product_counts: dict[str, int] = {}
    for row in rows:
        product_type = _clean_text(row.get("产品类型"))
        product_counts[product_type] = product_counts.get(product_type, 0) + 1
    candidate_pair_count = sum(
        count * (count - 1) // 2
        for count in product_counts.values()
    )
    if row_count < 2 or not candidate_pair_count:
        return [], candidate_pair_count

    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    normalized = np.divide(
        matrix,
        norms,
        out=np.zeros_like(matrix),
        where=norms > 0,
    )
    cluster_ids = np.asarray(
        [assignments.get(id(row), -1) for row in rows],
        dtype=np.int32,
    )
    product_types = np.asarray(
        [_clean_text(row.get("产品类型")) for row in rows],
        dtype=object,
    )

    pool_limit = max(64, pair_limit * 6)
    same_low_heap: list[tuple[Any, ...]] = []
    cross_high_heap: list[tuple[Any, ...]] = []
    boundary_heap: list[tuple[Any, ...]] = []
    max_similarity_cells = 1_000_000
    block_size = max(1, min(256, max_similarity_cells // row_count))

    for block_start in range(0, row_count, block_size):
        block_end = min(row_count, block_start + block_size)
        block_scores = normalized[block_start:block_end] @ normalized.T
        for local_index, left_index in enumerate(range(block_start, block_end)):
            right_indices = np.arange(left_index + 1, row_count)
            if not right_indices.size:
                continue
            product_mask = product_types[right_indices] == product_types[left_index]
            right_indices = right_indices[product_mask]
            if not right_indices.size:
                continue
            similarities = block_scores[local_index, right_indices]
            same_mask = cluster_ids[right_indices] == cluster_ids[left_index]

            same_positions = np.flatnonzero(same_mask)
            if same_positions.size:
                take = min(pool_limit, int(same_positions.size))
                local_take = np.argpartition(
                    similarities[same_positions],
                    take - 1,
                )[:take]
                for position in same_positions[local_take]:
                    similarity = float(similarities[position])
                    _push_bounded_candidate(
                        same_low_heap,
                        (-similarity, left_index, int(right_indices[position])),
                        pool_limit,
                    )

            cross_positions = np.flatnonzero(~same_mask)
            if cross_positions.size:
                take = min(pool_limit, int(cross_positions.size))
                cross_scores = similarities[cross_positions]
                local_take = np.argpartition(cross_scores, -take)[-take:]
                for position in cross_positions[local_take]:
                    similarity = float(similarities[position])
                    _push_bounded_candidate(
                        cross_high_heap,
                        (similarity, left_index, int(right_indices[position])),
                        pool_limit,
                    )

            take = min(pool_limit, int(similarities.size))
            distances = np.abs(similarities - threshold)
            local_take = np.argpartition(distances, take - 1)[:take]
            for position in local_take:
                similarity = float(similarities[position])
                same_cluster = bool(same_mask[position])
                _push_bounded_candidate(
                    boundary_heap,
                    (
                        -abs(similarity - threshold),
                        left_index,
                        int(right_indices[position]),
                        similarity,
                        same_cluster,
                    ),
                    pool_limit,
                )
        if progress_callback:
            progress_callback("pair_sampling", block_end, row_count)

    def pair_payload(
        left_index: int,
        right_index: int,
        similarity: float,
        same_cluster: bool,
    ) -> dict[str, Any]:
        return {
            "left": rows[left_index],
            "right": rows[right_index],
            "left_index": left_index,
            "right_index": right_index,
            "similarity": similarity,
            "same_cluster": same_cluster,
        }

    same_candidates = sorted(
        (
            pair_payload(left, right, -negative_score, True)
            for negative_score, left, right in same_low_heap
        ),
        key=lambda pair: pair["similarity"],
    )
    cross_candidates = sorted(
        (
            pair_payload(left, right, similarity, False)
            for similarity, left, right in cross_high_heap
        ),
        key=lambda pair: pair["similarity"],
        reverse=True,
    )
    boundary_candidates = sorted(
        (
            pair_payload(left, right, similarity, same_cluster)
            for _negative_distance, left, right, similarity, same_cluster in boundary_heap
        ),
        key=lambda pair: abs(pair["similarity"] - threshold),
    )

    selected: list[dict[str, Any]] = []
    selected_ids: set[tuple[int, int]] = set()

    def append_pair(pair: dict[str, Any]) -> bool:
        pair_id = (pair["left_index"], pair["right_index"])
        if pair_id in selected_ids or len(selected) >= pair_limit:
            return False
        selected.append(pair)
        selected_ids.add(pair_id)
        return True

    same_limit = pair_limit // 2
    cross_limit = pair_limit - same_limit
    same_added = 0
    for pair in same_candidates:
        if same_added >= same_limit:
            break
        if append_pair(pair):
            same_added += 1

    cross_added = 0
    for pair in cross_candidates:
        if cross_added >= cross_limit:
            break
        if pair["similarity"] < max(0.0, threshold - boundary_margin):
            continue
        if append_pair(pair):
            cross_added += 1

    for pair in [*boundary_candidates, *same_candidates, *cross_candidates]:
        if len(selected) >= pair_limit:
            break
        append_pair(pair)

    selected.sort(key=lambda pair: abs(pair["similarity"] - threshold))
    return selected, candidate_pair_count


def build_cluster_validation_rows(
    feature_rows: list[dict[str, Any]],
    semantic_threshold: float = 0.84,
    max_pairs: int = 20,
    boundary_margin: float = 0.08,
    embedding_client: EmbeddingClient | None = None,
    use_mimo: bool = True,
    mimo_client: MimoClient | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    threshold = max(0.0, min(float(semantic_threshold), 1.0))
    pair_limit = max(2, min(int(max_pairs), 200))
    eligible_rows = [row for row in feature_rows if _topic_evidence(row)[1]]
    semantic_client = embedding_client or EmbeddingClient.from_env()
    if semantic_client is None:
        raise EmbeddingError("EMBEDDING_BASE_URL or EMBEDDING_MODEL is not configured")
    texts = [_topic_semantic_text(row) for row in eligible_rows]
    if progress_callback:
        progress_callback("embedding", 0, len(texts))
        vectors = semantic_client.embed_texts(
            texts,
            progress_callback=lambda completed, total: progress_callback(
                "embedding",
                completed,
                total,
            ),
        )
    else:
        vectors = semantic_client.embed_texts(texts)
    if progress_callback:
        progress_callback("clustering", 0, len(eligible_rows))
    _groups, clustering_meta, assignments = _semantic_topic_groups_from_vectors(
        eligible_rows,
        vectors,
        threshold,
    )
    if progress_callback:
        progress_callback("clustering", len(eligible_rows), len(eligible_rows))
    selected, candidate_pair_count = _select_cluster_validation_pairs(
        eligible_rows,
        vectors,
        assignments,
        threshold,
        pair_limit,
        boundary_margin,
        progress_callback=progress_callback,
    )

    reviewer = mimo_client if use_mimo else None
    if reviewer is None and use_mimo:
        reviewer = MimoClient.from_env()
    validation_rows: list[dict[str, Any]] = []
    for pair_index, pair in enumerate(selected, start=1):
        left = pair["left"]
        right = pair["right"]
        left_id = _cluster_validation_record_id(left, pair["left_index"] + 1)
        right_id = _cluster_validation_record_id(right, pair["right_index"] + 1)
        pair_digest = hashlib.sha1(
            f"{left_id}|{right_id}|{threshold:.4f}".encode("utf-8")
        ).hexdigest()[:10].upper()
        model_review: dict[str, Any] = {}
        model_status = "未调用"
        model_name = ""
        if reviewer and hasattr(reviewer, "review_cluster_pair"):
            model_name = reviewer.config.model
            try:
                review_result = reviewer.review_cluster_pair(
                    _cluster_validation_payload(left),
                    _cluster_validation_payload(right),
                    float(pair["similarity"]),
                    threshold,
                )
                model_review = review_result.candidate
                model_status = "已标注"
            except MimoError as exc:
                model_review = {"reason": str(exc)}
                model_status = "标注失败"
        elif use_mimo:
            model_status = "未配置 MiMo"

        validation_rows.append(
            {
                "验证对ID": f"PAIR-{pair_digest}",
                "样本类型": "同簇低相似边界" if pair["same_cluster"] else "跨簇高相似边界",
                "聚类预测": "同一主题" if pair["same_cluster"] else "不同主题",
                "语义相似度": round(float(pair["similarity"]), 4),
                "聚类阈值": round(threshold, 4),
                **_cluster_validation_record_fields("记录A", left, left_id),
                **_cluster_validation_record_fields("记录B", right, right_id),
                "大模型判断": _clean_text(model_review.get("decision")),
                "大模型主题": _clean_text(model_review.get("topic_label")),
                "大模型原因": _clean_text(model_review.get("reason")),
                "大模型关键差异": _clean_text(model_review.get("key_difference")),
                "大模型置信度": model_review.get("confidence", ""),
                "大模型名称": model_name,
                "大模型Prompt版本": CLUSTER_PAIR_REVIEW_PROMPT_VERSION if reviewer else "",
                "大模型状态": model_status,
                "人工判断": "",
                "人工错误类型": "",
                "人工备注": "",
                "审核人": "",
                "审核时间": "",
            }
        )
        if progress_callback:
            progress_callback("large_model", pair_index, len(selected))

    clustering_meta.update(
        {
            "embedding_model": semantic_client.config.model,
            "eligible_rows": len(eligible_rows),
            "candidate_pairs": candidate_pair_count,
            "validation_pairs": len(validation_rows),
            "same_cluster_pairs": sum(row["聚类预测"] == "同一主题" for row in validation_rows),
            "cross_cluster_pairs": sum(row["聚类预测"] == "不同主题" for row in validation_rows),
            "large_model_enabled": bool(reviewer),
        }
    )
    return validation_rows, clustering_meta


def evaluate_cluster_validation_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [
        row
        for row in rows
        if _clean_text(row.get("人工判断")) in {"同一主题", "不同主题", "不确定"}
    ]
    decisive = [
        row
        for row in reviewed
        if _clean_text(row.get("人工判断")) in {"同一主题", "不同主题"}
    ]
    clustering_correct = sum(
        _clean_text(row.get("聚类预测")) == _clean_text(row.get("人工判断"))
        for row in decisive
    )
    model_labeled = [
        row
        for row in decisive
        if _clean_text(row.get("大模型判断")) in {"同一主题", "不同主题"}
    ]
    model_correct = sum(
        _clean_text(row.get("大模型判断")) == _clean_text(row.get("人工判断"))
        for row in model_labeled
    )
    predicted_same = [row for row in decisive if _clean_text(row.get("聚类预测")) == "同一主题"]
    predicted_different = [row for row in decisive if _clean_text(row.get("聚类预测")) == "不同主题"]
    false_merge = [
        row
        for row in predicted_same
        if _clean_text(row.get("人工判断")) == "不同主题"
    ]
    false_split = [
        row
        for row in predicted_different
        if _clean_text(row.get("人工判断")) == "同一主题"
    ]
    uncertain = [
        row
        for row in reviewed
        if _clean_text(row.get("人工判断")) == "不确定"
    ]
    return {
        "total_pairs": len(rows),
        "reviewed_pairs": len(reviewed),
        "pending_pairs": len(rows) - len(reviewed),
        "uncertain_pairs": len(uncertain),
        "decisive_pairs": len(decisive),
        "clustering_correct": clustering_correct,
        "clustering_accuracy": round(clustering_correct / len(decisive), 4) if decisive else None,
        "large_model_labeled_pairs": len(model_labeled),
        "large_model_correct": model_correct,
        "large_model_accuracy": round(model_correct / len(model_labeled), 4) if model_labeled else None,
        "predicted_same_pairs": len(predicted_same),
        "predicted_same_correct": sum(
            _clean_text(row.get("人工判断")) == "同一主题"
            for row in predicted_same
        ),
        "predicted_different_pairs": len(predicted_different),
        "predicted_different_correct": sum(
            _clean_text(row.get("人工判断")) == "不同主题"
            for row in predicted_different
        ),
        "false_merge_pairs": len(false_merge),
        "false_merge_rate": round(len(false_merge) / len(predicted_same), 4) if predicted_same else None,
        "false_split_pairs": len(false_split),
        "false_split_rate": round(len(false_split) / len(predicted_different), 4)
        if predicted_different
        else None,
    }


def cluster_validation_from_workbook(
    source_path: str | Path,
    product_type: str | None = None,
    semantic_threshold: float = 0.84,
    max_pairs: int = 20,
    use_mimo: bool = True,
    embedding_client: EmbeddingClient | None = None,
    mimo_client: MimoClient | None = None,
    image_downloader: ImageDownloader | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_rows = _read_source_rows(source_path)
    selected_rows, excluded_rows = filter_source_rows_by_product_type(source_rows, product_type)
    preprocessed_rows = preprocess_source_rows(selected_rows)
    eligible_rows, validation_excluded_rows = filter_preprocessed_rows_for_model(preprocessed_rows)
    excluded_rows.extend(validation_excluded_rows)
    reviewer = mimo_client if use_mimo else None
    if reviewer is None and use_mimo:
        reviewer = MimoClient.from_env()
    feature_rows, _run_id = generate_phone_candidate_rows(
        eligible_rows,
        [],
        use_mimo=use_mimo,
        mimo_client=reviewer,
        image_downloader=image_downloader,
        progress_callback=progress_callback,
    )
    validation_rows, summary = build_cluster_validation_rows(
        feature_rows,
        semantic_threshold=semantic_threshold,
        max_pairs=max_pairs,
        embedding_client=embedding_client,
        use_mimo=use_mimo,
        mimo_client=reviewer,
        progress_callback=progress_callback,
    )
    summary.update(
        {
            "source_total_rows": len(source_rows),
            "selected_rows": len(selected_rows),
            "eligible_rows": len(eligible_rows),
            "excluded_rows": len(excluded_rows),
            "conversation_signal_model_enabled": bool(reviewer),
        }
    )
    return validation_rows, summary


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
        historical_reply = _historical_actual_reply(row)
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
                f"历史实际回复：{historical_reply[:240]}" if historical_reply else "",
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
    if any(_clean_text(row.get("语义标注图片必要性")) == "需要" for row in rows):
        return True
    text = " ".join(
        _clean_text(row.get(field))
        for row in rows
        for field in ("聊天内容", "异常现象", "问题意图", "解题方式", "主题标签")
    )
    markers = ("图片", "照片", "外观", "显示", "坏点", "磕点", "划痕", "拆修", "胶", "颜色", "裂")
    return bool(_topic_image_links(rows) and any(marker in text for marker in markers))


def _topic_query(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = rows[0]
    questions = [
        _semantic_excerpt(row.get("聊天内容"), 240) or _clean_text(row.get("核心问题"))
        for row in rows[:5]
    ]
    return {
        "产品类型": _clean_text(base.get("产品类型")),
        "一级分类": _clean_text(base.get("模型主题一级分类")) or _clean_text(base.get("一级分类")),
        "二级分类": _clean_text(base.get("模型主题二级分类")) or _clean_text(base.get("二级分类")),
        "核心问题": "；".join(question for question in questions if question),
        "历史实际回复": _merge_unique_text(
            [_historical_actual_reply(row) for row in rows[:5]],
            separator="\n",
        ),
        "判定依据": _merge_unique_text([row.get("语义标注依据") for row in rows[:5]], separator="；"),
        "问题意图": _clean_text(base.get("问题意图")),
        "对象/部位": _clean_text(base.get("对象/部位")),
        "异常现象": _clean_text(base.get("异常现象")),
        "解题方式": _clean_text(base.get("解题方式")),
        "标准关键词": _merge_unique_keywords([row.get("主题标签") for row in rows]),
    }


def _topic_rule_draft(
    topic_id: str,
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool = True,
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
    concrete_allowed = (
        (bool(matches) if use_standard_references else True)
        and _has_explicit_boundary_case(text)
        and not any(marker in text for marker in UNCERTAINTY_MARKERS)
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
            use_standard_references=use_standard_references,
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
        "standard_refs": (
            [_standard_reference(item) for item, _score in matches[:1]]
            if use_standard_references
            else []
        ),
        "applicable_scope": _safe_join(
            [_clean_text(query.get("产品类型")), standard.scope if standard else ""], "；"
        ),
        "confidence": 0.72 if concrete_allowed else 0.45,
        "reasoning_summary": (
            (
                "主题仅聚合明确边界问题，且命中可信标准。"
                if use_standard_references
                else "主题案例证据清楚、一致，可形成待人工审核的案例型知识候选。"
            )
            if concrete_allowed
            else "按主题证据沉淀通用核验流程；不将单个工单的个案结论外推。"
        ),
        "needs_human_review": not concrete_allowed,
        "image_evidence_summary": _topic_evidence_summary(rows),
        "requires_images": _topic_needs_images(rows),
        "image_usage_instruction": (
            (
                "保留现场图片，辅助说明需要核验的部位、现象和标准边界。"
                if use_standard_references
                else "保留脱敏案例图，辅助说明问题部位、现象和处理情形。"
            )
            if _topic_needs_images(rows)
            else "文字已足以表达规则，不需要保留图片。"
        ),
        "topic_id": topic_id,
    }


def _candidate_contains_standard_reference(candidate: dict[str, Any]) -> bool:
    if candidate.get("standard_refs"):
        return True
    text = " ".join(
        _clean_text(candidate.get(field))
        for field in (
            "title",
            "subtitles",
            "content",
            "recommended_reply",
            "reasoning_summary",
            "image_evidence_summary",
            "image_usage_instruction",
        )
    )
    return bool(
        re.search(
            r"(质检标准|回收标准|平台标准|标准编号|标准条款|标准项|关联标准|引用标准|"
            r"(?:依据|按照|对照|参照).{0,24}标准|STD[-_：:]|【[^】]+】\s*-\s*【[^】]+】)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _topic_requires_process(
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
    candidate: dict[str, Any],
    use_standard_references: bool = True,
) -> bool:
    text = " ".join(
        _clean_text(row.get(field))
        for row in rows
        for field in ("核心问题", "判定结论", "判定依据", "聊天内容", "异常现象", "解题方式")
    )
    return (
        (use_standard_references and not matches)
        or any(marker in text for marker in UNCERTAINTY_MARKERS)
        or not _has_explicit_boundary_case(text)
        or (
            use_standard_references
            and
            candidate.get("knowledge_form") == "具体判定"
            and not candidate.get("standard_refs")
        )
    )


def _compact_knowledge_content(value: Any, limit: int = 650) -> str:
    text = _normalize_lines(value)
    replacements = {
        "转人工确认": "补充证据后再判定",
        "转人工复核": "补充证据后再判定",
        "转人工": "补充证据后再判定",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"(?<!\n)\s*(?=\d+[.、])", "\n", text)

    def clip_line(line: str, line_limit: int = 170) -> str:
        if len(line) <= line_limit:
            return line
        head = line[:line_limit]
        boundary = max(head.rfind(marker) for marker in ("。", "；", ";", "，", ","))
        return (head[: boundary + 1] if boundary >= 40 else head).rstrip()

    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or line in lines:
            continue
        lines.append(clip_line(line))
        if len(lines) >= 6:
            break
    selected: list[str] = []
    used = 0
    for line in lines:
        remaining = limit - used - (1 if selected else 0)
        if remaining <= 0:
            break
        clipped = clip_line(line, remaining)
        if not clipped:
            break
        selected.append(clipped)
        used += len(clipped) + (1 if len(selected) > 1 else 0)
    return "\n".join(selected).rstrip("；;，, ")


def _topic_product_type(
    query: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    product_type = query.get("产品类型编码") or query.get("产品类型")
    category = resolve_product_category(product_type)
    if category:
        return category.name
    for row in rows:
        candidate = row.get("产品类型编码") or row.get("产品类型")
        category = resolve_product_category(candidate)
        if category:
            return category.name
    return UNKNOWN_PRODUCT_NAME


def _normalized_applicable_scope(
    product_type: str,
    candidate_scope: Any,
    rows: list[dict[str, Any]],
) -> str:
    scope = _clean_text(candidate_scope)
    normalized_scope = normalize_product_scope(product_type, scope)
    if scope and normalized_scope != f"{product_type}-通用":
        return normalized_scope
    evidence = " ".join(
        [
            scope,
            *[
                _clean_text(row.get(field))
                for row in rows
                for field in ("聊天内容", "核心问题", "机型", "主题标签", "主标准路径")
            ],
        ]
    ).lower()
    has_apple = any(marker in evidence for marker in ("苹果", "iphone", "ipad", "ios", "watchos"))
    has_android = any(marker in evidence for marker in ("安卓", "android", "鸿蒙", "harmony"))
    platform = "苹果" if has_apple and not has_android else "安卓" if has_android and not has_apple else "通用"
    return normalize_product_scope(product_type, platform)


def _recommended_reply(
    title: str,
    content: str,
    existing_reply: str = "",
    use_standard_references: bool = True,
) -> str:
    source_content = _compact_knowledge_content(existing_reply or content, limit=500)
    points = [
        re.sub(r"^\s*(?:[-•]|\d+[.、])\s*", "", line).strip()
        for line in source_content.splitlines()
        if line.strip()
        and not line.endswith("：")
        and not line.startswith("适用主题")
    ][:3]
    short_points = []
    for point in points:
        if len(point) > 56:
            head = point[:56]
            boundary = max(head.rfind(marker) for marker in ("。", "；", "，", ","))
            point = head[: boundary + 1] if boundary >= 20 else head
        short_points.append(point.rstrip("。；;"))
    body = "；".join(short_points)
    if not body:
        body = (
            "请先确认具体对象和现象，补充必要图片、截图或检测结果，并对照当前有效标准。"
            if use_standard_references
            else "请先确认具体对象和现象，补充必要图片、截图或检测结果，再结合当前情况处理。"
        )
    closing = (
        "如现有证据不能对应标准，请补充证据后再判定。"
        if use_standard_references
        else "如当前情况与案例证据不一致，请补充信息后再处理。"
    )
    reply = f"您好，关于“{title}”，建议{body}。{closing}"
    if len(reply) <= 180:
        return reply
    head = reply[:180]
    boundary = max(head.rfind(marker) for marker in ("。", "；", "，", ","))
    return (head[: boundary + 1] if boundary >= 80 else head).rstrip()


def _compact_recommended_reply(value: Any) -> str:
    reply = re.sub(r"\s+", " ", _clean_text(value)).strip()
    reply = (
        reply.replace("转人工确认", "补充证据后再判定")
        .replace("转人工复核", "补充证据后再判定")
        .replace("转人工", "补充证据后再判定")
    )
    if len(reply) <= 180:
        return reply
    head = reply[:180]
    boundary = max(head.rfind(marker) for marker in ("。", "；", "，", ","))
    return (head[: boundary + 1] if boundary >= 80 else head).rstrip()


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
    use_standard_references: bool = True,
) -> dict[str, Any]:
    query = _topic_query(rows)
    if _topic_requires_process(
        rows,
        matches,
        candidate,
        use_standard_references=use_standard_references,
    ):
        candidate = _topic_rule_draft(
            topic_id,
            rows,
            matches,
            use_standard_references=use_standard_references,
        )
        candidate["needs_human_review"] = True
        candidate["confidence"] = min(float(candidate["confidence"]), 0.45)
        downgrade_reason = (
            "主题缺少可外推的明确边界证据或可信标准，已强制降级为流程方法。"
            if use_standard_references
            else "主题缺少可外推的明确边界证据，已强制降级为流程方法。"
        )
        model_error = _safe_join([model_error, downgrade_reason], "；")
    preserved_standard_refs = _merge_unique_text(
        [row.get("关联标准项") for row in rows],
        separator="；",
    )
    preserved_source_versions = _merge_unique_text(
        [row.get("来源版本") for row in rows],
        separator="；",
    )
    preserved_topic_standard_versions = _merge_unique_text(
        [row.get("主题标准版本") for row in rows],
        separator="；",
    )
    standard_refs = (
        _format_model_refs(candidate.get("standard_refs", []), matches)
        if use_standard_references
        else preserved_standard_refs
    )
    standard = matches[0][0] if use_standard_references and matches else None
    matched_existing = bool(standard and standard.knowledge_type == "已有知识")
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
        (
            "保留现场图片，作为部位、现象和标准边界的辅助说明。"
            if use_standard_references
            else "保留脱敏案例图，作为问题部位、现象和处理情形的辅助说明。"
        )
        if requires_images else "文字已足以说明处理方法，不需要保留图片。"
    )
    levels = list(dict.fromkeys(_topic_evidence(row)[0] for row in rows))
    confidence = float(candidate.get("confidence", 0.0))
    needs_review = (
        candidate.get("needs_human_review", False)
        or (use_standard_references and not matches)
        or (use_standard_references and not candidate.get("standard_refs"))
        or confidence < min_confidence
        or candidate.get("knowledge_form") != "具体判定"
    )
    product_type = _topic_product_type(query, rows)
    content = _compact_knowledge_content(
        standard.response_snippet if matched_existing and standard else candidate.get("content")
    )
    title = (
        _clean_text(standard.title)
        if matched_existing and standard and _clean_text(standard.title)
        else _clean_text(candidate.get("title"))
    )
    applicable_scope = _normalized_applicable_scope(
        product_type,
        standard.scope if matched_existing and standard else candidate.get("applicable_scope"),
        rows,
    )
    recommended_reply = _compact_recommended_reply(candidate.get("recommended_reply"))
    if not recommended_reply:
        historical_reply = _clean_text(query.get("历史实际回复"))
        if not use_standard_references and "标准" in historical_reply:
            historical_reply = ""
        recommended_reply = _recommended_reply(
            title,
            content,
            (
                standard.response_snippet
                if matched_existing and standard
                else historical_reply
            ),
            use_standard_references=use_standard_references,
        )
    keywords = _merge_unique_keywords(
        [
            _clean_text(query.get("问题意图")),
            _clean_text(query.get("对象/部位")),
            _clean_text(query.get("异常现象")),
            _clean_text(query.get("标准关键词")) if use_standard_references else "",
            *[_clean_text(row.get("主题标签")) for row in rows],
        ]
    )
    illustration = "\n".join(image_links) if requires_images else ""
    return {
        "主题ID": topic_id,
        "知识ID": topic_id,
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
        "主题图片链接": illustration,
        "主题图片必要性": "需要保留" if requires_images else "不需要保留",
        "主题图片说明": image_note,
        "主题检索标准Top5": (
            _format_retrieved_standards(matches)
            if matches
            else "未搜索到相关知识（待人工补充）"
            if use_standard_references
            else ""
        ),
        "主题标准版本": (
            "\n".join(
                f"{_standard_reference(item)}:{item.version}"
                for item, _score in matches
                if _standard_reference(item)
            )
            if use_standard_references
            else preserved_topic_standard_versions or preserved_source_versions
        ),
        "主题置信度": round(confidence, 3),
        "是否重点复核": "是" if needs_review else "否",
        "主题模型提供方": provider,
        "主题模型名称": model_name,
        "主题Prompt版本": prompt_version,
        "主题模型运行ID": model_run_id,
        "主标题": title,
        "副标题": "\n".join(candidate.get("subtitles", [])),
        "知识内容": content,
        "图例": illustration,
        "知识分类": "检测方法" if candidate.get("knowledge_form") == "流程方法" else "场景判定",
        "知识来源": (
            "已有知识优先匹配"
            if matched_existing
            else "方向二主题候选"
            if use_standard_references
            else "方向二案例沉淀"
        ),
        "关联标准项": standard_refs,
        "适用范围": applicable_scope,
        "生效状态": "待审核",
        "来源版本": (
            standard.version
            if standard and standard.version
            else preserved_source_versions
        ),
        "变更类型": "修改" if matched_existing else "新增",
        "失效原因": "",
        "检索关键词": keywords,
        "关键词": keywords,
        "校验备注": _safe_join(
            [
                f"主题聚合样本数：{len(rows)}",
                f"主题知识形态：{candidate.get('knowledge_form', '流程方法')}",
                f"已有知识优先匹配：{standard.title}" if matched_existing and standard else "",
                _clean_text(candidate.get("reasoning_summary")),
                "无可信标准，待人工补充。"
                if use_standard_references and not matches
                else "无标准引用模式：仅依据第二部分案例证据生成。"
                if not use_standard_references
                else "",
                model_error,
            ],
            "；",
        ),
        "推荐回复": recommended_reply,
        "是否值得沉淀": "",
        "是否可用": "",
        "如何修改": "",
        "问题反馈": "",
        **{field: "" for field in TOPIC_MODEL_INITIAL_REVIEW_COLUMNS},
        **{field: "" for field in TOPIC_REVIEW_COLUMNS},
        "流程状态": "review_pending",
        "模型阶段状态": stage_status,
    }


def _rule_topic_initial_review(
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool = True,
) -> dict[str, Any]:
    title = _clean_text(topic.get("主标题"))
    content = _clean_text(topic.get("知识内容"))
    refs = _clean_text(topic.get("关联标准项"))
    if "/" in title or "／" in title or title.count("、") >= 2:
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
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
            "knowledge_value": "待确认",
            "error_type": "标题不准" if not title else "话术不合适",
            "reason": "转写草稿缺少可审核的标题或知识正文。",
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "不足",
            "content_consistency": "不一致",
            "title_quality": "需修改" if not title else "清晰",
            "confidence": 0.94,
            "priority_review": True,
        }
    if use_standard_references and not matches:
        return {
            "decision": "证据不足待补充",
            "knowledge_value": "待确认",
            "error_type": "标准未覆盖/标准召回不足",
            "reason": "未检索到可信生效标准，不能由模型初标放行。",
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.9,
            "priority_review": True,
        }
    if use_standard_references and not refs:
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
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
            "knowledge_value": "待确认",
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
            "knowledge_value": "值得沉淀",
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
        "knowledge_value": "值得沉淀",
        "error_type": "",
        "reason": (
            "转写草稿具备主题级标题、正文、分类和可追溯标准引用，可进入人工复标。"
            if use_standard_references
            else "转写草稿具备主题级标题、正文、分类、案例证据和来源追溯，可进入人工复标。"
        ),
        "standard_consistency": "一致" if use_standard_references else "无可信标准",
        "evidence_sufficiency": "充分" if _clean_text(topic.get("主题证据等级")) == "完整会话" else "部分充分",
        "confidence": 0.76,
        "priority_review": _clean_text(topic.get("是否重点复核")) == "是",
    }


def _apply_topic_initial_review_guard(
    review: dict[str, Any],
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool = True,
) -> dict[str, Any]:
    guarded = dict(review)
    if not use_standard_references:
        standard_error_types = {
            "标准未覆盖/标准召回不足",
            "标准项映射错",
        }
        if guarded.get("error_type") in standard_error_types:
            fallback = _rule_topic_initial_review(
                topic,
                [],
                use_standard_references=False,
            )
            fallback["reason"] = _safe_join(
                [
                    _clean_text(fallback.get("reason")),
                    "本模式不使用标准引用，已忽略模型提出的标准补充要求。",
                ],
                "；",
            )
            return fallback
        guarded["standard_consistency"] = "无可信标准"
        return guarded
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
            "模型初标是否值得沉淀": _clean_text(review.get("knowledge_value"))
            or (
                "不值得沉淀"
                if _clean_text(review.get("decision")) == "驳回"
                else "待确认"
                if _clean_text(review.get("decision")) == "证据不足待补充"
                else "值得沉淀"
            ),
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
    cluster_review_floor: float = DEFAULT_CLUSTER_REVIEW_FLOOR,
    cluster_auto_merge_threshold: float = DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    cluster_review_limit: int = DEFAULT_CLUSTER_REVIEW_LIMIT,
    embedding_client: EmbeddingClient | None = None,
    clustering_meta: dict[str, Any] | None = None,
    use_standard_references: bool = True,
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
    if normalized_mode not in {"direct_mimo", "semantic", "semantic_mimo", "rule"}:
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
    if normalized_mode == "direct_mimo" and eligible_topic_rows:
        cluster_reviewer = mimo_client or (MimoClient.from_env() if use_mimo else None)
        try:
            if cluster_reviewer is None:
                raise MimoError("纯大模型聚类需要已配置 MiMo")
            topic_groups, direct_meta = _direct_mimo_topic_groups(
                eligible_topic_rows,
                cluster_reviewer,
            )
            meta.update(direct_meta)
        except MimoError as exc:
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
            meta["cluster_count"] = len(topic_groups)
    elif normalized_mode in {"semantic", "semantic_mimo"} and eligible_topic_rows:
        semantic_client = embedding_client or EmbeddingClient.from_env()
        try:
            if semantic_client is None:
                raise EmbeddingError("EMBEDDING_BASE_URL or EMBEDDING_MODEL is not configured")
            if normalized_mode == "semantic_mimo":
                cluster_reviewer = mimo_client or (MimoClient.from_env() if use_mimo else None)
                if cluster_reviewer is None:
                    raise MimoError("聚类大模型裁决需要已配置 MiMo")
                topic_groups, semantic_meta = _semantic_mimo_topic_groups(
                    eligible_topic_rows,
                    semantic_client,
                    cluster_reviewer,
                    threshold,
                    review_floor=cluster_review_floor,
                    auto_merge_threshold=cluster_auto_merge_threshold,
                    review_limit=cluster_review_limit,
                )
            else:
                topic_groups, semantic_meta = _semantic_topic_groups(
                    eligible_topic_rows,
                    semantic_client,
                    threshold,
                )
            meta.update(semantic_meta)
        except MimoError as exc:
            topic_groups, semantic_meta = _semantic_topic_groups(
                eligible_topic_rows,
                semantic_client,
                threshold,
            )
            meta.update(semantic_meta)
            meta.update(
                {
                    "effective_mode": "semantic",
                    "provider": "embedding-mimo-fallback",
                    "error": str(exc),
                }
            )
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
    auto_review_policy = AutoReviewPolicy.from_env()

    for key, rows in topic_groups:
        topic_id = _topic_id(key)
        query = _topic_query(rows)
        matches = (
            retrieve_standard_matches(query, catalog, top_k=5)
            if use_standard_references
            else []
        )
        fallback = _topic_rule_draft(
            topic_id,
            rows,
            matches,
            use_standard_references=use_standard_references,
        )
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
                label_topic = client.label_topic
                topic_payload = {
                    "topic_id": topic_id,
                    "sample_count": len(rows),
                    "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
                    "features": query,
                    "evidence_summary": _topic_evidence_summary(rows),
                }
                if "use_standard_references" in inspect.signature(label_topic).parameters:
                    result = label_topic(
                        topic_payload,
                        matches,
                        use_standard_references=use_standard_references,
                    )
                else:
                    result = label_topic(topic_payload, matches)
                model_candidate = result.candidate
                if (
                    not use_standard_references
                    and _candidate_contains_standard_reference(model_candidate)
                ):
                    raise MimoError("无标准引用模式检测到模型草稿包含标准引用，已回退为案例规则草稿")
                candidate = model_candidate
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
            use_standard_references=use_standard_references,
        )
        initial_review = _rule_topic_initial_review(
            topic,
            matches,
            use_standard_references=use_standard_references,
        )
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
        review_matches = (
            retrieve_standard_matches(review_query, catalog, top_k=5)
            if use_standard_references
            else []
        )
        topic["主题初标复核标准Top5"] = (
            _format_retrieved_standards(review_matches)
            if review_matches
            else "未搜索到相关知识（待人工补充）"
            if use_standard_references
            else ""
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
            initial_review_prompt = TOPIC_REVIEW_PROMPT_VERSION
            try:
                review_topic = client.review_topic
                review_args = [
                    {
                        "topic_id": topic_id,
                        "sample_count": len(rows),
                        "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
                        "features": query,
                        "evidence_summary": _topic_evidence_summary(rows),
                    },
                    {field: _clean_text(topic.get(field)) for field in KNOWLEDGE_MASTER_COLUMNS},
                    review_matches,
                ]
                review_parameters = inspect.signature(review_topic).parameters
                review_kwargs: dict[str, Any] = {}
                if "transcription_matches" in review_parameters:
                    review_kwargs["transcription_matches"] = matches
                if "use_standard_references" in review_parameters:
                    review_kwargs["use_standard_references"] = use_standard_references
                review_result = review_topic(*review_args, **review_kwargs)
                initial_review = review_result.candidate
                review_request_audit = review_result.request_audit
                review_response_audit = review_result.response_audit
                initial_review_status = "topic_initial_reviewed_model"
            except MimoError as exc:
                initial_review_status = "topic_initial_review_failed"
                initial_review_error = str(exc)
        else:
            initial_review_error = "未配置支持主题初标的 MiMo，使用规则模型初标。"
        initial_review = _apply_topic_initial_review_guard(
            initial_review,
            topic,
            review_matches,
            use_standard_references=use_standard_references,
        )
        _attach_topic_initial_review(
            topic,
            initial_review,
            initial_review_provider,
            initial_review_model,
            initial_review_prompt,
            initial_review_run_id,
            initial_review_status,
        )
        apply_auto_review_annotation(topic, auto_review_policy)
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
                    "历史实际回复": _historical_actual_reply(row),
                    "图片链接": _clean_text(row.get("图片链接")),
                    "图片处理状态": _clean_text(row.get("图片处理状态")),
                    "产品类型": _clean_text(row.get("产品类型")),
                    "一级分类": _clean_text(row.get("一级分类")),
                    "二级分类": _clean_text(row.get("二级分类")),
                    "模型主题一级分类": _clean_text(row.get("模型主题一级分类")),
                    "模型主题二级分类": _clean_text(row.get("模型主题二级分类")),
                    "主题标签": _clean_text(row.get("主题标签")),
                    "标签聚类键": _topic_tag_cluster_key(row),
                    "语义标注依据": _clean_text(row.get("语义标注依据")),
                    "语义标注置信度": row.get("语义标注置信度", ""),
                    "语义标注图片必要性": _clean_text(row.get("语义标注图片必要性")),
                    "语义标注提供方": _clean_text(row.get("语义标注提供方")),
                    "语义标注模型": _clean_text(row.get("语义标注模型")),
                    "语义标注Prompt版本": _clean_text(row.get("语义标注Prompt版本")),
                    "语义标注状态": _clean_text(row.get("语义标注状态")),
                    "语义标注错误": _clean_text(row.get("语义标注错误")),
                    "主标准路径": _clean_text(row.get("主标准路径")),
                    "证据等级": evidence_level,
                    "纳入主题原因": reason,
                    "聚类决策": _clean_text(row.get("_聚类决策")),
                    "聚类候选相似度": row.get("_聚类候选相似度", ""),
                    "聚类裁决提供方": _clean_text(row.get("_聚类裁决提供方")),
                    "聚类裁决原因": _clean_text(row.get("_聚类裁决原因")),
                    "聚类裁决置信度": row.get("_聚类裁决置信度", ""),
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
        {"说明": "topic_review_queue 是 1～N 个原子问题形成的主题级候选，不按固定两两配对。"},
        {"说明": "完整会话或有可用现场图片的记录可形成主题候选；证据不足的记录进入 evidence_gap_rows。"},
        {"说明": "topic_model_drafts 保存主题级转写草稿、案例图和推荐回复；模型初标不直接修改候选知识内容。"},
        {"说明": "发给组员时标注“是否值得沉淀、是否可用、如何修改、问题反馈”；不值得沉淀的纯个案知识不进入送审，这些字段同时作为模型自动审核准确率的验证金标。"},
        {"说明": "验证模式下组员标注用于计算准确率；生产自动审核启用后，模型通过候选替代第三部分人工复标，风险候选进入人工例外队列。"},
        {"说明": "无标准引用模式导出知识ID、主标题、副标题、知识内容、图例、推荐回复、知识分类、关联标准项、适用范围和关键词共10项；本流程不新增标准关联，已有值保留并单独搁置。"},
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
    cluster_review_floor: float = DEFAULT_CLUSTER_REVIEW_FLOOR,
    cluster_auto_merge_threshold: float = DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    cluster_review_limit: int = DEFAULT_CLUSTER_REVIEW_LIMIT,
    embedding_client: EmbeddingClient | None = None,
    use_standard_references: bool = True,
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
        cluster_review_floor=cluster_review_floor,
        cluster_auto_merge_threshold=cluster_auto_merge_threshold,
        cluster_review_limit=cluster_review_limit,
        embedding_client=embedding_client,
        clustering_meta=clustering_meta,
        use_standard_references=use_standard_references,
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
            "知识ID": _clean_text(topic.get("知识ID")) or _clean_text(topic.get("主题ID")),
            "图例": _clean_text(topic.get("图例")) or _clean_text(topic.get("主题图片链接")),
            "关键词": _clean_text(topic.get("关键词")) or _clean_text(topic.get("检索关键词")),
            **{field: _clean_text(topic.get(field)) for field in KNOWLEDGE_MASTER_COLUMNS},
            "推荐回复": _clean_text(topic.get("推荐回复")),
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
        "topic_signal_labeled_rows": sum(
            _clean_text(row.get("语义标注状态")) == "topic_signal_labeled"
            for row in feature_rows
        ),
        "topic_signal_fallback_rows": sum(
            _clean_text(row.get("语义标注状态")) != "topic_signal_labeled"
            for row in feature_rows
        ),
        "auto_review_enabled": AutoReviewPolicy.from_env().enabled,
        "auto_review_approved_rows": sum(
            _clean_text(row.get("自动审核状态")) == "auto_approved"
            for row in topic_rows
        ),
        "auto_review_exception_rows": sum(
            _clean_text(row.get("自动审核状态")) == "manual_exception"
            for row in topic_rows
        ),
        "clustering_requested_mode": clustering_meta.get("requested_mode", clustering_mode),
        "clustering_effective_mode": clustering_meta.get("effective_mode", clustering_mode),
        "clustering_provider": clustering_meta.get("provider", ""),
        "clustering_model": clustering_meta.get("model", ""),
        "clustering_threshold": clustering_meta.get("threshold", semantic_threshold),
        "clustering_error": clustering_meta.get("error", ""),
        "clustering_review_model": clustering_meta.get("mimo_review_model", ""),
        "clustering_review_floor": clustering_meta.get("review_floor", ""),
        "clustering_auto_merge_threshold": clustering_meta.get("auto_merge_threshold", ""),
        "clustering_review_limit": clustering_meta.get("review_limit", ""),
        "clustering_review_calls": clustering_meta.get("mimo_review_calls", 0),
        "clustering_review_approved": clustering_meta.get("mimo_review_approved", 0),
        "clustering_review_rejected": clustering_meta.get("mimo_review_rejected", 0),
        "clustering_review_uncertain": clustering_meta.get("mimo_review_uncertain", 0),
        "clustering_review_failed": clustering_meta.get("mimo_review_failed", 0),
        "clustering_auto_merged": clustering_meta.get("mimo_auto_merged", 0),
        "clustering_tag_auto_merged": clustering_meta.get("mimo_tag_auto_merged", 0),
        "clustering_review_limit_reached": clustering_meta.get("mimo_review_limit_reached", 0),
        "atomic_extraction_calls": clustering_meta.get("atomic_extraction_calls", 0),
        "atomic_extraction_failed": clustering_meta.get("atomic_extraction_failed", 0),
        "atomic_unit_count": clustering_meta.get("atomic_unit_count", 0),
        "direct_cluster_calls": clustering_meta.get("direct_cluster_calls", 0),
        "direct_cluster_failed": clustering_meta.get("direct_cluster_failed", 0),
        "direct_review_singletons": clustering_meta.get("direct_review_singletons", 0),
        "standard_references_enabled": use_standard_references,
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


def build_case_knowledge_rows(
    rows: list[dict[str, Any]],
    *,
    clear_standard_references: bool = False,
) -> list[dict[str, Any]]:
    """Build the case-derived contract without deleting preserved source metadata."""
    # Retain the legacy keyword for caller compatibility, but never use it to
    # delete source metadata.
    _ = clear_standard_references
    result: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "知识ID": _clean_text(row.get("知识ID"))
            or _clean_text(row.get("主题ID"))
            or _clean_text(row.get("候选ID")),
            "主标题": _clean_text(row.get("主标题")),
            "副标题": _clean_text(row.get("副标题")),
            "知识内容": _clean_text(row.get("知识内容")),
            "图例": _clean_text(row.get("图例")) or _clean_text(row.get("主题图片链接")),
            "推荐回复": _clean_text(row.get("推荐回复")),
            "知识分类": _clean_text(row.get("知识分类")),
            "关联标准项": _clean_text(row.get("关联标准项")),
            "适用范围": _clean_text(row.get("适用范围")),
            "关键词": _clean_text(row.get("关键词"))
            or _clean_text(row.get("检索关键词")),
        }
        result.append(item)
    return result


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
    *,
    use_standard_references: bool = True,
) -> None:
    """Export either the legacy standard-aware contract or the case-only contract."""
    if not use_standard_references:
        write_rows_to_workbook(
            {
                "候选知识": (
                    CASE_KNOWLEDGE_COLUMNS,
                    build_case_knowledge_rows(topic_rows),
                )
            },
            workbook_path,
        )
        return
    write_rows_to_workbook(
        {
            "候选知识": (
                KNOWLEDGE_MASTER_COLUMNS + KNOWLEDGE_REVIEW_EXTENSION_COLUMNS,
                [
                    {
                        column: _clean_text(row.get(column))
                        for column in KNOWLEDGE_MASTER_COLUMNS + KNOWLEDGE_REVIEW_EXTENSION_COLUMNS
                    }
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
    cluster_review_floor: float = DEFAULT_CLUSTER_REVIEW_FLOOR,
    cluster_auto_merge_threshold: float = DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    cluster_review_limit: int = DEFAULT_CLUSTER_REVIEW_LIMIT,
    embedding_client: EmbeddingClient | None = None,
    progress_callback: Callable[[str, str, str, dict[str, Any]], None] | None = None,
    use_standard_references: bool | None = None,
) -> dict[str, Any]:
    def report(
        stage_id: str,
        status: str,
        detail: str,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        if progress_callback:
            progress_callback(stage_id, status, detail, metrics or {})

    standards_enabled = (
        bool(standards_path)
        if use_standard_references is None
        else bool(use_standard_references)
    )
    report(
        "load_input",
        "running",
        "正在读取会话数据与标准目录。"
        if standards_enabled
        else "正在读取会话数据；本次不使用标准引用。",
    )
    standard_catalog = load_standard_catalog(standards_path) if standards_enabled else []
    source_rows = _read_source_rows(source_path)
    report(
        "load_input",
        "completed",
        "输入文件读取完成。",
        {
            "source_rows": len(source_rows),
            "standards": len(standard_catalog),
        },
    )

    report("preprocess", "running", "正在执行品类筛选、字段清洗和证据校验。")
    selected_rows, excluded_rows = filter_source_rows_by_product_type(source_rows, product_type)
    preprocessed_rows = preprocess_source_rows(selected_rows)
    eligible_rows, validation_excluded_rows = filter_preprocessed_rows_for_model(preprocessed_rows)
    excluded_rows.extend(validation_excluded_rows)
    eligible_raw_rows = [
        source_row
        for source_row, preprocessed_row in zip(selected_rows, preprocessed_rows)
        if _clean_text(preprocessed_row.get("可进入模型初标")) == "是"
    ]
    report(
        "preprocess",
        "completed",
        "清洗与证据分流完成。",
        {
            "selected_rows": len(selected_rows),
            "eligible_rows": len(eligible_rows),
            "excluded_rows": len(excluded_rows),
        },
    )

    audit_store = AuditStore.from_env(audit_db_path)
    active_run_id = uuid.uuid4().hex
    for index, row in enumerate(excluded_rows, start=1):
        audit_store.record_excluded(
            active_run_id,
            _record_id_for_row(row, index),
            row,
            _clean_text(row.get("排除原因")) or "未通过候选生成校验",
        )
    report(
        "semantic_label",
        "running",
        "正在提取会话语义、证据特征并检索标准。"
        if standards_enabled
        else "正在从会话、历史回复和案例图中提取语义与证据特征。",
    )
    feature_rows, run_id = generate_phone_candidate_rows(
        eligible_rows,
        standard_catalog,
        min_confidence=min_confidence,
        raw_source_rows=eligible_raw_rows,
        use_mimo=use_mimo,
        audit_store=audit_store,
        run_id=active_run_id,
        use_standard_references=standards_enabled,
    )
    report(
        "semantic_label",
        "completed",
        "会话语义标注完成。",
        {
            "feature_rows": len(feature_rows),
            "model_labeled_rows": sum(
                _clean_text(row.get("语义标注状态")) == "topic_signal_labeled"
                for row in feature_rows
            ),
        },
    )

    output_path = _ensure_output_dir(output_dir)
    workbook_path = output_path / "review_queue.xlsx"
    topic_workbook_path = output_path / "topic_review_queue.xlsx"
    candidate_workbook_path = output_path / "candidate_knowledge.xlsx"
    write_review_workbook(preprocessed_rows, feature_rows, excluded_rows, workbook_path)
    report("topic_build", "running", "正在聚类主题、转写知识并执行模型初标。")
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
        cluster_review_floor=cluster_review_floor,
        cluster_auto_merge_threshold=cluster_auto_merge_threshold,
        cluster_review_limit=cluster_review_limit,
        embedding_client=embedding_client,
        use_standard_references=standards_enabled,
    )
    report(
        "topic_build",
        "completed",
        "主题聚类与知识转写完成。",
        {
            "topic_rows": topic_summary.get("topic_rows", 0),
            "evidence_gap_rows": topic_summary.get("evidence_gap_rows", 0),
            "pending_cluster_rows": topic_summary.get("pending_cluster_rows", 0),
        },
    )

    report("export_review", "running", "正在生成待审核工作簿和候选知识文件。")
    _, topic_rows = read_workbook_rows(topic_workbook_path, sheet_name="topic_review_queue")
    write_topic_candidate_knowledge_workbook(
        topic_rows,
        candidate_workbook_path,
        use_standard_references=standards_enabled,
    )
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
            "standard_references_enabled": standards_enabled,
        }
    )
    summary.update(topic_summary)
    (output_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report(
        "export_review",
        "completed",
        "待审核队列已生成，自动化流程进入人工审核阶段。",
        {
            "topic_rows": summary.get("topic_rows", 0),
            "candidate_file": str(candidate_workbook_path),
        },
    )
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
