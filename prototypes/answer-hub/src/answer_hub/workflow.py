from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict, replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from heapq import heappop, heappush
from pathlib import Path
from typing import Any, Callable, Iterable
import hashlib
import inspect
import json
import os
import re
import unicodedata
import uuid

import numpy as np

from .auto_review import (
    AutoReviewPolicy,
    UNWORTHY_VALUES,
    WORTHY_VALUES,
    apply_auto_review_annotation,
)
from .audit import AuditStore
from .ai_result import AI_RESULT_FIELDS, parse_ai_result
from .business_taxonomy import (
    AGGREGATE_BUSINESS_LINE_CODE,
    SELF_OPERATED_BUSINESS_LINE_CODE,
    SELF_OPERATED_BUSINESS_LINE_NAME,
    UNKNOWN_BUSINESS_LINE_NAME,
    default_business_line,
    business_line_from_record,
    business_line_metadata,
)
from .catalog import StandardCatalogItem, is_active_standard, load_standard_catalog
from .classification_catalog import (
    ClassificationCatalogItem,
    load_classification_catalog,
    retrieve_classification_matches,
)
from .clustering_rules import (
    ClusteringFingerprint,
    ClusteringRuleMatch,
    build_clustering_fingerprint,
    clustering_rules_metadata,
    match_clustering_judgment_rule,
)
from .cz_integration import CzIntegrationAdapter
from .draft_quality import (
    assess_case_only_draft,
    has_source_specific_case_content,
)
from .embedding import EmbeddingClient, EmbeddingError
from .excel_io import read_workbook_rows, write_rows_to_workbook
from .images import ImageDownloader, ImageEvidence, split_image_urls
from .knowledge_categories import knowledge_category_from_topic_stage
from .knowledge_value import (
    has_draftable_source_rule,
    has_explicit_reusable_knowledge,
)
from .mimo import (
    ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
    CLUSTER_UNIT_PROMPT_VERSION,
    CLUSTER_PAIR_REVIEW_PROMPT_VERSION,
    MimoClient,
    MimoError,
    PROMPT_VERSION,
    TOPIC_REVIEW_PROMPT_VERSION,
    TOPIC_SIGNAL_PROMPT_VERSION,
    TOPIC_STAGE_PROMPT_VERSION,
    candidate_title_structure_issue,
    candidate_title_style_issue,
)
from .operations import partition_redaction_rows
from .product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    canonical_product_code,
    canonical_product_name,
    configured_product_names,
    is_concrete_unconfigured_product,
    product_taxonomy_metadata,
    resolve_product_category,
)
from .terminology import ensure_terminology_loaded
from .topic_registry import TopicRegistry, TopicResolution


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
DEFAULT_CLUSTER_ADMISSION_MIN_CONFIDENCE = 0.75
CLUSTER_ADMISSION_POLICY_VERSION = (
    "cluster-admission-v2-hard-scope-gate-provisional-singleton"
)
MAX_CLUSTER_REVIEW_CANDIDATES = 3
DEFAULT_DIRECT_RECONCILE_FLOOR = 0.40
DEFAULT_DIRECT_RECONCILE_MODEL_FLOOR = 0.68
DEFAULT_DIRECT_RECONCILE_LIMIT = 24
# Verified on the fixed 20-row MiMo pressure sample: four workers with small
# requests completed without timeout/circuit-breaker failures. Higher
# concurrency added malformed structured output without improving throughput.
DEFAULT_DIRECT_MIMO_BATCH_SIZE = 6
DEFAULT_DIRECT_MIMO_MAX_WORKERS = 4
DEFAULT_DIRECT_ATOMIC_BATCH_SIZE = 4
DEFAULT_DIRECT_ATOMIC_BATCH_MAX_CHARS = 16000
DIRECT_MIMO_PROGRESS_VERSION = 3
DIRECT_RECONCILE_ALGORITHM_VERSION = "direct-reconcile-v6-quality-boundaries"
DIRECT_LOCAL_RESCUE_ALGORITHM_VERSION = (
    "direct-local-rescue-v2-explicit-dual-topics"
)
DIRECT_RECONCILE_QUERY_RULE_FLOOR = 0.25
DIRECT_RECONCILE_FAMILY_RULE_FLOOR = 0.40
DEFAULT_TOPIC_MODEL_CALL_LIMIT = 1000
CLASSIFICATION_CATALOG_ENV = "ANSWER_HUB_CLUSTERING_CLASSIFICATION_CATALOG"
CLUSTER_V1_ACCURACY_THRESHOLD = 0.80
CLUSTER_V1_MIN_DECISIVE_PAIRS = 20
CLUSTER_MEDIA_POLICIES = {"always", "on_demand", "never"}


def resolve_cluster_media_policy(
    value: str | None,
    *,
    cluster_only: bool,
    clustering_mode: str,
) -> str:
    normalized = _clean_text(value).lower()
    if normalized and normalized not in CLUSTER_MEDIA_POLICIES:
        raise ValueError(
            "聚类媒体策略只能为 always、on_demand 或 never"
        )
    if normalized:
        return normalized
    if cluster_only and clustering_mode.strip().lower() == "direct_mimo":
        return "never"
    return ""
TOPIC_TRANSCRIPTION_SKIP_STATUSES = {
    "skipped_not_worthy",
    "skipped_generic_draft",
    "skipped_standard_not_found",
}

DIRECT_RECONCILABLE_PROVIDERS = frozenset(
    {
        "mimo-direct",
        "mimo-direct-retry",
        "mimo-direct-review",
        "mimo-direct-post-guard",
    }
)

_DIRECT_RECONCILE_UNKNOWN_VALUES = {
    "",
    "待确认",
    "未知",
    "通用",
    "不限",
    "无明确阈值",
    "无明确标准",
}

_DIRECT_RECONCILE_GENERIC_CATEGORIES = {
    "基本情况",
    "信息查询",
    "流程操作",
}

_DIRECT_RECONCILE_ALIAS_REPLACEMENTS = (
    ("电池健康值", "电池健康度"),
    ("最大容量", "电池健康度"),
    ("健康容量", "电池健康度"),
    ("查询机型", "型号查询"),
    ("查询型号", "型号查询"),
    ("型号核实", "型号查询"),
    ("机型核实", "型号查询"),
    ("小型号", "型号查询"),
    ("设备型号", "型号查询"),
    ("内存/硬盘", "内存硬盘"),
    ("存储/硬盘", "内存硬盘"),
    ("硬盘容量", "内存硬盘容量"),
    ("内存容量", "内存硬盘容量"),
    ("硬盘品牌", "内存硬盘品牌"),
    ("内存品牌", "内存硬盘品牌"),
    ("白光灯检测", "白光检测"),
    ("白光检查", "白光检测"),
    ("检测方法咨询", "检测方法"),
    ("验机工具读取", "工具读取"),
    ("工具读数", "工具读取"),
    ("一根线", "工具读取"),
)

_DIRECT_RECONCILE_TRUSTED_TARGETS = frozenset(
    {
        "model_query",
        "memory_storage_brand",
        "new_device_eligibility",
        "camera_lens_surface_condition",
        "screen_color_spot",
        "screen_color_aging",
    }
)

_DIRECT_RECONCILE_SCOPE_AGNOSTIC_TARGETS = (
    _DIRECT_RECONCILE_TRUSTED_TARGETS
)

_DIRECT_RECONCILE_THRESHOLD_AGNOSTIC_TARGETS = frozenset(
    {
        "model_query",
        "memory_storage_brand",
    }
)

_DIRECT_RECONCILE_MULTI_CLUSTER_TARGETS = frozenset(
    {
        "model_query",
        "memory_storage_brand",
        "new_device_eligibility",
        "screen_color_spot",
        "screen_color_aging",
    }
)

_DIRECT_RECONCILE_TRUSTED_TARGET_FLOORS = {
    "model_query": 0.18,
    "memory_storage_brand": 0.18,
    "new_device_eligibility": 0.22,
    "camera_lens_surface_condition": 0.24,
    "screen_color_spot": 0.24,
    "screen_color_aging": 0.24,
}


def _topic_transcription_is_skipped(value: Any) -> bool:
    return _clean_text(value) in TOPIC_TRANSCRIPTION_SKIP_STATUSES

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

CONTENT_TYPE_DEFINITION = "定义型"
CONTENT_TYPE_THRESHOLD = "阈值型"
CONTENT_TYPE_VERIFICATION = "核验型"
CONTENT_TYPE_DISTINCTION = "区分型"

_CONTENT_TYPE_DISTINCTION_MARKERS = (
    "如何区分",
    "怎么区分",
    "区别",
    "优先按",
    "转按",
    "分别计数",
    "分别统计",
    "有触感和无触感",
    "无触感和有触感",
)
_CONTENT_TYPE_VERIFICATION_MARKERS = (
    "检测方法",
    "核验方法",
    "测试方法",
    "测量方法",
    "用指甲",
    "横向刮",
    "按压",
    "摇晃",
    "连接后",
    "切换",
    "查询",
    "核对",
    "确认",
    "拍照",
    "录像",
    "对焦",
)
_CONTENT_TYPE_ACTION_MARKERS = (
    "检查",
    "检测",
    "核验",
    "测试",
    "测量",
    "观察",
    "刮擦",
    "按压",
    "摇晃",
    "连接",
    "切换",
    "拍摄",
    "拍照",
    "查询",
    "核对",
    "确认",
    "录像",
    "对焦",
)
_CONTENT_TYPE_THRESHOLD_MARKERS = (
    "不超过",
    "超过",
    "大于",
    "小于",
    "不少于",
    "至少",
    "≤",
    "≥",
    ">",
    "<",
    "直径",
    "数量",
    "长度",
    "面积",
    "次数",
    "时长",
)
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
    "回收业务层级",
    "回收业务层级编码",
    "产品类型",
    "产品类型编码",
    "一级分类",
    "二级分类",
    "参考话术",
    "历史实际回复",
    "ai_result",
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
    "模型正文类型",
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
    "模型调用状态",
    "模型输出校验状态",
    "模型质量状态",
    "知识草稿状态",
    "标准引用门禁状态",
    "标准引用标签",
    "图片证据门禁状态",
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

TOPIC_STAGE_CLASSIFICATION_COLUMNS = [
    "主题问题分类",
    "主题沉淀价值",
    "主题分类原因",
    "主题价值原因",
    "主题可复用知识摘要",
    "主题分类置信度",
    "主题分类重点复核",
    "主题分类提供方",
    "主题分类模型名称",
    "主题分类Prompt版本",
    "主题分类运行ID",
    "主题分类状态",
    "主题分类错误",
    "主题转写状态",
]

TOPIC_HUMAN_VALIDATION_COLUMNS = [
    "人工主题问题分类",
]

CLUSTER_ADMISSION_COLUMNS = [
    "聚类准入状态",
    "聚类准入置信度",
    "聚类准入原因",
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
    "正文类型",
    "知识分类",
    "知识来源",
    "关联标准项",
    "候选项/处理项",
    "适用范围",
    "适用品牌",
    "适用机型",
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
    "适用品牌",
    "适用机型",
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
    "主题代表性记录ID",
    "主题工单ID",
    "历史主题处理结果",
    "历史主题匹配ID",
    "历史主题匹配置信度",
    "历史主题匹配原因",
    "主题证据版本",
    "本次新增证据数",
    "本次重复证据数",
    "主题聚类键",
    "主题问题意图",
    "主题对象/部位",
    "主题异常现象",
    "主题解题方式",
    "主题证据等级",
    "主题证据摘要",
    "主题事实引用",
    "主题事实证据包",
    "主题无来源内容",
    "主题图例来源",
    "主题视频来源",
    "主题视频链接",
    "主题检索标准Top5",
    "主题初标复核标准Top5",
    "主题标准版本",
    "主题标准检索来源",
    "主题标准检索状态",
    "主题标准快照版本",
    "主题标准检索错误",
    "主题置信度",
    "是否重点复核",
    *CLUSTER_ADMISSION_COLUMNS,
    *TOPIC_STAGE_CLASSIFICATION_COLUMNS,
    *TOPIC_HUMAN_VALIDATION_COLUMNS,
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

CLUSTER_ONLY_COLUMNS = [
    "聚类主题ID",
    "聚类主题",
    "主题样本数",
    "回收业务层级",
    "产品类型",
    "主题对象/部位",
    "主题异常现象",
    "主题解题方式",
    "主题来源记录ID",
    "主题工单ID",
    "成员核心问题",
    "主题聚类键",
    "聚类决策",
    "聚类提供方",
    "聚类原因",
    *CLUSTER_ADMISSION_COLUMNS,
    "是否重点复核",
    "人工聚类判断",
    "人工备注",
]

TOPIC_SOURCE_MAPPING_COLUMNS = [
    "主题ID",
    "事实ID",
    "事实引用",
    "是否代表性证据",
    "代表性选择原因",
    "来源记录ID",
    "工单ID",
    "原始工单ID",
    "核心问题",
    "人工核心问题",
    "人工判定结论",
    "聊天内容",
    "历史实际回复",
    "图片链接",
    "案例图引用",
    "视频链接",
    "图片处理状态",
    "视频处理状态",
    "回收业务层级",
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
    "历史主题处理结果",
    "历史主题匹配ID",
    "历史主题匹配置信度",
    "历史主题匹配原因",
    "主题证据版本",
    *CLUSTER_ADMISSION_COLUMNS,
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
    "主题问题分类",
    "主题沉淀价值",
    "转写状态",
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
    "AI结果解析状态",
    "AI结果字段",
    "AI结果冲突字段",
    "AI结果原始核心问题",
    "AI结果原始产品类型",
    "AI结果原始一级分类",
    "AI结果原始二级分类",
    "AI结果原始判定结论",
    "AI结果原始判定依据",
    "AI结果原始参考话术",
]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _ai_result_source_value(row: dict[str, Any]) -> Any:
    for key in ("ai_result", "AI结果", "AI_RESULT"):
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return ""


def _ai_result_compact(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _clean_text(value)).casefold()


def _ai_result_values_match(field: str, left: Any, right: Any) -> bool:
    if field == "产品类型":
        left_category = resolve_product_category(_clean_text(left))
        right_category = resolve_product_category(_clean_text(right))
        if left_category and right_category:
            return left_category.code == right_category.code
    return _ai_result_compact(left) == _ai_result_compact(right)


def _apply_ai_result_fields(row: dict[str, Any]) -> list[str]:
    raw_value = _ai_result_source_value(row)
    if isinstance(raw_value, (dict, list)):
        row["ai_result"] = json.dumps(raw_value, ensure_ascii=False, sort_keys=True)
    elif raw_value not in (None, ""):
        row["ai_result"] = raw_value
    parsed = parse_ai_result(raw_value)
    conflicts: list[str] = []
    for field in AI_RESULT_FIELDS:
        parsed_value = _clean_text(parsed.get(field))
        row[f"AI结果原始{field}"] = parsed_value
        existing_value = _clean_text(row.get(field))
        if parsed_value and not existing_value:
            row[field] = parsed_value
        elif (
            parsed_value
            and existing_value
            and not _ai_result_values_match(field, existing_value, parsed_value)
        ):
            conflicts.append(field)
    if not raw_value:
        status = "未提供"
    elif parsed:
        status = "已解析"
    else:
        status = "未识别"
    row["AI结果解析状态"] = status
    row["AI结果字段"] = "、".join(field for field in AI_RESULT_FIELDS if parsed.get(field))
    row["AI结果冲突字段"] = "、".join(conflicts)
    return conflicts


def _checkpoint_needs_ai_result_reprocessing(
    preprocessed_rows: list[dict[str, Any]],
) -> bool:
    """Detect checkpoints created before ai_result evidence was materialized."""
    return any(
        _ai_result_source_value(row)
        and _clean_text(row.get("AI结果解析状态")) != "已解析"
        for row in preprocessed_rows
    )


def _source_work_order_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("工单ID必须以文本格式提供，不能使用布尔值")
    if isinstance(value, int):
        if abs(value) >= 1_000_000_000_000_000:
            raise ValueError(
                "工单ID必须以文本格式提供；15位及以上数值单元格可能已丢失精度"
            )
        return str(value)
    if isinstance(value, float):
        if not value.is_integer() or abs(value) >= 1_000_000_000_000_000:
            raise ValueError(
                "工单ID必须以文本格式提供；数值或科学计数法单元格可能已丢失精度"
            )
        return str(int(value))
    text = str(value).replace("\u3000", " ").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?[eE][+-]?\d+", text) or re.fullmatch(
        r"\d{15,}\.0+",
        text,
    ):
        raise ValueError(
            "工单ID必须以文本格式提供；科学计数法或小数形式可能已丢失精度"
        )
    return text


def _original_work_order_id_for_row(row: dict[str, Any]) -> str:
    current = _source_work_order_id(row.get("工单ID"))
    original_value = row.get("原始工单ID")
    original = (
        _source_work_order_id(original_value)
        if original_value not in (None, "")
        else current
    )
    if original and current != original:
        raise ValueError(
            "工单ID在聚类过程中发生变化："
            f"原始值={original!r}，当前值={current!r}"
        )
    return original


def _business_line_for_row(row: dict[str, Any]) -> str:
    cached = _clean_text(row.get("_回收业务层级规范值"))
    if cached:
        return cached
    line = business_line_from_record(row)
    normalized = line.name if line else UNKNOWN_BUSINESS_LINE_NAME
    row["_回收业务层级规范值"] = normalized
    row["_回收业务层级规范编码"] = line.code if line else ""
    return normalized


def _business_line_code_for_row(row: dict[str, Any]) -> str:
    _business_line_for_row(row)
    return _clean_text(row.get("_回收业务层级规范编码"))


def _resolved_product_type_for_row(row: dict[str, Any]) -> str:
    product_value = row.get("产品类型编码") or row.get("产品类型")
    category = resolve_product_category(product_value)
    if category:
        return category.name
    product_text = _clean_text(product_value)
    if (
        _business_line_for_row(row) == "聚合回收"
        and is_concrete_unconfigured_product(product_text)
    ):
        return product_text
    return ""


def _business_scope_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        _business_line_for_row(row),
        _resolved_product_type_for_row(row),
    )


def _normalize_lines(value: Any) -> str:
    if value is None:
        return ""
    lines = []
    for line in str(value).splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


_CLUSTER_EVIDENCE_OBJECT_MARKERS = (
    ("屏幕", ("屏幕", "显示屏", "内屏", "外屏")),
    ("摄像头", ("后置摄像头", "前置摄像头", "摄像头", "后摄", "前摄")),
    ("镜头", ("镜头", "镜片", "镜筒")),
    ("后壳", ("后壳", "后盖", "背板")),
    ("中框", ("中框", "边框", "侧框")),
    ("外壳", ("外壳", "机身外观", "机身")),
    ("电池", ("电池", "最大容量", "电池健康")),
    ("主板", ("主板",)),
    ("螺丝", ("螺丝",)),
    ("硬盘", ("固态硬盘", "机械硬盘", "硬盘")),
    ("内存", ("运行内存", "内存条", "内存")),
    ("键盘", ("键盘",)),
    ("转轴", ("转轴", "铰链")),
    ("扬声器", ("扬声器", "喇叭")),
    ("麦克风", ("麦克风", "话筒")),
)


def _conversation_evidence_for_cluster_conflict(value: Any) -> str:
    """Exclude transfer metadata before comparing human corrections with chat."""
    evidence_lines = []
    for line in _normalize_lines(value).splitlines():
        without_timestamp = re.sub(
            r"^\s*\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?::\d{2})?\s*",
            "",
            line,
        )
        if re.match(
            r"^\s*(问题类型|问题描述|转人工原因)\s*[:：]",
            without_timestamp,
        ):
            continue
        evidence_lines.append(without_timestamp)
    return "\n".join(evidence_lines)


def _cluster_evidence_object_keys(value: Any) -> set[str]:
    text = _clean_text(value)
    return {
        object_key
        for object_key, aliases in _CLUSTER_EVIDENCE_OBJECT_MARKERS
        if any(alias in text for alias in aliases)
    }


def _human_evidence_conflict_reason(source_row: dict[str, Any]) -> str:
    """Return only unambiguous chat-versus-human-correction object conflicts."""
    chat_objects = _cluster_evidence_object_keys(
        _conversation_evidence_for_cluster_conflict(source_row.get("聊天内容"))
    )
    if not chat_objects:
        return ""
    for label, value in (
        ("人工校正核心问题", source_row.get("核心问题") or source_row.get("原始核心问题")),
        ("人工校正判定结论", source_row.get("判定结论") or source_row.get("原始判定结论")),
    ):
        structured_objects = _cluster_evidence_object_keys(value)
        if structured_objects and not structured_objects.intersection(chat_objects):
            return (
                f"{label}与完整聊天包含的具体对象不同："
                f"{ '、'.join(sorted(structured_objects)) }"
                f" vs { '、'.join(sorted(chat_objects)) }"
            )
    return ""


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


def _extract_transfer_issue_description(value: Any) -> str:
    raw_text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if "问题描述" not in raw_text:
        return ""
    for line in raw_text.splitlines():
        if "问题描述" not in line:
            continue
        match = re.search(
            (
                r"问题描述\s*[:：]\s*(?P<description>.*?)"
                r"(?=\s*(?:转人工原因|问题类型)\s*[:：]|$)"
            ),
            line,
        )
        if match:
            description = _clean_text(match.group("description")).strip(
                "：:。；;！？!?"
            )
            if description:
                return description
    return ""


def _usable_topic_title(value: Any) -> str:
    title = _clean_text(value).strip("：:。；;！？!?")
    if not title:
        return ""
    if any(marker in title for marker in ("问题类型", "转人工原因")):
        return ""
    generic_titles = {
        "主题",
        "同一主题",
        "测试主题",
        "当前主题",
        "待确认",
        "待复核",
        "待复核主题",
    }
    return "" if title in generic_titles else title[:120]


def _topic_question_text(row: dict[str, Any]) -> str:
    core_problem = _usable_topic_title(row.get("核心问题"))
    if core_problem:
        return core_problem
    description = _extract_transfer_issue_description(row.get("聊天内容"))
    if description:
        return description
    return _semantic_excerpt(row.get("聊天内容"), 240)


def _topic_specific_signature(row: dict[str, Any]) -> str:
    candidates = [
        _clean_text(row.get("核心问题")),
        _extract_transfer_issue_description(row.get("聊天内容")),
        _historical_actual_reply(row),
        _clean_text(row.get("语义标注依据")),
    ]
    generic_values = {
        "",
        "图片",
        "发送图片",
        "转人工",
        "人工吗",
        "更相信人工",
        "老师帮忙看下",
        "麻烦看下",
        "帮忙看下",
        "看下图片",
    }
    for candidate in candidates:
        text = _clean_text(candidate)
        if not text:
            continue
        text = re.sub(r"\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?::\d{2})?", "", text)
        text = re.sub(r"(问题类型|问题描述|转人工原因)\s*[:：]\s*", "", text)
        text = re.sub(r"^(回收师|用户|客服|后台|人工客服).{0,24}?(?:时|中|发现|咨询|询问)", "", text)
        text = re.sub(r"(这是一个|这属于).{0,40}?(?:咨询|问题)。?$", "", text)
        text = re.sub(r"(希望|需要|请求).{0,36}?(?:确认|判断|判定|指导|支持)。?$", "", text)
        text = re.sub(r"[\s，,。；;：:、（）()“”\"'《》<>【】\[\]|｜/\\-]+", "", text)
        if len(text) < 4 or text in generic_values:
            continue
        return text[:48]
    return ""


def _cluster_label_as_topic_title(
    value: Any,
    query: dict[str, Any],
) -> str:
    title = _usable_topic_title(value)
    if not title or not re.search(r"[|｜]", title):
        return title

    parts = list(
        dict.fromkeys(
            _clean_text(part).strip("：:。；;！？!?")
            for part in re.split(r"\s*[|｜]\s*", title)
            if _clean_text(part)
        )
    )
    natural_markers = ("如何", "怎么", "是否", "能否", "可否")
    for part in parts:
        if any(marker in part for marker in natural_markers):
            return part[:120]

    support_markers = (
        "图片",
        "视频",
        "案例",
        "证据",
        "补充",
        "官方信息",
    )
    base = next(
        (
            part
            for part in parts
            if not any(marker in part for marker in support_markers)
        ),
        "",
    )
    if base in {"", "其他待确认", "待确认", "其他"}:
        base = _clean_text(query.get("对象/部位"))
    if not base:
        return ""

    context = " ".join(
        [
            *parts,
            _clean_text(query.get("问题意图")),
            _clean_text(query.get("解题方式")),
        ]
    )
    suffix = (
        "如何查询与确认"
        if "查询" in context and "核验" not in context
        else "如何核验"
    )
    return f"{base}{suffix}"[:120]


def _untranscribed_topic_title(query: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    cluster_title_seen = False
    for row in rows:
        raw_cluster_title = _clean_text(row.get("_聚类主题标题"))
        cluster_title_seen = cluster_title_seen or bool(raw_cluster_title)
        title = (
            ""
            if _has_internal_title_tag(raw_cluster_title)
            or not _cluster_title_matches_topic(raw_cluster_title, query)
            else _cluster_label_as_topic_title(raw_cluster_title, query)
        )
        if title:
            natural_title = _as_natural_question_title(title)
            if natural_title:
                return natural_title

    core_question_mismatch = bool(
        _clean_text(query.get("核心问题"))
        and not _cluster_title_matches_topic(
            query.get("核心问题"),
            query,
            include_core=False,
        )
    )
    if (
        cluster_title_seen
        or _topic_has_multiple_targets(query)
        or core_question_mismatch
    ):
        structured_title = _structured_topic_question_title(query, rows)
        if structured_title:
            return structured_title

    for row in rows:
        title = _usable_topic_title(row.get("核心问题"))
        if title:
            natural_title = _as_natural_question_title(_guess_title(title))
            if natural_title:
                return natural_title

    for row in rows:
        description = _extract_transfer_issue_description(row.get("聊天内容"))
        if description:
            natural_title = _as_natural_question_title(_guess_title(description))
            if natural_title:
                return natural_title

    for row in rows:
        title = _usable_topic_title(row.get("核心问题"))
        if title:
            natural_title = _as_natural_question_title(_guess_title(title))
            if natural_title:
                return natural_title

    first_question = _clean_text(query.get("核心问题")).split("；", 1)[0]
    if _usable_topic_title(first_question):
        title = _guess_title(first_question)
        if title:
            natural_title = _as_natural_question_title(title)
            if natural_title:
                return natural_title

    structured_title = _structured_topic_question_title(query, rows)
    if structured_title:
        return structured_title
    return f"{_topic_product_type(query, rows)}待确认问题如何核验"


def _cluster_title_matches_topic(
    title: Any,
    query: dict[str, Any],
    *,
    include_core: bool = False,
) -> bool:
    """Reject a model label that clearly belongs to another atomic topic."""
    normalized_title = re.sub(
        r"\s+",
        "",
        _clean_text(title).casefold(),
    )
    if not normalized_title or _has_internal_title_tag(normalized_title):
        return False
    def anchor_variants(value: Any) -> set[str]:
        text = re.sub(r"\s+", "", _clean_text(value)).casefold()
        if not text or text in {
            "待确认",
            "未知",
            "通用",
            "不限",
            "无明确阈值",
        }:
            return set()
        variants = {
            part
            for part in re.split(r"[｜|；;，,、/\\]+", text)
            if len(part) >= 2
        }
        expanded = set(variants)
        for part in variants:
            for prefix in ("疑似", "存在", "是否", "缺少"):
                if part.startswith(prefix) and len(part) - len(prefix) >= 2:
                    expanded.add(part[len(prefix) :])
            for suffix in ("痕迹", "现象", "情况", "问题", "的判定方法", "判定方法"):
                if part.endswith(suffix) and len(part) - len(suffix) >= 2:
                    expanded.add(part[: -len(suffix)])
        return expanded

    subject_terms = anchor_variants(query.get("对象/部位"))
    phenomenon_terms = anchor_variants(query.get("异常现象"))
    target_terms = anchor_variants(query.get("判定目标"))
    if include_core:
        target_terms.update(anchor_variants(query.get("核心问题")))
    if not subject_terms and not phenomenon_terms and not target_terms:
        return True
    subject_hit = bool(subject_terms.intersection({normalized_title})) or any(
        term in normalized_title for term in subject_terms
    )
    phenomenon_hit = any(term in normalized_title for term in phenomenon_terms)
    target_hit = any(term in normalized_title for term in target_terms)
    # A visible cluster title must carry the business object and either the
    # observed phenomenon or the explicit judgment target.  A title such as
    # “后盖素皮如何判定” has the object but drops the actual defect, while
    # “无帮助如何判定” has neither and must be rebuilt from structured data.
    if subject_terms and not subject_hit:
        return False
    if phenomenon_terms:
        return phenomenon_hit or target_hit
    return subject_hit or target_hit


def _topic_has_multiple_targets(query: dict[str, Any]) -> bool:
    source_text = "\n".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "人工判定结论",
        )
        if _clean_text(query.get(field))
    )
    return bool(
        re.search(r"(?:^|[；;。\n])\s*[12]\s*[.、)]", source_text)
        or any(
            marker in source_text
            for marker in (
                "同时",
                "分别",
                "两个问题",
                "两个主要问题",
                "两项问题",
            )
        )
    )


def _structured_topic_question_title(
    query: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    def business_term(value: Any) -> str:
        text = _clean_text(value)
        if not text or text in {"待确认", "未知", "通用", "不限"}:
            return ""
        text = re.sub(r"[（(][^（）()]{0,60}[）)]", "", text)
        parts = [
            part.strip()
            for part in re.split(r"[/／|｜、,，]+", text)
            if part.strip()
        ]
        if len(parts) >= 3:
            text = "、".join(parts[:2]) + "等"
        elif len(parts) == 2:
            text = "和".join(parts)
        else:
            text = parts[0] if parts else text
        return text.strip("：:；;，,、 ")

    subject_term = business_term(query.get("对象/部位"))
    phenomenon_term = business_term(query.get("异常现象"))
    if subject_term.endswith("镜头") and phenomenon_term.startswith("镜头"):
        phenomenon_term = phenomenon_term[len("镜头") :].lstrip("表面")
    structured_title = _safe_join(
        [
            _topic_product_type(query, rows),
            subject_term,
            phenomenon_term,
        ],
        "",
    )
    if not structured_title:
        return ""
    return _as_natural_question_title(structured_title)


def _record_id_for_row(row: dict[str, Any], index: int) -> str:
    return (
        _clean_text(row.get("数据ID"))
        or _original_work_order_id_for_row(row)
        or f"row-{index:05d}"
    )


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
    text = re.sub(r"\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?::\d{2})?", "", text)
    text = re.sub(r"^(老师|您好|你好|麻烦|请问|请教|帮我|帮忙|想问下|问下|看看|看下)[,，\s]*", "", text)
    text = re.sub(r"^(这个|这种|这样|此|该)[,，\s]*", "", text)
    text = text.replace("问题描述：", "").replace("问题类型：质检问题", "")
    text = text.strip("：:。；;！？!?")
    if re.fullmatch(r"(?:麻烦|请|帮忙|帮我)?(?:看一下|看下|看看)(?:图片)?", text):
        return ""
    text = re.sub(r"(?:麻烦|帮忙)?(?:看一下|看下|看看)(?:图片)?$", "", text).strip()
    text = text.replace("合盖合不上", "合盖无法闭合")
    if text.endswith("合盖无法闭合"):
        return f"{text}如何处理"
    if "坏点" in text and "漏液" in text:
        return "屏幕坏点和漏液如何区分"
    if "电池健康度" in text and any(marker in text for marker in ("无法读取", "读不出", "读取不到")):
        return "电池健康度无法读取如何判定"
    if "键帽缺失" in text:
        return "键帽缺失如何判定"
    if "散热器" in text and any(marker in text for marker in ("断裂", "正常", "出厂")):
        return "散热器区域是否正常如何判定"
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


_INTERNAL_TITLE_TAG_PATTERN = re.compile(
    r"(?:意图|对象|现象|处理|标准)\s*[:：]"
)
_NATURAL_QUESTION_TITLE_MARKERS = (
    "如何",
    "怎么",
    "怎样",
    "什么",
    "哪些",
    "是否",
    "能否",
    "可否",
    "能不能",
    "可不可以",
    "吗",
)


def _natural_question_title_issue(value: Any) -> str:
    """Return the reason a visible knowledge title cannot be used as a question."""
    title = _clean_text(value)
    structure_issue = candidate_title_structure_issue(title)
    if structure_issue:
        return structure_issue
    if _has_internal_title_tag(title):
        return "内部结构化标签"
    if not any(marker in title for marker in _NATURAL_QUESTION_TITLE_MARKERS):
        return "非自然问句"
    return ""


def _has_internal_title_tag(value: Any) -> bool:
    return bool(_INTERNAL_TITLE_TAG_PATTERN.search(_clean_text(value)))


def _as_natural_question_title(value: Any) -> str:
    """Keep a human-readable title in natural-question form without labels."""
    title = _clean_text(value).strip("：:。；;！？!?")
    title = re.sub(r"(如何判定|怎么判定)定$", r"\1", title)
    title = re.sub(r"(如何判定)(?:如何判定)+$", r"\1", title)
    issue = _natural_question_title_issue(title)
    if not title or issue not in {"", "非自然问句"}:
        return ""
    if not issue:
        return title[:120]
    suffixes = (
        ("判定方法", "如何判定"),
        ("判定", "如何判定"),
        ("核验", "如何核验"),
        ("确认", "如何确认"),
        ("查询", "如何查询"),
        ("处理", "如何处理"),
    )
    for ending, replacement in suffixes:
        if title.endswith(ending):
            prefix = title[:-len(ending)].rstrip("的地之 ")
            normalized = f"{prefix}{replacement}" if replacement else title
            return normalized[:120]
    return f"{title}如何判定"[:120]


def _natural_topic_title_from_source(
    query: dict[str, Any],
    rows: list[dict[str, Any]],
    standard: StandardCatalogItem | None = None,
) -> str:
    source_questions = [
        _clean_text(row.get(field))
        for row in rows
        for field in ("原始核心问题", "人工核心问题", "核心问题")
    ]
    source_questions.extend(
        [
            _clean_text(query.get("人工核心问题")),
            _clean_text(query.get("核心问题")),
        ]
    )
    for question in source_questions:
        if _has_internal_title_tag(question):
            continue
        structured_title = _natural_title_from_structured_atomic_question(
            question
        )
        if structured_title:
            return _as_natural_question_title(structured_title)
        if (
            not question
            or _title_is_case_narrative(question)
            or candidate_title_structure_issue(question)
            or candidate_title_style_issue(question)
        ):
            continue
        title = _guess_title(question, standard)
        if (
            title
            and not _title_is_case_narrative(title)
            and not candidate_title_structure_issue(title)
            and not candidate_title_style_issue(title)
        ):
            return _as_natural_question_title(title)
    for row in rows:
        description = _extract_transfer_issue_description(row.get("聊天内容"))
        if (
            not description
            or _title_is_case_narrative(description)
            or candidate_title_structure_issue(description)
        ):
            continue
        title = _guess_title(description, standard)
        if (
            title
            and not _title_is_case_narrative(title)
            and not candidate_title_structure_issue(title)
        ):
            return _as_natural_question_title(title)
    return _safe_join(
        [
            _topic_product_type(query, rows),
            _clean_text(query.get("对象/部位")),
            _clean_text(query.get("异常现象")),
        ],
        "",
    )[:100] + "如何判定"


def _natural_title_from_structured_atomic_question(value: Any) -> str:
    raw = _clean_text(value).split("；", 1)[0].split(";", 1)[0]
    if "｜" not in raw and "|" not in raw:
        return ""
    parts = [
        _clean_text(part)
        for part in re.split(r"[｜|]", raw)
        if _clean_text(part)
    ]
    if len(parts) < 3:
        return ""
    if resolve_product_category(parts[0]):
        parts = parts[1:]
    if len(parts) < 2:
        return ""
    subject = parts[0]
    issue = parts[1]
    resolution = parts[2] if len(parts) >= 3 else ""
    subject = re.sub(r"(?<=[\u4e00-\u9fff])(?=Switch\b)", " ", subject)
    subject = re.sub(r"(?<=Lite)(?=[\u4e00-\u9fff])", " ", subject)
    issue = issue.replace("日版机型", "")
    issue = issue.replace("是否在回收范围内", "是否可回收")
    issue = issue.replace("包装盒缺失是否影响回收判定", "包装盒缺失如何处理")
    issue = issue.replace("及", "，").strip("，、 ")
    if resolution.startswith("根据"):
        match = re.match(r"根据(.+?)判定", resolution)
        if match:
            basis = match.group(1).replace("直径尺寸", "直径")
            return f"{subject}{issue}如何按{basis}判定"[:120]
    if issue.startswith(("是否", "如何", "能否", "可否")):
        return f"{subject}{issue}"[:120]
    return f"{subject}{issue}如何处理"[:120]


def _title_is_case_narrative(value: Any) -> bool:
    title = _clean_text(value)
    return bool(
        re.match(
            r"^(?:回收师|用户|客服|答疑人员)"
            r"(?:询问|描述|反馈|咨询|遇到|发现|在)",
            title,
        )
        or re.match(r"^(?:回收师)?\s*询问(?:[:：])?", title)
        or re.search(r"(?:^|[；;])\s*\d+[.、]\s*", title)
        or title.count("？") + title.count("?") > 1
        or len(title) > 72
    )


def _title_requires_structured_rebuild(value: Any) -> bool:
    """Reject case-report and multi-question titles at the final export gate."""
    title = _clean_text(value)
    if not title:
        return True
    if _title_is_case_narrative(title):
        return True
    if candidate_title_structure_issue(title) or candidate_title_style_issue(title):
        return True
    if re.search(r"(?:上传|发来|发送|图片|视频|截图|本次会话|当前案例)", title):
        return True
    if re.search(r"(?:^|[；;])\s*\d+[.、]\s*", title):
        return True
    return title.count("？") + title.count("?") > 1 or len(title) > 72


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


_APPLE_PLATFORM_MARKERS = ("苹果", "apple", "ipad", "ios", "ipados")
_NON_APPLE_PLATFORM_MARKERS = (
    "安卓",
    "android",
    "鸿蒙",
    "harmony",
    "harmonyos",
)
_UNKNOWN_DEVICE_SCOPE_VALUES = {
    "",
    "通用",
    "待确认",
    "待确定",
    "未确认",
    "未识别",
    "未知",
    "不确定",
}


def _query_platform_family(query: dict[str, Any]) -> str:
    platform = _clean_text(query.get("平台"))
    brand = _clean_text(query.get("品牌"))
    model = _clean_text(query.get("机型"))
    query_scope_text = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "聊天内容",
            "人工核心问题",
            "人工判定结论",
            "平台",
            "品牌",
            "机型",
            "适用品牌",
            "适用机型",
        )
    ).lower()
    structured_scope_text = " ".join((platform, brand, model)).lower()
    has_apple_evidence = any(
        marker in query_scope_text for marker in _APPLE_PLATFORM_MARKERS
    )
    has_non_apple_platform_evidence = any(
        marker in structured_scope_text
        for marker in _NON_APPLE_PLATFORM_MARKERS
    )
    has_specific_non_apple_device = any(
        value not in _UNKNOWN_DEVICE_SCOPE_VALUES
        and not any(
            marker in value.lower() for marker in _APPLE_PLATFORM_MARKERS
        )
        for value in (brand, model)
    )
    has_non_apple_evidence = (
        has_non_apple_platform_evidence or has_specific_non_apple_device
    )
    if has_apple_evidence and has_non_apple_evidence:
        return "unknown"
    if has_apple_evidence:
        return "apple"
    if has_non_apple_evidence:
        return "non_apple"
    return "unknown"


def _standard_match_rejection_reasons(
    query: dict[str, Any],
    standard: StandardCatalogItem,
) -> list[str]:
    """Apply business-field gates after lexical standard retrieval.

    Retrieval is intentionally broad so recall is not lost.  This second pass
    rejects candidates whose product scope, object, phenomenon, or judgment
    target does not describe the same business question.
    """
    reasons: list[str] = []
    query_product = canonical_product_name(
        _clean_text(query.get("产品类型")),
        unknown=_clean_text(query.get("产品类型")),
    )
    standard_scope_text = _clean_text(standard.scope)
    standard_scope_base = standard_scope_text.split("-", 1)[0]
    standard_product = canonical_product_name(
        standard_scope_base,
        unknown=standard_scope_base,
    )
    if standard_product == standard_scope_base:
        for configured_name in configured_product_names():
            if configured_name in standard_scope_text:
                standard_product = configured_name
                break
    if query_product and standard_product and query_product != standard_product:
        reasons.append("product_scope_mismatch")

    scope_qualifier = (
        standard_scope_text.split("-", 1)[1]
        if "-" in standard_scope_text
        else ""
    )
    if scope_qualifier and scope_qualifier != "通用":
        query_scope_text = " ".join(
            _clean_text(query.get(field))
            for field in (
                "核心问题",
                "聊天内容",
                "人工核心问题",
                "人工判定结论",
                "平台",
                "品牌",
                "机型",
                "适用品牌",
                "适用机型",
            )
        ).lower()
        platform_family = _query_platform_family(query)
        if scope_qualifier == "苹果":
            if platform_family != "apple":
                reasons.append("scope_qualifier_mismatch")
        elif scope_qualifier == "安卓":
            if platform_family != "non_apple":
                reasons.append("scope_qualifier_mismatch")
        else:
            qualifier_aliases = {
                "小米/红米": ("小米", "红米", "xiaomi", "redmi"),
                "VIVO": ("vivo", "iqoo"),
                "OPPO": ("oppo", "一加"),
            }
            qualifier_markers = qualifier_aliases.get(
                scope_qualifier,
                (scope_qualifier.lower(),),
            )
            if not any(
                marker.lower() in query_scope_text
                for marker in qualifier_markers
            ):
                reasons.append("scope_qualifier_mismatch")

    query_object = _clean_text(query.get("对象/部位"))
    query_phenomenon = _clean_text(query.get("异常现象"))
    query_path = " ".join(
        _clean_text(query.get(field))
        for field in ("一级分类", "二级分类", "核心问题")
    )
    standard_text = standard.searchable_text()
    pre_target_evidence = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "人工判定结论",
            "判定依据",
            "历史实际回复",
            "异常现象",
            "解题方式",
        )
    )
    pre_is_tool_user_judgment = (
        "用户判断" in pre_target_evidence
        and any(
            marker in pre_target_evidence
            for marker in (
                "一根线",
                "验机工具",
                "验机侠",
                "工具读出",
                "工具结果",
                "序列号",
                "拆修",
            )
        )
    )
    allow_repair_target_bridge = (
        pre_is_tool_user_judgment
        and "拆修" in standard_text
        and any(
            marker in standard_text
            for marker in ("工具读出", "用户判断", "电池-工具")
        )
    )
    if query_object and len(query_object) >= 2:
        object_key = _normalized_topic_claim(query_object)
        if (
            object_key
            and object_key not in _normalized_topic_claim(standard_text)
            and not allow_repair_target_bridge
        ):
            # Allow a more specific standard label such as “固态硬盘” to
            # satisfy a broader source object such as “硬盘”.
            object_tokens = re.findall(r"[\u4e00-\u9fff]{2,8}", query_object)
            object_tokens.extend(
                query_object[index : index + 2]
                for index in range(max(0, len(query_object) - 1))
            )
            if not any(token in standard_text for token in object_tokens):
                reasons.append("object_mismatch")
    strong_phenomenon_markers = (
        "划痕",
        "磕点",
        "碎裂",
        "裂纹",
        "黑屏",
        "闪动",
        "模糊",
        "进水",
        "浸液",
        "卡顿",
        "异响",
        "插入困难",
        "掉漆",
        "脱胶",
        "拆修",
    )
    if (
        query_phenomenon
        and len(query_phenomenon) >= 2
        and any(marker in query_phenomenon for marker in strong_phenomenon_markers)
    ):
        phenomenon_key = _normalized_topic_claim(query_phenomenon)
        if phenomenon_key and phenomenon_key not in _normalized_topic_claim(standard_text):
            shared_strong_markers = [
                marker
                for marker in strong_phenomenon_markers
                if marker in query_phenomenon and marker in standard_text
            ]
            phenomenon_tokens = re.findall(
                r"[\u4e00-\u9fff]{2,8}", query_phenomenon
            )
            if (
                not allow_repair_target_bridge
                and not shared_strong_markers
                and phenomenon_tokens
                and not any(
                token in standard_text for token in phenomenon_tokens
                )
            ):
                reasons.append("phenomenon_mismatch")

    target_text = " ".join(
        _clean_text(query.get(field))
        for field in ("问题意图", "异常现象", "核心问题", "人工判定结论")
    )
    brand_target = any(
        marker in target_text
        for marker in ("品牌", "原装", "第三方", "是否为", "是不是")
    )
    standard_has_brand_rule = any(
        marker in standard_text
        for marker in ("品牌", "原装", "第三方", "更换", "替换")
    )
    if brand_target and not standard_has_brand_rule:
        reasons.append("judgment_target_mismatch")

    target_evidence = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "人工判定结论",
            "判定依据",
            "历史实际回复",
            "异常现象",
            "解题方式",
        )
    )
    is_tool_user_judgment = (
        "用户判断" in target_evidence
        and any(
            marker in target_evidence
            for marker in (
                "一根线",
                "验机工具",
                "验机侠",
                "工具读出",
                "工具结果",
                "序列号",
                "拆修",
            )
        )
    )
    has_observed_repair_evidence = bool(
        re.search(
            r"(?:现场(?:已)?(?:确认|发现)|已确认|确认存在|发现"
            r"|报告(?:已)?(?:显示|标注)|存在明确|有明确)"
            r"[^。；;\n]{0,24}(?:拆修|维修痕迹|工具读出异常)",
            target_evidence,
        )
    )
    standard_is_direct_tool_abnormal = (
        "工具读出异常" in standard_text
        and "用户判断" not in _clean_text(standard.response_snippet)
    )
    has_actual_battery_health_reading = bool(
        re.search(r"\d+(?:\.\d+)?\s*(?:%|％)", target_evidence)
    ) or any(
        marker in target_evidence
        for marker in ("最大容量", "电池健康值", "支持APP", "支持 App")
    )
    standard_is_battery_health = any(
        "电池健康度" in _clean_text(value)
        for value in (
            standard.title,
            standard.category_l2,
            standard.standard_path,
        )
    )
    if (
        is_tool_user_judgment
        and not has_actual_battery_health_reading
        and standard_is_battery_health
    ):
        # “用户判断”是验机工具对部件/序列号结果的待人工判断状态，
        # 不是电池健康度的数值读取结果，不能召回到健康度标准。
        reasons.append("judgment_target_mismatch")
    if (
        is_tool_user_judgment
        and standard_is_direct_tool_abnormal
        and not has_observed_repair_evidence
    ):
        # “用户判断”只说明工具无法直接下结论。未有来源事实证明对应
        # 部位存在拆修现象时，不能把它直接转换为“工具读出异常”候选项。
        reasons.append("user_judgment_observation_required")

    if query_path:
        path_parts = [
            _clean_text(query.get("一级分类")),
            _clean_text(query.get("二级分类")),
        ]
        placeholder_path_parts = {
            "待确认",
            "待确定",
            "未确认",
            "未识别",
            "未知",
            "不确定",
        }
        meaningful_parts = [
            part
            for part in path_parts
            if len(part) >= 2 and part not in placeholder_path_parts
        ]
        if (
            meaningful_parts
            and not allow_repair_target_bridge
            and not any(
            _normalized_topic_claim(part) in _normalized_topic_claim(standard.standard_path)
            or _normalized_topic_claim(part) in _normalized_topic_claim(standard_text)
            for part in meaningful_parts
            )
        ):
            reasons.append("standard_path_mismatch")
    return list(dict.fromkeys(reasons))


def _validated_standard_matches(
    query: dict[str, Any],
    candidates: list[tuple[StandardCatalogItem, float]],
) -> tuple[list[tuple[StandardCatalogItem, float]], list[dict[str, Any]]]:
    accepted: list[tuple[StandardCatalogItem, float]] = []
    rejected: list[dict[str, Any]] = []
    for item, score in candidates:
        reasons = _standard_match_rejection_reasons(query, item)
        if reasons:
            rejected.append(
                {
                    "standard_id": item.standard_id,
                    "standard_path": item.standard_path,
                    "score": score,
                    "reasons": reasons,
                }
            )
        else:
            accepted.append((item, score))
    return accepted, rejected


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
    topic = re.sub(
        r"[（(][^）)]*[）)]",
        "",
        path_parts[-1] if path_parts else "",
    ).strip()
    if re.fullmatch(r"(?:第?\d+(?:行)?|无|暂无|n/?a|-+)", topic, re.IGNORECASE):
        for fallback in (standard.category_l2, standard.category_l1, standard.scope):
            cleaned = _clean_text(fallback)
            if cleaned and not re.fullmatch(
                r"(?:第?\d+(?:行)?|无|暂无|n/?a|-+)",
                cleaned,
                re.IGNORECASE,
            ):
                return cleaned
        return ""
    return topic


def _process_title(
    core_problem: str,
    source_l1: str,
    source_l2: str,
    standard: StandardCatalogItem | None = None,
    product_type: str = "",
) -> str:
    kind = _process_kind(core_problem, source_l1, source_l2)
    standard_topic = _process_standard_topic(standard)
    product = _clean_text(product_type)

    def with_product(topic: str) -> str:
        cleaned = _clean_text(topic)
        if not product or product in cleaned:
            return cleaned
        return f"{product}{cleaned}"

    if kind == "model_query":
        return f"{product}设备机型如何查询与确认"
    if kind == "repair":
        return f"{with_product(standard_topic or '疑似拆修或维修痕迹')}如何核验"
    if kind == "liquid":
        return f"{with_product(standard_topic or '浸液风险')}如何核验"
    if kind == "function":
        topic = standard_topic or source_l2 or "设备功能"
        if "功能" not in topic:
            topic = f"{topic}功能"
        return f"{with_product(topic)}如何核验"
    if kind == "display":
        topic = standard_topic or "屏幕显示异常"
        if "屏幕" not in topic:
            topic = f"屏幕{topic}"
        return f"{with_product(topic)}如何通过图片核验"
    if kind == "appearance":
        return f"{with_product(standard_topic or '设备外观异常')}如何通过图片核验"
    category = _safe_join([source_l1, source_l2], " / ")
    return f"{with_product(category or _guess_title(core_problem))}如何核验"


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


def _rebuild_title_from_structured_fields(
    query: dict[str, Any],
    standard: StandardCatalogItem | None = None,
) -> str:
    """Build a searchable question without relying on model title output."""
    product = canonical_product_name(
        _clean_text(query.get("产品类型")),
        unknown=_clean_text(query.get("产品类型")),
    )
    subject = (
        _clean_text(query.get("对象/部位"))
        or _clean_text(query.get("二级分类"))
        or _clean_text(standard.category_l2 if standard else "")
    )
    phenomenon = (
        _clean_text(query.get("异常现象"))
        or _clean_text(query.get("三级分类"))
        or _clean_text(query.get("判定目标"))
    )
    intent = _clean_text(query.get("问题意图"))
    if not product or not subject:
        return ""
    user_judgment_text = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "异常现象",
            "解题方式",
        )
    )
    if (
        product == "平板电脑"
        and subject == "电池"
        and "用户判断" in user_judgment_text
        and any(
            marker in user_judgment_text
            for marker in ("验机", "一根线", "工具读出", "工具结果")
        )
    ):
        return "平板电脑电池拆修检测显示“用户判断”时如何处理？"
    if "优先级" in intent or "优先级" in phenomenon:
        title = f"{product}{subject}应按什么优先级读取"
        return title.rstrip("？?") + "？"
    if any(marker in intent for marker in ("区分", "区别", "边界")):
        distinction = re.sub(r"(?:边界|判定)$", "", phenomenon).strip(
            " /、，,；;"
        )
        distinction = re.sub(r"[/、]", "与", distinction)
        title = f"{product}{subject}{distinction}应如何区分"
        return title.rstrip("？?") + "？"
    if any(marker in intent for marker in ("信息查询", "位置查询")) or any(
        marker in phenomenon for marker in ("查看位置", "在哪里", "位置")
    ):
        title = f"{product}{subject}应在哪里查看"
        return title.rstrip("？?") + "？"
    if re.match(
        r"^(?:是否|能否|可否|如何|怎么|怎样|什么|哪些)",
        phenomenon,
    ):
        title = f"{product}{subject}{phenomenon}"
        return title.rstrip("？?") + "？"
    if any(
        phenomenon.endswith(ending)
        for ending in (
            "如何判定",
            "怎么判定",
            "如何核验",
            "怎么核验",
            "如何处理",
            "怎么处理",
        )
    ):
        title = f"{product}{subject}{phenomenon}"
        return title.rstrip("？?") + "？"
    if any(marker in intent for marker in ("查询", "读取", "查看")):
        action = "应如何查询"
    elif any(marker in intent for marker in ("处理", "售后")):
        action = "应如何处理"
    elif any(marker in intent for marker in ("判定", "判断")):
        action = "应如何判定"
    else:
        action = "应如何核验"
    title = f"{product}{subject}{phenomenon}{action}"
    return title.rstrip("？?") + "？"


def _candidate_subtitles(
    row: dict[str, Any],
    title: str,
    standard: StandardCatalogItem | None,
    content: str = "",
    content_type: str = "",
) -> str:
    """Build recall-oriented natural questions for atomic candidates."""
    if not content_type:
        content_type = _classify_topic_content_type(
            {
                "核心问题": row.get("核心问题"),
                "人工判定结论": row.get("判定结论"),
                "判定依据": row.get("判定依据"),
                "问题意图": row.get("问题意图"),
                "对象/部位": row.get("对象/部位"),
                "异常现象": row.get("异常现象"),
                "解题方式": row.get("解题方式"),
            },
            [row],
            [(standard, 1.0)] if standard else [],
        )
    return "\n".join(_fallback_recall_subtitles(title, content, content_type))


def _finalize_topic_subtitles(
    raw_subtitles: Any,
    title: str,
    content: str,
    content_type: str,
    models: list[str] | None = None,
) -> str:
    """Convert model recall strings into one-topic natural questions."""
    topic_anchor_terms = (
        "电池健康度",
        "电池循环",
        "电池",
        "后置摄像头",
        "后摄",
        "摄像头",
        "镜片",
        "屏幕",
        "漏液",
        "进灰",
        "色斑",
        "序列号",
        "SN",
        "距离感应器",
        "接口",
        "充电线",
        "充电器",
        "外壳",
        "键盘",
        "键帽",
        "硬盘",
        "内存",
        "镜头",
    )
    topic_text = f"{title}\n{content}"
    topic_anchors = {
        term
        for term in topic_anchor_terms
        if term.lower() in topic_text.lower()
    }

    if isinstance(raw_subtitles, str):
        values = raw_subtitles.splitlines()
    elif isinstance(raw_subtitles, (list, tuple)):
        values = [str(value) for value in raw_subtitles]
    else:
        values = []

    normalized: list[str] = []
    for raw in values:
        value = _clean_text(raw)
        if not value:
            continue
        if "|" in value or "｜" in value:
            rebuilt = _natural_title_from_structured_atomic_question(value)
            if not rebuilt:
                continue
            value = rebuilt
        value = _strip_specific_models_from_text(value, models)
        value = re.sub(r"^(?:意图|对象|现象|处理|标准)\s*[:：]\s*", "", value)
        if not value:
            continue
        subtitle_anchors = {
            term
            for term in topic_anchor_terms
            if term.lower() in value.lower()
        }
        if (
            topic_anchors
            and subtitle_anchors
            and topic_anchors.isdisjoint(subtitle_anchors)
        ):
            continue
        if not any(marker in value for marker in _NATURAL_QUESTION_TITLE_MARKERS):
            value = f"{value.rstrip('。；;')}如何核验？"
        if not value.endswith(("？", "?")):
            value = value.rstrip("。；; ") + "？"
        if value != title and value not in normalized:
            normalized.append(value[:120])
        if len(normalized) >= 3:
            break

    if not normalized:
        normalized = _fallback_recall_subtitles(title, content, content_type)
    # 副标题只是检索辅助，不再输出多个问题，避免一个候选混入不同主题。
    return normalized[0] if normalized else ""


def _build_experience_review_content(query: dict[str, Any]) -> str:
    """Build a conservative body when no authoritative standard is available."""
    subject = _safe_join(
        [
            _clean_text(query.get("对象/部位")),
            _clean_text(query.get("异常现象")),
        ],
        "的",
    )
    method = _clean_text(query.get("解题方式"))
    # 来源案例中的阈值只能证明该案例曾这样处理，不能在无标准时被
    # 转写成可复用规则。因此经验补充只保留核验动作，去掉数值和阈值对照。
    method = re.sub(
        r"(?:并)?与[^，。；;\n]*(?:阈值|标准)(?:比较|对照)?",
        "",
        method,
    )
    method = re.sub(
        r"(?:大于|小于|超过|不少于|不超过|高于|低于|至少|至多|不低于|不大于|"
        r"≤|≥|>=|>|<)\s*"
        r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万]+)"
        r"\s*(?:mm|毫米|cm|厘米|%|％|颗|个|处|次|秒)?",
        "",
        method,
        flags=re.IGNORECASE,
    )
    method = method.replace("补充证据后再判定", "")
    method = re.sub(r"[，,；;：:]+\s*$", "", method).strip()
    if not method:
        method = "补充清晰照片、视频、系统截图或检测结果"
    subject_text = subject or "当前问题"
    return (
        "当前来源未提供可直接套用的明确规则，不能据此作出确定结论。\n"
        f"核验对象：{subject_text}。\n"
        f"核验方法：{method}。\n"
        "处理边界：证据不足、现象无法复现或与来源情形不一致时，补充证据后转人工审核。"
    )


def _candidate_content(
    row: dict[str, Any],
    standard: StandardCatalogItem | None,
    knowledge_form: str,
) -> str:
    core_problem = _clean_text(row.get("核心问题"))
    judgment = _clean_text(row.get("判定结论"))
    basis = _short_basis(_clean_text(row.get("判定依据")))
    reference_script = _normalize_lines(row.get("参考话术"))
    content_type = _classify_topic_content_type(
        {
            "核心问题": row.get("核心问题"),
            "人工判定结论": row.get("判定结论"),
            "判定依据": row.get("判定依据"),
            "问题意图": row.get("问题意图"),
            "对象/部位": row.get("对象/部位"),
            "异常现象": row.get("异常现象"),
            "解题方式": row.get("解题方式"),
        },
        [row],
        [(standard, 1.0)] if standard else [],
    )
    if standard and standard.response_snippet:
        compact_standard = _build_compact_standard_content(
            standard,
            content_type,
            query=row,
        )
        if compact_standard:
            return compact_standard

    if knowledge_form == "流程方法":
        raw_process = _build_process_content(
            core_problem,
            judgment,
            basis,
            _clean_text(row.get("一级分类")),
            _clean_text(row.get("二级分类")),
            standard,
        )
        points = _compact_standard_rule_points(raw_process, content_type)
        if points:
            return "\n".join(
                f"{index}. {point}"
                for index, point in enumerate(points, start=1)
            )
        return _compact_knowledge_content(raw_process)

    source_points = [
        _normalize_lines(value)
        for value in (judgment, basis, reference_script)
        if _normalize_lines(value)
    ]
    maximum = {
        "定义型": 2,
        "阈值型": 3,
        "核验型": 4,
        "区分型": 5,
    }.get(content_type, 3)
    if source_points:
        return "\n".join(
            f"{index}. {point[:220]}"
            for index, point in enumerate(source_points[:maximum], start=1)
        )
    return "1. 当前来源未提供可复用的明确规则，需补充对象、现象和判定证据后再审核。"


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
    content_type = _classify_topic_content_type(
        {
            "核心问题": row.get("核心问题"),
            "人工判定结论": row.get("判定结论"),
            "判定依据": row.get("判定依据"),
            "问题意图": row.get("问题意图"),
            "对象/部位": row.get("对象/部位"),
            "异常现象": row.get("异常现象"),
            "解题方式": row.get("解题方式"),
        },
        [row],
        matches,
    )
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

    content = _candidate_content(row, standard, knowledge_form)
    row.update(
        {
            "候选ID": f"KC-{source_id}" if source_id else "",
            "来源记录ID": source_id,
            "主标题": title,
            "副标题": _candidate_subtitles(
                row,
                title,
                standard,
                content,
                content_type,
            ),
            "知识内容": content,
            "正文类型": content_type,
            "知识分类": knowledge_category_from_topic_stage("", knowledge_form),
            "知识来源": "方向二会话候选",
            "关联标准项": _primary_standard_path(standard.standard_path) if standard else "",
            "适用范围": canonical_product_name(
                row.get("产品类型编码") or row.get("产品类型"),
                unknown=_clean_text(row.get("产品类型")),
            ),
            "适用品牌": _clean_text(row.get("适用品牌")),
            "适用机型": _clean_text(row.get("适用机型")),
            "生效状态": "待审核",
            "来源版本": standard.version if standard and standard.version else "待补充",
            "变更类型": "新增",
            "失效原因": "",
            "检索关键词": _candidate_keywords(row, title, standard),
            "校验备注": "；".join(note for note in notes if note),
            "候选知识形态": knowledge_form,
            "模型正文类型": content_type,
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
        if not _clean_text(actual_value):
            actual_value = parse_ai_result(
                _ai_result_source_value(row)
            ).get("产品类型", "")
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
        ai_result_conflicts = _apply_ai_result_fields(row)
        row["序号"] = _clean_text(row.get("序号") or index)
        row["上传者"] = _clean_text(row.get("上传者"))
        row["分析时间"] = _clean_text(row.get("分析时间"))
        work_order_id = _original_work_order_id_for_row(row)
        row["原始工单ID"] = work_order_id
        row["工单ID"] = work_order_id
        row["数据ID"] = _clean_text(row.get("数据ID")) or row["工单ID"] or f"row-{index:05d}"
        row["回收单号"] = _clean_text(row.get("回收单号"))
        row["聊天内容"] = _normalize_lines(row.get("聊天内容"))
        row["图片链接"] = _normalize_image_links(row.get("图片链接"))
        row["视频链接"] = _normalize_image_links(row.get("视频链接"))
        source_core_problem = _clean_text(
            row.get("核心问题") or row.get("原始核心问题")
        )
        source_judgment_conclusion = _clean_text(
            row.get("判定结论") or row.get("原始判定结论")
        )
        row["原始核心问题"] = source_core_problem
        row["核心问题"] = source_core_problem
        row["原始判定结论"] = source_judgment_conclusion
        row["判定结论"] = source_judgment_conclusion
        row["判定依据"] = _normalize_lines(row.get("判定依据"))
        raw_business_line = _clean_text(
            row.get("回收业务层级编码")
            or row.get("回收业务层级")
            or row.get("回收业务")
            or row.get("业务线")
        )
        raw_product_type = _clean_text(row.get("产品类型"))
        raw_product_code = _clean_text(row.get("产品类型编码"))
        raw_category = _clean_text(row.get("类目"))
        category_product = resolve_product_category(raw_category)
        legacy_product = resolve_product_category(raw_product_code or raw_product_type)
        product_category = category_product or legacy_product
        # The source workbook's 类目 is the business-owned product category.
        # Keep 产品类型 as an audit field, but do not let a legacy value such as
        # “电脑” move a clearly identified notebook row into aggregate recall.
        if raw_business_line:
            business_line = business_line_from_record(row)
        elif category_product:
            business_line = default_business_line()
        else:
            business_line = business_line_from_record(row)
        row["回收业务层级原值"] = raw_business_line
        row["回收业务层级"] = (
            business_line.name
            if business_line
            else UNKNOWN_BUSINESS_LINE_NAME
        )
        row["回收业务层级编码"] = business_line.code if business_line else ""
        row["产品类型原值"] = raw_product_type
        row["类目原值"] = raw_category
        aggregate_product = bool(
            business_line
            and business_line.code == AGGREGATE_BUSINESS_LINE_CODE
            and is_concrete_unconfigured_product(
                raw_product_type or raw_product_code
            )
        )
        row["产品类型"] = (
            product_category.name
            if product_category
            else raw_product_type or raw_product_code
            if aggregate_product
            else UNKNOWN_PRODUCT_NAME
        )
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
        if category_product:
            notes.append("产品类型按类目优先")
            if raw_product_type and not legacy_product:
                notes.append(
                    f"产品类型原值未识别：{raw_product_type}；已按类目“{category_product.name}”处理"
                )
            elif legacy_product and legacy_product.name != category_product.name:
                notes.append(
                    f"类目与产品类型不一致：类目={category_product.name}，产品类型={raw_product_type}；已按类目处理"
                )
        if ai_result_conflicts:
            notes.append(
                "ai_result 与已有结构化字段冲突："
                + "、".join(ai_result_conflicts)
                + "；保留已有字段并强制人工复核"
            )
        if product_category is None and aggregate_product:
            notes.append(
                f"产品类型未纳入自营12品类：{row['产品类型']}；"
                "按聚合回收产品品类保留并强制人工复核"
            )
        elif product_category is None:
            notes.append(
                f"产品类型未在当前品类配置中识别：{raw_product_type or raw_product_code or '空'}；进入人工确认"
            )
        if business_line is None:
            notes.append(
                f"回收业务层级未识别：{raw_business_line or '空'}；进入人工确认"
            )
        elif not business_line.product_categories_configured:
            notes.append(
                f"{business_line.name}产品品类口径尚未配置；"
                "当前只做业务层级隔离并强制人工复核"
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
                "回收业务层级": _business_line_for_row(source_row),
                "回收业务层级编码": _business_line_code_for_row(source_row),
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
    values = [
        _clean_text(row.get("产品类型")),
        _clean_text(row.get("问题意图")),
        _clean_text(row.get("对象/部位")),
        _clean_text(row.get("异常现象")),
        _clean_text(row.get("解题方式")),
        _clean_text(row.get("主标准路径")),
    ]
    computed = " | ".join(value for value in values if value)
    if computed and sum(1 for value in values if value) >= 3:
        return computed
    return _clean_text(row.get("标签聚类键"))


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


def _canonical_export_applicable_scope(row: dict[str, Any]) -> str:
    for field in (
        "产品类型编码",
        "产品类型",
        "适用范围",
        "模型适用范围",
    ):
        value = _clean_text(row.get(field))
        if not value:
            continue
        category = resolve_product_category(value)
        if category:
            return category.name
        prefix = re.split(r"[-—–]", value, maxsplit=1)[0].strip()
        category = resolve_product_category(prefix)
        if category:
            return category.name
    return ""


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
        "正文类型": _clean_text(row.get("正文类型")) or _clean_text(row.get("模型正文类型")),
        "知识分类": category,
        "知识来源": _clean_text(row.get("知识来源")) or "方向二会话候选",
        "关联标准项": refs,
        "适用范围": _canonical_export_applicable_scope(row),
        "适用品牌": _clean_text(row.get("适用品牌")),
        "适用机型": _clean_text(row.get("适用机型")),
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
        "正文类型": _clean_text(row.get("正文类型")),
        "图例": _clean_text(row.get("图例")) or _clean_text(row.get("主题图片链接")),
        "推荐回复": _clean_text(row.get("推荐回复")),
        "知识分类": _clean_text(row.get("知识分类")),
        "知识来源": _clean_text(row.get("知识来源")) or "方向二主题候选",
        "关联标准项": _clean_text(row.get("关联标准项")),
        "适用范围": _canonical_export_applicable_scope(row),
        "适用品牌": _clean_text(row.get("适用品牌")),
        "适用机型": _clean_text(row.get("适用机型")),
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
        "主题问题分类": _clean_text(row.get("主题问题分类")),
        "人工主题问题分类": _clean_text(row.get("人工主题问题分类")),
        "主题沉淀价值": _clean_text(row.get("主题沉淀价值")),
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
        normalized_row = _enforce_standard_reference_consistency(
            dict(row),
            use_standard_references=bool(
                _clean_text(row.get("标准引用标签"))
                or _clean_text(row.get("关联标准项"))
            ),
        )
        decision = _clean_text(normalized_row.get("审核结论")) or _simple_review_decision(normalized_row)
        knowledge_value = _clean_text(normalized_row.get("是否值得沉淀")).lower()
        if knowledge_value in UNWORTHY_VALUES:
            decision = "驳回"
        if (
            decision in {"通过", "修改后通过"}
            and _clean_text(normalized_row.get("自动审核状态")) != "auto_approved"
            and knowledge_value not in WORTHY_VALUES
        ):
            continue
        if not decision:
            continue
        if not _review_decision_allowed(decision):
            raise ValueError(f"Unsupported topic review decision: {decision}")
        review_time = _clean_text(normalized_row.get("审核时间")) or datetime.now().isoformat(timespec="seconds")
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
        if (
            _clean_text(normalized_row.get("知识来源"))
            == "方向二经验补充候选"
            and not _clean_text(normalized_row.get("关联标准项"))
        ):
            # 经验补充可以保留在审核反馈中，但没有可追溯标准前不能进入
            # 正式送审候选或训练样本，避免单案例口径被反向固化。
            continue
        final_row = _topic_final_master_row(normalized_row, review_time)
        final_rows.append(final_row)
        if _clean_text(normalized_row.get("是否进入训练集")) in {"是", "yes", "true", "1"}:
            training_rows.append(
                {
                    "task": "topic_knowledge_generation",
                    "topic_id": _clean_text(normalized_row.get("主题ID")),
                    "input": {
                        "sample_count": _clean_text(normalized_row.get("主题样本数")),
                        "source_record_ids": _clean_text(normalized_row.get("主题来源记录ID")),
                        "evidence_level": _clean_text(normalized_row.get("主题证据等级")),
                        "evidence_summary": _clean_text(normalized_row.get("主题证据摘要")),
                        "retrieved_standards": _clean_text(normalized_row.get("主题检索标准Top5")),
                        "standard_versions": _clean_text(normalized_row.get("主题标准版本")),
                        "model_candidate": {
                            field: _clean_text(normalized_row.get(field))
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


def _topic_group_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        _business_line_for_row(row),
        _clean_text(row.get("产品类型")),
        _clean_text(row.get("模型主题一级分类")) or _clean_text(row.get("一级分类")),
        _clean_text(row.get("模型主题二级分类")) or _clean_text(row.get("二级分类")),
        _clean_text(row.get("主标准路径")),
        _clean_text(row.get("问题意图")),
        _clean_text(row.get("对象/部位")),
        _clean_text(row.get("异常现象")),
        _topic_specific_signature(row),
        _topic_tag_cluster_key(row),
    )


def _is_tablet_battery_user_judgment_row(row: dict[str, Any]) -> bool:
    query = {
        "产品类型": _clean_text(row.get("产品类型")),
        "核心问题": _clean_text(row.get("核心问题")),
        "人工核心问题": _clean_text(row.get("人工核心问题")),
        "人工判定结论": _clean_text(row.get("人工判定结论")),
        "判定依据": _clean_text(row.get("判定依据")),
        "历史实际回复": _clean_text(row.get("历史实际回复")),
        "异常现象": _clean_text(row.get("异常现象")),
        "解题方式": _clean_text(row.get("解题方式")),
        "聊天内容": _clean_text(row.get("聊天内容")),
    }
    corrected = _retarget_battery_user_judgment_query(query)
    return (
        _clean_text(corrected.get("一级分类")) == "拆修及浸液情况"
        and _clean_text(corrected.get("二级分类")) == "电池拆修"
    )


def _merge_known_equivalent_topic_groups(
    topic_groups: list[tuple[tuple[str, ...], list[dict[str, Any]]]],
) -> list[tuple[tuple[str, ...], list[dict[str, Any]]]]:
    """Merge only the confirmed equivalent tablet battery user-judgment topic."""
    normalized_groups: list[tuple[tuple[str, ...], list[dict[str, Any]]]] = []
    battery_group_indexes: dict[tuple[str, str], int] = {}
    canonical_question = "平板电池验机工具显示用户判断时如何处理"
    for key, rows in topic_groups:
        if rows and all(_is_tablet_battery_user_judgment_row(row) for row in rows):
            business_line = _business_line_for_row(rows[0])
            product_type = _clean_text(rows[0].get("产品类型"))
            identity = (business_line, product_type)
            for row in rows:
                row.update(
                    {
                        "一级分类": "拆修及浸液情况",
                        "二级分类": "电池拆修",
                        "问题意图": "标准判定",
                        "对象/部位": "电池",
                        "异常现象": "验机工具读出用户判断",
                        "核心问题": canonical_question,
                        "解题方式": "核对工具报告和对应电池拆修结论",
                        "_聚类主题标题": canonical_question,
                    }
                )
            if identity in battery_group_indexes:
                normalized_groups[battery_group_indexes[identity]][1].extend(rows)
                continue
            canonical_key = (
                "normalized",
                business_line,
                product_type,
                "battery_tool_user_judgment",
            )
            battery_group_indexes[identity] = len(normalized_groups)
            normalized_groups.append((canonical_key, rows))
            continue
        normalized_groups.append((key, rows))
    return normalized_groups


def _semantic_excerpt(value: Any, max_chars: int = 800) -> str:
    text = _clean_text(value)
    if len(text) <= max_chars:
        return text
    head_size = int(max_chars * 0.7)
    tail_size = max_chars - head_size
    return f"{text[:head_size]}\n[...]\n{text[-tail_size:]}"


def _topic_semantic_text(row: dict[str, Any]) -> str:
    fields = (
        ("回收业务层级", _business_line_for_row(row)),
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
        business_line = _business_line_for_row(row)
        product_type = _clean_text(row.get("产品类型"))
        best_index = -1
        best_score = -1.0
        matching_indices = [
            index
            for index, cluster in enumerate(grouped)
            if cluster["business_line"] == business_line
            and cluster["product_type"] == product_type
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
                    "business_line": business_line,
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
            cluster["business_line"],
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
        for field in (
            "聊天内容",
            "原始核心问题",
            "原始判定结论",
            "核心问题",
            "主题标签",
            "主标准路径",
        )
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


def _is_recoverable_atomic_validation_failure(
    source_row: dict[str, Any],
    failure_reason: str,
) -> bool:
    """Allow a complete source row to recover from one known model-only guard.

    The recovered unit remains manual-review-only.  This does not accept the
    invalid model output; it uses the existing conservative source fallback
    when the model rejected its own broad screen category despite a complete
    source-side atomic question.
    """
    if "屏幕显示现象必须归入显示问题" not in failure_reason:
        return False
    return all(
        _clean_text(source_row.get(field))
        for field in ("核心问题", "对象/部位", "异常现象", "解题方式")
    )


def _direct_local_notebook_query_targets(
    row: dict[str, Any],
) -> list[str]:
    if canonical_product_name(row.get("产品类型"), unknown="") != "笔记本":
        return []
    text = _clean_text(row.get("聊天内容")).lower()
    if not text:
        return []

    target_markers = {
        "bios_lock": (
            "bios锁",
            "bios 锁",
            "有没有bios锁",
            "无锁",
            "没锁",
        ),
        "model_query": (
            "小型号",
            "具体型号",
            "设备型号",
            "型号",
            "机型",
            "年款",
            "第几款",
            "哪一款",
        ),
        "memory_storage_brand": (
            "品牌硬盘",
            "品牌内存",
            "内存和硬盘都是品牌",
            "内存硬盘都是品牌",
            "硬盘内存品牌",
            "硬盘是不是品牌",
            "内存是不是品牌",
            "硬盘是品牌",
            "内存是品牌",
            "品牌认证",
            "非品牌",
            "第三方硬盘",
            "第三方内存",
        ),
        "fingerprint_support": (
            "支持指纹",
            "指纹功能",
            "指纹识别",
            "有指纹吗",
        ),
        "serial_query": (
            "序列号",
            "后壳标签",
            "底部标签",
            "背板标签",
        ),
    }
    detected: list[tuple[int, int, str]] = []
    for order, (target, markers) in enumerate(target_markers.items()):
        positions = [
            text.find(marker)
            for marker in markers
            if marker in text
        ]
        if positions:
            detected.append((min(positions), order, target))
    detected.sort()
    return [target for _position, _order, target in detected]


def _direct_local_notebook_query_topics(
    source_row: dict[str, Any],
    original: dict[str, Any],
) -> list[dict[str, Any]]:
    targets = _direct_local_notebook_query_targets(source_row)
    if len(targets) < 2:
        return []

    product_type = (
        canonical_product_name(source_row.get("产品类型"), unknown="")
        or _clean_text(source_row.get("产品类型"))
        or "笔记本"
    )
    base_confidence = min(
        _float_or_default(original.get("confidence"), 0.0),
        0.78,
    )
    templates = {
        "bios_lock": {
            "normalized_issue": f"{product_type}｜BIOS锁｜锁状态查询｜确认是否存在BIOS锁",
            "category_l1": "信息查询",
            "category_l2": "BIOS锁查询",
            "intent": "信息查询",
            "subject": "BIOS锁",
            "phenomenon": "锁状态待确认",
            "judgment_target": "确认设备是否存在BIOS锁",
            "resolution_mode": "进入BIOS或根据启动状态核对锁状态",
            "standard_path": "BIOS锁查询",
        },
        "model_query": {
            "normalized_issue": f"{product_type}｜设备型号｜型号/机型查询｜确认具体型号",
            "category_l1": "信息查询",
            "category_l2": "型号查询",
            "intent": "信息查询",
            "subject": "设备型号",
            "phenomenon": "型号或年款待确认",
            "judgment_target": "确认设备具体型号和年款",
            "resolution_mode": "通过系统信息、官网或机身标签核对型号",
            "standard_path": "型号查询",
        },
        "memory_storage_brand": {
            "normalized_issue": (
                f"{product_type}｜内存和硬盘｜品牌属性查询｜"
                "确认是否为品牌认证配件"
            ),
            "category_l1": "信息查询",
            "category_l2": "内存/硬盘品牌属性",
            "intent": "信息查询",
            "subject": "内存和硬盘",
            "phenomenon": "品牌属性待确认",
            "judgment_target": "确认内存和硬盘品牌属性",
            "resolution_mode": "通过系统信息或验机工具核对品牌信息",
            "standard_path": "内存/硬盘品牌属性查询",
        },
        "fingerprint_support": {
            "normalized_issue": (
                f"{product_type}｜指纹识别｜功能支持查询｜"
                "确认机型是否支持指纹"
            ),
            "category_l1": "信息查询",
            "category_l2": "指纹支持查询",
            "intent": "信息查询",
            "subject": "指纹识别",
            "phenomenon": "是否支持指纹待确认",
            "judgment_target": "确认设备是否支持指纹识别",
            "resolution_mode": "根据具体型号核对官方配置信息",
            "standard_path": "指纹功能支持查询",
        },
        "serial_query": {
            "normalized_issue": (
                f"{product_type}｜序列号/机身标签｜信息查询｜"
                "确认序列号或标签状态"
            ),
            "category_l1": "信息查询",
            "category_l2": "序列号查询",
            "intent": "信息查询",
            "subject": "序列号/机身标签",
            "phenomenon": "序列号或标签状态待确认",
            "judgment_target": "确认序列号及机身标签状态",
            "resolution_mode": "通过机身标签、BIOS或官网核对序列号",
            "standard_path": "序列号查询",
        },
    }
    rescued: list[dict[str, Any]] = []
    for target in targets:
        template = templates[target]
        rescued.append(
            {
                **original,
                **template,
                "product_category": product_type,
                "scope_type": "品类专用",
                "platform": "通用",
                "brand": "通用",
                "model_scope": "通用",
                "threshold_or_exception": "无明确阈值",
                "evidence_summary": _safe_join(
                    [
                        _clean_text(original.get("evidence_summary")),
                        (
                            "本地根据聊天中明确出现的多个独立查询目标拆分，"
                            f"当前目标：{template['judgment_target']}。"
                        ),
                    ],
                    "；",
                ),
                "confidence": base_confidence,
                "requires_review": True,
                "_local_multi_topic_rescue_reason": (
                    "local_structured_info_query_rescue"
                ),
            }
        )
    return rescued


def _direct_local_explicit_dual_topic_specs(
    row: dict[str, Any],
) -> tuple[str, tuple[dict[str, str], ...]]:
    product_type = (
        canonical_product_name(row.get("产品类型"), unknown="")
        or _clean_text(row.get("产品类型"))
        or "待确认"
    )
    text = f"{row.get('核心问题', '')}\n{row.get('聊天内容', '')}"

    if (
        "序列号" in text
        and "乱码" in text
        and "碎裂" in text
        and any(marker in text for marker in ("需要判", "要判", "要的"))
    ):
        return (
            "local_serial_plus_screen_damage_rescue",
            (
                {
                    "normalized_issue": (
                        f"{product_type}｜序列号/爬虫工具｜读取乱码｜"
                        "确认序列号处理方式"
                    ),
                    "category_l1": "信息查询",
                    "category_l2": "序列号读取异常",
                    "intent": "信息查询",
                    "subject": "序列号",
                    "phenomenon": "爬虫或工具读取乱码",
                    "judgment_target": "确认序列号读取乱码的处理方式",
                    "resolution_mode": "按序列号读取异常流程处理",
                    "standard_path": "序列号读取异常",
                },
                {
                    "normalized_issue": (
                        f"{product_type}｜屏幕/面板｜碎裂｜"
                        "确认是否需要判定"
                    ),
                    "category_l1": "外观问题",
                    "category_l2": "屏幕碎裂",
                    "intent": "标准判定",
                    "subject": "屏幕/面板",
                    "phenomenon": "碎裂",
                    "judgment_target": "确认屏幕碎裂是否需要判定",
                    "resolution_mode": "按屏幕碎裂标准判定",
                    "standard_path": "屏幕碎裂判定",
                },
            ),
        )

    if (
        product_type == "笔记本"
        and "色斑" in text
        and "印记" in text
        and any(
            marker in text
            for marker in ("印记也要判", "色斑和印记", "色斑、印记")
        )
    ):
        return (
            "local_notebook_screen_spot_surface_rescue",
            (
                {
                    "normalized_issue": (
                        "笔记本｜屏幕显示｜色斑｜确认显示色斑"
                    ),
                    "category_l1": "显示问题",
                    "category_l2": "屏幕色斑",
                    "intent": "标准判定",
                    "subject": "屏幕显示",
                    "phenomenon": "色斑",
                    "judgment_target": "确认是否属于屏幕显示色斑",
                    "resolution_mode": "按屏幕显示色斑标准判定",
                    "standard_path": "屏幕显示色斑判定",
                },
                {
                    "normalized_issue": (
                        "笔记本｜屏幕表面｜印记｜确认表面印记"
                    ),
                    "category_l1": "外观问题",
                    "category_l2": "屏幕表面印记",
                    "intent": "标准判定",
                    "subject": "屏幕表面",
                    "phenomenon": "印记",
                    "judgment_target": "确认是否属于屏幕表面印记",
                    "resolution_mode": "按屏幕表面外观标准判定",
                    "standard_path": "屏幕表面印记判定",
                },
            ),
        )

    if (
        product_type == "相机镜头"
        and any(marker in text for marker in ("进灰", "异物"))
        and "消光漆" in text
        and "脱落" in text
    ):
        return (
            "local_lens_internal_dual_condition_rescue",
            (
                {
                    "normalized_issue": (
                        "相机镜头｜镜片内部｜进灰/异物｜"
                        "确认镜头内部状态"
                    ),
                    "category_l1": "镜片内部问题",
                    "category_l2": "镜头进灰异物",
                    "intent": "标准判定",
                    "subject": "镜片内部",
                    "phenomenon": "进灰或异物",
                    "judgment_target": "确认镜头内部是否进灰或存在异物",
                    "resolution_mode": "按镜片内部进灰异物标准判定",
                    "standard_path": "镜头内部进灰异物判定",
                },
                {
                    "normalized_issue": (
                        "相机镜头｜镜头内部消光漆｜脱落｜"
                        "确认消光漆状态"
                    ),
                    "category_l1": "镜片内部问题",
                    "category_l2": "消光漆脱落",
                    "intent": "标准判定",
                    "subject": "镜头内部消光漆",
                    "phenomenon": "消光漆脱落",
                    "judgment_target": "确认镜头内部消光漆是否脱落",
                    "resolution_mode": "按镜头内部消光漆标准判定",
                    "standard_path": "镜头内部消光漆脱落判定",
                },
            ),
        )

    if (
        product_type == "笔记本"
        and any(marker in text for marker in ("什么机型", "具体型号", "哪一款"))
        and any(marker in text for marker in ("有膜", "屏幕膜", "保护膜"))
        and any(marker in text for marker in ("年款", "款的", "款", "型号"))
    ):
        return (
            "local_model_plus_screen_film_rescue",
            (
                {
                    "normalized_issue": (
                        "笔记本｜设备型号｜型号/机型查询｜确认具体型号"
                    ),
                    "category_l1": "信息查询",
                    "category_l2": "型号查询",
                    "intent": "信息查询",
                    "subject": "设备型号",
                    "phenomenon": "型号或年款待确认",
                    "judgment_target": "确认设备具体型号和年款",
                    "resolution_mode": "通过系统信息或机身标签核对型号",
                    "standard_path": "型号查询",
                },
                {
                    "normalized_issue": (
                        "笔记本｜屏幕膜｜出厂配置查询｜"
                        "确认机型是否带屏幕膜"
                    ),
                    "category_l1": "信息查询",
                    "category_l2": "屏幕膜出厂配置",
                    "intent": "信息查询",
                    "subject": "屏幕膜",
                    "phenomenon": "是否为出厂配置",
                    "judgment_target": "确认机型出厂是否带屏幕膜",
                    "resolution_mode": "根据具体型号核对出厂配置信息",
                    "standard_path": "屏幕膜出厂配置查询",
                },
            ),
        )

    return "", ()


def _direct_local_multi_topic_rescue_reason(row: dict[str, Any]) -> str:
    explicit_reason, _specs = _direct_local_explicit_dual_topic_specs(row)
    if explicit_reason:
        return explicit_reason

    if len(_direct_local_notebook_query_targets(row)) >= 2:
        return "local_structured_info_query_rescue"

    text = f"{row.get('核心问题', '')}\n{row.get('聊天内容', '')}"
    repair_component_terms = (
        "主板",
        "屏幕",
        "电池",
        "后壳",
        "摄像头",
        "排线",
        "底部",
        "白色贴纸",
    )
    repair_signal_terms = (
        "非原厂",
        "第三方",
        "贴纸",
        "标签",
        "维修痕迹",
        "维修特征",
        "拆机图",
        "样式不符",
    )
    repair_component_hits = sum(1 for term in repair_component_terms if term in text)
    repair_signal_hits = sum(1 for term in repair_signal_terms if term in text)
    answer_splits_by_component = any(
        term in text
        for term in (
            "屏幕-",
            "电池-",
            "后壳-",
            "主板-",
            "其它零部件",
            "其他零部件",
        )
    )
    if (
        repair_component_hits >= 2
        and repair_signal_hits >= 1
        and ("怎么判" in text or "怎么判定" in text or answer_splits_by_component)
    ):
        return "local_repair_multi_object_rescue"

    info_query_groups = (
        ("bios锁", "BIOS锁", "无锁", "没锁"),
        ("型号", "年款", "机型"),
        ("硬盘", "内存", "品牌认证"),
        ("指纹", "支持指纹"),
    )
    info_query_hits = sum(
        1 for group in info_query_groups if any(term in text for term in group)
    )
    if info_query_hits >= 3 and any(
        term in text for term in ("是的", "不支持", "品牌认证", "没锁")
    ):
        return "local_info_query_multi_target_rescue"

    model_or_label_terms = (
        "怎么看第几款",
        "怎么核对",
        "哪一款",
        "小型号",
        "序列号",
        "标签",
        "型号",
    )
    independent_component_terms = (
        "显卡硬盘",
        "内存硬盘",
        "显卡",
        "硬盘",
        "内存",
        "这些有问题",
        "有什么问题",
        "有啥问题",
    )
    has_model_or_label_topic = any(term in text for term in model_or_label_terms)
    has_independent_component_topic = any(
        term in text for term in independent_component_terms
    )
    has_separate_followup = any(
        term in text
        for term in ("有没有什么问题", "这些有问题吗", "有问题吗", "有啥问题")
    )
    if (
        has_model_or_label_topic
        and has_independent_component_topic
        and has_separate_followup
    ):
        return "local_model_label_plus_component_rescue"
    return ""


def _direct_local_multi_topic_rescue_topics(
    source_row: dict[str, Any],
    topics: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    if len(topics) != 1:
        return topics, ""
    reason = _direct_local_multi_topic_rescue_reason(source_row)
    if not reason:
        return topics, ""

    original = dict(topics[0])
    explicit_reason, explicit_specs = _direct_local_explicit_dual_topic_specs(
        source_row
    )
    if reason == explicit_reason and explicit_specs:
        product_type = (
            canonical_product_name(source_row.get("产品类型"), unknown="")
            or _clean_text(source_row.get("产品类型"))
            or _clean_text(original.get("product_category"))
            or "待确认"
        )
        base_confidence = min(
            _float_or_default(original.get("confidence"), 0.0),
            0.78,
        )
        rescued = [
            {
                **original,
                **spec,
                "product_category": product_type,
                "scope_type": "品类专用",
                "platform": "通用",
                "brand": "通用",
                "model_scope": "通用",
                "threshold_or_exception": "无明确阈值",
                "evidence_summary": _safe_join(
                    [
                        _clean_text(original.get("evidence_summary")),
                        (
                            "本地根据聊天中明确出现的两个独立业务目标拆分，"
                            f"当前目标：{spec['judgment_target']}。"
                        ),
                    ],
                    "；",
                ),
                "confidence": base_confidence,
                "requires_review": True,
                "_local_multi_topic_rescue_reason": reason,
            }
            for spec in explicit_specs
        ]
        return rescued, reason

    if reason == "local_structured_info_query_rescue":
        rescued = _direct_local_notebook_query_topics(
            source_row,
            original,
        )
        if rescued:
            return rescued, reason

    base_issue = _clean_text(original.get("normalized_issue")) or _clean_text(
        source_row.get("核心问题")
    )
    common = {
        **original,
        "requires_review": True,
        "confidence": min(_float_or_default(original.get("confidence"), 0.0), 0.65),
        "evidence_summary": _safe_join(
            [
                _clean_text(original.get("evidence_summary")),
                "本地多主题兜底命中，模型原始只抽出一个主题，保守拆分后进入人工复核。",
            ],
            "；",
        ),
        "threshold_or_exception": "本地兜底拆分，需人工确认",
    }
    if reason == "local_repair_multi_object_rescue":
        rescued = [
            {
                **common,
                "normalized_issue": f"{base_issue}（拆修/非原厂对象1）",
                "category_l1": "拆修问题",
                "intent": "拆修对象1复核",
                "standard_path": "拆修/非原厂对象1复核",
            },
            {
                **common,
                "normalized_issue": f"{base_issue}（拆修/非原厂对象2）",
                "category_l1": "拆修问题",
                "intent": "拆修对象2复核",
                "standard_path": "拆修/非原厂对象2复核",
            },
        ]
    else:
        rescued = [
            {
                **common,
                "normalized_issue": f"{base_issue}（信息/标签目标1）",
                "category_l1": "信息查询",
                "intent": "信息查询目标1复核",
                "standard_path": "信息查询目标1复核",
            },
            {
                **common,
                "normalized_issue": f"{base_issue}（硬件/功能目标2）",
                "category_l1": "信息查询",
                "intent": "信息查询目标2复核",
                "standard_path": "信息查询目标2复核",
            },
        ]
    for topic in rescued:
        topic["_local_multi_topic_rescue_reason"] = reason
    return rescued, reason


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _direct_clustering_rule_match(
    row: dict[str, Any],
) -> ClusteringRuleMatch | None:
    if _business_line_for_row(row) != SELF_OPERATED_BUSINESS_LINE_NAME:
        row["_聚类判定规则缓存状态"] = "非自营回收不适用"
        return None
    cache_state = _clean_text(row.get("_聚类判定规则缓存状态"))
    stored_family = _clean_text(
        row.get("_聚类标准族") or row.get("clustering_standard_family")
    )
    stored_policy = _clean_text(
        row.get("_聚类合并策略") or row.get("clustering_merge_policy")
    )
    if stored_family and stored_policy:
        return ClusteringRuleMatch(
            rule_id=_clean_text(
                row.get("_聚类判定规则ID") or row.get("clustering_rule_id")
            ),
            standard_family=stored_family,
            merge_policy=stored_policy,
            phenomenon_value=_clean_text(
                row.get("_聚类现象值") or row.get("clustering_phenomenon_value")
            ),
            usage="",
            category_l1=_clean_text(
                row.get("模型主题一级分类") or row.get("category_l1")
            ),
            category_l2=_clean_text(
                row.get("模型主题二级分类") or row.get("category_l2")
            ),
        )
    if cache_state == "未匹配":
        return None

    match = _runtime_clustering_rule_match(row)
    if match is None:
        row["_聚类判定规则缓存状态"] = "未匹配"
        return None
    row.update(
        {
            "_聚类判定规则缓存状态": "已匹配",
            "_聚类判定规则ID": match.rule_id,
            "_聚类标准族": match.standard_family,
            "_聚类现象值": match.phenomenon_value,
            "_聚类合并策略": match.merge_policy,
        }
    )
    return match


def _source_clustering_rule_match(
    row: dict[str, Any],
) -> ClusteringRuleMatch | None:
    """Use source evidence to pre-classify a row before MiMo labels it.

    This is deliberately separate from standard retrieval.  The local rule
    catalog only supplies a clustering boundary; it must never become a
    knowledge正文 or an exported standard association.
    """
    if _business_line_for_row(row) != SELF_OPERATED_BUSINESS_LINE_NAME:
        return None
    product_category = _resolved_product_type_for_row(row) or _clean_text(
        row.get("产品类型")
    )
    source_core_problem = _clean_text(
        row.get("核心问题") or row.get("原始核心问题")
    )
    source_conversation = _safe_join(
        [
            _clean_text(row.get("聊天内容")),
            source_core_problem,
            _clean_text(row.get("判定结论") or row.get("原始判定结论")),
            _clean_text(row.get("判定依据")),
            _clean_text(row.get("历史实际回复")),
            _clean_text(row.get("参考话术")),
        ],
        "\n",
    )
    if not (product_category and (source_core_problem or source_conversation)):
        return None
    return match_clustering_judgment_rule(
        product_category=product_category,
        subject="",
        phenomenon="",
        normalized_issue=source_core_problem,
        conversation=source_conversation,
    )


def _model_clustering_rule_match(
    row: dict[str, Any],
) -> ClusteringRuleMatch | None:
    """Match only the model's structured fields, excluding source text."""
    product_category = _clean_text(
        row.get("product_category") or row.get("产品类型")
    )
    if not product_category:
        return None
    return match_clustering_judgment_rule(
        product_category=product_category,
        subject=_clean_text(row.get("subject") or row.get("对象/部位")),
        phenomenon=_clean_text(
            row.get("phenomenon") or row.get("异常现象")
        ),
        normalized_issue=_clean_text(
            row.get("normalized_issue") or row.get("核心问题")
        ),
        conversation="",
    )


def _clustering_rule_boundary(
    match: ClusteringRuleMatch | None,
) -> tuple[str, str, str]:
    if match is None:
        return "", "", ""
    return (
        _clean_text(match.standard_family),
        _clean_text(match.merge_policy),
        _clean_text(match.phenomenon_value),
    )


def _annotate_source_clustering_rule(row: dict[str, Any]) -> dict[str, Any]:
    """Attach an internal pre-clustering label to a source row."""
    annotated = dict(row)
    match = _source_clustering_rule_match(annotated)
    if match is None:
        annotated["_预聚类规则状态"] = "rule_not_matched"
        return annotated
    annotated.update(
        {
            "_预聚类规则状态": "rule_matched",
            "_预聚类判定规则ID": match.rule_id,
            "_预聚类标准族": match.standard_family,
            "_预聚类现象值": match.phenomenon_value,
            "_预聚类合并策略": match.merge_policy,
        }
    )
    return annotated


def _classification_catalog_query(row: dict[str, Any]) -> str:
    return _safe_join(
        [
            _clean_text(row.get("聊天内容")),
            _clean_text(row.get("核心问题") or row.get("原始核心问题")),
            _clean_text(row.get("判定结论") or row.get("原始判定结论")),
            _clean_text(row.get("判定依据")),
            _clean_text(row.get("历史实际回复")),
            _clean_text(row.get("参考话术")),
        ],
        "\n",
    )


def _annotate_source_classification_catalog(
    row: dict[str, Any],
    catalog: tuple[ClassificationCatalogItem, ...],
) -> dict[str, Any]:
    annotated = dict(row)
    if not catalog:
        annotated["_分类库状态"] = "classification_catalog_disabled"
        return annotated
    product_category = _resolved_product_type_for_row(annotated) or _clean_text(
        annotated.get("产品类型")
    )
    matches, status = retrieve_classification_matches(
        _classification_catalog_query(annotated),
        product_category=product_category,
        catalog=catalog,
        top_k=5,
    )
    annotated["_分类库状态"] = status
    annotated["_分类库候选"] = [
        {
            "class_id": match.item.class_id,
            "category_id": match.item.category_id,
            "path_str": match.item.path_str,
            "parent_path": match.path_candidate,
            "leaf": match.item.leaf,
            "score": match.score,
            "matched_terms": list(match.matched_terms),
            "is_negative": match.item.is_negative,
            "source_file": match.item.source_file,
            "source_row": match.item.source_row,
        }
        for match in matches
    ]
    if matches:
        annotated["_分类库候选标准路径"] = _safe_join(
            [match.path_candidate for match in matches[:3]],
            "；",
        )
        annotated["_分类库候选问题分类"] = _safe_join(
            [
                " > ".join(
                    part
                    for part in match.item.path[1:3]
                    if _clean_text(part)
                )
                for match in matches[:3]
            ],
            "；",
        )
    return annotated


def _classification_catalog_bucket_key(
    row: dict[str, Any],
) -> tuple[str, ...] | None:
    """Return a conservative candidate-path boundary for clustering.

    The classification catalog is a recall aid only.  It can narrow a model
    batch when the top candidate is unambiguous, but it never replaces the
    curated clustering-rule gate.
    """
    status = _clean_text(row.get("_分类库状态"))
    candidates = row.get("_分类库候选")
    if status != "classification_matched" or not isinstance(candidates, list):
        return None
    first = candidates[0] if candidates else {}
    path = _clean_text(first.get("parent_path") or first.get("path_str"))
    return ("分类库候选", path) if path else None


def _runtime_clustering_rule_match(
    row: dict[str, Any],
) -> ClusteringRuleMatch | None:
    return match_clustering_judgment_rule(
        product_category=_clean_text(
            row.get("product_category") or row.get("产品类型")
        ),
        subject=_clean_text(row.get("subject") or row.get("对象/部位")),
        phenomenon=_clean_text(
            row.get("phenomenon")
            or row.get("异常现象")
            or row.get("_聚类现象值")
            or row.get("clustering_phenomenon_value")
        ),
        normalized_issue=_clean_text(
            row.get("normalized_issue") or row.get("核心问题")
        ),
        conversation=_safe_join(
            [
                _clean_text(
                    row.get("source_conversation") or row.get("聊天内容")
                ),
                _clean_text(row.get("source_core_problem"))
                or _clean_text(row.get("原始核心问题")),
                _clean_text(row.get("source_judgment_conclusion"))
                or _clean_text(row.get("原始判定结论"))
                or _clean_text(row.get("判定结论")),
            ],
            "\n",
        ),
    )


def _direct_clustering_rule_conflict_reason(rows: list[dict[str, Any]]) -> str:
    explicit_conflicts = {
        _clean_text(row.get("_聚类规则冲突原因"))
        for row in rows
        if _clean_text(row.get("_聚类规则状态")) == "rule_model_conflict"
        and _clean_text(row.get("_聚类规则冲突原因"))
    }
    if explicit_conflicts:
        return sorted(explicit_conflicts)[0]

    matches = [_direct_clustering_rule_match(row) for row in rows]
    if not matches or any(match is None for match in matches):
        return ""

    standard_families = {
        _clean_text(match.standard_family)
        for match in matches
        if _clean_text(match.standard_family)
    }
    if len(standard_families) > 1:
        return "聚类判定口径不同，不能自动合并"

    merge_policies = {
        _clean_text(match.merge_policy)
        for match in matches
        if _clean_text(match.merge_policy)
    }
    phenomenon_values = {
        _clean_text(match.phenomenon_value)
        for match in matches
        if _clean_text(match.phenomenon_value)
    }
    if (
        merge_policies == {"separate_by_phenomenon"}
        and len(phenomenon_values) > 1
    ):
        standard_family = next(iter(standard_families), "当前标准族")
        return (
            f"同一聚类标准族“{standard_family}”但现象值不同，"
            "必须拆分"
        )
    if (
        merge_policies == {"separate_by_query_target"}
        and len(phenomenon_values) > 1
    ):
        standard_family = next(iter(standard_families), "当前标准族")
        return (
            f"同一聚类标准族“{standard_family}”但查询目标不同，"
            "必须拆分"
        )
    return ""


def _direct_clustering_rule_allows_comparison(rows: list[dict[str, Any]]) -> bool:
    matches = [_direct_clustering_rule_match(row) for row in rows]
    if not matches or any(match is None for match in matches):
        return False

    standard_families = {
        _clean_text(match.standard_family)
        for match in matches
        if _clean_text(match.standard_family)
    }
    merge_policies = {
        _clean_text(match.merge_policy)
        for match in matches
        if _clean_text(match.merge_policy)
    }
    phenomenon_values = {
        _clean_text(match.phenomenon_value)
        for match in matches
        if _clean_text(match.phenomenon_value)
    }
    if len(standard_families) != 1 or len(merge_policies) != 1:
        return False
    # A shared stored standard family is not sufficient by itself: verify that
    # the runtime rules independently recognize every row as the same family.
    # This keeps a camera lens from inheriting a housing rule only because a
    # stale or overly broad stored label says they are both appearance issues.
    if merge_policies == {"same_standard_family"}:
        runtime_matches = [_runtime_clustering_rule_match(row) for row in rows]
        runtime_families = [
            _clean_text(match.standard_family) if match is not None else ""
            for match in runtime_matches
        ]
        return (
            len(runtime_families) == len(rows)
            and all(runtime_families)
            and len(set(runtime_families)) == 1
            and _normalized_direct_reconcile_value(runtime_families[0])
            == _normalized_direct_reconcile_value(next(iter(standard_families)))
        )
    return (
        merge_policies == {"separate_by_phenomenon"}
        and len(phenomenon_values) == 1
    )


def _direct_atomic_bucket_key(unit: dict[str, Any]) -> tuple[str, ...]:
    business_line = _business_line_for_row(unit)
    product_category = _clean_text(unit.get("product_category"))
    if product_category and product_category != UNKNOWN_PRODUCT_NAME:
        if _clean_text(unit.get("_聚类规则状态")) == "rule_model_conflict":
            # A source-rule/model disagreement must not share a model batch
            # with another topic.  It will be retained as an explicit review
            # singleton after clustering.
            return (
                business_line,
                product_category,
                "聚类规则冲突",
                _clean_text(unit.get("unit_id")),
            )
        # Prefer the model's structured match for normal batching.  The
        # source pre-match remains available in the prompt and as a fallback
        # only when the model did not provide enough structured fields.
        rule_match = _model_clustering_rule_match(unit)
        if rule_match is None and not any(
            _clean_text(unit.get(field))
            for field in (
                "subject",
                "phenomenon",
                "judgment_target",
                "standard_path",
            )
        ):
            rule_match = _direct_clustering_rule_match(unit)
        if rule_match is not None:
            merge_policy = _clean_text(rule_match.merge_policy)
            phenomenon_value = _clean_text(rule_match.phenomenon_value)
            boundary_parts = [
                business_line,
                product_category,
                "聚类判定口径",
                _clean_text(rule_match.standard_family),
            ]
            return (
                *boundary_parts,
            )
        classification_boundary = _classification_catalog_bucket_key(unit)
        if classification_boundary is not None:
            return (
                business_line,
                product_category,
                *classification_boundary,
            )
        return (business_line, product_category)

    # Business line and product type are pre-clustering boundaries. Unknown
    # products stay isolated until a reviewer confirms their product scope.
    return (
        business_line,
        UNKNOWN_PRODUCT_NAME,
        _clean_text(unit.get("unit_id")),
    )


def _direct_mimo_source_signature(row: dict[str, Any]) -> tuple[str, ...] | None:
    chat = _normalize_lines(row.get("聊天内容"))
    image_links = _normalize_lines(row.get("图片链接"))
    video_links = _normalize_lines(row.get("视频链接"))
    if not (chat or image_links or video_links):
        return None
    return (
        _clean_text(row.get("工单ID")) or _clean_text(row.get("数据ID")) or _clean_text(row.get("来源记录ID")),
        _clean_text(row.get("回收单号")),
        _business_line_for_row(row),
        _clean_text(row.get("产品类型")),
        _clean_text(row.get("核心问题") or row.get("原始核心问题")),
        _clean_text(row.get("判定结论") or row.get("原始判定结论")),
        _clean_text(row.get("AI结果冲突字段")),
        chat,
        image_links,
        video_links,
    )


def _direct_mimo_dedup_signature(
    row: dict[str, Any],
) -> tuple[str, ...] | None:
    chat = _normalize_lines(row.get("聊天内容"))
    image_links = _normalize_lines(row.get("图片链接"))
    video_links = _normalize_lines(row.get("视频链接"))
    if not (chat or image_links or video_links):
        return None
    return (
        _clean_text(row.get("工单ID"))
        or _clean_text(row.get("数据ID"))
        or _clean_text(row.get("来源记录ID")),
        _clean_text(row.get("回收单号")),
        _business_line_for_row(row),
        _clean_text(row.get("产品类型")),
        _clean_text(row.get("AI结果冲突字段")),
        chat,
        image_links,
        video_links,
    )


def _direct_mimo_deduplicate_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    unique_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, ...], str] = {}
    for row in rows:
        signature = _direct_mimo_dedup_signature(row)
        if signature is None:
            unique_rows.append(row)
            continue
        sample_id = (
            _clean_text(row.get("样本ID"))
            or _clean_text(row.get("数据ID"))
            or _clean_text(row.get("工单ID"))
            or _clean_text(row.get("回收单号"))
        )
        if signature in seen:
            duplicate_rows.append(
                {
                    "sample_id": sample_id,
                    "duplicate_of": seen[signature],
                    "reason": "重复源记录：工单ID、聊天内容和媒体链接一致，已在聚类前去重",
                }
            )
            continue
        seen[signature] = sample_id
        unique_rows.append(row)
    return unique_rows, duplicate_rows


def _direct_mimo_progress_key(
    source_index: int,
    source_row: dict[str, Any],
) -> str:
    signature = _direct_mimo_source_signature(source_row) or (
        _clean_text(source_row.get("数据ID")),
        _clean_text(source_row.get("工单ID")),
        _clean_text(source_row.get("聊天内容")),
    )
    payload = json.dumps(
        {
            "source_index": source_index,
            "signature": signature,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _direct_mimo_progress_signatures(
    reviewer: MimoClient | None = None,
) -> dict[str, dict[str, Any]]:
    terminology = ensure_terminology_loaded()
    rules = clustering_rules_metadata()
    business_lines = business_line_metadata()
    products = product_taxonomy_metadata()
    rules_digest = ""
    rules_path = Path(_clean_text(rules.get("path")))
    if rules_path.is_file():
        try:
            rules_digest = hashlib.sha256(rules_path.read_bytes()).hexdigest()
        except OSError:
            rules_digest = ""
    shared = {
        "terminology_version": _clean_text(terminology.get("version")),
        "quality_rules_schema_version": _clean_text(rules.get("schema_version")),
        "quality_rules_digest": rules_digest,
        "business_taxonomy_version": _clean_text(
            business_lines.get("version")
        ),
        "business_taxonomy_digest": _clean_text(
            business_lines.get("digest")
        ),
        "product_taxonomy_version": _clean_text(products.get("version")),
        "product_taxonomy_digest": _clean_text(products.get("digest")),
    }
    reviewer_config = getattr(reviewer, "config", None)
    model = _clean_text(getattr(reviewer_config, "model", ""))
    media_model = (
        _clean_text(getattr(reviewer_config, "media_model", ""))
        or model
    )
    media_policy = (
        _clean_text(getattr(reviewer_config, "cluster_media_policy", ""))
        or "on_demand"
    )
    media_min_text_chars = getattr(
        reviewer_config,
        "cluster_media_min_text_chars",
        220,
    )
    try:
        media_min_text_chars = int(media_min_text_chars)
    except (TypeError, ValueError):
        media_min_text_chars = 220
    try:
        reconcile_model_floor = max(
            DEFAULT_DIRECT_RECONCILE_FLOOR,
            min(
                1.0,
                float(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_RECONCILE_MODEL_FLOOR",
                        str(DEFAULT_DIRECT_RECONCILE_MODEL_FLOOR),
                    )
                ),
            ),
        )
    except ValueError:
        reconcile_model_floor = DEFAULT_DIRECT_RECONCILE_MODEL_FLOOR
    try:
        atomic_batch_size = max(
            1,
            min(
                int(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_ATOMIC_BATCH_SIZE",
                        str(DEFAULT_DIRECT_ATOMIC_BATCH_SIZE),
                    )
                ),
                12,
            ),
        )
    except ValueError:
        atomic_batch_size = DEFAULT_DIRECT_ATOMIC_BATCH_SIZE
    try:
        atomic_batch_max_chars = max(
            4000,
            min(
                int(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_ATOMIC_BATCH_MAX_CHARS",
                        str(DEFAULT_DIRECT_ATOMIC_BATCH_MAX_CHARS),
                    )
                ),
                60000,
            ),
        )
    except ValueError:
        atomic_batch_max_chars = DEFAULT_DIRECT_ATOMIC_BATCH_MAX_CHARS
    return {
        "atomic": {
            **shared,
            "prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
            "local_rescue_version": DIRECT_LOCAL_RESCUE_ALGORITHM_VERSION,
            "model": model,
            "media_model": media_model,
            "media_policy": media_policy,
            "media_min_text_chars": media_min_text_chars,
            "batch_size": atomic_batch_size,
            "batch_max_chars": atomic_batch_max_chars,
        },
        "cluster": {
            **shared,
            "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
            "model": model,
        },
        "reconcile": {
            **shared,
            "prompt_version": CLUSTER_PAIR_REVIEW_PROMPT_VERSION,
            "algorithm_version": DIRECT_RECONCILE_ALGORITHM_VERSION,
            "model": model,
            "model_review_floor": reconcile_model_floor,
        },
    }


def _read_direct_mimo_progress_payload(
    progress_path: Path | None,
    source_keys: list[str],
) -> dict[str, Any]:
    if progress_path is None or not progress_path.is_file():
        return {}
    try:
        payload = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != DIRECT_MIMO_PROGRESS_VERSION
        or payload.get("source_keys") != source_keys
    ):
        return {}
    return payload


def _load_direct_mimo_progress(
    progress_path: Path | None,
    source_keys: list[str],
    progress_signatures: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    signatures = progress_signatures or _direct_mimo_progress_signatures()
    payload = _read_direct_mimo_progress_payload(progress_path, source_keys)
    if payload.get("atomic_signature") != signatures["atomic"]:
        return {}
    results = payload.get("atomic_results")
    return results if isinstance(results, dict) else {}


def _load_direct_mimo_cluster_progress(
    progress_path: Path | None,
    source_keys: list[str],
    progress_signatures: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    signatures = progress_signatures or _direct_mimo_progress_signatures()
    payload = _read_direct_mimo_progress_payload(progress_path, source_keys)
    if payload.get("cluster_signature") != signatures["cluster"]:
        return {}
    results = payload.get("cluster_results")
    return results if isinstance(results, dict) else {}


def _load_direct_mimo_reconcile_progress(
    progress_path: Path | None,
    source_keys: list[str],
    progress_signatures: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    signatures = progress_signatures or _direct_mimo_progress_signatures()
    payload = _read_direct_mimo_progress_payload(progress_path, source_keys)
    if payload.get("reconcile_signature") != signatures["reconcile"]:
        return {}
    results = payload.get("reconcile_results")
    return results if isinstance(results, dict) else {}


def _direct_mimo_cluster_progress_key(batch: list[dict[str, Any]]) -> str:
    payload_fields = (
        "unit_id",
        "atomic_id",
        "source_conversation",
        "evidence_summary",
        "normalized_issue",
        "product_category",
        "scope_type",
        "platform",
        "brand",
        "model_scope",
        "category_l1",
        "category_l2",
        "intent",
        "subject",
        "phenomenon",
        "judgment_target",
        "resolution_mode",
        "standard_path",
        "threshold_or_exception",
        "_聚类判定规则ID",
        "_聚类标准族",
        "_聚类现象值",
        "_聚类合并策略",
        "_原子需要复核",
    )
    atomic_payloads = [
        {
            field: (
                bool(unit.get(field))
                if field == "_原子需要复核"
                else _clean_text(unit.get(field))[:1200]
            )
            for field in payload_fields
        }
        for unit in sorted(
            batch,
            key=lambda item: _clean_text(
                item.get("unit_id") or item.get("atomic_id")
            ),
        )
    ]
    return hashlib.sha256(
        json.dumps(
            atomic_payloads,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _direct_mimo_reconcile_progress_key(
    candidate_row: dict[str, Any],
    target_rows: list[dict[str, Any]],
) -> str:
    candidate_ids = sorted(
        {
            _clean_text(candidate_row.get("_原子知识ID"))
            or _clean_text(candidate_row.get("数据ID"))
            or _clean_text(candidate_row.get("工单ID"))
        }
        - {""}
    )
    target_ids = sorted(
        {
            _clean_text(row.get("_原子知识ID"))
            or _clean_text(row.get("数据ID"))
            or _clean_text(row.get("工单ID"))
            for row in target_rows
        }
        - {""}
    )
    payload = {
        "candidate_ids": candidate_ids,
        "target_ids": target_ids,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _write_direct_mimo_progress(
    progress_path: Path | None,
    source_keys: list[str],
    atomic_results: dict[str, dict[str, Any]],
    cluster_results: dict[str, dict[str, Any]] | None = None,
    reconcile_results: dict[str, dict[str, Any]] | None = None,
    progress_signatures: dict[str, dict[str, Any]] | None = None,
) -> None:
    if progress_path is None:
        return
    signatures = progress_signatures or _direct_mimo_progress_signatures()
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = progress_path.with_name(
        f".{progress_path.name}.{uuid.uuid4().hex}.tmp"
    )
    existing_payload = _read_direct_mimo_progress_payload(
        progress_path,
        source_keys,
    )
    preserved_cluster_results: dict[str, dict[str, Any]] = {}
    if cluster_results is None:
        if (
            existing_payload.get("cluster_signature") == signatures["cluster"]
            and isinstance(existing_payload.get("cluster_results"), dict)
        ):
            preserved_cluster_results = existing_payload["cluster_results"]
    elif cluster_results is not None:
        preserved_cluster_results = cluster_results
    preserved_reconcile_results: dict[str, dict[str, Any]] = {}
    if reconcile_results is None:
        if (
            existing_payload.get("reconcile_signature")
            == signatures["reconcile"]
            and isinstance(existing_payload.get("reconcile_results"), dict)
        ):
            preserved_reconcile_results = existing_payload["reconcile_results"]
    else:
        preserved_reconcile_results = reconcile_results
    try:
        temporary_path.write_text(
            json.dumps(
                {
                    "version": DIRECT_MIMO_PROGRESS_VERSION,
                    "updated_at": datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                    "source_keys": source_keys,
                    "atomic_signature": signatures["atomic"],
                    "cluster_signature": signatures["cluster"],
                    "reconcile_signature": signatures["reconcile"],
                    "atomic_results": atomic_results,
                    "cluster_results": preserved_cluster_results,
                    "reconcile_results": preserved_reconcile_results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(progress_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _direct_reconcile_bucket_compatible(
    left_product_category: Any,
    right_product_category: Any,
) -> bool:
    def product_and_family(value: Any) -> tuple[str, str]:
        if isinstance(value, (list, tuple)):
            product = value[0] if value else ""
            topic_family = value[5] if len(value) > 5 else ""
            return (
                _normalized_direct_reconcile_value(product),
                _normalized_direct_reconcile_value(topic_family),
            )
        return (_normalized_direct_reconcile_value(value), "")

    left_product, left_family = product_and_family(left_product_category)
    right_product, right_family = product_and_family(right_product_category)
    if not left_product or left_product != right_product:
        return False
    return not (left_family and right_family and left_family != right_family)


def _normalized_direct_reconcile_value(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _clean_text(value).lower())
    for source, target in _DIRECT_RECONCILE_ALIAS_REPLACEMENTS:
        normalized = normalized.replace(source, target)
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _direct_reconcile_rule_match(row: dict[str, Any]) -> ClusteringRuleMatch | None:
    return _direct_clustering_rule_match(row)


def _direct_reconcile_fingerprint(
    row: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> ClusteringFingerprint:
    cache_key = id(row)
    if fingerprint_cache is not None:
        cached = fingerprint_cache.get(cache_key)
        if cached is not None:
            return cached

    fingerprint = build_clustering_fingerprint(
        product_category=_clean_text(
            row.get("产品类型") or row.get("product_category")
        ),
        category_l1=_clean_text(
            row.get("模型主题一级分类") or row.get("category_l1")
        ),
        intent=_clean_text(row.get("问题意图") or row.get("intent")),
        subject=_clean_text(row.get("对象/部位") or row.get("subject")),
        phenomenon=_clean_text(row.get("异常现象") or row.get("phenomenon")),
        normalized_issue=_clean_text(
            row.get("核心问题") or row.get("normalized_issue")
        ),
        judgment_target=_clean_text(
            row.get("判定目标") or row.get("judgment_target")
        ),
        resolution_mode=_clean_text(
            row.get("解题方式") or row.get("resolution_mode")
        ),
        standard_path=_clean_text(
            row.get("主标准路径") or row.get("standard_path")
        ),
        conversation=_clean_text(
            row.get("语义标注依据") or row.get("evidence_summary")
        ),
    )
    if fingerprint_cache is not None:
        fingerprint_cache[cache_key] = fingerprint
    return fingerprint


def _direct_reconcile_topic_kind(
    row: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> str:
    fingerprint = _direct_reconcile_fingerprint(
        row,
        fingerprint_cache=fingerprint_cache,
    )
    return fingerprint.query_target or fingerprint.detection_target


def _direct_reconcile_topic_signature(
    row: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> tuple[str, ...]:
    fingerprint = _direct_reconcile_fingerprint(
        row,
        fingerprint_cache=fingerprint_cache,
    )
    return (
        fingerprint.product_category,
        fingerprint.standard_family,
        fingerprint.merge_policy,
        fingerprint.object_key,
        fingerprint.phenomenon_value,
        fingerprint.query_target,
        fingerprint.detection_target,
    )


def _direct_reconcile_scope_values(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _normalized_direct_reconcile_value(row.get("产品类型")),
        _normalized_direct_reconcile_value(row.get("_原子平台") or row.get("platform")),
        _normalized_direct_reconcile_value(row.get("_原子品牌") or row.get("brand")),
        _normalized_direct_reconcile_value(
            row.get("_原子机型范围") or row.get("model_scope")
        ),
    )


def _direct_reconcile_is_generic_query(
    row: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> bool:
    signature = _direct_reconcile_topic_signature(
        row,
        fingerprint_cache=fingerprint_cache,
    )
    return bool(signature[5] or signature[6])


def _direct_reconcile_targets(
    row: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> tuple[str, str]:
    signature = _direct_reconcile_topic_signature(
        row,
        fingerprint_cache=fingerprint_cache,
    )
    return signature[5], signature[6]


def _direct_reconcile_same_target(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> bool:
    left_target = _direct_reconcile_targets(
        left,
        fingerprint_cache=fingerprint_cache,
    )
    right_target = _direct_reconcile_targets(
        right,
        fingerprint_cache=fingerprint_cache,
    )
    return bool(any(left_target) and left_target == right_target)


def _direct_reconcile_shared_target(
    rows: list[dict[str, Any]],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> str:
    if not rows:
        return ""
    targets = [
        _direct_reconcile_targets(
            row,
            fingerprint_cache=fingerprint_cache,
        )
        for row in rows
    ]
    if not any(targets[0]) or any(target != targets[0] for target in targets[1:]):
        return ""
    return targets[0][0] or targets[0][1]


def _direct_reconcile_shared_trusted_target(
    rows: list[dict[str, Any]],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> str:
    target = _direct_reconcile_shared_target(
        rows,
        fingerprint_cache=fingerprint_cache,
    )
    return target if target in _DIRECT_RECONCILE_TRUSTED_TARGETS else ""


def _direct_reconcile_trusted_target_floor(target: str) -> float:
    return _DIRECT_RECONCILE_TRUSTED_TARGET_FLOORS.get(
        target,
        DIRECT_RECONCILE_QUERY_RULE_FLOOR,
    )


def _direct_reconcile_multi_cluster_target(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> str:
    target = _direct_reconcile_shared_trusted_target(
        [*left_rows, *right_rows],
        fingerprint_cache=fingerprint_cache,
    )
    return (
        target
        if target in _DIRECT_RECONCILE_MULTI_CLUSTER_TARGETS
        else ""
    )


def _direct_reconcile_same_topic_family(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> bool:
    left_signature = _direct_reconcile_topic_signature(
        left,
        fingerprint_cache=fingerprint_cache,
    )
    right_signature = _direct_reconcile_topic_signature(
        right,
        fingerprint_cache=fingerprint_cache,
    )
    left_family, right_family = left_signature[1], right_signature[1]
    if left_family and right_family:
        if left_family != right_family:
            return False
        if left_signature[2] != right_signature[2]:
            return False
        if left_signature[2] == "samestandardfamily":
            return True
        return (
            left_signature[3] == right_signature[3]
            and left_signature[4] == right_signature[4]
        )

    if not left_family and not right_family:
        if _direct_reconcile_same_target(
            left,
            right,
            fingerprint_cache=fingerprint_cache,
        ):
            return True

        # Some standards are intentionally left unmatched when one case
        # contains multiple threshold outcomes. Keep the old conservative
        # fallback only when every classification field is explicit and equal.
        comparable_fields = (
            ("产品类型", "product_category"),
            ("模型主题一级分类", "category_l1"),
            ("问题意图", "intent"),
            ("对象/部位", "subject"),
            ("异常现象", "phenomenon"),
        )
        for source_field, topic_field in comparable_fields:
            left_value = _normalized_direct_reconcile_value(
                left.get(source_field) or left.get(topic_field)
            )
            right_value = _normalized_direct_reconcile_value(
                right.get(source_field) or right.get(topic_field)
            )
            if (
                not left_value
                or not right_value
                or left_value in _DIRECT_RECONCILE_UNKNOWN_VALUES
                or right_value in _DIRECT_RECONCILE_UNKNOWN_VALUES
                or left_value != right_value
            ):
                return False
        return True
    return False



def _direct_reconcile_text(row: dict[str, Any]) -> str:
    fields = (
        "核心问题",
        "模型主题一级分类",
        "模型主题二级分类",
        "问题意图",
        "对象/部位",
        "异常现象",
        "判定目标",
        "解题方式",
        "主标准路径",
        "语义标注依据",
    )
    return "|".join(
        normalized
        for field in fields
        if (normalized := _normalized_direct_reconcile_value(row.get(field)))
    )


def _direct_reconcile_character_similarity(
    left_text: str,
    right_text: str,
) -> float:
    def grams(value: str) -> Counter[str]:
        normalized = _normalized_direct_reconcile_value(value)
        return Counter(
            normalized[index : index + size]
            for size in (2, 3)
            for index in range(max(0, len(normalized) - size + 1))
        )

    left_grams = grams(left_text)
    right_grams = grams(right_text)
    if not left_grams or not right_grams:
        return 0.0
    shared = left_grams.keys() & right_grams.keys()
    dot_product = sum(
        left_grams[gram] * right_grams[gram]
        for gram in shared
    )
    left_norm = sum(value * value for value in left_grams.values()) ** 0.5
    right_norm = sum(value * value for value in right_grams.values()) ** 0.5
    return (
        dot_product / (left_norm * right_norm)
        if left_norm and right_norm
        else 0.0
    )


def _direct_reconcile_pair_similarity(
    left: dict[str, Any],
    right: dict[str, Any],
) -> float:
    left_text = _direct_reconcile_text(left)
    right_text = _direct_reconcile_text(right)
    if not left_text or not right_text:
        return 0.0
    return max(
        SequenceMatcher(None, left_text, right_text).ratio(),
        _direct_reconcile_character_similarity(left_text, right_text),
    )


def _direct_reconcile_similarity(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
) -> float:
    scores: list[float] = []
    for left in left_rows:
        for right in right_rows:
            scores.append(_direct_reconcile_pair_similarity(left, right))
    return max(scores, default=0.0)


def _direct_reconcile_candidate_floor(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    default_floor: float,
    *,
    fingerprint_cache: dict[int, ClusteringFingerprint] | None = None,
) -> float:
    pairs = [
        (left, right)
        for left in left_rows
        for right in right_rows
    ]
    trusted_target = _direct_reconcile_shared_trusted_target(
        [*left_rows, *right_rows],
        fingerprint_cache=fingerprint_cache,
    )
    if trusted_target:
        return min(
            default_floor,
            _direct_reconcile_trusted_target_floor(trusted_target),
        )
    if any(
        _direct_reconcile_same_target(
            left,
            right,
            fingerprint_cache=fingerprint_cache,
        )
        for left, right in pairs
    ):
        return min(default_floor, DIRECT_RECONCILE_QUERY_RULE_FLOOR)
    if any(
        _direct_reconcile_same_topic_family(
            left,
            right,
            fingerprint_cache=fingerprint_cache,
        )
        for left, right in pairs
    ):
        return min(default_floor, DIRECT_RECONCILE_FAMILY_RULE_FLOOR)
    return default_floor


def _direct_reconcile_rule_merge_reason(
    candidate_row: dict[str, Any],
    cluster_rows: list[dict[str, Any]],
    similarity: float,
) -> str:
    if not cluster_rows:
        return ""
    candidate_scope = _direct_reconcile_scope_values(candidate_row)
    shared_target = _direct_reconcile_shared_target(
        [candidate_row, *cluster_rows]
    )
    trusted_target = (
        shared_target
        if shared_target in _DIRECT_RECONCILE_TRUSTED_TARGETS
        else ""
    )
    same_family = all(
        _direct_reconcile_same_topic_family(candidate_row, target_row)
        for target_row in cluster_rows
    )
    same_target = all(
        _direct_reconcile_same_target(candidate_row, target_row)
        for target_row in cluster_rows
    )
    if not same_family and not same_target:
        return ""
    required_similarity = (
        _direct_reconcile_trusted_target_floor(trusted_target)
        if trusted_target
        else DIRECT_RECONCILE_FAMILY_RULE_FLOOR
        if same_family
        else DIRECT_RECONCILE_QUERY_RULE_FLOOR
    )
    if similarity < required_similarity:
        return ""
    if not trusted_target and len(cluster_rows) > 1 and any(
        _direct_reconcile_pair_similarity(candidate_row, target_row)
        < required_similarity
        for target_row in cluster_rows
    ):
        return ""

    for target_row in cluster_rows:
        target_scope = _direct_reconcile_scope_values(target_row)
        if candidate_scope[0] != target_scope[0]:
            return ""
        if trusted_target in _DIRECT_RECONCILE_SCOPE_AGNOSTIC_TARGETS:
            continue
        for left_value, right_value in zip(
            candidate_scope[1:],
            target_scope[1:],
        ):
            if (
                left_value not in _DIRECT_RECONCILE_UNKNOWN_VALUES
                and right_value not in _DIRECT_RECONCILE_UNKNOWN_VALUES
                and left_value != right_value
            ):
                return ""
    if trusted_target not in _DIRECT_RECONCILE_THRESHOLD_AGNOSTIC_TARGETS:
        candidate_thresholds = set(
            re.findall(
                r"\d+(?:\.\d+)?",
                _clean_text(candidate_row.get("主标准路径"))
                + _clean_text(candidate_row.get("阈值/例外")),
            )
        )
        for target_row in cluster_rows:
            target_thresholds = set(
                re.findall(
                    r"\d+(?:\.\d+)?",
                    _clean_text(target_row.get("主标准路径"))
                    + _clean_text(target_row.get("阈值/例外")),
                )
            )
            if (
                candidate_thresholds
                and target_thresholds
                and candidate_thresholds != target_thresholds
            ):
                return ""
    if trusted_target:
        return (
            "术语归一后属于同一可信业务目标，"
            "且品类与必要边界无冲突"
        )
    if same_family:
        return "术语归一后属于同一标准族，且对象、判定目标和阈值无冲突"
    if same_target:
        return "术语归一后属于同一查询或检测目标，且适用范围和阈值无冲突"
    return ""


def _direct_reconcile_has_hard_conflict(
    candidate: dict[str, Any],
    cluster_rows: list[dict[str, Any]],
) -> bool:
    all_rows = [candidate, *cluster_rows]
    if len({_business_line_for_row(row) for row in all_rows}) > 1:
        return True
    if any(not _resolved_product_type_for_row(row) for row in all_rows):
        return True
    trusted_target = _direct_reconcile_shared_trusted_target(all_rows)
    if (
        not trusted_target
        and _direct_clustering_rule_conflict_reason(all_rows)
    ):
        return True

    candidate_signature = _direct_reconcile_topic_signature(candidate)

    unknown_values = {"", "待确认", "未知", "通用", "不限"}
    scope_fields = ("_原子平台", "_原子品牌", "_原子机型范围")
    candidate_source_id = (
        _clean_text(candidate.get("数据ID"))
        or _clean_text(candidate.get("工单ID"))
    )
    candidate_atomic_id = _clean_text(candidate.get("_原子知识ID"))
    for member in cluster_rows:
        member_signature = _direct_reconcile_topic_signature(member)
        if candidate_signature[0] != member_signature[0]:
            return True
        member_source_id = (
            _clean_text(member.get("数据ID"))
            or _clean_text(member.get("工单ID"))
        )
        member_atomic_id = _clean_text(member.get("_原子知识ID"))
        if (
            candidate_source_id
            and candidate_source_id == member_source_id
            and candidate_atomic_id
            and member_atomic_id
            and candidate_atomic_id != member_atomic_id
        ):
            return True
        if (
            not trusted_target
            and
            candidate_signature[1]
            and member_signature[1]
            and candidate_signature[1] != member_signature[1]
        ):
            return True
        if (
            any(candidate_signature[5:7])
            and any(member_signature[5:7])
            and candidate_signature[5:7] != member_signature[5:7]
        ):
            return True
        if (
            not trusted_target
            and
            candidate_signature[1]
            and member_signature[1]
            and not _direct_reconcile_same_topic_family(candidate, member)
        ):
            return True
        if trusted_target not in _DIRECT_RECONCILE_SCOPE_AGNOSTIC_TARGETS:
            for field in scope_fields:
                candidate_value = _clean_text(candidate.get(field))
                member_value = _clean_text(member.get(field))
                if (
                    candidate_value not in unknown_values
                    and member_value not in unknown_values
                    and candidate_value != member_value
                ):
                    return True
        if trusted_target not in _DIRECT_RECONCILE_THRESHOLD_AGNOSTIC_TARGETS:
            candidate_thresholds = set(
                re.findall(
                    r"\d+(?:\.\d+)?",
                    _clean_text(candidate.get("_原子阈值例外")),
                )
            )
            member_thresholds = set(
                re.findall(
                    r"\d+(?:\.\d+)?",
                    _clean_text(member.get("_原子阈值例外")),
                )
            )
            if (
                candidate_thresholds
                and member_thresholds
                and candidate_thresholds != member_thresholds
            ):
                return True
    return False


def _direct_cluster_hard_conflict_reason(rows: list[dict[str, Any]]) -> str:
    if len(rows) <= 1:
        return ""

    atomic_ids = {
        _clean_text(row.get("_原子知识ID"))
        for row in rows
        if _clean_text(row.get("_原子知识ID"))
    }
    source_ids = [
        _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
        for row in rows
    ]
    if len(atomic_ids) > 1 and len(set(source_ids)) == 1:
        return "同一会话拆出的多个原子问题不能在自动聚类阶段重新合并"

    business_lines = {_business_line_for_row(row) for row in rows}
    if len(business_lines) > 1:
        return "回收业务层级不同，绝对不能自动聚类合并"

    if any(not _resolved_product_type_for_row(row) for row in rows):
        return "未配置品类或未识别产品不能自动形成多成员主题簇"

    product_categories = {
        _resolved_product_type_for_row(row)
        for row in rows
    }
    if len(product_categories) > 1:
        return "产品品类不同，绝对不能自动聚类合并"

    trusted_targets = [
        target
        if target in _DIRECT_RECONCILE_TRUSTED_TARGETS
        else ""
        for target in (
            _direct_reconcile_topic_kind(row)
            for row in rows
        )
    ]
    explicit_trusted_targets = {
        target
        for target in trusted_targets
        if target
    }
    if (
        len(explicit_trusted_targets) > 1
        or (
            explicit_trusted_targets
            and any(not target for target in trusted_targets)
        )
    ):
        return "可信业务目标不同，不能保留在同一主题簇"

    judgment_rule_conflict = _direct_clustering_rule_conflict_reason(rows)
    if judgment_rule_conflict:
        return judgment_rule_conflict

    uncertain_values = {"", "待确认", "未知", "其他待确认", "通用", "不限"}
    guarded_fields = (
        ()
        if _direct_clustering_rule_allows_comparison(rows)
        else (
            ("模型主题二级分类", "二级分类不同"),
            ("对象/部位", "判定对象不同"),
            ("主标准路径", "标准路径不同"),
        )
    )
    for field, reason in guarded_fields:
        values = {
            _clean_text(row.get(field))
            for row in rows
            if _clean_text(row.get(field)) not in uncertain_values
        }
        if len(values) > 1:
            return reason
    return ""


def _reconcile_direct_topic_groups(
    topic_groups: list[tuple[tuple[str, ...], list[dict[str, Any]]]],
    reviewer: MimoClient,
    meta: dict[str, Any],
    *,
    review_floor: float = DEFAULT_DIRECT_RECONCILE_FLOOR,
    model_review_floor: float = DEFAULT_DIRECT_RECONCILE_MODEL_FLOOR,
    review_limit: int = DEFAULT_DIRECT_RECONCILE_LIMIT,
    reconcile_results: dict[str, dict[str, Any]] | None = None,
    cache_update_callback: Callable[
        [dict[str, dict[str, Any]]],
        None,
    ]
    | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[tuple[tuple[str, ...], list[dict[str, Any]]]]:
    try:
        review_limit = max(
            0,
            int(os.getenv("ANSWER_HUB_DIRECT_RECONCILE_LIMIT", str(review_limit))),
        )
    except ValueError:
        review_limit = max(0, review_limit)
    try:
        model_review_floor = max(
            review_floor,
            min(
                1.0,
                float(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_RECONCILE_MODEL_FLOOR",
                        str(model_review_floor),
                    )
                ),
            ),
        )
    except ValueError:
        model_review_floor = max(review_floor, min(1.0, model_review_floor))
    cached_reconcile_results = (
        reconcile_results if isinstance(reconcile_results, dict) else {}
    )
    meta.update(
        {
            "direct_reconcile_floor": review_floor,
            "direct_reconcile_model_floor": model_review_floor,
            "direct_reconcile_limit": review_limit,
            "direct_reconcile_candidates": 0,
            "direct_reconcile_candidates_completed": 0,
            "direct_reconcile_calls": 0,
            "direct_reconcile_cache_hits": 0,
            "direct_reconcile_approved": 0,
            "direct_reconcile_rejected": 0,
            "direct_reconcile_uncertain": 0,
            "direct_reconcile_failed": 0,
            "direct_reconcile_hard_rejected": 0,
            "direct_reconcile_limit_reached": 0,
            "direct_reconcile_rule_approved": 0,
            "direct_reconcile_model_skipped": 0,
            "direct_reconcile_model_floor_skipped": 0,
        }
    )
    has_model_reviewer = bool(
        hasattr(reviewer, "review_cluster_membership")
        or hasattr(reviewer, "review_cluster_pair")
    )

    records = [
        {
            "key": key,
            "business_line": _business_line_for_row(rows[0]),
            "product_category": _clean_text(rows[0].get("产品类型")),
            "rows": list(rows),
            "reconcilable": all(
                _clean_text(row.get("_聚类裁决提供方"))
                in DIRECT_RECONCILABLE_PROVIDERS
                for row in rows
            ),
        }
        for key, rows in topic_groups
    ]
    # `records` retains every row for this reconciliation pass, so object ids
    # are stable cache keys without writing transient values into row data.
    fingerprint_cache: dict[int, ClusteringFingerprint] = {}
    candidates: list[tuple[float, int, int]] = []
    for left_index, left in enumerate(records):
        if not left["reconcilable"]:
            continue
        for right_index in range(left_index + 1, len(records)):
            right = records[right_index]
            if (
                not right["reconcilable"]
                or left["business_line"] != right["business_line"]
                or not _direct_reconcile_bucket_compatible(
                    left["product_category"],
                    right["product_category"],
                )
                or (
                    len(left["rows"]) > 1
                    and len(right["rows"]) > 1
                    and not _direct_reconcile_multi_cluster_target(
                        left["rows"],
                        right["rows"],
                        fingerprint_cache=fingerprint_cache,
                    )
                )
            ):
                continue
            similarity = _direct_reconcile_similarity(left["rows"], right["rows"])
            candidate_floor = _direct_reconcile_candidate_floor(
                left["rows"],
                right["rows"],
                review_floor,
                fingerprint_cache=fingerprint_cache,
            )
            if similarity >= candidate_floor:
                candidates.append((similarity, left_index, right_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    meta["direct_reconcile_candidates"] = len(candidates)
    if progress_callback and candidates:
        progress_callback(
            "正在执行同品类单例二次归并。",
            {
                "direct_reconcile_candidates_completed": 0,
                "direct_reconcile_candidates_total": len(candidates),
                "direct_reconcile_calls": 0,
                "direct_reconcile_cache_hits": 0,
                "direct_reconcile_limit": review_limit,
                "direct_reconcile_model_floor": model_review_floor,
            },
        )

    parent = list(range(len(records)))
    cluster_rows = {index: list(record["rows"]) for index, record in enumerate(records)}
    cluster_indices = {index: {index} for index in range(len(records))}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def report_reconcile_progress(
        detail: str,
        completed: int,
    ) -> None:
        meta["direct_reconcile_candidates_completed"] = completed
        if progress_callback:
            progress_callback(
                detail,
                {
                    "direct_reconcile_candidates_completed": completed,
                    "direct_reconcile_candidates_total": len(candidates),
                    "direct_reconcile_calls": meta["direct_reconcile_calls"],
                    "direct_reconcile_cache_hits": meta[
                        "direct_reconcile_cache_hits"
                    ],
                    "direct_reconcile_rule_approved": meta[
                        "direct_reconcile_rule_approved"
                    ],
                    "direct_reconcile_limit": review_limit,
                    "direct_reconcile_model_floor": model_review_floor,
                },
            )

    for candidate_index, (similarity, left_index, right_index) in enumerate(
        candidates,
        start=1,
    ):
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root == right_root:
            if candidate_index % 50 == 0:
                report_reconcile_progress(
                    "正在筛选同品类二次归并候选。",
                    candidate_index,
                )
            continue
        left_rows = cluster_rows[left_root]
        right_rows = cluster_rows[right_root]
        multi_cluster_target = _direct_reconcile_multi_cluster_target(
            left_rows,
            right_rows,
            fingerprint_cache=fingerprint_cache,
        )
        if (
            len(left_rows) > 1
            and len(right_rows) > 1
            and not multi_cluster_target
        ):
            if candidate_index % 50 == 0:
                report_reconcile_progress(
                    "正在筛选同品类二次归并候选。",
                    candidate_index,
                )
            continue
        if len(left_rows) == len(right_rows) == 1:
            candidate_root = max(left_root, right_root)
            target_root = min(left_root, right_root)
        elif len(left_rows) == 1:
            candidate_root, target_root = left_root, right_root
        elif len(right_rows) == 1:
            candidate_root, target_root = right_root, left_root
        else:
            candidate_root = max(left_root, right_root)
            target_root = min(left_root, right_root)

        candidate_rows = cluster_rows[candidate_root]
        candidate_row = candidate_rows[0]
        target_rows = cluster_rows[target_root]
        if any(
            _direct_reconcile_has_hard_conflict(
                current_candidate,
                target_rows,
            )
            for current_candidate in candidate_rows
        ):
            meta["direct_reconcile_hard_rejected"] += 1
            if candidate_index % 50 == 0:
                report_reconcile_progress(
                    "正在应用二次归并硬门禁。",
                    candidate_index,
                )
            continue
        auto_reason = _direct_reconcile_rule_merge_reason(
            candidate_row,
            target_rows,
            similarity,
        )
        if auto_reason:
            merged_title_candidates = [
                *(
                    _clean_text(row.get("_聚类主题标题"))
                    for row in candidate_rows
                ),
                *(_clean_text(row.get("_聚类主题标题")) for row in target_rows),
            ]
            merged_title = max((title for title in merged_title_candidates if title), key=len, default="")
            for current_candidate in candidate_rows:
                current_candidate.update(
                    {
                        "_聚类主题标题": merged_title,
                        "_聚类决策": "规则自动合并",
                        "_聚类候选相似度": round(similarity, 4),
                        "_聚类裁决提供方": "mimo-direct-reconcile-rule",
                        "_聚类裁决原因": auto_reason,
                        "_聚类裁决置信度": 0.99,
                    }
                )
            if merged_title:
                for member_row in target_rows:
                    member_row["_聚类主题标题"] = merged_title
            parent[candidate_root] = target_root
            cluster_rows[target_root].extend(cluster_rows[candidate_root])
            cluster_rows[candidate_root] = []
            cluster_indices[target_root].update(cluster_indices[candidate_root])
            cluster_indices[candidate_root] = set()
            meta["direct_reconcile_approved"] += 1
            meta["direct_reconcile_rule_approved"] += 1
            if candidate_index % 25 == 0:
                report_reconcile_progress(
                    "正在使用本地质检口径归并同品类主题。",
                    candidate_index,
                )
            continue
        if len(candidate_rows) > 1:
            meta["direct_reconcile_model_floor_skipped"] += 1
            if candidate_index % 50 == 0:
                report_reconcile_progress(
                    "正在跳过不满足本地门禁的多成员簇候选。",
                    candidate_index,
                )
            continue
        if similarity < model_review_floor:
            meta["direct_reconcile_model_floor_skipped"] += 1
            if candidate_index % 50 == 0:
                report_reconcile_progress(
                    "正在跳过低相似度二次模型候选。",
                    candidate_index,
                )
            continue
        if meta["direct_reconcile_calls"] >= review_limit:
            meta["direct_reconcile_limit_reached"] += 1
            report_reconcile_progress(
                "二次模型复核已达到本轮调用上限。",
                candidate_index - 1,
            )
            break

        reconcile_cache_key = _direct_mimo_reconcile_progress_key(
            candidate_row,
            target_rows,
        )
        cached_review = cached_reconcile_results.get(reconcile_cache_key)
        review_from_cache = bool(
            isinstance(cached_review, dict)
            and isinstance(cached_review.get("review"), dict)
        )
        if review_from_cache:
            review = dict(cached_review["review"])
            meta["direct_reconcile_cache_hits"] += 1
        else:
            if not has_model_reviewer:
                meta["direct_reconcile_model_skipped"] += 1
                continue
            report_reconcile_progress(
                "正在调用模型执行同品类二次归并复核。",
                candidate_index - 1,
            )
            meta["direct_reconcile_calls"] += 1
            try:
                if hasattr(reviewer, "review_cluster_membership"):
                    review = reviewer.review_cluster_membership(
                        _cluster_validation_payload(candidate_row),
                        [
                            _cluster_membership_member_payload(member)
                            for member in target_rows
                        ],
                        similarity,
                        model_review_floor,
                    ).candidate
                else:
                    review = reviewer.review_cluster_pair(
                        _cluster_validation_payload(candidate_row),
                        _cluster_validation_payload(target_rows[0]),
                        similarity,
                        model_review_floor,
                    ).candidate
            except Exception:
                meta["direct_reconcile_failed"] += 1
                review_failure_reason = (
                    "同品类二次归并复核失败，已保守保留原主题"
                )
                for review_row in [candidate_row, *target_rows]:
                    review_row["_聚类需要复核"] = True
                    review_row["人工优先复核原因"] = _safe_join(
                        [
                            _clean_text(
                                review_row.get("人工优先复核原因")
                            ),
                            review_failure_reason,
                        ],
                        "；",
                    )
                report_reconcile_progress(
                    "同品类二次归并复核失败，已保守保留原主题。",
                    candidate_index,
                )
                continue
            cached_reconcile_results[reconcile_cache_key] = {
                "review": review,
                "similarity": round(similarity, 4),
                "candidate_atomic_ids": sorted(
                    {
                        _clean_text(candidate_row.get("_原子知识ID"))
                    }
                    - {""}
                ),
                "target_atomic_ids": sorted(
                    {
                        _clean_text(row.get("_原子知识ID"))
                        for row in target_rows
                    }
                    - {""}
                ),
            }
            if cache_update_callback:
                cache_update_callback(cached_reconcile_results)

        decision = _clean_text(review.get("decision"))
        if decision == "同一主题":
            candidate_row.update(
                {
                    "_聚类主题标题": _clean_text(review.get("topic_label")),
                    "_聚类决策": "单例二次裁决确认合并",
                    "_聚类候选相似度": round(similarity, 4),
                    "_聚类裁决提供方": (
                        "mimo-direct-reconcile-cache"
                        if review_from_cache
                        else "mimo-direct-reconcile"
                    ),
                    "_聚类裁决原因": _clean_text(review.get("reason")),
                    "_聚类裁决置信度": review.get("confidence", ""),
                }
            )
            if _clean_text(review.get("topic_label")):
                for member_row in target_rows:
                    member_row["_聚类主题标题"] = _clean_text(review.get("topic_label"))
            parent[candidate_root] = target_root
            cluster_rows[target_root].extend(cluster_rows[candidate_root])
            cluster_rows[candidate_root] = []
            cluster_indices[target_root].update(cluster_indices[candidate_root])
            cluster_indices[candidate_root] = set()
            meta["direct_reconcile_approved"] += 1
        elif decision == "不同主题":
            meta["direct_reconcile_rejected"] += 1
        else:
            meta["direct_reconcile_uncertain"] += 1
            for review_row in [candidate_row, *target_rows]:
                review_row["_聚类需要复核"] = True
                review_row["人工优先复核原因"] = _safe_join(
                    [
                        _clean_text(
                            review_row.get("人工优先复核原因")
                        ),
                        "同品类二次归并裁决不确定",
                    ],
                    "；",
                )
        report_reconcile_progress(
            (
                "已复用二次归并缓存结果。"
                if review_from_cache
                else "已完成一次同品类二次归并复核。"
            ),
            candidate_index,
        )

    reconciled: list[tuple[tuple[str, ...], list[dict[str, Any]]]] = []
    for index, record in enumerate(records):
        if find(index) != index:
            continue
        rows = cluster_rows[index]
        member_ids = sorted(
            {
                _clean_text(row.get("_原子知识ID"))
                for row in rows
                if _clean_text(row.get("_原子知识ID"))
            }
        )
        merged = len(cluster_indices[index]) > 1
        key = (
            (
                "direct_mimo",
                record["business_line"],
                record["product_category"],
                "reconciled",
                f"cluster-{min(cluster_indices[index]) + 1}",
                *member_ids,
            )
            if merged
            else record["key"]
        )
        reconciled.append((key, rows))
    if progress_callback and candidates:
        report_reconcile_progress(
            "同品类二次归并阶段完成。",
            min(
                len(candidates),
                max(
                    meta["direct_reconcile_candidates_completed"],
                    len(candidates)
                    if not meta["direct_reconcile_limit_reached"]
                    else meta["direct_reconcile_candidates_completed"],
                ),
            ),
        )
    if meta["direct_reconcile_calls"] or meta["direct_reconcile_cache_hits"]:
        meta["provider"] = "mimo-atomic-extraction+direct-topic-clustering+singleton-reconciliation"
    return reconciled


def _direct_mimo_topic_groups(
    rows: list[dict[str, Any]],
    reviewer: MimoClient,
    classification_catalog: tuple[ClassificationCatalogItem, ...] = (),
    batch_size: int | None = None,
    atomic_max_workers: int | None = None,
    progress_path: Path | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[list[tuple[tuple[str, ...], list[dict[str, Any]]]], dict[str, Any]]:
    deduped_rows, duplicate_rows = _direct_mimo_deduplicate_rows(rows)
    # Pre-classify from source evidence before any MiMo call.  The local
    # clustering catalog defines the first grouping boundary; it is not a
    # standard reference and must never become exported knowledge content.
    deduped_rows = [
        _annotate_source_clustering_rule(
            _annotate_source_classification_catalog(
                row,
                classification_catalog,
            )
        )
        for row in deduped_rows
    ]
    if batch_size is None:
        try:
            batch_size = int(
                os.getenv(
                    "ANSWER_HUB_DIRECT_MIMO_BATCH_SIZE",
                    str(DEFAULT_DIRECT_MIMO_BATCH_SIZE),
                )
            )
        except ValueError:
            batch_size = DEFAULT_DIRECT_MIMO_BATCH_SIZE
    batch_size = max(2, min(int(batch_size), 40))
    atomic_units: list[dict[str, Any]] = []
    row_by_atomic_id: dict[str, dict[str, Any]] = {}
    used_atomic_ids: set[str] = set()
    meta: dict[str, Any] = {
        "provider": "mimo-atomic-extraction+direct-topic-clustering",
        "model": reviewer.config.model,
        "atomic_prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
        "cluster_prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
        "atomic_extraction_calls": 0,
        "atomic_extraction_request_jobs": 0,
        "atomic_extraction_model_requests": 0,
        "atomic_extraction_batch_calls": 0,
        "atomic_extraction_batch_splits": 0,
        "atomic_extraction_failed": 0,
        "atomic_extraction_failure_reasons": [],
        "atomic_extraction_recovered": 0,
        "atomic_extraction_recovery_reasons": [],
        "atomic_extraction_cache_hits": 0,
        "atomic_unit_count": 0,
        "duplicate_source_count": len(duplicate_rows),
        "duplicate_source_samples": [item["sample_id"] for item in duplicate_rows[:20]],
        "direct_cluster_calls": 0,
        "direct_cluster_cache_hits": 0,
        "direct_cluster_failed": 0,
        "direct_cluster_failure_reasons": [],
        "direct_cluster_circuit_open": False,
        "direct_cluster_retry_splits": 0,
        "direct_cluster_retry_succeeded": 0,
        "direct_cluster_retry_exhausted_batches": 0,
        "direct_cluster_last_error": "",
        "direct_review_singletons": 0,
        "direct_review_candidates": 0,
        "direct_split_singletons": 0,
        "direct_unassigned_singletons": 0,
        "direct_post_guard_split_clusters": 0,
        "direct_post_guard_singletons": 0,
        "direct_foreign_member_ids_ignored": 0,
        "direct_foreign_member_id_samples": [],
        "direct_batch_size": batch_size,
        "atomic_unit_id_collisions_resolved": 0,
        "atomic_unit_id_collision_samples": [],
        "atomic_product_conflicts": 0,
        "atomic_product_conflict_samples": [],
        "atomic_human_evidence_conflicts": 0,
        "atomic_human_evidence_conflict_samples": [],
        "local_multi_topic_rescue": 0,
        "local_multi_topic_rescue_samples": [],
        "clustering_judgment_rule_match_count": 0,
        "clustering_judgment_rule_ids": [],
        "clustering_rule_pre_match_count": sum(
            _clean_text(row.get("_预聚类规则状态")) == "rule_matched"
            for row in deduped_rows
        ),
        "clustering_rule_model_conflict_count": 0,
        "clustering_rule_not_matched_count": sum(
            _clean_text(row.get("_预聚类规则状态")) == "rule_not_matched"
            for row in deduped_rows
        ),
        "classification_catalog_enabled": bool(classification_catalog),
        "classification_catalog_match_count": sum(
            _clean_text(row.get("_分类库状态"))
            == "classification_matched"
            for row in deduped_rows
        ),
        "classification_catalog_ambiguous_count": sum(
            _clean_text(row.get("_分类库状态"))
            == "classification_ambiguous"
            for row in deduped_rows
        ),
        "classification_catalog_not_matched_count": sum(
            _clean_text(row.get("_分类库状态"))
            == "classification_not_matched"
            for row in deduped_rows
        ),
    }
    model_calls_before_atomic: int | None = None
    metrics_snapshot = getattr(reviewer, "metrics_snapshot", None)
    if callable(metrics_snapshot):
        try:
            model_calls_before_atomic = int(
                metrics_snapshot().get("model_calls", 0)
            )
        except (AttributeError, TypeError, ValueError):
            model_calls_before_atomic = None

    def unique_atomic_id(base_id: str, source_index: int, topic_index: int) -> str:
        candidate = f"{base_id}-U{topic_index}"
        if candidate not in used_atomic_ids:
            used_atomic_ids.add(candidate)
            return candidate

        meta["atomic_unit_id_collisions_resolved"] += 1
        if len(meta["atomic_unit_id_collision_samples"]) < 20:
            meta["atomic_unit_id_collision_samples"].append(candidate)
        candidate = f"{base_id}-R{source_index:05d}-U{topic_index}"
        duplicate_index = 2
        while candidate in used_atomic_ids:
            candidate = f"{base_id}-R{source_index:05d}-U{topic_index}-D{duplicate_index}"
            duplicate_index += 1
        used_atomic_ids.add(candidate)
        return candidate

    def record_failure_detail(
        key: str,
        reason: str,
        sample_ids: Iterable[str],
    ) -> None:
        normalized_reason = _clean_text(reason)[:240]
        if not normalized_reason:
            return
        details = meta.setdefault(key, [])
        sample_values = [
            _clean_text(sample_id)
            for sample_id in sample_ids
            if _clean_text(sample_id)
        ]
        for item in details:
            if item.get("reason") != normalized_reason:
                continue
            item["count"] = int(item.get("count", 0) or 0) + 1
            item["sample_ids"] = list(
                dict.fromkeys(
                    [*item.get("sample_ids", []), *sample_values]
                )
            )[:5]
            return
        if len(details) < 20:
            details.append(
                {
                    "reason": normalized_reason,
                    "count": 1,
                    "sample_ids": list(dict.fromkeys(sample_values))[:5],
                }
            )

    try:
        max_workers = max(
            1,
            min(
                int(
                    os.getenv(
                        "ANSWER_HUB_MIMO_MAX_WORKERS",
                        str(DEFAULT_DIRECT_MIMO_MAX_WORKERS),
                    )
                ),
                8,
            ),
        )
    except ValueError:
        max_workers = DEFAULT_DIRECT_MIMO_MAX_WORKERS
    configured_max_workers = max_workers
    if atomic_max_workers is not None:
        max_workers = min(max_workers, max(1, atomic_max_workers))
    meta["max_workers"] = max_workers
    try:
        progress_flush_every = max(
            1,
            min(
                int(os.getenv("ANSWER_HUB_DIRECT_PROGRESS_FLUSH_EVERY", "5")),
                50,
            ),
        )
    except ValueError:
        progress_flush_every = 5
    meta["direct_progress_flush_every"] = progress_flush_every
    batch_atomic_supported = callable(
        getattr(reviewer, "analyze_cluster_units_batch", None)
    ) and callable(getattr(reviewer, "can_batch_cluster_units", None))
    try:
        atomic_batch_size = max(
            1,
            min(
                int(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_ATOMIC_BATCH_SIZE",
                        str(DEFAULT_DIRECT_ATOMIC_BATCH_SIZE),
                    )
                ),
                12,
            ),
        )
    except ValueError:
        atomic_batch_size = DEFAULT_DIRECT_ATOMIC_BATCH_SIZE
    try:
        atomic_batch_max_chars = max(
            4000,
            min(
                int(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_ATOMIC_BATCH_MAX_CHARS",
                        str(DEFAULT_DIRECT_ATOMIC_BATCH_MAX_CHARS),
                    )
                ),
                60000,
            ),
        )
    except ValueError:
        atomic_batch_max_chars = DEFAULT_DIRECT_ATOMIC_BATCH_MAX_CHARS
    if not batch_atomic_supported:
        atomic_batch_size = 1
    meta["atomic_extraction_batch_supported"] = batch_atomic_supported
    meta["atomic_extraction_batch_size"] = atomic_batch_size
    meta["atomic_extraction_batch_max_chars"] = atomic_batch_max_chars

    def extract_atomic_topics(
        item: tuple[int, dict[str, Any]],
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]], bool, str]:
        source_index, source_row = item
        try:
            result = reviewer.analyze_cluster_units(source_row)
            topics = list(result.candidate.get("topics") or [])
            failed = False
        except Exception as exc:
            failure_reason = f"{type(exc).__name__}: {_clean_text(str(exc))[:240]}"
            fallback_topic = _direct_atomic_fallback(source_row)
            if _is_recoverable_atomic_validation_failure(
                source_row,
                failure_reason,
            ):
                fallback_topic["_atomic_extraction_recovery_reason"] = (
                    failure_reason
                )
                failed = False
            else:
                fallback_topic["_atomic_extraction_failure_reason"] = failure_reason
                failed = True
            topics = [fallback_topic]
        if not topics:
            topics = [_direct_atomic_fallback(source_row)]
        topics, rescue_reason = _direct_local_multi_topic_rescue_topics(
            source_row,
            topics,
        )
        return source_index, source_row, topics, failed, rescue_reason

    indexed_rows = list(enumerate(deduped_rows, start=1))
    source_keys = [
        _direct_mimo_progress_key(source_index, source_row)
        for source_index, source_row in indexed_rows
    ]
    progress_signatures = _direct_mimo_progress_signatures(reviewer)
    cached_atomic_results = _load_direct_mimo_progress(
        progress_path,
        source_keys,
        progress_signatures,
    )
    cached_cluster_results = _load_direct_mimo_cluster_progress(
        progress_path,
        source_keys,
        progress_signatures,
    )
    cached_reconcile_results = _load_direct_mimo_reconcile_progress(
        progress_path,
        source_keys,
        progress_signatures,
    )
    extracted_by_index: dict[
        int,
        tuple[int, dict[str, Any], list[dict[str, Any]], bool, str],
    ] = {}
    pending_items: list[tuple[int, dict[str, Any], str]] = []
    for position, (source_index, source_row) in enumerate(indexed_rows):
        source_key = source_keys[position]
        cached = cached_atomic_results.get(source_key)
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("topics"), list)
            and not bool(cached.get("failed"))
        ):
            extracted_by_index[source_index] = (
                source_index,
                source_row,
                list(cached["topics"]),
                bool(cached.get("failed")),
                _clean_text(cached.get("rescue_reason")),
            )
            meta["atomic_extraction_cache_hits"] += 1
        else:
            pending_items.append((source_index, source_row, source_key))

    total_rows = len(indexed_rows)
    completed_rows = len(extracted_by_index)
    atomic_batches_completed = 0
    atomic_batches_total = 0

    def report_atomic_progress(detail: str) -> None:
        if progress_callback:
            progress_callback(
                detail,
                {
                    "atomic_extraction_completed": completed_rows,
                    "atomic_extraction_total": total_rows,
                    "atomic_extraction_batches_completed": atomic_batches_completed,
                    "atomic_extraction_batches_total": atomic_batches_total,
                    "atomic_extraction_batch_size": atomic_batch_size,
                    "atomic_extraction_cache_hits": meta[
                        "atomic_extraction_cache_hits"
                    ],
                    "atomic_extraction_model_requests": meta[
                        "atomic_extraction_model_requests"
                    ],
                    "atomic_extraction_batch_calls": meta[
                        "atomic_extraction_batch_calls"
                    ],
                    "atomic_extraction_workers": max_workers,
                },
            )

    def store_atomic_result(
        source_key: str,
        result: tuple[
            int,
            dict[str, Any],
            list[dict[str, Any]],
            bool,
            str,
        ],
    ) -> None:
        nonlocal completed_rows
        source_index, _source_row, topics, failed, rescue_reason = result
        extracted_by_index[source_index] = result
        cached_atomic_results[source_key] = {
            "topics": topics,
            "failed": failed,
            "rescue_reason": rescue_reason,
        }
        meta["atomic_extraction_calls"] += 1
        if failed:
            meta["atomic_extraction_failed"] += 1
        recovery_reasons = {
            _clean_text(topic.get("_atomic_extraction_recovery_reason"))
            for topic in topics
            if _clean_text(topic.get("_atomic_extraction_recovery_reason"))
        }
        if recovery_reasons:
            meta["atomic_extraction_recovered"] += 1
            for recovery_reason in recovery_reasons:
                record_failure_detail(
                    "atomic_extraction_recovery_reasons",
                    recovery_reason,
                    [
                        _clean_text(_source_row.get("数据ID"))
                        or _clean_text(_source_row.get("工单ID"))
                    ],
                )
        completed_rows += 1
        if (
            completed_rows == total_rows
            or completed_rows % progress_flush_every == 0
        ):
            _write_direct_mimo_progress(
                progress_path,
                source_keys,
                cached_atomic_results,
                cached_cluster_results,
                progress_signatures=progress_signatures,
            )

    atomic_jobs: list[list[tuple[int, dict[str, Any], str]]] = []
    batchable_by_product: dict[
        tuple[str, str],
        list[tuple[int, dict[str, Any], str]],
    ] = {}
    for pending_item in pending_items:
        _source_index, source_row, _source_key = pending_item
        can_batch = False
        if batch_atomic_supported and atomic_batch_size > 1:
            try:
                can_batch = bool(reviewer.can_batch_cluster_units(source_row))
            except Exception:
                can_batch = False
        if not can_batch:
            atomic_jobs.append([pending_item])
            continue
        product_key = (
            _business_line_for_row(source_row),
            (
                canonical_product_name(
                    source_row.get("产品类型"),
                    unknown="",
                )
                or _clean_text(source_row.get("产品类型"))
                or UNKNOWN_PRODUCT_NAME
            ),
        )
        batchable_by_product.setdefault(product_key, []).append(pending_item)

    for product_items in batchable_by_product.values():
        current_job: list[tuple[int, dict[str, Any], str]] = []
        current_chars = 0
        for pending_item in product_items:
            row_chars = min(
                len(_clean_text(pending_item[1].get("聊天内容"))),
                9000,
            ) + 500
            if current_job and (
                len(current_job) >= atomic_batch_size
                or current_chars + row_chars > atomic_batch_max_chars
            ):
                atomic_jobs.append(current_job)
                current_job = []
                current_chars = 0
            current_job.append(pending_item)
            current_chars += row_chars
        if current_job:
            atomic_jobs.append(current_job)

    atomic_batches_total = len(atomic_jobs)
    meta["atomic_extraction_batches_completed"] = atomic_batches_completed
    meta["atomic_extraction_batches_total"] = atomic_batches_total
    report_atomic_progress("正在拆分原子问题，正在等待首批 MiMo 响应。")

    def extract_atomic_job(
        job: list[tuple[int, dict[str, Any], str]],
    ) -> tuple[
        list[
            tuple[
                str,
                tuple[
                    int,
                    dict[str, Any],
                    list[dict[str, Any]],
                    bool,
                    str,
                ],
            ]
        ],
        dict[str, int],
    ]:
        stats = {
            "model_requests": 0,
            "batch_calls": 0,
            "batch_splits": 0,
        }
        if len(job) == 1:
            source_index, source_row, source_key = job[0]
            stats["model_requests"] = 1
            return (
                [
                    (
                        source_key,
                        extract_atomic_topics((source_index, source_row)),
                    )
                ],
                stats,
            )

        stats["model_requests"] = 1
        stats["batch_calls"] = 1
        try:
            batch_results = reviewer.analyze_cluster_units_batch(
                [source_row for _index, source_row, _key in job]
            )
            if len(batch_results) != len(job):
                raise MimoError("批量原子问题提取返回数量与输入不一致")
            extracted_results = []
            for (
                source_index,
                source_row,
                source_key,
            ), batch_result in zip(job, batch_results):
                topics = list(batch_result.candidate.get("topics") or [])
                if not topics:
                    topics = [_direct_atomic_fallback(source_row)]
                topics, rescue_reason = _direct_local_multi_topic_rescue_topics(
                    source_row,
                    topics,
                )
                extracted_results.append(
                    (
                        source_key,
                        (
                            source_index,
                            source_row,
                            topics,
                            False,
                            rescue_reason,
                        ),
                    )
                )
            return extracted_results, stats
        except Exception:
            stats["batch_splits"] = 1
            midpoint = len(job) // 2
            left_results, left_stats = extract_atomic_job(job[:midpoint])
            right_results, right_stats = extract_atomic_job(job[midpoint:])
            for field in stats:
                stats[field] += left_stats[field] + right_stats[field]
            return [*left_results, *right_results], stats

    def store_atomic_job_result(
        job_result: tuple[
            list[
                tuple[
                    str,
                    tuple[
                        int,
                        dict[str, Any],
                        list[dict[str, Any]],
                        bool,
                        str,
                    ],
                ]
            ],
            dict[str, int],
        ],
    ) -> None:
        nonlocal atomic_batches_completed
        extracted_results, stats = job_result
        meta["atomic_extraction_request_jobs"] += stats["model_requests"]
        meta["atomic_extraction_model_requests"] += stats["model_requests"]
        meta["atomic_extraction_batch_calls"] += stats["batch_calls"]
        meta["atomic_extraction_batch_splits"] += stats["batch_splits"]
        for source_key, result in extracted_results:
            store_atomic_result(source_key, result)
        atomic_batches_completed += 1
        meta["atomic_extraction_batches_completed"] = atomic_batches_completed
        batch_failed = any(result[3] for _source_key, result in extracted_results)
        if batch_failed:
            detail = "原子问题批次失败，已保守降级并进入人工复核。"
        elif stats["batch_splits"]:
            detail = "原子问题批次已拆分为更小批次重试。"
        else:
            detail = "正在拆分原子问题。"
        report_atomic_progress(detail)

    executor: ThreadPoolExecutor | None = None
    try:
        if max_workers == 1:
            for job in atomic_jobs:
                store_atomic_job_result(extract_atomic_job(job))
        elif atomic_jobs:
            executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="answer-hub-atomic",
            )
            futures = [
                executor.submit(extract_atomic_job, job)
                for job in atomic_jobs
            ]
            for future in as_completed(futures):
                store_atomic_job_result(future.result())
    except BaseException:
        _write_direct_mimo_progress(
            progress_path,
            source_keys,
            cached_atomic_results,
            cached_cluster_results,
            progress_signatures=progress_signatures,
        )
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            executor = None
        raise
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    if model_calls_before_atomic is not None and callable(metrics_snapshot):
        try:
            model_calls_after_atomic = int(
                metrics_snapshot().get("model_calls", 0)
            )
            meta["atomic_extraction_model_requests"] = max(
                0,
                model_calls_after_atomic - model_calls_before_atomic,
            )
        except (AttributeError, TypeError, ValueError):
            pass

    extracted_rows = [
        extracted_by_index[source_index]
        for source_index, _source_row in indexed_rows
    ]

    for source_index, source_row, topics, failed, rescue_reason in extracted_rows:
        base_id = (
            _clean_text(source_row.get("数据ID"))
            or _clean_text(source_row.get("工单ID"))
            or f"ROW-{source_index:05d}"
        )
        if rescue_reason:
            meta["local_multi_topic_rescue"] += 1
            meta["local_multi_topic_rescue_samples"].append(base_id)
        for topic_index, topic in enumerate(topics, start=1):
            topic = dict(topic)
            atomic_failure_reason = _clean_text(
                topic.pop("_atomic_extraction_failure_reason", "")
            )
            atomic_recovery_reason = _clean_text(
                topic.pop("_atomic_extraction_recovery_reason", "")
            )
            if atomic_failure_reason:
                record_failure_detail(
                    "atomic_extraction_failure_reasons",
                    atomic_failure_reason,
                    [base_id],
                )
            business_line = business_line_from_record(source_row)
            effective_business_line = (
                business_line.name
                if business_line
                else UNKNOWN_BUSINESS_LINE_NAME
            )
            business_line_requires_review = bool(
                business_line is None
                or not business_line.product_categories_configured
            )
            source_product = _resolved_product_type_for_row(source_row)
            source_product_unknown = not bool(source_product)
            model_product = canonical_product_name(
                topic.get("product_category"),
                unknown="",
            )
            product_conflict = bool(
                source_product
                and model_product
                and source_product != model_product
            )
            human_evidence_conflict_reason = _human_evidence_conflict_reason(
                source_row
            )
            if product_conflict:
                meta["atomic_product_conflicts"] += 1
                if len(meta["atomic_product_conflict_samples"]) < 20:
                    meta["atomic_product_conflict_samples"].append(
                        {
                            "sample_id": base_id,
                            "source_product": source_product,
                            "model_product": model_product,
                        }
                    )
            if human_evidence_conflict_reason:
                meta["atomic_human_evidence_conflicts"] += 1
                if len(meta["atomic_human_evidence_conflict_samples"]) < 20:
                    meta["atomic_human_evidence_conflict_samples"].append(
                        {
                            "sample_id": base_id,
                            "reason": human_evidence_conflict_reason,
                        }
                    )
            effective_product = source_product or UNKNOWN_PRODUCT_NAME
            topic["product_category"] = effective_product
            try:
                topic_confidence = float(topic.get("confidence"))
            except (TypeError, ValueError):
                topic_confidence = 0.0
            raw_requires_review = topic.get("requires_review")
            model_requires_review = bool(
                raw_requires_review is True
                or _clean_text(raw_requires_review).lower()
                in {"1", "true", "yes", "是"}
            )
            classification_catalog_ambiguous = (
                _clean_text(source_row.get("_分类库状态"))
                == "classification_ambiguous"
            )
            atomic_requires_review = bool(
                failed
                or atomic_recovery_reason
                or bool(_clean_text(source_row.get("AI结果冲突字段")))
                or product_conflict
                or bool(human_evidence_conflict_reason)
                or classification_catalog_ambiguous
                or source_product_unknown
                or business_line_requires_review
                or topic_confidence < 0.75
                or model_requires_review
            )
            priority_review_reason = _safe_join(
                [
                    "原子问题提取失败，已使用保守规则降级" if failed else "",
                    (
                        f"原子提取具体失败：{atomic_failure_reason}"
                        if atomic_failure_reason
                        else ""
                    ),
                    (
                        "原子问题模型校验未通过，已按来源结构化字段保守恢复，"
                        f"必须人工复核：{atomic_recovery_reason}"
                        if atomic_recovery_reason
                        else ""
                    ),
                    (
                        "ai_result 与已有结构化字段冲突："
                        f"{_clean_text(source_row.get('AI结果冲突字段'))}"
                        if _clean_text(source_row.get("AI结果冲突字段"))
                        else ""
                    ),
                    (
                        "模型识别品类与源数据品类冲突，已按源数据品类硬隔离："
                        f"{model_product}→{source_product}"
                        if product_conflict
                        else ""
                    ),
                    human_evidence_conflict_reason,
                    (
                        "分类库候选存在歧义，已保留同品类聚类机会，"
                        "需人工确认最终问题路径"
                        if classification_catalog_ambiguous
                        else ""
                    ),
                    (
                        "源数据品类待确认，已保持未知品类逐条隔离"
                        if source_product_unknown
                        else ""
                    ),
                    (
                        f"{effective_business_line}产品品类口径尚未配置，"
                        "当前只做业务层级隔离"
                        if business_line_requires_review
                        else ""
                    ),
                    (
                        f"原子问题语义标注置信度较低（{topic_confidence:.3f}）"
                        if topic_confidence < 0.75
                        else ""
                    ),
                    "模型要求人工复核" if model_requires_review else "",
                    _clean_text(topic.get("_local_multi_topic_rescue_reason")),
                ],
                "；",
            )
            atomic_id = unique_atomic_id(base_id, source_index, topic_index)
            unit = {
                "unit_id": atomic_id,
                "sample_id": base_id,
                "source_conversation": _clean_text(source_row.get("聊天内容")),
                "source_core_problem": _clean_text(
                    source_row.get("核心问题")
                    or source_row.get("原始核心问题")
                ),
                "source_judgment_conclusion": _clean_text(
                    source_row.get("判定结论")
                    or source_row.get("原始判定结论")
                ),
                "source_judgment_basis": _clean_text(
                    source_row.get("判定依据")
                ),
                "source_reference_reply": _clean_text(
                    source_row.get("参考话术")
                ),
                "ai_result_conflict_fields": _clean_text(
                    source_row.get("AI结果冲突字段")
                ),
                "historical_actual_reply": _historical_actual_reply(source_row),
                **topic,
                "business_line": effective_business_line,
                "business_line_code": (
                    business_line.code if business_line else ""
                ),
                "_原子需要复核": atomic_requires_review,
                "_原子提取恢复原因": atomic_recovery_reason,
                "_原子品类冲突": product_conflict,
                "_原子品类冲突说明": (
                    f"模型识别品类为{model_product}，源数据品类为{source_product}。"
                    if product_conflict
                    else ""
                ),
                "_预聚类规则状态": _clean_text(
                    source_row.get("_预聚类规则状态")
                ),
                "_预聚类判定规则ID": _clean_text(
                    source_row.get("_预聚类判定规则ID")
                ),
                "_预聚类标准族": _clean_text(
                    source_row.get("_预聚类标准族")
                ),
                "_预聚类现象值": _clean_text(
                    source_row.get("_预聚类现象值")
                ),
                "_预聚类合并策略": _clean_text(
                    source_row.get("_预聚类合并策略")
                ),
                "_分类库状态": _clean_text(source_row.get("_分类库状态")),
                "_分类库候选": source_row.get("_分类库候选")
                if isinstance(source_row.get("_分类库候选"), list)
                else [],
                "_分类库候选标准路径": _clean_text(
                    source_row.get("_分类库候选标准路径")
                ),
                "_分类库候选问题分类": _clean_text(
                    source_row.get("_分类库候选问题分类")
                ),
            }
            source_rule_match = _source_clustering_rule_match(source_row)
            model_rule_match = _model_clustering_rule_match(unit)
            if source_rule_match is not None:
                source_boundary = _clustering_rule_boundary(source_rule_match)
                model_boundary = _clustering_rule_boundary(model_rule_match)
                if model_rule_match is not None and model_boundary != source_boundary:
                    rule_match = source_rule_match
                    unit.update(
                        {
                            "_聚类规则状态": "rule_model_conflict",
                            "_聚类规则冲突原因": (
                                "本地规则关键词预识别与模型结构化识别不一致，"
                                "已按规则边界隔离并转人工复核"
                            ),
                            "_聚类判定规则ID": source_rule_match.rule_id,
                            "_聚类标准族": source_rule_match.standard_family,
                            "_聚类现象值": source_rule_match.phenomenon_value,
                            "_聚类合并策略": source_rule_match.merge_policy,
                        }
                    )
                    meta["clustering_rule_model_conflict_count"] += 1
                    atomic_requires_review = True
                    unit["_原子需要复核"] = True
                else:
                    rule_match = source_rule_match
                    unit["_聚类规则状态"] = "rule_matched"
            else:
                rule_match = model_rule_match
                unit["_聚类规则状态"] = (
                    "rule_matched_model_derived"
                    if model_rule_match is not None
                    else "rule_not_matched"
                )
            if rule_match is None:
                rule_match = _direct_clustering_rule_match(unit)
            if rule_match is not None:
                unit.update(
                    {
                        "_聚类判定规则ID": rule_match.rule_id,
                        "_聚类标准族": rule_match.standard_family,
                        "_聚类现象值": rule_match.phenomenon_value,
                        "_聚类合并策略": rule_match.merge_policy,
                    }
                )
                meta["clustering_judgment_rule_match_count"] += 1
                if (
                    rule_match.rule_id
                    not in meta["clustering_judgment_rule_ids"]
                ):
                    meta["clustering_judgment_rule_ids"].append(
                        rule_match.rule_id
                    )
            if _clean_text(unit.get("_聚类规则状态")) == "rule_model_conflict":
                priority_review_reason = _safe_join(
                    [
                        priority_review_reason,
                        _clean_text(unit.get("_聚类规则冲突原因")),
                    ],
                    "；",
                )
            atomic_units.append(unit)
            atomic_row = dict(source_row)
            atomic_row.pop("标签聚类键", None)
            atomic_row.update(
                {
                    "_原子知识ID": atomic_id,
                    "_原子需要复核": atomic_requires_review,
                    "_原子品类冲突": product_conflict,
                    "_原子品类冲突说明": (
                        f"模型识别品类为{model_product}，源数据品类为{source_product}。"
                        if product_conflict
                        else ""
                    ),
                    "_模型产品类型": model_product,
                    "回收业务层级": effective_business_line,
                    "回收业务层级编码": (
                        business_line.code if business_line else ""
                    ),
                    "_原子适用范围类型": _clean_text(topic.get("scope_type")),
                    "_原子平台": _clean_text(topic.get("platform")),
                    "_原子品牌": _clean_text(topic.get("brand")),
                    "_原子机型范围": _clean_text(topic.get("model_scope")),
                    "_原子阈值例外": _clean_text(topic.get("threshold_or_exception")),
                    "_聚类判定规则ID": _clean_text(
                        unit.get("_聚类判定规则ID")
                    ),
                    "_聚类标准族": _clean_text(unit.get("_聚类标准族")),
                    "_聚类现象值": _clean_text(unit.get("_聚类现象值")),
                    "_聚类合并策略": _clean_text(
                        unit.get("_聚类合并策略")
                    ),
                    "_预聚类规则状态": _clean_text(
                        unit.get("_预聚类规则状态")
                    ),
                    "_聚类规则状态": _clean_text(
                        unit.get("_聚类规则状态")
                    ),
                    "_聚类规则冲突原因": _clean_text(
                        unit.get("_聚类规则冲突原因")
                    ),
                    "原始核心问题": _clean_text(
                        unit.get("source_core_problem")
                    ),
                    "原始判定结论": _clean_text(
                        unit.get("source_judgment_conclusion")
                    ),
                    "核心问题": _clean_text(topic.get("normalized_issue"))
                    or _clean_text(source_row.get("核心问题")),
                    "产品类型": effective_product,
                    "模型主题一级分类": _clean_text(topic.get("category_l1")),
                    "模型主题二级分类": _clean_text(topic.get("category_l2")),
                    "问题意图": _clean_text(topic.get("intent")),
                    "对象/部位": _clean_text(topic.get("subject")),
                    "异常现象": _clean_text(topic.get("phenomenon")),
                    "判定目标": _clean_text(topic.get("judgment_target")),
                    "解题方式": _clean_text(topic.get("resolution_mode")),
                    "主标准路径": _clean_text(topic.get("standard_path")),
                    "语义标注依据": _clean_text(topic.get("evidence_summary")),
                    "语义标注置信度": topic.get("confidence", ""),
                    "语义标注状态": (
                        "atomic_unit_labeled_local_multi_topic_rescue"
                        if _clean_text(topic.get("_local_multi_topic_rescue_reason"))
                        else "atomic_unit_labeled"
                    ),
                    "人工优先复核原因": priority_review_reason,
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
            atomic_row["标签聚类键"] = _topic_tag_cluster_key(atomic_row)
            row_by_atomic_id[atomic_id] = atomic_row

    meta["atomic_unit_count"] = len(atomic_units)
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for unit in atomic_units:
        buckets.setdefault(_direct_atomic_bucket_key(unit), []).append(unit)

    topic_groups: list[tuple[tuple[str, ...], list[dict[str, Any]]]] = []
    cluster_index = 0
    total_cluster_batches = sum(
        (len(bucket_units) + batch_size - 1) // batch_size
        for bucket_units in buckets.values()
    )
    completed_cluster_batches = 0
    meta["direct_cluster_batches_total"] = total_cluster_batches
    meta["direct_cluster_batches_completed"] = completed_cluster_batches

    cluster_stat_fields = (
        "direct_cluster_calls",
        "direct_cluster_failed",
        "direct_cluster_retry_splits",
        "direct_cluster_retry_succeeded",
        "direct_cluster_retry_exhausted_batches",
    )

    def empty_cluster_stats() -> dict[str, Any]:
        return {
            **{field: 0 for field in cluster_stat_fields},
            "direct_cluster_circuit_open": False,
            "direct_cluster_last_error": "",
        }

    def merge_cluster_stats(values: dict[str, Any]) -> None:
        for field in cluster_stat_fields:
            meta[field] += int(values.get(field, 0))
        meta["direct_cluster_circuit_open"] = bool(
            meta["direct_cluster_circuit_open"]
            or values.get("direct_cluster_circuit_open")
        )
        if _clean_text(values.get("direct_cluster_last_error")):
            meta["direct_cluster_last_error"] = _clean_text(
                values.get("direct_cluster_last_error")
            )

    def combine_cluster_stats(
        target: dict[str, Any],
        source: dict[str, Any],
    ) -> None:
        for field in cluster_stat_fields:
            target[field] += int(source.get(field, 0))
        target["direct_cluster_circuit_open"] = bool(
            target["direct_cluster_circuit_open"]
            or source.get("direct_cluster_circuit_open")
        )
        if _clean_text(source.get("direct_cluster_last_error")):
            target["direct_cluster_last_error"] = _clean_text(
                source.get("direct_cluster_last_error")
            )

    def cluster_batch_with_retry(
        batch: list[dict[str, Any]],
        *,
        retried: bool = False,
    ) -> tuple[
        list[tuple[list[dict[str, Any]], dict[str, Any], str, bool]],
        dict[str, Any],
    ]:
        """Retry an invalid large model partition on smaller compatible batches."""
        stats = empty_cluster_stats()
        if len(batch) == 1:
            return (
                [
                    (
                        batch,
                        {
                            "clusters": [
                                {"member_atomic_ids": [batch[0]["unit_id"]]}
                            ],
                            "split_requests": [],
                            "review_requests": [],
                        },
                        "",
                        retried,
                    )
                ],
                stats,
            )

        stats["direct_cluster_calls"] += 1
        try:
            candidate = reviewer.cluster_atomic_units(batch).candidate
        except Exception as exc:
            stats["direct_cluster_failed"] += 1
            failure_reason = f"{type(exc).__name__}: {_clean_text(str(exc))[:240]}"
            stats["direct_cluster_last_error"] = failure_reason
            if "已熔断" in failure_reason or "响应总超时" in failure_reason:
                stats["direct_cluster_circuit_open"] = True
                return (
                    [
                        (
                            batch,
                            {
                                "clusters": [
                                    {"member_atomic_ids": [unit["unit_id"]]}
                                    for unit in batch
                                ],
                                "split_requests": [],
                                "review_requests": [],
                            },
                            failure_reason,
                            retried,
                        )
                    ],
                    stats,
                )
            if len(batch) > 2:
                stats["direct_cluster_retry_splits"] += 1
                midpoint = len(batch) // 2
                left_results, left_stats = cluster_batch_with_retry(
                    batch[:midpoint],
                    retried=True,
                )
                right_results, right_stats = cluster_batch_with_retry(
                    batch[midpoint:],
                    retried=True,
                )
                combine_cluster_stats(stats, left_stats)
                combine_cluster_stats(stats, right_stats)
                return ([*left_results, *right_results], stats)

            stats["direct_cluster_retry_exhausted_batches"] += 1
            return (
                [
                    (
                        batch,
                        {
                            "clusters": [
                                {"member_atomic_ids": [unit["unit_id"]]}
                                for unit in batch
                            ],
                            "split_requests": [],
                            "review_requests": [],
                        },
                        failure_reason,
                        retried,
                    )
                ],
                stats,
            )

        if retried:
            stats["direct_cluster_retry_succeeded"] += 1
        return ([(batch, candidate, "", retried)], stats)

    try:
        cluster_max_workers = max(
            1,
            min(
                configured_max_workers,
                int(
                    os.getenv(
                        "ANSWER_HUB_DIRECT_CLUSTER_MAX_WORKERS",
                        str(configured_max_workers),
                    )
                ),
                8,
            ),
        )
    except ValueError:
        cluster_max_workers = configured_max_workers
    meta["direct_cluster_max_workers"] = cluster_max_workers

    cluster_jobs: list[
        tuple[
            int,
            tuple[str, ...],
            int,
            list[dict[str, Any]],
            str,
        ]
    ] = []
    for bucket_key, bucket_units in sorted(
        buckets.items(),
        key=lambda item: item[0],
    ):
        ordered = sorted(
            bucket_units,
            key=lambda unit: _clean_text(unit.get("unit_id")),
        )
        for batch_index in range(0, len(ordered), max(1, batch_size)):
            batch = ordered[batch_index : batch_index + max(1, batch_size)]
            cluster_jobs.append(
                (
                    len(cluster_jobs),
                    bucket_key,
                    batch_index,
                    batch,
                    _direct_mimo_cluster_progress_key(batch),
                )
            )

    cluster_job_results: dict[
        int,
        list[tuple[list[dict[str, Any]], dict[str, Any], str, bool]],
    ] = {}

    def store_cluster_job_result(
        job: tuple[
            int,
            tuple[str, ...],
            int,
            list[dict[str, Any]],
            str,
        ],
        batch_results: list[
            tuple[list[dict[str, Any]], dict[str, Any], str, bool]
        ],
        stats: dict[str, Any],
        *,
        cache_hit: bool = False,
    ) -> None:
        nonlocal completed_cluster_batches
        job_index, _bucket_key, _batch_index, batch, cache_key = job
        cluster_job_results[job_index] = batch_results
        merge_cluster_stats(stats)
        for result_batch, _candidate, failure_reason, _retried in batch_results:
            if failure_reason:
                record_failure_detail(
                    "direct_cluster_failure_reasons",
                    failure_reason,
                    [
                        _clean_text(unit.get("sample_id"))
                        for unit in result_batch
                    ],
                )
        if cache_hit:
            meta["direct_cluster_cache_hits"] += 1
        else:
            if any(failure_reason for _batch, _candidate, failure_reason, _retried in batch_results):
                cached_cluster_results.pop(cache_key, None)
            else:
                serialized_results = []
                for result_batch, candidate, failure_reason, retried in batch_results:
                    serialized_results.append(
                        {
                            "atomic_ids": [
                                _clean_text(unit.get("unit_id"))
                                for unit in result_batch
                                if _clean_text(unit.get("unit_id"))
                            ],
                            "candidate": candidate,
                            "failure_reason": failure_reason,
                            "retried": retried,
                        }
                    )
                cached_cluster_results[cache_key] = {
                    "batch_results": serialized_results,
                }
        completed_cluster_batches += 1
        meta["direct_cluster_batches_completed"] = completed_cluster_batches
        if (
            completed_cluster_batches == total_cluster_batches
            or completed_cluster_batches % progress_flush_every == 0
        ):
            _write_direct_mimo_progress(
                progress_path,
                source_keys,
                cached_atomic_results,
                cached_cluster_results,
                cached_reconcile_results,
                progress_signatures=progress_signatures,
            )
        if progress_callback:
            progress_callback(
                "原子问题主题聚类已完成一个批次。",
                {
                    "direct_cluster_batches_completed": completed_cluster_batches,
                    "direct_cluster_batches_total": total_cluster_batches,
                    "direct_cluster_batch_size": len(batch),
                    "direct_cluster_workers": cluster_max_workers,
                    "direct_cluster_cache_hits": meta[
                        "direct_cluster_cache_hits"
                    ],
                },
            )

    pending_cluster_jobs = []
    for job in cluster_jobs:
        _job_index, _bucket_key, _batch_index, batch, cache_key = job
        cached_cluster = cached_cluster_results.get(cache_key)
        cached_batch_results = None
        if isinstance(cached_cluster, dict):
            raw_batch_results = cached_cluster.get("batch_results")
            if isinstance(raw_batch_results, list):
                batch_by_id = {
                    _clean_text(unit.get("unit_id")): unit
                    for unit in batch
                    if _clean_text(unit.get("unit_id"))
                }
                restored_results = []
                restored_ids: set[str] = set()
                for raw_result in raw_batch_results:
                    if not isinstance(raw_result, dict):
                        restored_results = []
                        break
                    atomic_ids = [
                        _clean_text(atomic_id)
                        for atomic_id in raw_result.get("atomic_ids", [])
                        if _clean_text(atomic_id)
                    ]
                    candidate = raw_result.get("candidate")
                    failure_reason = _clean_text(
                        raw_result.get("failure_reason")
                    )
                    if (
                        not atomic_ids
                        or not isinstance(candidate, dict)
                        or bool(failure_reason)
                        or any(
                            atomic_id not in batch_by_id
                            or atomic_id in restored_ids
                            for atomic_id in atomic_ids
                        )
                    ):
                        restored_results = []
                        break
                    restored_ids.update(atomic_ids)
                    restored_results.append(
                        (
                            [batch_by_id[atomic_id] for atomic_id in atomic_ids],
                            dict(candidate),
                            "",
                            bool(raw_result.get("retried")),
                        )
                    )
                if restored_ids == set(batch_by_id):
                    cached_batch_results = restored_results
            elif (
                isinstance(cached_cluster.get("candidate"), dict)
                and not _clean_text(cached_cluster.get("failure_reason"))
            ):
                cached_batch_results = [
                    (
                        batch,
                        dict(cached_cluster["candidate"]),
                        _clean_text(cached_cluster.get("failure_reason")),
                        bool(cached_cluster.get("retried")),
                    )
                ]
        if cached_batch_results is not None:
            store_cluster_job_result(
                job,
                cached_batch_results,
                empty_cluster_stats(),
                cache_hit=True,
            )
        else:
            pending_cluster_jobs.append(job)

    if progress_callback and pending_cluster_jobs:
        progress_callback(
            "正在并行聚类原子问题主题。",
            {
                "direct_cluster_batches_completed": completed_cluster_batches,
                "direct_cluster_batches_total": total_cluster_batches,
                "direct_cluster_workers": cluster_max_workers,
            },
        )

    cluster_executor: ThreadPoolExecutor | None = None
    try:
        if cluster_max_workers == 1:
            for job in pending_cluster_jobs:
                batch_results, stats = cluster_batch_with_retry(job[3])
                store_cluster_job_result(job, batch_results, stats)
        elif pending_cluster_jobs:
            cluster_executor = ThreadPoolExecutor(
                max_workers=cluster_max_workers,
                thread_name_prefix="answer-hub-cluster",
            )
            future_jobs = {
                cluster_executor.submit(
                    cluster_batch_with_retry,
                    job[3],
                ): job
                for job in pending_cluster_jobs
            }
            for future in as_completed(future_jobs):
                batch_results, stats = future.result()
                store_cluster_job_result(
                    future_jobs[future],
                    batch_results,
                    stats,
                )
    except BaseException:
        _write_direct_mimo_progress(
            progress_path,
            source_keys,
            cached_atomic_results,
            cached_cluster_results,
            cached_reconcile_results,
            progress_signatures=progress_signatures,
        )
        if cluster_executor is not None:
            cluster_executor.shutdown(wait=False, cancel_futures=True)
            cluster_executor = None
        raise
    finally:
        if cluster_executor is not None:
            cluster_executor.shutdown(wait=True)

    for (
        job_index,
        bucket_key,
        batch_index,
        batch,
        _cluster_cache_key,
    ) in cluster_jobs:
        if job_index in cluster_job_results:
            batch_results = cluster_job_results[job_index]
            for result_index, (result_batch, candidate, failure_reason, retried) in enumerate(
                batch_results,
                start=1,
            ):
                batch_failed = bool(failure_reason)
                assigned: set[str] = set()
                result_batch_atomic_ids = {
                    _clean_text(unit.get("unit_id"))
                    for unit in result_batch
                    if _clean_text(unit.get("unit_id"))
                }
                for cluster in candidate.get("clusters", []):
                    raw_member_ids = [
                        _clean_text(atomic_id)
                        for atomic_id in cluster.get("member_atomic_ids", [])
                        if _clean_text(atomic_id)
                    ]
                    foreign_member_ids = [
                        atomic_id
                        for atomic_id in raw_member_ids
                        if atomic_id not in result_batch_atomic_ids
                    ]
                    if foreign_member_ids:
                        meta["direct_foreign_member_ids_ignored"] += len(
                            foreign_member_ids
                        )
                        remaining_sample_capacity = max(
                            0,
                            20
                            - len(
                                meta[
                                    "direct_foreign_member_id_samples"
                                ]
                            ),
                        )
                        meta["direct_foreign_member_id_samples"].extend(
                            foreign_member_ids[:remaining_sample_capacity]
                        )
                    member_ids = list(
                        dict.fromkeys(
                            atomic_id
                            for atomic_id in raw_member_ids
                            if atomic_id in result_batch_atomic_ids
                        )
                    )
                    if not member_ids:
                        continue
                    assigned.update(member_ids)
                    member_rows = [row_by_atomic_id[atomic_id] for atomic_id in member_ids]
                    hard_conflict_reason = _direct_cluster_hard_conflict_reason(member_rows)
                    if hard_conflict_reason:
                        meta["direct_post_guard_split_clusters"] += 1
                        for atomic_id in member_ids:
                            meta["direct_post_guard_singletons"] += 1
                            cluster_index += 1
                            member_row = row_by_atomic_id[atomic_id]
                            member_row.update(
                                {
                                    "_聚类主题标题": "",
                                    "_聚类知识定义": "",
                                    "_聚类决策": "程序门禁拆分冲突聚类",
                                    "_聚类候选相似度": "",
                                    "_聚类裁决提供方": "mimo-direct-post-guard",
                                    "_聚类裁决原因": hard_conflict_reason,
                                    "_聚类裁决置信度": "",
                                    "_聚类需要复核": True,
                                    "人工优先复核原因": _safe_join(
                                        [
                                            _clean_text(
                                                member_row.get(
                                                    "人工优先复核原因"
                                                )
                                            ),
                                            hard_conflict_reason,
                                        ],
                                        "；",
                                    ),
                                }
                            )
                            topic_groups.append(
                                (
                                    (
                                        "direct_mimo",
                                        *bucket_key,
                                        f"post-guard-{cluster_index}",
                                        atomic_id,
                                    ),
                                    [member_row],
                                )
                            )
                        continue
                    cluster_index += 1
                    cluster_requires_review = bool(
                        cluster.get("requires_review")
                    )
                    classification_catalog_requires_review = any(
                        _clean_text(member_row.get("_分类库状态"))
                        == "classification_ambiguous"
                        for member_row in member_rows
                    )
                    for member_row in member_rows:
                        member_row.update(
                            {
                                "_聚类主题标题": _clean_text(cluster.get("theme_name")),
                                "_聚类知识定义": _clean_text(
                                    cluster.get("shared_knowledge_definition")
                                ),
                                "_聚类决策": (
                                    "聚类失败后保守单例"
                                    if batch_failed
                                    else "纯大模型1-N聚类（拆批重试）"
                                    if retried
                                    else "纯大模型1-N聚类"
                                ),
                                "_聚类候选相似度": "",
                                "_聚类裁决提供方": (
                                    "mimo-direct-failed"
                                    if batch_failed
                                    else "mimo-direct-retry"
                                    if retried
                                    else "mimo-direct"
                                ),
                                "_聚类裁决原因": (
                                    "最小兼容批次聚类调用失败，已保守保留为单成员主题；"
                                    + failure_reason
                                    if batch_failed
                                    else _safe_join(
                                        [
                                            _clean_text(cluster.get("merge_basis")),
                                            "上级批次调用失败，已拆批重试成功。"
                                            if retried
                                            else "",
                                        ],
                                        "；",
                                    )
                                    or "原子问题满足适用范围、对象、目标、标准路径和阈值例外一致性。"
                                ),
                                "_聚类裁决置信度": cluster.get(
                                    "confidence",
                                    "",
                                ),
                                "_聚类需要复核": (
                                    batch_failed
                                    or cluster_requires_review
                                    or classification_catalog_requires_review
                                ),
                                "人工优先复核原因": _safe_join(
                                    [
                                        _clean_text(
                                            member_row.get(
                                                "人工优先复核原因"
                                            )
                                        ),
                                        (
                                            "聚类调用失败，已保守保留为单成员主题"
                                            if batch_failed
                                            else ""
                                        ),
                                        (
                                            "聚类模型要求人工复核"
                                            if cluster_requires_review
                                            else ""
                                        ),
                                        (
                                            "分类库候选存在歧义，需人工确认最终问题路径"
                                            if classification_catalog_requires_review
                                            else ""
                                        ),
                                    ],
                                    "；",
                                ),
                            }
                        )
                    topic_groups.append(
                        (
                            (
                                "direct_mimo",
                                *bucket_key,
                                f"batch-{batch_index // max(1, batch_size) + 1}-{result_index}",
                                f"cluster-{cluster_index}",
                                *member_ids,
                            ),
                            member_rows,
                        )
                    )
                split_requests_by_id = {
                    _clean_text(request.get("atomic_id")): request
                    for request in candidate.get("split_requests", [])
                    if _clean_text(request.get("atomic_id")) in row_by_atomic_id
                }
                review_requests_by_id = {
                    _clean_text(request.get("atomic_id")): request
                    for request in candidate.get("review_requests", [])
                    if _clean_text(request.get("atomic_id")) in row_by_atomic_id
                }
                unassigned_ids = {
                    _clean_text(unit.get("unit_id"))
                    for unit in result_batch
                    if _clean_text(unit.get("unit_id")) not in assigned
                }
                unresolved_ids = {
                    *split_requests_by_id,
                    *review_requests_by_id,
                    *unassigned_ids,
                }
                for atomic_id in sorted(unresolved_ids):
                    meta["direct_review_singletons"] += 1
                    cluster_index += 1
                    member_row = row_by_atomic_id[atomic_id]
                    if atomic_id in split_requests_by_id:
                        request = split_requests_by_id[atomic_id]
                        provider = "mimo-direct-split"
                        decision = "模型要求继续拆分，暂保留单成员主题"
                        reason = _safe_join(
                            [
                                _clean_text(request.get("reason")),
                                "建议拆分方向："
                                + "、".join(
                                    _clean_text(item)
                                    for item in request.get(
                                        "suggested_splits",
                                        [],
                                    )
                                    if _clean_text(item)
                                ),
                            ],
                            "；",
                        )
                        meta["direct_split_singletons"] += 1
                    elif atomic_id in review_requests_by_id:
                        request = review_requests_by_id[atomic_id]
                        provider = "mimo-direct-review"
                        decision = "保留复核标记并参与同品类二次归并"
                        reason = _safe_join(
                            [
                                _clean_text(request.get("review_type")),
                                _clean_text(request.get("reason")),
                            ],
                            "：",
                        )
                        meta["direct_review_candidates"] += 1
                    else:
                        provider = "mimo-direct-guard"
                        decision = "未分配原子问题独立成簇"
                        reason = "模型输出未分配该原子问题，保守地保留为单成员主题。"
                        meta["direct_unassigned_singletons"] += 1
                    member_row.update(
                        {
                            "_聚类决策": decision,
                            "_聚类候选相似度": "",
                            "_聚类裁决提供方": provider,
                            "_聚类裁决原因": reason,
                            "_聚类裁决置信度": "",
                            "_聚类需要复核": True,
                            "人工优先复核原因": _safe_join(
                                [
                                    _clean_text(
                                        member_row.get("人工优先复核原因")
                                    ),
                                    reason,
                                ],
                                "；",
                            ),
                        }
                    )
                    topic_groups.append(
                        (
                            ("direct_mimo", *bucket_key, f"review-{cluster_index}", atomic_id),
                            [member_row],
                        )
                    )

    def persist_reconcile_results(
        reconcile_results: dict[str, dict[str, Any]],
    ) -> None:
        _write_direct_mimo_progress(
            progress_path,
            source_keys,
            cached_atomic_results,
            cached_cluster_results,
            reconcile_results,
            progress_signatures=progress_signatures,
        )

    topic_groups = _reconcile_direct_topic_groups(
        topic_groups,
        reviewer,
        meta,
        reconcile_results=cached_reconcile_results,
        cache_update_callback=persist_reconcile_results,
        progress_callback=progress_callback,
    )
    _write_direct_mimo_progress(
        progress_path,
        source_keys,
        cached_atomic_results,
        cached_cluster_results,
        cached_reconcile_results,
        progress_signatures=progress_signatures,
    )
    meta["cluster_count"] = len(topic_groups)
    return topic_groups, meta


def _rank_semantic_cluster_candidates(
    vector: np.ndarray,
    grouped: list[dict[str, Any]],
    business_line: str,
    product_type: str,
) -> list[tuple[int, float]]:
    matching_indices = [
        index
        for index, cluster in enumerate(grouped)
        if cluster["business_line"] == business_line
        and cluster["product_type"] == product_type
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
    if _business_line_for_row(left) != _business_line_for_row(right):
        return True
    if _direct_clustering_rule_conflict_reason([left, right]):
        return True
    if _direct_clustering_rule_allows_comparison([left, right]):
        return False

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
        business_line = _business_line_for_row(row)
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
            if cluster["business_line"] == business_line
            and cluster["product_type"] == product_type
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

        ranked_candidates = _rank_semantic_cluster_candidates(
            vector,
            grouped,
            business_line,
            product_type,
        )
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
                "business_line": business_line,
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
                (
                    "semantic_mimo",
                    cluster["business_line"],
                    cluster["product_type"],
                    f"cluster-{index}",
                    *source_ids,
                ),
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
            "_原子知识ID",
            "数据ID",
            "工单ID",
            "回收业务层级",
            "核心问题",
            "原始核心问题",
            "聊天内容",
            "图片链接",
            "视频链接",
            "图片处理状态",
            "图片证据摘要",
            "视频处理状态",
            "判定结论",
            "原始判定结论",
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
            "_原子知识ID",
            "数据ID",
            "工单ID",
            "回收业务层级",
            "产品类型",
            "机型",
            "核心问题",
            "原始核心问题",
            "原始判定结论",
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
    product_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        scope_key = _business_scope_key(row)
        product_counts[scope_key] = product_counts.get(scope_key, 0) + 1
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
        [
            "\x1f".join(_business_scope_key(row))
            for row in rows
        ],
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
    clustering_accuracy = round(clustering_correct / len(decisive), 4) if decisive else None
    release_ready = (
        len(decisive) >= CLUSTER_V1_MIN_DECISIVE_PAIRS
        and clustering_accuracy is not None
        and clustering_accuracy >= CLUSTER_V1_ACCURACY_THRESHOLD
    )
    if release_ready:
        release_status = "可上线第一版"
    elif len(decisive) < CLUSTER_V1_MIN_DECISIVE_PAIRS:
        release_status = "待补足人工标注"
    else:
        release_status = "准确率未达80%"
    return {
        "total_pairs": len(rows),
        "reviewed_pairs": len(reviewed),
        "pending_pairs": len(rows) - len(reviewed),
        "uncertain_pairs": len(uncertain),
        "decisive_pairs": len(decisive),
        "clustering_correct": clustering_correct,
        "clustering_accuracy": clustering_accuracy,
        "v1_release_ready": release_ready,
        "v1_release_status": release_status,
        "v1_release_accuracy_threshold": CLUSTER_V1_ACCURACY_THRESHOLD,
        "v1_release_min_decisive_pairs": CLUSTER_V1_MIN_DECISIVE_PAIRS,
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


def _atomic_scoped_source_excerpt(
    row: dict[str, Any],
    value: Any,
    limit: int,
) -> str:
    """Keep only the source section that belongs to this atomic question.

    Atomic records inherit the original human fields.  When a field explicitly
    contains more than one numbered question section, retaining it in full
    would reintroduce another atomic question into later topic transcription.
    For a clearly multi-topic semicolon sentence, keep the best matching clause
    only; ambiguous text is excluded rather than leaking a different topic.
    """
    text = _normalize_lines(value)
    if not text:
        return ""
    sections = [
        section.strip()
        for section in re.split(r"(?=^\s*\d+[.、])", text, flags=re.MULTILINE)
        if section.strip()
    ]
    topical_sections = [
        section
        for section in sections
        if re.match(r"^\s*\d+[.、]\s*(?:关于|[^：:\n]{1,30}问题[：:])", section)
    ]

    scope_text = "\n".join(
        _clean_text(row.get(field))
        for field in (
            "核心问题",
            "对象/部位",
            "异常现象",
            "判定目标",
            "解题方式",
            "语义标注依据",
        )
        if _clean_text(row.get(field))
    )
    scope_bigrams = _recommended_reply_meaningful_bigrams(scope_text)
    if not scope_bigrams:
        return ""

    if len(topical_sections) < 2:
        has_multi_topic_marker = bool(
            re.search(r"(?:同时|分别|两个问题|两项问题)", text)
        )
        if not has_multi_topic_marker:
            return _semantic_excerpt(text, limit)
        topical_sections = [
            section.strip()
            for section in re.split(r"[；;\n]+", text)
            if section.strip()
        ]
        if len(topical_sections) < 2:
            return ""

    scored_sections = [
        (
            len(
                scope_bigrams
                & _recommended_reply_meaningful_bigrams(section)
            ),
            section,
        )
        for section in topical_sections
    ]
    scored_sections.sort(key=lambda item: item[0], reverse=True)
    best_score, best_section = scored_sections[0]
    runner_up_score = scored_sections[1][0]
    if best_score < 2 or best_score == runner_up_score:
        # The source has multiple topics but the atomic labels cannot identify
        # one section reliably.  Keep it out of the reusable evidence package.
        return ""
    return _semantic_excerpt(best_section, limit)


def _topic_source_fact(
    row: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    evidence_level, _eligible, evidence_reason = _topic_evidence(row)
    source_id = (
        _clean_text(row.get("来源记录ID"))
        or _clean_text(row.get("数据ID"))
        or _clean_text(row.get("工单ID"))
        or f"row-{index:03d}"
    )
    human_core_problem = _atomic_scoped_source_excerpt(
        row,
        row.get("原始核心问题") or row.get("核心问题"),
        500,
    )
    human_judgment = _atomic_scoped_source_excerpt(
        row,
        row.get("原始判定结论") or row.get("判定结论"),
        500,
    )
    raw_image_urls = split_image_urls(
        _clean_text(row.get("图片链接"))
    )
    video_urls = split_image_urls(_clean_text(row.get("视频链接")))
    image_usable = _has_usable_image_evidence(row)
    image_urls = raw_image_urls if image_usable else []
    raw_human_fields = "\n".join(
        _clean_text(row.get(field))
        for field in (
            "原始核心问题",
            "核心问题",
            "原始判定结论",
            "判定结论",
            "历史实际回复",
        )
        if _clean_text(row.get(field))
    )
    has_multi_topic_human_fields = bool(
        re.search(r"(?:同时|分别|两个问题|两项问题)", raw_human_fields)
        or len(
            re.findall(
                r"^\s*\d+[.、]\s*(?:关于|[^：:\n]{1,30}问题[：:])",
                raw_human_fields,
                flags=re.MULTILINE,
            )
        ) >= 2
    )
    conversation = (
        ""
        if has_multi_topic_human_fields
        else _semantic_excerpt(row.get("聊天内容"), 800)
    )
    historical_reply = _atomic_scoped_source_excerpt(
        row,
        _historical_actual_reply(row),
        500,
    )
    judgment_basis = _atomic_scoped_source_excerpt(
        row,
        row.get("判定依据"),
        500,
    )
    semantic_basis = _atomic_scoped_source_excerpt(
        row,
        row.get("语义标注依据"),
        500,
    )
    threshold_or_exception = _clean_text(row.get("_原子阈值例外"))
    source_text_without_model_annotations = "\n".join(
        value
        for value in (
            human_core_problem,
            human_judgment,
            conversation,
            historical_reply,
            judgment_basis,
        )
        if value
    )
    normalized_threshold = _normalized_topic_claim(
        threshold_or_exception
    )
    normalized_source_text = _normalized_topic_claim(
        source_text_without_model_annotations
    )
    source_supported_threshold = (
        threshold_or_exception
        if normalized_threshold
        and normalized_threshold in normalized_source_text
        and _topic_claim_relations_are_supported(
            threshold_or_exception,
            source_text_without_model_annotations,
        )
        else ""
    )
    fact_text = "\n".join(
        value
        for value in (
            human_core_problem,
            human_judgment,
            conversation,
            historical_reply,
            judgment_basis,
            source_supported_threshold,
        )
        if value
    )
    has_boundary = bool(
        source_supported_threshold
        or _has_explicit_boundary_case(fact_text)
        or re.search(
            r"(仅限|只适用|不适用|除外|例外|其他.{0,12}(?:需|需要)|"
            r"闭合瞬间|开启瞬间|单次|持续|大于|小于|不少于|不超过)",
            fact_text,
        )
    )
    score = 0
    score += 6 if human_judgment else 0
    score += 4 if has_boundary else 0
    score += 3 if human_core_problem else 0
    score += 3 if historical_reply else 0
    score += 2 if judgment_basis else 0
    score += 2 if image_urls else 0
    score += 1 if len(conversation) >= 40 else 0
    return {
        "fact_id": f"F{index:02d}",
        "source_record_id": source_id,
        "work_order_id": _original_work_order_id_for_row(row),
        "atomic_question": _clean_text(row.get("核心问题")),
        "human_core_problem": human_core_problem,
        "human_judgment_conclusion": human_judgment,
        "judgment_basis": judgment_basis,
        "semantic_basis": semantic_basis,
        "historical_actual_reply": historical_reply,
        "conversation_excerpt": conversation,
        "threshold_or_exception": threshold_or_exception,
        "source_supported_threshold_or_exception": (
            source_supported_threshold
        ),
        "threshold_source_supported": bool(
            source_supported_threshold
        ),
        "image_urls": image_urls,
        "video_urls": video_urls,
        "unavailable_image_urls": (
            raw_image_urls if raw_image_urls and not image_usable else []
        ),
        "image_usable": image_usable,
        "image_processing_status": _clean_text(row.get("图片处理状态")),
        "image_evidence_summary": _clean_text(row.get("图片证据摘要")),
        "evidence_level": evidence_level,
        "evidence_reason": evidence_reason,
        "has_boundary": has_boundary,
        "selection_score": score,
    }


def _representative_topic_facts(
    facts: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def add(fact: dict[str, Any] | None, reason: str) -> None:
        if not fact or len(selected) >= limit:
            return
        fact_id = _clean_text(fact.get("fact_id"))
        if not fact_id or fact_id in selected_ids:
            return
        selected_fact = dict(fact)
        selected_fact["selection_reason"] = reason
        selected.append(selected_fact)
        selected_ids.add(fact_id)

    ranked = sorted(
        facts,
        key=lambda item: (
            -int(bool(item.get("has_boundary"))),
            -int(item.get("selection_score") or 0),
            _clean_text(item.get("fact_id")),
        ),
    )
    add(
        next((fact for fact in ranked if fact.get("has_boundary")), None),
        "包含明确条件、阈值或适用边界",
    )
    add(
        next((fact for fact in ranked if fact.get("image_urls")), None),
        "包含与案例事实对应的现场图片",
    )
    seen_judgments: set[str] = set()
    for fact in ranked:
        judgment = _clean_text(fact.get("human_judgment_conclusion"))
        if not judgment or judgment in seen_judgments:
            continue
        seen_judgments.add(judgment)
        add(fact, "覆盖人工判定结论")

    add(
        next(
            (
                fact
                for fact in ranked
                if fact.get("human_core_problem")
                and fact.get("human_judgment_conclusion")
                and fact.get("historical_actual_reply")
            ),
            None,
        ),
        "人工核心问题、判定结论和实际回复较完整",
    )
    for fact in ranked:
        add(fact, "补充主题代表性事实")
    return selected


def _topic_evidence_package(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts = [
        _topic_source_fact(row, index)
        for index, row in enumerate(rows, start=1)
    ]
    representative_facts = _representative_topic_facts(facts)
    source_fact_refs = [
        f"[{fact['fact_id']}] 来源记录={fact['source_record_id']}"
        for fact in facts
    ]
    return {
        "fact_count": len(facts),
        "facts": facts,
        "representative_facts": representative_facts,
        "representative_fact_ids": [
            fact["fact_id"] for fact in representative_facts
        ],
        "representative_source_ids": list(
            dict.fromkeys(
                fact["source_record_id"]
                for fact in representative_facts
                if fact.get("source_record_id")
            )
        ),
        "source_fact_refs": source_fact_refs,
    }


def _topic_model_evidence_package(
    evidence_package: dict[str, Any],
) -> dict[str, Any]:
    def without_video(fact: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in fact.items()
            if key not in {"video_urls", "video_processing_status"}
        }

    return {
        **evidence_package,
        "facts": [
            without_video(fact)
            for fact in evidence_package.get("facts") or []
        ],
        "representative_facts": [
            without_video(fact)
            for fact in evidence_package.get("representative_facts") or []
        ],
    }


def _topic_fact_references(evidence_package: dict[str, Any]) -> str:
    representative_ids = set(
        evidence_package.get("representative_fact_ids") or []
    )
    refs = []
    for fact in evidence_package.get("facts") or []:
        marker = "代表" if fact.get("fact_id") in representative_ids else "来源"
        refs.append(
            f"[{fact.get('fact_id')}] {marker}记录={fact.get('source_record_id')}"
        )
    return "\n".join(refs)


def _topic_evidence_package_json(
    evidence_package: dict[str, Any],
) -> str:
    remaining_images = 4
    remaining_videos = 2
    compact_facts: list[dict[str, Any]] = []
    text_limits = {
        "atomic_question": 400,
        "human_core_problem": 500,
        "human_judgment_conclusion": 500,
        "judgment_basis": 400,
        "semantic_basis": 400,
        "historical_actual_reply": 500,
        "conversation_excerpt": 600,
        "threshold_or_exception": 300,
        "source_supported_threshold_or_exception": 300,
        "evidence_reason": 300,
        "selection_reason": 200,
        "image_evidence_summary": 400,
    }
    allowed_fact_fields = {
        "fact_id",
        "source_record_id",
        "work_order_id",
        "atomic_question",
        "human_core_problem",
        "human_judgment_conclusion",
        "judgment_basis",
        "semantic_basis",
        "historical_actual_reply",
        "conversation_excerpt",
        "threshold_or_exception",
        "source_supported_threshold_or_exception",
        "threshold_source_supported",
        "image_urls",
        "video_urls",
        "image_usable",
        "image_processing_status",
        "image_evidence_summary",
        "evidence_level",
        "evidence_reason",
        "has_boundary",
        "selection_score",
        "selection_reason",
    }
    for fact in evidence_package.get("representative_facts") or []:
        compact_fact = {
            key: value
            for key, value in fact.items()
            if key in allowed_fact_fields
        }
        for field, limit in text_limits.items():
            if field in compact_fact:
                compact_fact[field] = _clean_text(compact_fact.get(field))[:limit]
        image_urls = [
            _clean_text(url)
            for url in compact_fact.get("image_urls") or []
            if _clean_text(url) and len(_clean_text(url)) <= 2048
        ][:remaining_images]
        video_urls = [
            _clean_text(url)
            for url in compact_fact.get("video_urls") or []
            if _clean_text(url) and len(_clean_text(url)) <= 2048
        ][:remaining_videos]
        compact_fact["image_urls"] = image_urls
        compact_fact["video_urls"] = video_urls
        remaining_images -= len(image_urls)
        remaining_videos -= len(video_urls)
        compact_facts.append(compact_fact)

    representative_ids = {
        _clean_text(fact.get("fact_id"))
        for fact in compact_facts
        if _clean_text(fact.get("fact_id"))
    }
    compact = {
        "fact_count": evidence_package.get("fact_count", 0),
        "representative_fact_ids": evidence_package.get(
            "representative_fact_ids",
            [],
        ),
        "representative_source_ids": evidence_package.get(
            "representative_source_ids",
            [],
        ),
        "representative_facts": compact_facts,
        "source_fact_refs": [
            ref
            for ref in evidence_package.get("source_fact_refs") or []
            if any(
                f"[{fact_id}]" in _clean_text(ref)
                for fact_id in representative_ids
            )
        ],
    }
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 30000:
        return encoded

    for fact in compact_facts:
        for field in text_limits:
            fact.pop(field, None)
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _topic_evidence_summary(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> str:
    evidence_package = evidence_package or _topic_evidence_package(rows)
    parts = []
    for fact in evidence_package.get("representative_facts") or []:
        fact_details = [
            f"[{fact.get('fact_id')}] 来源记录={fact.get('source_record_id')}",
            f"选择原因：{fact.get('selection_reason')}",
            (
                f"人工核心问题：{fact.get('human_core_problem')}"
                if fact.get("human_core_problem")
                else ""
            ),
            (
                f"人工判定结论：{fact.get('human_judgment_conclusion')}"
                if fact.get("human_judgment_conclusion")
                else ""
            ),
            (
                f"判定依据：{fact.get('judgment_basis') or fact.get('semantic_basis')}"
                if fact.get("judgment_basis") or fact.get("semantic_basis")
                else ""
            ),
            (
                f"历史实际回复：{fact.get('historical_actual_reply')}"
                if fact.get("historical_actual_reply")
                else ""
            ),
            (
                "案例图：" + "、".join(fact.get("image_urls") or [])
                if fact.get("image_urls")
                else ""
            ),
        ]
        parts.append(_safe_join(fact_details, " | "))
    return _merge_unique_text(parts, separator="\n")[:4000]


def _topic_case_images(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    evidence_package = evidence_package or _topic_evidence_package(rows)
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for fact in evidence_package.get("representative_facts") or []:
        for url in fact.get("image_urls") or []:
            if not url or url in seen:
                continue
            seen.add(url)
            images.append(
                {
                    "url": url,
                    "fact_id": _clean_text(fact.get("fact_id")),
                    "source_record_id": _clean_text(
                        fact.get("source_record_id")
                    ),
                    "work_order_id": _clean_text(
                        fact.get("work_order_id")
                    ),
                }
            )
            if len(images) >= 4:
                return images
    return images


def _topic_image_links(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> list[str]:
    return [
        item["url"]
        for item in _topic_case_images(rows, evidence_package)
    ]


def _topic_image_source_trace(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        (
            f"[{item['fact_id']}] 来源记录={item['source_record_id']}"
            f" | 图片={item['url']}"
        )
        for item in _topic_case_images(rows, evidence_package)
    )


def _topic_case_videos(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    evidence_package = evidence_package or _topic_evidence_package(rows)
    videos: list[dict[str, str]] = []
    seen: set[str] = set()
    for fact in evidence_package.get("representative_facts") or []:
        for url in fact.get("video_urls") or []:
            if not url or url in seen:
                continue
            seen.add(url)
            videos.append(
                {
                    "url": url,
                    "fact_id": _clean_text(fact.get("fact_id")),
                    "source_record_id": _clean_text(
                        fact.get("source_record_id")
                    ),
                    "work_order_id": _clean_text(
                        fact.get("work_order_id")
                    ),
                }
            )
            if len(videos) >= 2:
                return videos
    return videos


def _topic_video_links(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> list[str]:
    return [
        item["url"]
        for item in _topic_case_videos(rows, evidence_package)
    ]


def _topic_video_source_trace(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        (
            f"[{item['fact_id']}] 来源记录={item['source_record_id']}"
            f" | 视频={item['url']}"
        )
        for item in _topic_case_videos(rows, evidence_package)
    )


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


def _topic_image_measurement_gate(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether visual evidence is enough for a numeric conclusion."""
    source_text_parts = [
        _clean_text(row.get(field))
        for row in rows
        for field in (
            "聊天内容",
            "核心问题",
            "人工核心问题",
            "异常现象",
            "判定结论",
            "解题方式",
        )
    ]
    source_text = " ".join(part for part in source_text_parts if part)
    visual_object_markers = (
        "缝隙",
        "划痕",
        "磕碰",
        "磕点",
        "凹陷",
        "进灰",
        "灰尘",
        "异物",
        "掉漆",
        "磨损",
        "裂纹",
        "碎裂",
        "面积",
        "长度",
        "宽度",
        "直径",
        "数量",
    )
    has_visual_measurement_object = any(
        marker in source_text for marker in visual_object_markers
    )
    measurement_required = has_visual_measurement_object and bool(
        re.search(
            r"(?:超过|不少于|不超过|大于|小于|≤|≥|>=|<=)\s*"
            r"\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|个|颗|处)"
            r"|\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米)",
            source_text,
            re.IGNORECASE,
        )
    )
    measurement_available = measurement_required and bool(
        re.search(
            r"(?:实测|测量|量尺|带尺|尺子|直径为|长度为|宽度为|尺寸为|面积为)"
            r"[^。；;\n]{0,24}?(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万]+)"
            r"\s*(?:mm|毫米|cm|厘米|个|颗|处)",
            source_text,
            re.IGNORECASE,
        )
    )
    has_images = bool(_topic_image_links(rows)) or any(
        _clean_text(row.get("图片链接"))
        or _clean_text(row.get("主题图片链接"))
        for row in rows
    )
    image_context = has_images or any(
        marker in source_text for marker in ("图片", "照片", "截图", "图片核验")
    )
    if not image_context:
        measurement_required = False
    if not measurement_required:
        status = "not_needed"
    elif measurement_available:
        status = "required_available"
    elif has_images:
        status = "required_missing"
    else:
        status = "unusable"
    return {
        "measurement_required": measurement_required,
        "measurement_available": measurement_available,
        "visual_conclusion_allowed": not measurement_required
        or measurement_available,
        "status": status,
    }


def _measurement_review_content(query: dict[str, Any]) -> str:
    subject = _safe_join(
        [
            _clean_text(query.get("对象/部位")),
            _clean_text(query.get("异常现象")),
        ],
        "的",
    ) or "当前异常"
    return "\n".join(
        [
            f"1. 先确认{subject}是否能在图片中清晰复现。",
            "2. 当前图片只能辅助确认异常位置，不能直接确认具体尺寸、数量或处理档位。",
            "3. 请补充带量尺或可比对尺寸的近景照片，并记录实际测量结果。",
            "4. 取得尺寸证据后，再对照对应标准确定处理项。",
        ]
    )


def _append_measurement_evidence_boundary(
    content: str,
    *,
    query: dict[str, Any],
) -> str:
    """保留来源规则，只阻止未测量图片直接变成个案结论。"""
    normalized = _compact_knowledge_content(content, limit=800)
    if not normalized:
        return _measurement_review_content(query)
    boundary = (
        "当前案例图片未提供可核验的尺寸、数量或测量结果，"
        "以上规则只能作为判定口径，不能据此直接确定本案例的档位或结论；"
        "补充测量证据后再判定。"
    )
    if boundary not in normalized:
        normalized = f"{normalized}\n{boundary}"
    return normalized


def _topic_platform_from_source(rows: list[dict[str, Any]]) -> str:
    structured_platform = _merge_unique_text(
        [
            row.get("_原子平台")
            or row.get("平台")
            or row.get("系统类型")
            for row in rows
        ],
        separator="；",
    )
    structured_brand = _merge_unique_text(
        [
            row.get("_原子品牌")
            or row.get("品牌")
            or row.get("适用品牌")
            for row in rows
        ],
        separator="；",
    )
    structured_model = _merge_unique_text(
        [
            row.get("_原子机型范围")
            or row.get("机型")
            or row.get("适用机型")
            for row in rows
        ],
        separator="；",
    )
    source_text = " ".join(
        _clean_text(row.get(field))
        for row in rows
        for field in (
            "聊天内容",
            "原始核心问题",
            "核心问题",
            "原始判定结论",
            "判定结论",
        )
    ).lower()
    platform_family = _query_platform_family(
        {
            "核心问题": source_text,
            "平台": structured_platform,
            "品牌": structured_brand,
            "机型": structured_model,
        }
    )
    if platform_family == "apple":
        return "iOS"
    if platform_family == "non_apple":
        return "Android"

    generic_device_descriptions = {
        "这台",
        "该",
        "这个",
        "一台",
        "某台",
        "设备",
        "机器",
        "当前设备",
        "客户设备",
        "用户设备",
    }
    for match in re.finditer(
        r"(?:回收|检测|查看|设备为|机型为|型号为|一台)\s*"
        r"([^，,。；;？?\n]{2,40}?)(?:平板电脑|平板)",
        source_text,
        flags=re.IGNORECASE,
    ):
        device_description = match.group(1).strip()
        device_description = re.sub(
            r"^(?:一台|这台|该|这个|某台|设备|机器)\s*",
            "",
            device_description,
        ).strip()
        if (
            device_description
            and device_description not in generic_device_descriptions
            and not any(
                marker in device_description
                for marker in _APPLE_PLATFORM_MARKERS
            )
        ):
            return "Android"
    return ""


def _topic_query(
    rows: list[dict[str, Any]],
    evidence_package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = rows[0]
    evidence_package = evidence_package or _topic_evidence_package(rows)
    representative_facts = evidence_package.get("representative_facts") or []
    questions = [
        _clean_text(fact.get("atomic_question"))
        or _clean_text(fact.get("human_core_problem"))
        or _semantic_excerpt(fact.get("conversation_excerpt"), 240)
        for fact in representative_facts
    ]
    return {
        "产品类型": _clean_text(base.get("产品类型")),
        "一级分类": _clean_text(base.get("模型主题一级分类")) or _clean_text(base.get("一级分类")),
        "二级分类": _clean_text(base.get("模型主题二级分类")) or _clean_text(base.get("二级分类")),
        "核心问题": "；".join(question for question in questions if question),
        "人工核心问题": _merge_unique_text(
            [fact.get("human_core_problem") for fact in representative_facts],
            separator="；",
        ),
        "人工判定结论": _merge_unique_text(
            [
                fact.get("human_judgment_conclusion")
                for fact in representative_facts
            ],
            separator="；",
        ),
        "平台": _merge_unique_text(
            [
                row.get("_原子平台")
                or row.get("平台")
                or row.get("系统类型")
                for row in rows
            ],
            separator="；",
        )
        or _topic_platform_from_source(rows),
        "品牌": _merge_unique_text(
            [
                row.get("_原子品牌")
                or row.get("品牌")
                or row.get("适用品牌")
                for row in rows
            ],
            separator="；",
        ),
        "机型": _merge_unique_text(
            [
                row.get("_原子机型范围")
                or row.get("机型")
                or row.get("适用机型")
                for row in rows
            ],
            separator="；",
        ),
        "历史实际回复": _merge_unique_text(
            [
                fact.get("historical_actual_reply")
                for fact in representative_facts
            ],
            separator="\n",
        ),
        "聊天内容": _merge_unique_text(
            [row.get("聊天内容") for row in rows],
            separator="\n",
        ),
        "参考话术": _merge_unique_text(
            [row.get("参考话术") for row in rows],
            separator="\n",
        ),
        "判定依据": _merge_unique_text(
            [
                fact.get("judgment_basis") or fact.get("semantic_basis")
                for fact in representative_facts
            ],
            separator="；",
        ),
        "问题意图": _clean_text(base.get("问题意图")),
        "对象/部位": _clean_text(base.get("对象/部位")),
        "异常现象": _clean_text(base.get("异常现象")),
        "解题方式": _clean_text(base.get("解题方式")),
        "标准关键词": _merge_unique_keywords([row.get("主题标签") for row in rows]),
        "来源事实引用": _topic_fact_references(evidence_package),
    }


_TOPIC_INTERNAL_ANALYSIS_LABELS = (
    "问题背景：",
    "判断对象：",
    "来源核验依据：",
    "人工处理结论：",
)


def _topic_content_uses_internal_analysis_labels(value: Any) -> bool:
    content = _clean_text(value)
    return sum(
        marker in content
        for marker in _TOPIC_INTERNAL_ANALYSIS_LABELS
    ) >= 2


def _classify_topic_content_type(
    query: dict[str, Any],
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
) -> str:
    """Choose the compact body template from supported source evidence."""
    fields = (
        "核心问题",
        "人工核心问题",
        "人工判定结论",
        "判定依据",
        "历史实际回复",
        "问题意图",
        "对象/部位",
        "异常现象",
        "解题方式",
    )
    source_parts = [_clean_text(query.get(field)) for field in fields]
    source_parts.extend(
        _clean_text(row.get(field))
        for row in rows
        for field in (
            "核心问题",
            "判定结论",
            "判定依据",
            "聊天内容",
            "异常现象",
            "解题方式",
        )
    )
    source_parts.extend(
        _clean_text(value)
        for standard, _score in matches
        for value in (
            standard.title,
            standard.response_snippet,
            standard.standard_path,
        )
    )
    text = "\n".join(part for part in source_parts if part)
    if any(marker in text for marker in _CONTENT_TYPE_DISTINCTION_MARKERS):
        return CONTENT_TYPE_DISTINCTION
    method_text = re.sub(
        r"(?:检测|核验|测试|测量)方法\s*[：:]\s*(?:/|无|暂无|未提供)",
        "",
        text,
    )
    actions = {
        marker
        for marker in _CONTENT_TYPE_ACTION_MARKERS
        if marker in text
    }
    has_explicit_method = any(
        marker in method_text
        for marker in _CONTENT_TYPE_VERIFICATION_MARKERS
    )
    if has_explicit_method and actions:
        return CONTENT_TYPE_VERIFICATION
    if len(actions) >= 2 and any(
        marker in text
        for marker in ("方法", "步骤", "操作", "核验", "检测", "测试")
    ):
        return CONTENT_TYPE_VERIFICATION
    has_numeric_threshold = bool(_TOPIC_NUMERIC_CLAIM_PATTERN.search(text))
    if has_numeric_threshold and any(
        marker in text
        for marker in _CONTENT_TYPE_THRESHOLD_MARKERS
    ):
        return CONTENT_TYPE_THRESHOLD
    return CONTENT_TYPE_DEFINITION


def _compact_standard_rule_points(
    value: Any,
    content_type: str,
) -> list[str]:
    # 标准正文是可复用业务规则，不能因为历史模板的点数上限而截断
    # 分支、例外或最终处理项。保留一个宽松上限，防止异常长文本失控。
    maximum_points = 12
    text = _normalize_lines(value).replace("<br>", "\n")
    # 标准正文中的图片占位只保留在“图例”字段，不能进入知识正文；
    # 先清理图片 URL，再做编号和分段识别，避免 URL 被拆成半截文本。
    text = re.sub(r"\[img:[^\]]+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\n)\s*(?=\d+[.、]\s*)", "\n", text)
    points: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_text(raw_line)
        if not line:
            continue
        line = re.sub(
            r"^(?:标准定义|判定规则|检测方法|核验方法|处理步骤|"
            r"例外与边界|适用范围|标准说明|判断标准|判断与勾选|"
            r"特殊情况|勾选项)\s*[：:]\s*",
            "",
            line,
        ).strip()
        line = re.sub(r"^[-•]\s*", "", line).strip()
        line = re.sub(r"^\d+[.、]\s*", "", line).strip()
        if (
            not line
            or line in {
                "【标准说明】",
                "【判断标准】",
                "【判断与勾选】",
                "【检测方法】",
                "【特殊情况】",
                "【标准补充】",
            }
            or line in {"/", "无", "暂无", "未提供"}
            or line.startswith("http://")
            or line.startswith("https://")
        ):
            continue
        if line.endswith(("：", ":")):
            continue
        if line not in points:
            points.append(line)
        if len(points) >= maximum_points:
            break
    return points


_STANDARD_OPTION_PATH_PATTERN = re.compile(
    r"(?:【[^】]+】\s*[-—>＞]\s*)+【[^】]+】\s*[。；;：:]?"
)


def _standard_option_path(value: Any) -> str:
    """Return the explicitly declared selectable path, not a path list."""
    for raw_line in _normalize_lines(value).splitlines():
        line = _clean_text(raw_line)
        match = re.search(
            r"^(?:勾选项|对应勾选项|选择项)\s*[：:]\s*"
            r"((?:【[^】]+】\s*[-—>＞]\s*)+【[^】]+】)",
            line,
        )
        if match:
            return match.group(1).strip()
        bare_path = re.sub(r"^(?:[-•]|\d+[.、])\s*", "", line).strip()
        if _is_standard_option_path(bare_path):
            return bare_path.strip("。；;：: ")
    return ""


def _is_standard_option_path(value: Any) -> bool:
    return bool(_STANDARD_OPTION_PATH_PATTERN.fullmatch(_clean_text(value)))


def _is_incomplete_standard_rule_point(value: Any) -> bool:
    """Drop image-dependent fragments that cannot be read as a rule alone."""
    point = _clean_text(value)
    if not point:
        return True
    if "按图例" in point or "见图" in point:
        return True
    return (
        point.count("（") > point.count("）")
        or point.count("(") > point.count(")")
    )


def _query_requests_battery_health_unavailable(
    query: dict[str, Any] | None,
) -> bool:
    if not query:
        return False
    text = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "人工判定结论",
            "判定依据",
            "历史实际回复",
            "异常现象",
            "解题方式",
        )
    )
    if "电池健康度" not in text:
        return False
    has_unavailable_signal = any(
        marker in text
        for marker in ("无法读取", "读不出", "读取不到", "无法获取", "无法检测")
    )
    has_reading_path_signal = any(
        marker in text
        for marker in ("本机", "验机工具", "验机侠", "一根线", "支持APP", "支持 App")
    )
    has_reading_value = bool(
        re.search(r"\d+(?:\.\d+)?\s*(?:%|％)", text)
    )
    return has_unavailable_signal and has_reading_path_signal and not has_reading_value


def _battery_health_unavailable_rule_points(points: list[str]) -> list[str]:
    """Keep only the reading chain for a no-reading battery-health question."""
    relevant: list[str] = []
    unavailable_point_added = False
    for point in points:
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|％)", point):
            continue
        if any(marker in point for marker in ("打开", "设置-", "App Store", "下载", "点击")):
            continue
        if any(
            marker in point
            for marker in (
                "优先",
                "本机",
                "验机",
                "一根线",
                "支持",
                "无法获取",
                "无法取得",
                "无法检测",
                "读取不到",
                "读不出",
            )
        ):
            if "电池健康度无法检测" in point:
                if unavailable_point_added:
                    continue
                unavailable_point_added = True
            relevant.append(point)
    return list(dict.fromkeys(relevant))


def _build_compact_standard_content(
    standard: StandardCatalogItem | None,
    content_type: str,
    *,
    query: dict[str, Any] | None = None,
) -> str:
    if not standard or not standard.response_snippet:
        return ""
    points = _compact_standard_rule_points(
        standard.response_snippet,
        content_type,
    )
    option_path = _standard_option_path(standard.response_snippet)
    rule_points = [
        point
        for point in points
        if not _is_standard_option_path(point)
        and not _is_incomplete_standard_rule_point(point)
    ]
    battery_health_unavailable = _query_requests_battery_health_unavailable(
        query
    )
    if battery_health_unavailable:
        rule_points = _battery_health_unavailable_rule_points(rule_points)
    option_paths = _full_standard_option_paths(standard.response_snippet)
    if battery_health_unavailable:
        option_paths = [
            path
            for path in option_paths
            if "电池健康度无法检测" in path
        ]
    if len(option_paths) > 1:
        rule_points = [
            point
            for point in rule_points
            if not _full_standard_option_paths(point)
        ]
        if not rule_points:
            return (
                "1. 该标准包含多个候选项；请先完成标准要求的测量或核验，"
                "再在候选项/处理项中选择对应档位。"
            )
    if option_path and len(option_paths) == 1 and rule_points:
        return "\n\n".join(
            [
                f"1. 满足以下任一条件时，勾选{option_path}：",
                *rule_points,
            ]
        )
    return "\n".join(
        f"{index}. {point}"
        for index, point in enumerate(rule_points, start=1)
    )


def _full_standard_option_paths(value: Any) -> list[str]:
    paths: list[str] = []
    for raw_line in _normalize_lines(value).splitlines():
        line = _clean_text(raw_line)
        for match in _STANDARD_OPTION_PATH_PATTERN.finditer(line):
            path = match.group(0).strip("。；;：: ")
            if path and path not in paths:
                paths.append(path)
    return paths


def _standard_handling_options(
    standard: StandardCatalogItem,
    query: dict[str, Any] | None = None,
) -> list[str]:
    """Export selectable paths in the dedicated candidate field only."""
    paths = _full_standard_option_paths(standard.response_snippet)
    if _query_requests_battery_health_unavailable(query):
        paths = [path for path in paths if "电池健康度无法检测" in path]
    if paths:
        return [f"勾选{path}" for path in paths[:8]]
    invalid_leaf_labels = {
        "标准说明",
        "判断标准",
        "判断与勾选",
        "检测方法",
        "特殊情况",
        "标准补充",
        "苹果",
        "安卓",
        "鸿蒙",
        "通用",
    }
    explicit_options = [
        option
        for option in _extract_handling_options_from_text(
            standard.response_snippet
        )
        if "\n" not in _clean_text(option)
        and not any(
            label in invalid_leaf_labels
            for label in re.findall(r"【([^】]+)】", _clean_text(option))
        )
    ]
    if explicit_options:
        return list(dict.fromkeys(explicit_options))[:8]
    return [
        f"勾选{path}"
        for path in _full_standard_option_paths(standard.standard_path)[:8]
    ]


def _fallback_recall_subtitles(
    title: str,
    content: str,
    content_type: str,
) -> list[str]:
    subject = _clean_text(title).rstrip("？?")
    subject = re.sub(r"^(?:什么是|如何|怎么)", "", subject)
    subject = re.sub(
        r"(?:如何区分|怎么区分|如何判定|怎么判定|"
        r"达到什么条件需要判定|如何核验|怎么核验|"
        r"如何处理|怎么处理)$",
        "",
        subject,
    ).strip()
    if not subject:
        return []
    subtitles: list[str] = []

    def add(question: str) -> None:
        value = _clean_text(question)
        if value and value != title and value not in subtitles:
            subtitles.append(value[:120])

    if content_type == CONTENT_TYPE_DISTINCTION:
        add(f"{subject}分别怎么判断？")
    elif content_type == CONTENT_TYPE_THRESHOLD:
        add(f"{subject}达到什么条件需要判定？")
    elif content_type == CONTENT_TYPE_VERIFICATION:
        add(f"{subject}怎么核验？")
    else:
        add(f"{subject}有哪些判定要点？")
    if "有触感" in content and "无触感" in content:
        add(f"{subject}中的有触感和无触感情况如何区分？")
    if any(marker in content for marker in ("直径", "长度", "数量", "面积")):
        add(f"{subject}的尺寸和数量如何计算？")
    if any(marker in content for marker in ("测量", "测试", "检测", "核验", "刮擦")):
        add(f"{subject}如何核验？")
    add(f"{subject}有哪些处理或判定条件？")
    return subtitles[:6]


def _extract_handling_options_from_text(value: Any) -> list[str]:
    """Extract concrete handling options from standard or source text."""
    text = _clean_text(value)
    if not text:
        return []
    options: list[str] = []
    seen_cores: set[str] = set()
    patterns = (
        # 1) 勾选/选择/选/填写/输入 + 【选项】
        r"(?:勾选|选择|选|填写|输入)\s*【[^】]+】",
        # 2) 按【选项】处理/判定/填写/勾选
        r"按\s*【[^】]+】\s*(?:处理|判定|填写|勾选)",
        # 3) 判定为/属于/不属于/算 + 【选项】；单独的“按【X】”若后随
        #    处理/判定/填写/勾选则已在模式 2 捕获，此处用负向先行排除
        r"(?:判定为|属于|不属于|算|按(?!\s*【[^】]+】\s*(?:处理|判定|填写|勾选)))\s*【[^】]+】",
        # 4) 来源历史回复常用不带方括号的固定质检选项。
        r"(?:勾选|选择|选)\s*[“\"']?(?:无法检测|不支持|正常|异常)[”\"']?",
        # 5) 历史回复中的中文引号选项，例如勾选“屏幕-工具读出异常”。
        r"(?:勾选|选择|选)\s*[“\"']([^”\"']{2,40})[”\"']",
        # 6) 标准正文常见的“勾选项：标准路径”写法。
        r"勾选项\s*[：:]\s*(【[^】]+】(?:\s*-\s*【[^】]+】){1,4})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            option = (
                match.group(1).strip()
                if pattern.startswith("勾选项") and match.lastindex
                else match.group(0).strip()
            )
            if not option:
                continue
            local_context = text[
                max(0, match.start() - 36): min(len(text), match.end() + 12)
            ]
            if option.startswith("不属于") or re.search(
                r"(?:不能|不可|不得|不要|无需|不需要|不应|未发现[^。；;\n]{0,24})"
                r"[^。；;\n]{0,24}(?:直接)?\s*(?:勾选|选择|选)",
                local_context,
            ):
                # “不能直接勾选 X” and “未发现现象时不勾选 X” are
                # boundary rules, not affirmative handling options.
                continue
            # 同一选项保留首次出现的完整形式（含尾随动词），
            # “按【X】判定”与“按【X】”视为同一选项。
            core = re.sub(
                r"^(.*【[^】]+】)\s*(?:处理|判定|填写|勾选)$",
                r"\1",
                option,
            )
            if core in seen_cores:
                continue
            seen_cores.add(core)
            options.append(option)
    return options[:8]

def _topic_source_fact_has_explicit_conclusion(query: dict[str, Any]) -> bool:
    """Return whether source facts give an explicit handling option.

    Only concrete options like 勾选【X】 / 判定为【X】 / 按【X】处理 count.
    Bare deterministic conclusions without a selectable option (for example
    "按正常外观状态处理" or "未达到0.5mm阈值") must NOT count, because
    they depend on case thresholds that have no authoritative standard.
    """
    candidates = [
        _clean_text(query.get("人工判定结论")),
        _clean_text(query.get("判定结论")),
        _clean_text(query.get("历史实际回复")),
        _clean_text(query.get("解题方式")),
    ]
    for value in candidates:
        if _extract_handling_options_from_text(value):
            return True
    return False

def _strip_numeric_thresholds(value: str) -> str:
    """Remove numeric threshold comparisons from source facts when no
    authoritative standard backs them, so a case-only value like
    "未达到0.5mm阈值" cannot leak into a reusable rule body."""
    text = _clean_text(value)
    if not text:
        return ""
    # 1) 范围值: 0.3-0.4mm / 0.3~0.4mm
    text = re.sub(
        r"\d+(?:\.\d+)?\s*(?:-|~|～|至|到)\s*\d+(?:\.\d+)?"
        r"\s*(?:mm|毫米|cm|厘米|%|％|颗|个|处|次|秒)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # 2) 比较 + 数字 + 单位 (大于0.5mm才判定 / 未达到0.5mm阈值)
    text = re.sub(
        r"(?:大于|小于|超过|不少于|不超过|高于|低于|至少|至多|不低于|不大于|"
        r"达到|未达到|未达|≥|≤|>=|<=|>|<)\s*"
        r"\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|%|％|颗|个|处|次|秒)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # 3) 独立数字阈值 (0.5mm 单独出现)
    text = re.sub(
        r"\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|%|％|颗|个|处|次|秒)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # 4) 裸数字（避免 1. 2. 序号，只处理带小数点的阈值数字）
    text = re.sub(
        r"(?<![\d.。])\d+\.\d+(?![\d.])",
        "",
        text,
    )
    text = re.sub(r"[，,；;：:]+\s*$", "", text).strip()
    text = re.sub(r"[，,；;：:]\s*[，,；;：:]", "；", text)
    return text.strip("，,；;：: ").strip()


def _build_topic_source_fact_content(
    query: dict[str, Any],
    *,
    use_standard_references: bool = True,
    conservative: bool = False,
) -> str:
    issue = (
        _clean_text(query.get("人工核心问题"))
        or _clean_text(query.get("核心问题"))
    )
    subject = _safe_join(
        [
            _clean_text(query.get("对象/部位")),
            _clean_text(query.get("异常现象")),
        ],
        " / ",
    )
    basis = _clean_text(query.get("判定依据"))
    historical_reply = _clean_text(query.get("历史实际回复"))
    judgment = _clean_text(query.get("人工判定结论"))
    if not use_standard_references:
        def case_only_text(value: str) -> str:
            return (
                value.replace("质检标准", "既有处理口径")
                .replace("平台标准", "平台口径")
                .replace("当前有效标准", "当前处理口径")
                .replace("标准", "口径")
            )

        issue = case_only_text(issue)
        subject = case_only_text(subject)
        basis = case_only_text(basis)
        historical_reply = case_only_text(historical_reply)
        judgment = case_only_text(judgment)
    if conservative:
        # 未命中权威标准时，来源里的数值阈值只能证明该案例曾这样处理，
        # 不能转写成可复用规则。这里剥离阈值并弱化确定性结论。
        basis = _strip_numeric_thresholds(basis)
        historical_reply = _strip_numeric_thresholds(historical_reply)
        judgment = _strip_numeric_thresholds(judgment)
    sections: list[str] = []
    if issue:
        sections.append(f"适用情形：{issue}")
    verification_points = [subject, basis]
    normalized_judgment = _normalized_topic_claim(judgment)
    normalized_reply = _normalized_topic_claim(historical_reply)
    if (
        historical_reply
        and not (
            normalized_judgment
            and normalized_reply
            and (
                normalized_judgment in normalized_reply
                or normalized_reply in normalized_judgment
            )
        )
    ):
        verification_points.append(historical_reply)
    verification = _safe_join(verification_points, "；")
    if verification:
        sections.append(f"核验要点：{verification}")
    if judgment:
        sections.append(f"处理结论：{judgment}")
    elif historical_reply:
        sections.append(f"处理方式：{historical_reply}")
    sections.append(
        "适用边界：仅覆盖上述来源事实明确记录的对象、条件和结论；"
        "来源未说明的其他情形不得直接套用，需要补充对应事实后再判断。"
    )
    return "\n".join(sections)


def _template_style_knowledge_content(value: Any) -> str:
    """Format an auditable draft like the bulk-import template's body examples."""
    content, _embedded_reply = _split_embedded_recommended_reply(value)
    if not content:
        return ""
    if content.startswith("判定要点："):
        content = content.split("判定要点：", 1)[1].lstrip("\n")
    points: list[str] = []
    for line in content.splitlines():
        cleaned = re.sub(
            r"^(?:适用情形|核验要点|处理结论|处理方式|适用边界)[：:]\s*",
            "",
            line.strip(),
        )
        if cleaned:
            points.append(cleaned)
    if not points:
        points = [content]
    return "\n".join(
        f"{index}. {point}" for index, point in enumerate(points[:5], start=1)
    )


def _topic_rule_draft(
    topic_id: str,
    rows: list[dict[str, Any]],
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool = True,
) -> dict[str, Any]:
    query = _topic_query(rows)
    standard = matches[0][0] if matches else None
    topic_payload = _topic_stage_payload(
        topic_id,
        rows,
        use_standard_references=use_standard_references,
    )
    text = " ".join(
        [
            _clean_text(query.get("核心问题")),
            _clean_text(query.get("人工核心问题")),
            _clean_text(query.get("人工判定结论")),
            _clean_text(query.get("判定依据")),
            _clean_text(query.get("历史实际回复")),
        ]
    )
    has_draftable_source_rule = _topic_has_draftable_source_rule(topic_payload)
    has_substantive_historical_reply = len(
        _clean_text(query.get("历史实际回复"))
    ) >= 12
    # A failed standard lookup must not erase a concrete, auditable rule already
    # present in the reviewed case. The resulting experience candidate remains
    # human-review-only, especially when the source itself is uncertain. Keep
    # the stricter original rule for the no-standard-reference validation mode.
    has_source_rule = (
        (
            use_standard_references
            and not matches
            and has_draftable_source_rule
        )
        or (
            not use_standard_references
            and (has_draftable_source_rule or has_substantive_historical_reply)
            and not any(marker in text for marker in UNCERTAINTY_MARKERS)
        )
    )
    concrete_allowed = (
        (bool(matches) if use_standard_references else True)
        and _has_explicit_boundary_case(text)
        and not any(marker in text for marker in UNCERTAINTY_MARKERS)
    )
    knowledge_form = "具体判定" if concrete_allowed or has_source_rule else "流程方法"
    content_type = _classify_topic_content_type(query, rows, matches)
    title = (
        _guess_title(_clean_text(query.get("核心问题")), standard)
        if concrete_allowed or has_source_rule
        else _process_title(
            _clean_text(query.get("核心问题")),
            _clean_text(query.get("一级分类")),
            _clean_text(query.get("二级分类")),
            standard,
            _clean_text(query.get("产品类型")),
        )
    )
    if not title:
        title = _rebuild_title_from_structured_fields(query, standard)
    if use_standard_references and standard:
        # A model failure with an authoritative standard falls back to the
        # standard rules themselves, never to a generic process template.
        content = _build_compact_standard_content(
            standard,
            content_type,
            query=query,
        )
    elif use_standard_references and not standard:
        # No standard means evidence review only; do not manufacture a rule.
        content = _build_experience_review_content(query)
    else:
        content = (
            _build_model_content(
                _clean_text(query.get("核心问题")),
                _clean_text(query.get("人工判定结论")),
                _clean_text(query.get("判定依据")),
                "",
                standard,
            )
            if concrete_allowed
            else _build_topic_source_fact_content(
                query,
                use_standard_references=use_standard_references,
            )
            if has_source_rule
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
    if has_source_rule:
        content = _template_style_knowledge_content(content)
    return {
        "title": title,
        "subtitles": _fallback_recall_subtitles(
            title,
            content,
            content_type,
        ),
        "content": content,
        "content_type": content_type,
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
        "confidence": 0.72 if concrete_allowed else 0.62 if has_source_rule else 0.45,
        "reasoning_summary": (
            (
                "主题仅聚合明确边界问题，且命中可信标准。"
                if use_standard_references
                else "主题案例证据清楚、一致，可形成待人工审核的案例型知识候选。"
            )
            if concrete_allowed
            else "来源会话或历史实际回复包含可复用处理口径，已整理为待人工审核的案例型知识候选。"
            if has_source_rule
            else "按主题证据沉淀通用核验流程；不将单个工单的个案结论外推。"
        ),
        "needs_human_review": not concrete_allowed or has_source_rule,
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
    # Only treat verifiable standard identifiers as standard references.
    # Conversational mentions like "标准" or "标准条款" in historical replies
    # are case facts, not references, and must not roll back a valid draft.
    return bool(
        re.search(
            r"(?:STD|QC)[-_：:][A-Z0-9_-]{2,}|"
            r"【[^】]+】\s*-\s*【[^】]+】|"
            r"(?:标准编号|标准路径|标准条款号|标准名称|关联标准|引用标准)"
            r"[：:\s]+[^，。；;\n]{1,60}|"
            r"(?:依据|按照|对照|参照|根据)\s*(?:现行|当前|总部|平台|质检|回收)?"
            r"(?:标准|口径)\s*[：:]\s*[^，。；;\n]{1,80}",
            text,
            flags=re.IGNORECASE,
        )
    )


def _topic_draft_is_generic(
    candidate: dict[str, Any],
    rows: list[dict[str, Any]],
) -> bool:
    source_values = [
        _historical_actual_reply(row)
        for row in rows
    ] + [
        _clean_text(row.get(field))
        for row in rows
        for field in (
            "聊天内容",
            "核心问题",
            "原始核心问题",
            "判定结论",
            "原始判定结论",
            "判定依据",
            "语义标注依据",
        )
    ]
    assessment = assess_case_only_draft(
        content=_clean_text(candidate.get("content")),
        source_values=source_values,
    )
    return assessment.decision == "manual_review"


_CASE_ANALYSIS_TITLE_PATTERN = re.compile(
    r"^(?:回收师|用户|客户|客服).{0,48}(?:询问|咨询|提问|描述|反馈|反映)"
)
_CASE_ANALYSIS_CONTENT_MARKERS = (
    "本次会话",
    "本案例",
    "根据现场描述",
    "根据会话",
    "关键事实",
    "处理结论",
    "回收师描述",
    "用户描述",
)
_CASE_ANALYSIS_REPLY_MARKERS = (
    "本次会话",
    "本案例",
    "回收师",
    "用户描述",
    "根据现场描述",
    "根据会话",
)


def _topic_draft_is_case_analysis(candidate: dict[str, Any]) -> bool:
    """Return whether a knowledge draft is narrating one case instead of a reusable rule."""
    title = _clean_text(candidate.get("title"))
    content = _clean_text(candidate.get("content"))
    recommended_reply = _clean_text(candidate.get("recommended_reply"))
    content_marker_count = sum(
        marker in content for marker in _CASE_ANALYSIS_CONTENT_MARKERS
    )
    reply_contains_case_subject = any(
        marker in recommended_reply for marker in _CASE_ANALYSIS_REPLY_MARKERS
    )
    return bool(
        _CASE_ANALYSIS_TITLE_PATTERN.search(title)
        or content_marker_count >= 2
        or (content_marker_count >= 1 and reply_contains_case_subject)
    )


def _topic_has_unavailable_required_images(rows: list[dict[str, Any]]) -> bool:
    if not _topic_needs_images(rows):
        return False
    return any(
        "不可用:" in _clean_text(row.get("图片处理状态"))
        and not _has_usable_image_evidence(row)
        for row in rows
    )


def _topic_draft_has_source_specific_content(
    candidate: dict[str, Any],
    rows: list[dict[str, Any]],
) -> bool:
    source_values = [
        _historical_actual_reply(row)
        for row in rows
    ] + [
        _clean_text(row.get(field))
        for row in rows
        for field in (
            "聊天内容",
            "核心问题",
            "原始核心问题",
            "判定结论",
            "原始判定结论",
            "判定依据",
            "语义标注依据",
        )
    ]
    return has_source_specific_case_content(
        content=_clean_text(candidate.get("content")),
        source_values=source_values,
    )


def _manual_pending_topic_stage(
    classification: dict[str, Any],
) -> dict[str, Any]:
    return {
        **classification,
        "knowledge_value": "待确认",
        "value_reason": _safe_join(
            [
                _clean_text(classification.get("value_reason")),
                "转写正文只有通用模板，未包含来源会话或历史实际回复中的具体事实、条件和处理结论。",
            ],
            "；",
        ),
        "reusable_knowledge": "未形成可追溯的具体知识正文，禁止以通用模板直接沉淀。",
        "needs_human_review": True,
    }


def _topic_model_budget_pending_stage(
    classification: dict[str, Any],
    call_limit: int,
) -> dict[str, Any]:
    return {
        **classification,
        "knowledge_value": "待确认",
        "value_reason": _safe_join(
            [
                _clean_text(classification.get("value_reason")),
                f"本轮主题模型调用达到上限 {call_limit}，已转人工优先审核。",
            ],
            "；",
        ),
        "reusable_knowledge": "主题模型调用额度已用尽，保留来源证据等待人工确认。",
        "needs_human_review": True,
    }


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
    has_retrieved_standard = use_standard_references and bool(matches)
    return (
        any(marker in text for marker in UNCERTAINTY_MARKERS)
        or (
            not _has_explicit_boundary_case(text)
            and not has_retrieved_standard
        )
        or (
            use_standard_references
            and
            candidate.get("knowledge_form") == "具体判定"
            and not candidate.get("standard_refs")
        )
    )


def _compact_knowledge_content(value: Any, limit: int = 650) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Source replies may retain their original question number before being
    # wrapped as a new knowledge point (for example, "1. 2. 重启记录问题").
    # Keep the outer knowledge number and remove only the nested source one.
    text = re.sub(
        r"(?m)^(\s*\d+[.、]\s+)(?:\d+[.、]\s+)+",
        r"\1",
        text,
    )
    replacements = {
        "转人工确认": "补充证据后再判定",
        "转人工复核": "补充证据后再判定",
        "转人工": "补充证据后再判定",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(
        r"补充证据后(?:补充证据后)?再判定审核",
        "补充证据后转人工审核",
        text,
    )
    # Only split compact numbered lists such as "1. 第一步". Requiring
    # whitespace after the marker avoids breaking measurements like "0.3-0.4mm".
    text = re.sub(r"(?<!\n)\s*(?=\d+[.、]\s+\S)", "\n", text)

    def clip_line(line: str, line_limit: int = 170) -> str:
        if len(line) <= line_limit:
            return line
        head = line[:line_limit]
        boundary = max(head.rfind(marker) for marker in ("。", "；", ";", "，", ","))
        return (head[: boundary + 1] if boundary >= 40 else head).rstrip()

    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if line in lines:
            continue
        lines.append(clip_line(line))
        if len(lines) >= 6:
            break
    selected: list[str] = []
    used = 0
    for line in lines:
        separator_length = 1 if selected else 0
        if line == "":
            if selected and selected[-1] != "":
                selected.append("")
                used += separator_length
            continue
        remaining = limit - used - separator_length
        if remaining <= 0:
            break
        clipped = clip_line(line, remaining)
        if not clipped:
            break
        selected.append(clipped)
        used += len(clipped) + separator_length
    return "\n".join(selected).rstrip("；;，, ")


_EMBEDDED_RECOMMENDED_REPLY_PATTERN = re.compile(
    r"(?:^|\n|[；;。]\s*)【?(?:推荐回复|答复建议|回复建议)】?\s*[：:]?\s*(.*)$",
    flags=re.IGNORECASE | re.DOTALL,
)


def _split_embedded_recommended_reply(
    content: Any,
    recommended_reply: Any = "",
) -> tuple[str, str]:
    """Keep the importable body and the user-facing reply in separate fields."""
    body = _clean_text(content)
    reply = _compact_recommended_reply(recommended_reply)
    match = _EMBEDDED_RECOMMENDED_REPLY_PATTERN.search(body)
    if not match:
        return body, reply
    embedded_reply = _compact_recommended_reply(match.group(1))
    body = body[: match.start()].rstrip("\n；;，, ")
    return body, reply or embedded_reply


_KNOWLEDGE_CONTENT_TITLE_PATTERN = re.compile(
    r"^(?:#{1,6}\s*)?(?:主标题|标题|知识标题)\s*[：:]\s*.*$"
)
_KNOWLEDGE_CONTENT_SCOPE_PATTERN = re.compile(
    r"^(?:适用范围|适用情形|适用主题)\s*[：:]\s*.*$"
)
_KNOWLEDGE_CONTENT_SECTIONS = (
    (
        "判定规则",
        re.compile(
            r"^(?:判定规则|判定标准|标准依据|判定要点|案例结论|实际结论|处理结论)"
            r"\s*[：:]?\s*(.*)$"
        ),
    ),
    (
        "处理步骤",
        re.compile(r"^(?:处理步骤|处理流程|核验方法|核验要点|核验流程|处理方式)\s*[：:]?\s*(.*)$"),
    ),
    (
        "例外与边界",
        re.compile(r"^(?:例外与边界|处理边界|适用边界|边界说明|适用限制)\s*[：:]?\s*(.*)$"),
    ),
)
_KNOWLEDGE_CONTENT_SECTION_LINE_PATTERN = re.compile(
    r"^(?:判定规则|处理步骤|例外与边界)\s*[：:]?\s*$"
)


def _structured_knowledge_content(
    value: Any,
    *,
    title: str,
    standard: StandardCatalogItem | None,
    use_standard_references: bool,
    evidence_package: dict[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """Keep the knowledge body importable and separate from title and reply."""

    sections: dict[str, list[str]] = {
        "判定规则": [],
        "处理步骤": [],
        "例外与边界": [],
    }
    active_section = ""
    normalized_title = _normalized_topic_claim(title)
    for raw_line in _compact_knowledge_content(value).splitlines():
        line = _clean_text(raw_line)
        if not line or _KNOWLEDGE_CONTENT_TITLE_PATTERN.match(line):
            continue
        if _KNOWLEDGE_CONTENT_SCOPE_PATTERN.match(line):
            continue
        if normalized_title and _normalized_topic_claim(line) == normalized_title:
            continue
        section_match = next(
            (
                (section_name, pattern.match(line))
                for section_name, pattern in _KNOWLEDGE_CONTENT_SECTIONS
                if pattern.match(line)
            ),
            None,
        )
        if section_match:
            active_section, match = section_match
            detail = _clean_text(match.group(1))
            if detail:
                sections[active_section].append(detail)
            continue
        sections[active_section or "判定规则"].append(line)

    issues: list[str] = []
    standard_rule = _clean_text(standard.response_snippet) if standard else ""
    source_texts = _topic_source_claim_texts(evidence_package, [])
    if standard_rule:
        sections["判定规则"] = [standard_rule]
        sections["处理步骤"] = [
            "标准未提供可复用的处理步骤，需人工补充后再审核。"
        ]
        sections["例外与边界"] = [
            "标准未提供可复用的例外与边界，不能直接外推；需人工补充后再审核。"
        ]
    else:
        for section_name, values in sections.items():
            source_backed_values = [
                item
                for item in values
                if _topic_claim_is_source_supported(item, source_texts)
            ]
            if len(source_backed_values) != len(values):
                issues.append(f"{section_name}包含无来源内容，已移除并转人工复核。")
            sections[section_name] = source_backed_values
    if not sections["判定规则"]:
        sections["判定规则"].append(
            "未命中可引用的明确规则，不得依据单个案例外推确定性结论。"
        )
        issues.append("知识正文缺少可追溯判定规则，已标记人工复核。")
    if not sections["处理步骤"]:
        sections["处理步骤"].extend(
            [
                "1. 核对来源中的具体对象、现象和触发条件。",
                "2. 补充支持判断的图片、测试记录或查询结果。",
                "3. 证据完整后由人工按适用口径判断。",
            ]
        )
        issues.append("知识正文缺少处理步骤，已补充审核流程。")
    if not sections["例外与边界"]:
        sections["例外与边界"].append(
            "不符合适用范围、标准未覆盖或证据不足时，不得直接套用；补充证据后转人工审核。"
        )
        issues.append("知识正文缺少例外与边界，已补充人工审核边界。")

    return (
        "\n".join(
            f"{section_name}：\n" + "\n".join(sections[section_name])
            for section_name in ("判定规则", "处理步骤", "例外与边界")
        ),
        tuple(issues),
    )


def _topic_product_type(
    query: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    product_type = query.get("产品类型编码") or query.get("产品类型")
    category = resolve_product_category(product_type)
    if category:
        return category.name
    if (
        _business_line_for_row(rows[0]) == "聚合回收"
        and is_concrete_unconfigured_product(product_type)
    ):
        return _clean_text(product_type)
    for row in rows:
        if candidate := _resolved_product_type_for_row(row):
            return candidate
    return UNKNOWN_PRODUCT_NAME


def _normalized_applicable_scope(
    product_type: str,
    candidate_scope: Any,
    rows: list[dict[str, Any]],
    *,
    trusted_scope: bool = False,
) -> str:
    # CZ 的适用范围只接受产品品类；品牌和机型使用独立字段。
    _ = candidate_scope, rows, trusted_scope
    return product_type


def _split_applicability_values(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = re.split(
            r"[\n,，;；|、]+",
            _clean_text(value),
        )
    return list(
        dict.fromkeys(
            cleaned
            for item in raw_values
            if (
                cleaned := _clean_text(item).strip()
            )
        )
    )


_SOURCE_SPECIFIC_MODEL_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9])iPhone\s*\d{1,2}"
        r"(?:\s*(?:Pro(?:\s*Max)?|Plus|mini))?(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])Switch\s*Lite(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"拯救者\s*[A-Za-z]?\d{3,4}[A-Za-z0-9+\-]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:Canon\s*)?EOS\s*[A-Z]?\d{1,3}[A-Za-z0-9+\-]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:Sony\s*)?A\d{3,4}[A-Za-z0-9+\-]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:ThinkBook|ThinkPad)\s+[A-Za-z0-9+\- ]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])MacBook(?:\s+[A-Za-z]+)?(?:\s+\d{1,3})?(?:\s*\([^)]*\))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])AirPods(?:\s+[A-Za-z]+)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])Apple\s+Pencil(?:\s+[A-Za-z]+)?",
        re.IGNORECASE,
    ),
)


def _source_specific_model_values(source_values: list[str]) -> list[str]:
    values: list[str] = []
    for source in source_values:
        for pattern in _SOURCE_SPECIFIC_MODEL_PATTERNS:
            for match in pattern.finditer(source):
                value = re.sub(r"\s+", " ", match.group(0)).strip()
                if value and value not in values:
                    values.append(value)
    return values


def _strip_specific_models_from_text(value: Any, models: list[str] | None = None) -> str:
    """Remove model-specific tokens from visible reusable knowledge text."""
    text = _clean_text(value)
    candidates = list(models or [])
    candidates.extend(_source_specific_model_values([text]))
    for model in sorted(dict.fromkeys(candidates), key=len, reverse=True):
        cleaned = _clean_text(model)
        if cleaned:
            text = re.sub(re.escape(cleaned), "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=(?:限定款|限量款|特别版))", "", text)
    text = re.sub(r"\s+([，。；：！？?])", r"\1", text)
    text = re.sub(r"([（(])\s*([）)])", r"\1\2", text)
    return text.strip(" ，,；;：:｜|\n")


def _supported_topic_applicability_values(
    candidate_values: Any,
    rows: list[dict[str, Any]],
    field: str,
    product_type: str,
) -> list[str]:
    source_fields = {
        "适用品牌": ("适用品牌", "品牌", "_原子品牌"),
        "适用机型": ("适用机型", "机型", "_原子机型范围"),
    }.get(field, (field,))
    preserved_values = [
        value
        for row in rows
        for source_field in source_fields
        for value in _split_applicability_values(row.get(source_field))
    ]
    source_values = [
        value
        for row in rows
        for value in (
            _clean_text(row.get("聊天内容")),
            _clean_text(row.get("核心问题")),
            _clean_text(row.get("原始核心问题")),
            _clean_text(row.get("判定结论")),
            _clean_text(row.get("原始判定结论")),
            _clean_text(row.get("判定依据")),
            _historical_actual_reply(row),
            _clean_text(row.get("品牌")),
            _clean_text(row.get("机型")),
            _clean_text(row.get("_原子品牌")),
            _clean_text(row.get("_原子机型范围")),
            _clean_text(row.get(field)),
        )
        if value
    ]
    ignored_values = {
        "",
        "通用",
        "不限",
        "全部",
        "所有",
        "品类通用",
        "品类专用",
        "平台通用",
        "平台专用",
        "待确认",
        "未知",
        product_type,
    }
    result: list[str] = []
    inferred_values = (
        _source_specific_model_values(source_values)
        if field == "适用机型"
        else []
    )
    for value in [
        *preserved_values,
        *_split_applicability_values(candidate_values),
        *inferred_values,
    ]:
        if value in ignored_values or value in result:
            continue
        if (
            value in preserved_values
            or _applicability_value_is_source_supported(
                value,
                source_values,
            )
        ):
            result.append(value)
    # 例如来源同时抽取出“AirPods”和更具体的“AirPods 一代”时，
    # 只保留更具体的适用机型，避免导出重复且会扩大适用范围的值。
    specific_values = [
        value
        for value in result
        if not any(
            value != other
            and len(_normalized_topic_claim(other)) > len(_normalized_topic_claim(value))
            and _normalized_topic_claim(value) in _normalized_topic_claim(other)
            for other in result
        )
    ]
    return specific_values


_TOPIC_SPECIFIC_MODEL_DIRECT_MARKERS = (
    "不支持",
    "支持",
    "不可回收",
    "可以回收",
    "可回收",
    "不收",
    "不影响",
    "影响",
    "不属于",
    "属于",
)


def _topic_specific_model_conclusion(
    query: dict[str, Any],
    model: str,
) -> str:
    for value in (
        query.get("人工判定结论"),
        query.get("历史实际回复"),
    ):
        conclusion = _clean_text(value)
        if not conclusion or not any(
            marker in conclusion
            for marker in _TOPIC_SPECIFIC_MODEL_DIRECT_MARKERS
        ):
            continue
        if "是否支持" in conclusion and "不支持" not in conclusion:
            continue
        if not _applicability_value_is_source_supported(
            model,
            [
                _clean_text(query.get("人工核心问题")),
                _clean_text(query.get("核心问题")),
                conclusion,
            ],
        ):
            continue
        if _normalized_topic_claim(model) not in _normalized_topic_claim(
            conclusion
        ):
            conclusion = f"{model}：{conclusion}"
        return conclusion
    return ""


def _topic_text_keeps_specific_conclusion(
    value: str,
    conclusion: str,
) -> bool:
    expected = [
        marker
        for marker in _TOPIC_SPECIFIC_MODEL_DIRECT_MARKERS
        if marker in conclusion
    ]
    return bool(expected) and all(marker in value for marker in expected)


def _specific_model_recommended_reply(
    model: str,
    conclusion: str,
) -> str:
    statement = conclusion.rstrip("。；;，, ")
    return _compact_recommended_reply(
        f"您好，{statement}。该结论仅适用于{model}，"
        "其他型号不能直接套用。"
    )


def _applicability_value_is_source_supported(
    value: str,
    source_values: list[str],
) -> bool:
    cleaned = _clean_text(value)
    if not cleaned or re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return False
    if re.fullmatch(r"[A-Za-z0-9 ._+\-]+", cleaned):
        pattern = re.compile(
            r"(?<![A-Za-z0-9])"
            + re.sub(r"\\\s+", r"\\s+", re.escape(cleaned))
            + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        return any(pattern.search(source) for source in source_values)
    normalized_value = _normalized_topic_claim(cleaned)
    return bool(
        len(normalized_value) >= 2
        and any(
            normalized_value in _normalized_topic_claim(source)
            for source in source_values
        )
    )


def _recommended_reply(
    title: str,
    content: str,
    existing_reply: str = "",
    use_standard_references: bool = True,
) -> str:
    source_content = _compact_knowledge_content(existing_reply or content, limit=500)
    raw_points = []
    for line in source_content.splitlines():
        point = re.sub(r"^\s*(?:[-•]|\d+[.、])\s*", "", line).strip()
        point = re.sub(r"^(?:您好|你好)[，,：:\s]*", "", point).strip()
        if not point or point.endswith("：") or point.startswith("适用主题"):
            continue
        label_match = re.match(
            r"^(适用情形|核验要点|核验方法|检查条件|触发条件|"
            r"处理结论|处理方式|适用边界|例外)[：:]\s*(.*)$",
            point,
        )
        label = label_match.group(1) if label_match else ""
        value = label_match.group(2).strip() if label_match else point
        value = re.sub(
            r"(?:当前案例|本次会话|当前来源)(?:中|里)?",
            "",
            value,
        ).strip()
        if value:
            raw_points.append((label, value))
    if existing_reply:
        points = [value for _label, value in raw_points[:3]]
    else:
        priority = {
            "处理结论": 0,
            "处理方式": 1,
            "核验要点": 2,
            "核验方法": 2,
            "检查条件": 2,
            "触发条件": 2,
            "适用边界": 3,
            "例外": 3,
            "适用情形": 4,
            "": 2,
        }
        points = [
            value
            for _label, value in sorted(
                raw_points,
                key=lambda item: priority.get(item[0], 2),
            )[:3]
        ]
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
        else "如现有证据不足或现象无法复现，请补充信息后再处理。"
    )
    # 推荐回复只回答知识点本身，不重复知识标题；标题属于独立字段。
    # 这样可以避免“您好，关于标题……”把候选重新变成案例摘要。
    reply = f"您好，{body}。{closing}"
    if len(reply) <= 180:
        return reply
    head = reply[:180]
    boundary = max(head.rfind(marker) for marker in ("。", "；", "，", ","))
    return (head[: boundary + 1] if boundary >= 80 else head).rstrip()


def _recommended_reply_from_final_content(
    final_content: str,
    *,
    evidence_status: str = "available",
) -> str:
    """Generate a short reply only from the already-finalized body."""
    content = _compact_knowledge_content(final_content, limit=800)
    if not content:
        return ""
    if evidence_status in {"required_missing", "unusable"}:
        return (
            "当前图片只能辅助确认异常位置，不能直接确认具体尺寸、数量或处理档位；"
            "请补充测量证据后再判定。"
        )
    points: list[str] = []
    for line in content.splitlines():
        point = re.sub(r"^\s*(?:[-•]|\d+[.、])\s*", "", line).strip()
        point = re.sub(
            r"^(?:标准定义|检测方法|核验方法|处理步骤|例外与边界)\s*[：:]\s*",
            "",
            point,
        )
        point = re.sub(r"(?:当前案例|本次会话|当前来源)(?:中|里)?", "", point)
        # Standard paths belong in the dedicated association/handling fields.
        # A reply may mention the terminal option, but should not start with
        # the full internal hierarchy copied from the knowledge body.
        path_only = re.fullmatch(
            r"(?:【[^】]+】\s*[-—>＞]\s*)+【([^】]+)】\s*[。；;：:]?",
            point,
        )
        if path_only:
            point = f"选择【{path_only.group(1)}】"
        else:
            point = re.sub(
                r"^(?:【[^】]+】\s*[-—>＞]\s*)+",
                "",
                point,
            ).strip()
        if point and not point.endswith(("：", ":")):
            points.append(point.rstrip("。；;"))
    if not points:
        return ""
    reply = "；".join(points[:3]) + "。"
    return reply[:180].rstrip("；，, ")


def _draft_status_for_model_result(
    *,
    model_error: str,
    quality_issues: list[str],
    has_standard: bool,
    model_call_failed: bool | None = None,
) -> tuple[str, str]:
    call_failed = (
        bool(_clean_text(model_error))
        if model_call_failed is None
        else model_call_failed
    )
    if call_failed:
        return (
            "model_failed",
            "standard_rule_fallback" if has_standard else "evidence_review_only",
        )
    if quality_issues:
        return "model_success", "blocked"
    return "model_success", "ready_for_human_review"


def _topic_model_failure_status(exc: Exception) -> tuple[str, str]:
    error = _clean_text(exc)
    validation_markers = (
        "JSON 校验失败",
        "输出缺少",
        "输出为空",
        "输出不是 JSON",
        "输出的 subtitles",
        "输出的 standard_refs",
        "输出的 applicable_",
        "输出的 confidence",
        "输出的 knowledge_form",
        "输出的 content_type",
        "正文必须使用对应数量的编号要点",
        "草稿包含标准引用",
        "重写草稿包含标准引用",
    )
    if isinstance(exc, MimoError) and any(
        marker in error for marker in validation_markers
    ):
        return "topic_model_validation_failed", "model_success"
    return "topic_model_call_failed", "model_failed"


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


_RECOMMENDED_REPLY_TOPIC_SWITCH_PATTERN = re.compile(
    r"(?:^|[。；;！？!?])\s*"
    r"(?:(?:另外|此外|还有|同时|再者|至于)\s*[，,]?\s*)?"
    r"(?:关于|针对)\s*[“\"']?"
    r"([^，,。；;：:\n]{1,32})"
    r"[”\"']?\s*[：:]"
)
_RECOMMENDED_REPLY_GENERIC_TOPIC_LABELS = {
    "该问题",
    "此问题",
    "上述问题",
    "本问题",
    "当前问题",
    "该情况",
    "此情况",
    "上述情况",
    "当前情况",
    "该主题",
    "此主题",
    "当前主题",
}
_RECOMMENDED_REPLY_GENERIC_BIGRAMS = {
    "您好",
    "你好",
    "关于",
    "建议",
    "请先",
    "如果",
    "当前",
    "情况",
    "问题",
    "信息",
    "证据",
    "补充",
    "处理",
    "判定",
    "确认",
    "需要",
    "可以",
    "进行",
    "相关",
    "无法",
    "不能",
    "人工",
    "回复",
    "主题",
    "内容",
    "结果",
    "存在",
    "设备",
}
_RECOMMENDED_REPLY_GENERIC_FOLLOWUP_PATTERN = re.compile(
    r"(?:如|若|如果).{0,30}"
    r"(?:不足|不清晰|不一致|无法|不能|有疑问).{0,30}"
    r"(?:补充|提供|确认|复核).{0,24}"
    r"(?:处理|判定|确认|复核)"
)
_RECOMMENDED_REPLY_DANGLING_PATTERN = re.compile(
    r"(?:上的|中的|内的|相关的|对应的|以及|并且|或者|"
    r"进入|查看|记录|选择|排除)"
    r"(?=[。！？!?])"
)


def _recommended_reply_topic_key(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(
        r"^(?:另一个|其他|其它|相关|对应|当前|本次)",
        "",
        text,
    )
    text = re.sub(
        r"(?:问题|情况|情形|事项|咨询|说明|处理)$",
        "",
        text,
    )
    return re.sub(
        r"[\s，,。；;：:！？!?（）()\[\]【】“”\"'、]+",
        "",
        text,
    ).lower()


def _recommended_reply_meaningful_bigrams(value: Any) -> set[str]:
    key = _recommended_reply_topic_key(value)
    return {
        key[index : index + 2]
        for index in range(len(key) - 1)
        if key[index : index + 2] not in _RECOMMENDED_REPLY_GENERIC_BIGRAMS
    }


def _recommended_reply_topic_label_is_current(
    label: str,
    title: str,
    content: str,
) -> bool:
    cleaned_label = _clean_text(label)
    if re.match(r"^(?:另一个|其他|其它)", cleaned_label):
        return False
    if cleaned_label in _RECOMMENDED_REPLY_GENERIC_TOPIC_LABELS:
        return True
    label_key = _recommended_reply_topic_key(cleaned_label)
    topic_key = _recommended_reply_topic_key(f"{title}\n{content}")
    if not label_key or not topic_key:
        return True
    if label_key in topic_key:
        return True
    if len(label_key) == 2:
        return label_key in topic_key
    shared_bigrams = (
        _recommended_reply_meaningful_bigrams(label_key)
        & _recommended_reply_meaningful_bigrams(topic_key)
    )
    return len(shared_bigrams) >= 2 or SequenceMatcher(
        None,
        label_key,
        topic_key,
    ).ratio() >= 0.55


def _recommended_reply_unrelated_segments(
    reply: str,
    title: str,
    content: str,
) -> list[str]:
    topic_key = _recommended_reply_topic_key(f"{title}\n{content}")
    topic_bigrams = _recommended_reply_meaningful_bigrams(topic_key)
    unrelated: list[str] = []
    for raw_segment in re.split(r"[。；;！？!?]+", reply):
        segment = _clean_text(raw_segment)
        segment_key = _recommended_reply_topic_key(segment)
        if len(segment_key) < 6:
            continue
        if _RECOMMENDED_REPLY_GENERIC_FOLLOWUP_PATTERN.search(segment):
            continue
        if re.match(
            r"^(?:(?:另外|此外|还有|同时|再者|至于)\s*[，,]?\s*)?"
            r"(?:关于|针对).{1,32}[：:]",
            segment,
        ):
            continue
        if segment_key in topic_key:
            continue
        shared_bigrams = (
            _recommended_reply_meaningful_bigrams(segment_key)
            & topic_bigrams
        )
        if len(shared_bigrams) >= 2:
            continue
        if SequenceMatcher(
            None,
            segment_key,
            topic_key,
        ).ratio() >= 0.35:
            continue
        unrelated.append(segment[:28])
    return list(dict.fromkeys(unrelated))


def _recommended_reply_quality_issues(
    value: Any,
    *,
    title: str = "",
    content: str = "",
) -> list[str]:
    reply = re.sub(r"\s+", " ", _clean_text(value)).strip()
    if not reply:
        return []
    issues: list[str] = []
    if any(
        marker in reply
        for marker in (
            "问题背景",
            "判断对象",
            "来源核验依据",
            "人工处理结论",
        )
    ):
        issues.append("包含内部分析标签")
    greeting_count = len(re.findall(r"(?:您好|你好)", reply))
    if greeting_count > 1 or re.search(r"建议\s*(?:您好|你好)", reply):
        issues.append("重复问候")
    if re.search(r"(?:[，,]\s*[；;。.!！？?]|[；;]\s*[。.!！？?])", reply):
        issues.append("异常标点")
    if not re.search(r"[。！？!?][”’\"）)\]】]*$", reply):
        issues.append("句尾残缺")
    if _RECOMMENDED_REPLY_DANGLING_PATTERN.search(reply):
        issues.append("句意残缺")
    topic_switches = [
        _clean_text(match.group(1))
        for match in _RECOMMENDED_REPLY_TOPIC_SWITCH_PATTERN.finditer(reply)
        if not _recommended_reply_topic_label_is_current(
            match.group(1),
            title,
            content,
        )
    ]
    if topic_switches:
        issues.append(
            "切换到其他主题：" + "、".join(dict.fromkeys(topic_switches))
        )
    unrelated_segments = _recommended_reply_unrelated_segments(
        reply,
        title,
        content,
    )
    if unrelated_segments:
        issues.append(
            "疑似主题外语句：" + "、".join(unrelated_segments)
        )
    return issues


def _unique_topic_values(
    rows: list[dict[str, Any]],
    field: str,
    *,
    limit: int = 20,
    max_chars: int = 1200,
) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = _clean_text(row.get(field))[:max_chars]
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _topic_stage_payload(
    topic_id: str,
    rows: list[dict[str, Any]],
    *,
    use_standard_references: bool = True,
) -> dict[str, Any]:
    evidence_package = _topic_model_evidence_package(
        _topic_evidence_package(rows)
    )
    representative_facts = evidence_package.get(
        "representative_facts"
    ) or []
    return {
        "theme_id": topic_id,
        "member_count": len(rows),
        "source_sample_ids": _unique_topic_values(rows, "数据ID"),
        "source_unit_ids": _unique_topic_values(rows, "_原子知识ID"),
        "product_categories": _unique_topic_values(rows, "产品类型"),
        "scope_types": _unique_topic_values(rows, "_原子适用范围类型"),
        "normalized_issues": _unique_topic_values(rows, "核心问题", limit=8),
        "upstream_core_problems": _unique_topic_values(
            rows,
            "原始核心问题",
            limit=8,
        ),
        "upstream_judgment_conclusions": _unique_topic_values(
            rows,
            "原始判定结论",
            limit=8,
        ),
        "category_l1": _unique_topic_values(rows, "模型主题一级分类"),
        "category_l2": _unique_topic_values(rows, "模型主题二级分类"),
        "intents": _unique_topic_values(rows, "问题意图"),
        "subjects": _unique_topic_values(rows, "对象/部位"),
        "phenomena": _unique_topic_values(rows, "异常现象"),
        "judgment_targets": _unique_topic_values(rows, "核心问题"),
        "resolution_modes": _unique_topic_values(rows, "解题方式"),
        "standard_paths": (
            _unique_topic_values(rows, "主标准路径")
            if use_standard_references
            else []
        ),
        "thresholds_or_exceptions": _unique_topic_values(rows, "_原子阈值例外"),
        "evidence_summaries": _unique_topic_values(
            rows,
            "语义标注依据",
            limit=6,
            max_chars=800,
        ),
        "historical_replies": list(
            dict.fromkeys(
                reply
                for row in rows
                if (reply := _historical_actual_reply(row))
            )
        )[:6],
        "conversation_evidence": _unique_topic_values(
            rows,
            "聊天内容",
            limit=6,
            max_chars=1800,
        ),
        "evidence_package": evidence_package,
        "representative_source_ids": evidence_package.get(
            "representative_source_ids",
            [],
        ),
        "source_fact_refs": evidence_package.get(
            "source_fact_refs",
            [],
        ),
        "representative_human_core_problems": [
            fact.get("human_core_problem")
            for fact in representative_facts
            if fact.get("human_core_problem")
        ],
        "representative_human_judgments": [
            fact.get("human_judgment_conclusion")
            for fact in representative_facts
            if fact.get("human_judgment_conclusion")
        ],
        "upstream_requires_review": any(
            _clean_text(row.get("是否重点复核")) == "是"
            or _clean_text(row.get("_原子适用范围类型")) == "待确认"
            for row in rows
        ),
    }


def _topic_reusable_source_values(topic_payload: dict[str, Any]) -> list[Any]:
    return [
        *topic_payload.get("upstream_judgment_conclusions", []),
        *topic_payload.get("representative_human_judgments", []),
        *topic_payload.get("resolution_modes", []),
        *topic_payload.get("thresholds_or_exceptions", []),
        *topic_payload.get("evidence_summaries", []),
        *topic_payload.get("historical_replies", []),
        *topic_payload.get("conversation_evidence", []),
    ]


def _topic_has_explicit_reusable_rule(topic_payload: dict[str, Any]) -> bool:
    return has_explicit_reusable_knowledge(
        source_values=_topic_reusable_source_values(topic_payload),
        threshold_values=topic_payload.get("thresholds_or_exceptions", []),
    )


def _topic_has_draftable_source_rule(topic_payload: dict[str, Any]) -> bool:
    return has_draftable_source_rule(
        source_values=_topic_reusable_source_values(topic_payload),
        threshold_values=topic_payload.get("thresholds_or_exceptions", []),
    )


def _rule_topic_stage_classification(
    topic_payload: dict[str, Any],
) -> dict[str, Any]:
    text = "\n".join(
        _clean_text(value)
        for field in (
            "intents",
            "subjects",
            "phenomena",
            "resolution_modes",
            "evidence_summaries",
            "historical_replies",
            "conversation_evidence",
        )
        for value in topic_payload.get(field, [])
    )
    if any(marker in text for marker in ("型号", "版本", "配置", "是什么", "是否支持")):
        topic_stage = "课外常识"
    elif any(
        marker in text
        for marker in ("怎么查", "如何查", "怎么测", "步骤", "操作", "读取", "核对")
    ):
        topic_stage = "质检流程"
    elif int(topic_payload.get("member_count") or 0) == 1 and any(
        marker in text for marker in ("图片", "视频", "当前案例", "看图", "这台")
    ):
        topic_stage = "案例解析"
    else:
        topic_stage = "质检标准"

    reusable = _topic_has_explicit_reusable_rule(topic_payload)
    return {
        "topic_stage": topic_stage,
        "knowledge_value": "值得沉淀" if reusable else "不值得沉淀",
        "stage_reason": "根据主题主要诉求和现有证据进行规则分类。",
        "value_reason": (
            "主题来源包含可复用规则、步骤、阈值或适用边界。"
            if reusable
            else "当前证据缺少可复用规则、步骤、阈值或适用边界。"
        ),
        "reusable_knowledge": (
            "可根据现有证据提炼稳定的判定口径、操作步骤或基础知识。"
            if reusable
            else "当前缺少可复用的阈值、边界、处理规则或操作步骤。"
        ),
        "confidence": 0.55 if reusable else 0.45,
        "needs_human_review": True,
    }


def _apply_topic_stage_guard(
    topic_payload: dict[str, Any],
    classification: dict[str, Any],
    *,
    has_authoritative_standard: bool = False,
) -> dict[str, Any]:
    guarded = dict(classification)
    if (
        _clean_text(guarded.get("knowledge_value")) != "值得沉淀"
        or _topic_has_explicit_reusable_rule(topic_payload)
        or has_authoritative_standard
    ):
        return guarded
    guarded.update(
        {
            "knowledge_value": "不值得沉淀",
            "value_reason": _safe_join(
                [
                    guarded.get("value_reason"),
                    "当前主题来源缺少明确阈值、通用规则、适用边界或可执行步骤，禁止仅凭案例数量外推为可复用知识。",
                ],
                "；",
            ),
            "reusable_knowledge": "当前来源证据缺少可复用的阈值、边界、通用处理规则或操作步骤。",
            "needs_human_review": True,
        }
    )
    return guarded


def _should_incubate_single_case_topic(
    topic_payload: dict[str, Any],
    classification: dict[str, Any],
    *,
    use_standard_references: bool,
) -> bool:
    return (
        not use_standard_references
        and int(topic_payload.get("member_count") or 0) == 1
        and _clean_text(classification.get("knowledge_value"))
        in {"不值得沉淀", "待确认"}
        and not _topic_has_explicit_reusable_rule(topic_payload)
    )


def _single_case_pending_cluster_reason(
    classification: dict[str, Any],
) -> str:
    return _safe_join(
        [
            "只有1条案例，缺少可复用规则、阈值、边界或可执行步骤，先保留为待聚合素材",
            "后续命中相似主题后再进入沉淀判断",
            _clean_text(classification.get("value_reason")),
        ],
        "；",
    )


def _pending_cluster_source_rows(
    topic_id: str,
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    reason: str,
    *,
    status: str = "incubating_pending_cluster",
    admission: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    pending_rows: list[dict[str, Any]] = []
    cluster_key = " | ".join(key)
    for row in rows:
        pending_row = dict(row)
        pending_row.update(
            {
                "主题ID": topic_id,
                "主题聚类键": cluster_key,
                "待聚合状态": status,
                "待聚合原因": reason,
                "聚类准入状态": _clean_text(
                    (admission or {}).get("status")
                ),
                "聚类准入置信度": (admission or {}).get(
                    "confidence",
                    "",
                ),
                "聚类准入原因": _clean_text(
                    (admission or {}).get("reason")
                ),
                "人工聚类判断": "",
                "人工备注": "",
            }
        )
        pending_rows.append(pending_row)
    return pending_rows


def _attach_topic_stage_classification(
    topic: dict[str, Any],
    classification: dict[str, Any],
    *,
    provider: str,
    model_name: str,
    prompt_version: str,
    model_run_id: str,
    status: str,
    error: str,
    transcription_status: str,
) -> None:
    topic.update(
        {
            "主题问题分类": _clean_text(classification.get("topic_stage")),
            "主题沉淀价值": _clean_text(classification.get("knowledge_value")),
            "主题分类原因": _clean_text(classification.get("stage_reason")),
            "主题价值原因": _clean_text(classification.get("value_reason")),
            "主题可复用知识摘要": _clean_text(
                classification.get("reusable_knowledge")
            ),
            "主题分类置信度": classification.get("confidence", ""),
            "主题分类重点复核": (
                "是" if classification.get("needs_human_review") else "否"
            ),
            "主题分类提供方": provider,
            "主题分类模型名称": model_name,
            "主题分类Prompt版本": prompt_version,
            "主题分类运行ID": model_run_id,
            "主题分类状态": status,
            "主题分类错误": error,
            "主题转写状态": transcription_status,
        }
    )


def _untranscribed_topic_candidate_row(
    topic_id: str,
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    classification: dict[str, Any],
) -> dict[str, Any]:
    evidence_package = _topic_evidence_package(rows)
    query = _topic_query(rows, evidence_package)
    source_ids = list(
        dict.fromkeys(
            _clean_text(fact.get("source_record_id"))
            for fact in evidence_package.get("facts") or []
            if _clean_text(fact.get("source_record_id"))
        )
    )
    work_order_ids = list(
        dict.fromkeys(
            _original_work_order_id_for_row(row)
            for row in rows
            if _original_work_order_id_for_row(row)
        )
    )
    title = _untranscribed_topic_title(query, rows)
    keywords = _merge_unique_keywords(
        [
            query.get("问题意图"),
            query.get("对象/部位"),
            query.get("异常现象"),
        ]
    )
    image_links = _topic_image_links(rows, evidence_package)
    image_source_trace = _topic_image_source_trace(
        rows,
        evidence_package,
    )
    video_links = _topic_video_links(rows, evidence_package)
    video_source_trace = _topic_video_source_trace(
        rows,
        evidence_package,
    )
    requires_images = _topic_needs_images(rows)
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
    if _clean_text(classification.get("knowledge_value")) == "待确认":
        note = (
            "该主题未形成包含具体事实、条件和处理结论的知识正文，"
            "已阻止通用模板作为知识草稿进入初标。请在候选价值复核中确认主题价值；"
            "如改判为值得沉淀，需要补充完整、可追溯的知识内容后再送审。"
        )
    else:
        note = (
            "该主题在知识转写前被标注为不值得沉淀，因此未生成知识草稿。"
            "请在候选价值复核中确认主题价值；如改判为值得沉淀，需要补充完整知识内容后再送审。"
        )
    return {
        "主题ID": topic_id,
        "知识ID": topic_id,
        "主题状态": "value_review_pending",
        "主题样本数": len(rows),
        "回收业务层级": _business_line_for_row(rows[0]),
        "主题来源记录ID": "\n".join(source_ids),
        "主题代表性记录ID": "\n".join(
            evidence_package.get("representative_source_ids") or []
        ),
        "主题工单ID": "\n".join(work_order_ids),
        "主题聚类键": " | ".join(key),
        "主题问题意图": _clean_text(query.get("问题意图")),
        "主题对象/部位": _clean_text(query.get("对象/部位")),
        "主题异常现象": _clean_text(query.get("异常现象")),
        "主题解题方式": _clean_text(query.get("解题方式")),
        "主题证据等级": "、".join(
            dict.fromkeys(_topic_evidence(row)[0] for row in rows)
        ),
        "主题证据摘要": _topic_evidence_summary(
            rows,
            evidence_package,
        ),
        "主题事实引用": _topic_fact_references(evidence_package),
        "主题事实证据包": _topic_evidence_package_json(
            evidence_package
        ),
        "主题无来源内容": "",
        "主题图例来源": image_source_trace,
        "主题视频来源": video_source_trace,
        "主题图片链接": "\n".join(image_links),
        "主题视频链接": "\n".join(video_links),
        "主题图片必要性": (
            "需要保留"
            if requires_images
            else "辅助图例"
            if image_links
            else "无案例图"
        ),
        "主题图片说明": (
            "保留脱敏案例图，供候选价值复核确认主题和沉淀价值。"
            if requires_images
            else "保留来源案例图作为辅助图例，不作为结论的唯一依据。"
            if image_links
            else "主题未进入知识转写，且来源没有案例图。"
        ),
        "主题标准版本": (
            preserved_topic_standard_versions or preserved_source_versions
        ),
        "主题置信度": classification.get("confidence", ""),
        "是否重点复核": "是",
        "主题模型提供方": "未执行",
        "主题模型名称": "",
        "主题Prompt版本": "",
        "主题模型运行ID": "",
        "人工主题问题分类": "",
        "模型初标结论": "未执行",
        "模型初标是否值得沉淀": _clean_text(
            classification.get("knowledge_value")
        ),
        "模型初标错误类型": "",
        "模型初标原因": note,
        "模型初标标准一致性": "",
        "模型初标证据充分性": "",
        "模型初标内容一致性": "",
        "模型初标图片必要性": "",
        "模型初标标题质量": "",
        "模型初标置信度": "",
        "模型初标重点复核": "是",
        "模型初标提供方": "未执行",
        "模型初标模型名称": "",
        "模型初标Prompt版本": "",
        "模型初标运行ID": "",
        "模型初标状态": "topic_initial_review_skipped",
        "主标题": title,
        "副标题": "",
        "知识内容": note,
        "图例": "\n".join(image_links),
        "推荐回复": "",
        "知识分类": knowledge_category_from_topic_stage(
            classification.get("topic_stage"),
        ),
        "知识来源": "方向二主题价值候选",
        "关联标准项": preserved_standard_refs,
        "适用范围": _topic_product_type(query, rows),
        "适用品牌": _merge_unique_text(
            [row.get("适用品牌") for row in rows],
            separator="\n",
        ),
        "适用机型": _merge_unique_text(
            [row.get("适用机型") for row in rows],
            separator="\n",
        ),
        "生效状态": "待审核",
        "来源版本": preserved_source_versions,
        "变更类型": "新增",
        "失效原因": "",
        "检索关键词": keywords,
        "关键词": keywords,
        "校验备注": note,
        "是否值得沉淀": "",
        "是否可用": "",
        "如何修改": "",
        "问题反馈": "",
    }


def _failed_topic_source_knowledge_content(
    query: dict[str, Any],
) -> str:
    """Build compact, source-backed review content after a draft is blocked.

    This is intentionally not a transcript of the case analysis.  It keeps the
    current topic's object, observable result, explicit selectable option and
    review boundary, while removing labels such as “关键事实” and staff chatter.
    """
    product = _clean_text(query.get("产品类型"))
    subject = _clean_text(query.get("对象/部位"))
    phenomenon = _clean_text(query.get("异常现象"))
    method = _clean_text(query.get("解题方式"))
    source_text = _safe_join(
        [
            _clean_text(query.get("核心问题")),
            _clean_text(query.get("人工判定结论")),
            _clean_text(query.get("判定依据")),
            _clean_text(query.get("历史实际回复")),
            _clean_text(query.get("聊天内容")),
            _clean_text(query.get("参考话术")),
            _clean_text(query.get("对象/部位")),
            _clean_text(query.get("异常现象")),
            _clean_text(query.get("解题方式")),
        ],
        "；",
    )
    has_tool_result = any(
        marker in source_text
        for marker in ("一根线", "验机工具", "验机侠", "工具读出", "工具结果")
    )
    options = _extract_handling_options_from_text(source_text)
    is_tool_user_judgment = (
        "用户判断" in source_text
        and has_tool_result
    )
    if is_tool_user_judgment:
        condition = " ".join(
            part for part in (product, subject, phenomenon) if part
        )
        observed_subject = subject or "对应部位"
        points = [
            (
                f"{condition}时，不能仅凭该提示直接勾选"
                "“工具读出异常”。"
            )
            if condition
            else "工具显示“用户判断”时，不能仅凭该提示直接勾选“工具读出异常”。",
            f"需要现场核验{observed_subject}是否存在明确拆修现象。",
            (
                "未发现对应部位的拆修现象时，不处理该提示，"
                "不勾选“工具读出异常”。"
            ),
            (
                "发现对应部位存在明确拆修现象时，"
                "按对应部位的工具读出异常项处理。"
            ),
        ]
        return "\n".join(
            f"{index}. {point}"
            for index, point in enumerate(
                list(dict.fromkeys(points)),
                start=1,
            )
        )
    points: list[str] = []

    condition_parts = [part for part in (product, subject, phenomenon) if part]
    if condition_parts:
        condition = " ".join(condition_parts)
        if has_tool_result:
            points.append(f"{condition}时，先核对验机工具报告中的对应结果。")
        elif method:
            points.append(f"{condition}时，按来源记录的核验方式处理。")

    if options:
        points.append(f"工具或来源结论明确时，{options[0]}。")
    elif method and not (
        is_tool_user_judgment
        and any(
            marker in method
            for marker in (
                "健康度",
                "最大容量",
                "支持APP",
                "支持 App",
                "无法检测",
            )
        )
    ):
        clean_method = re.sub(
            r"^(?:请|需|需要|应当|应|按)",
            "",
            method,
        ).strip("：:。；; ")
        if clean_method:
            points.append(f"核验时，{clean_method}。")
    elif source_text:
        clean_source = re.sub(
            r"(?:关键事实|匹配口径|定义|老师|您好|回收师)"
            r"\s*[：:，,]?\s*",
            "",
            source_text,
        )
        clean_source = re.sub(r"\s+", " ", clean_source).strip("；;。 ")
        if clean_source:
            points.append(f"来源记录的处理方式：{clean_source[:180]}。")

    points.append(
        "工具报告缺失、结果不明确或对象不一致时，补充对应报告后转人工审核。"
    )
    unique_points = list(dict.fromkeys(point for point in points if point))
    return "\n".join(
        f"{index}. {point}" for index, point in enumerate(unique_points[:4], start=1)
    )


def _strip_unverified_standard_language(value: Any) -> str:
    """Keep no-standard drafts from claiming a rule that was not retrieved."""
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(
        r"(?:依据|按照|按|根据)(?:平台|质检|总部|本地)?标准"
        r"(?:判定为|判定成|判断为|选择|勾选)"
        r"([^。；;\n]{1,48})",
        r"来源未提供可直接套用的标准，需由人工核验\1",
        text,
    )
    text = re.sub(
        r"(?:应当|应|需要|可直接)\s*(?:勾选|选择)"
        r"([^。；;\n]{1,48})(?:相应等级|对应等级)?",
        r"需补充证据后由人工确认处理项（\1）",
        text,
    )
    text = re.sub(
        r"(?:并)?勾选(?:相应|对应)(?:的)?(?:等级|选项)",
        "并由人工确认对应处理项",
        text,
    )
    text = re.sub(
        r"(?:依据|按照|按|根据)(?:平台|质检|总部|本地)?标准",
        "按来源事实核验",
        text,
    )
    return re.sub(r"\s{2,}", " ", text).strip()


def _standard_mapping_quality_failure(topic: dict[str, Any]) -> str:
    """Return a blocking reason when review says the retrieved standard is wrong."""
    error_type = _clean_text(topic.get("模型初标错误类型"))
    consistency = _clean_text(topic.get("模型初标标准一致性"))
    reason = _clean_text(topic.get("模型初标原因"))
    if "标准项映射错" in error_type or "标准项映射错" in reason:
        return "模型初标明确指出标准项映射错误，已撤销标准引用。"
    if consistency == "不一致" and any(
        marker in reason
        for marker in ("标准", "来源事实", "主题核心问题", "内容与")
    ):
        return "模型初标判定标准与来源事实不一致，已撤销标准引用。"
    return ""


def _source_rule_content_from_topic_evidence(topic: dict[str, Any]) -> str:
    """Recover explicit source rules after a retrieved standard is rejected.

    A rejected standard must never keep its paths or selectable options.  The
    representative source facts are still auditable, however, and can provide
    a reviewable body when an actual historical reply or human conclusion
    records a complete operational rule.
    """
    raw_package = _clean_text(topic.get("主题事实证据包"))
    if not raw_package:
        return ""
    try:
        package = json.loads(raw_package)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(package, dict):
        return ""
    facts = package.get("representative_facts") or package.get("facts") or []
    if not isinstance(facts, list):
        return ""

    def clean_source_rule_text(value: Any) -> str:
        text = _strip_unverified_standard_language(value)
        text = re.sub(r"^\s*(?:\d+[.、]\s*)+", "", text)
        return re.sub(r"\s+", " ", text).strip("。；; ")

    measurement_boundary_content = _source_measurement_boundary_content(
        topic,
        facts,
    )
    if measurement_boundary_content:
        return measurement_boundary_content

    points: list[str] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        historical_reply = _clean_text(fact.get("historical_actual_reply"))
        numbered_about_topics = re.findall(
            r"(?:^|\n)\s*\d+[.、]\s*关于",
            historical_reply,
        )
        if len(numbered_about_topics) >= 2:
            # Numbered “关于…” sections in one reply indicate separate
            # atomic questions; recovering the full reply would cross topics.
            continue
        historical_reply = re.sub(
            r"^(?:回收师|老师|您好|你好)[，,：:\s]*",
            "",
            historical_reply,
        ).strip()
        # Remove a full hierarchy copied from a rejected standard, but keep a
        # terminal option explicitly stated in the source reply, such as
        # “【电池健康度无法检测】”.
        historical_reply = re.sub(
            r"(?:【[^】]+】\s*[-—>＞]\s*)+【[^】]+】",
            "",
            historical_reply,
        )
        historical_reply = clean_source_rule_text(historical_reply)
        source_topic_labels = re.findall(
            r"(?:^|[。；;])\s*([^。；;：:]{2,20}问题)[：:]",
            historical_reply,
        )
        if len(set(source_topic_labels)) >= 2:
            # One historical reply can answer several questions from the same
            # conversation.  It cannot safely become a single-topic body.
            continue
        if historical_reply and any(
            marker in historical_reply
            for marker in (
                "先",
                "再",
                "查看",
                "检查",
                "进入",
                "读取",
                "填写",
                "确认",
                "使用",
                "按压",
                "选择",
            )
        ):
            points.append(historical_reply)

        conclusion = clean_source_rule_text(
            fact.get("human_judgment_conclusion")
        )
        if conclusion and "证据不足" not in conclusion:
            points.append(conclusion)

        boundary = clean_source_rule_text(
            fact.get("source_supported_threshold_or_exception")
        )
        if boundary:
            points.append(boundary)

    unique_points = list(dict.fromkeys(point for point in points if point))
    if not unique_points:
        return ""
    unique_points.append(
        "仅适用于来源已明确记录的对象、条件和处理步骤；无法核实对应事实时，补充证据后转人工审核。"
    )
    return "\n".join(
        f"{index}. {point}"
        for index, point in enumerate(unique_points[:4], start=1)
    )


def _source_measurement_boundary_content(
    topic: dict[str, Any],
    facts: list[Any],
) -> str:
    """Build a complete source-backed rule for a measurable boundary.

    This path is used only after a retrieved standard has been rejected.  It
    preserves an explicit source-supported threshold without inventing a CZ
    selectable option or treating the rejected standard as an authority.
    """
    subject = _clean_text(topic.get("主题对象/部位"))
    phenomenon = _clean_text(topic.get("主题异常现象"))
    method = _clean_text(topic.get("主题解题方式"))
    if not subject or not phenomenon:
        return ""

    for fact in facts:
        if not isinstance(fact, dict):
            continue
        boundary = _clean_text(
            fact.get("source_supported_threshold_or_exception")
        )
        source_text = " ".join(
            _clean_text(fact.get(field))
            for field in (
                "atomic_question",
                "human_judgment_conclusion",
                "historical_actual_reply",
                "judgment_basis",
            )
        )
        has_positive_abnormal_boundary = bool(
            re.search(r"(?:>|＞|大于|超过)", boundary)
        )
        has_source_backed_inverse_conclusion = bool(
            re.search(r"(?:未达到|不满足).{0,20}(?:不判|正常|不异常)", source_text)
        )
        is_measurement_boundary = bool(
            has_positive_abnormal_boundary
            and has_source_backed_inverse_conclusion
            and any(
                marker in " ".join([method, source_text, boundary])
                for marker in ("测量", "尺寸", "宽度", "直径", "长度", "mm")
            )
        )
        if not is_measurement_boundary:
            continue

        measurement_target = (
            f"{phenomenon}宽度"
            if phenomenon in {"缝隙", "间隙"}
            else f"{phenomenon}的尺寸"
        )
        return "\n".join(
            [
                f"1. 先确认{phenomenon}位于{subject}。",
                (
                    f"2. 对{measurement_target}进行可核验的测量，"
                    "并保留清晰图片和测量证据。"
                ),
                f"3. 仅当{boundary}时，才按异常处理。",
                (
                    f"4. 不满足上述条件时，不应仅因存在{phenomenon}"
                    "直接判异常。"
                ),
                (
                    f"5. 无法准确确认{phenomenon}位置或尺寸时，"
                    "补充清晰图片和测量证据后再判定。"
                ),
            ]
        )
    return ""


def _revoked_standard_source_title(topic: dict[str, Any]) -> str:
    """Rebuild a title from source fields when a rejected standard polluted it."""
    title = _clean_text(topic.get("主标题"))
    subject = _clean_text(topic.get("主题对象/部位"))
    phenomenon = _clean_text(topic.get("主题异常现象"))
    product_type = _clean_text(topic.get("适用范围"))
    if not subject or not phenomenon:
        return ""
    source_anchors = (subject, phenomenon)
    if any(anchor in title for anchor in source_anchors if len(anchor) >= 2):
        return ""
    return _as_natural_question_title(f"{product_type}{subject}{phenomenon}")


def _standard_reference_revoked_content(topic: dict[str, Any]) -> str:
    """Build a conservative, source-only body after a standard is revoked."""
    def source_field(field: str) -> str:
        value = _clean_text(topic.get(field))
        value = re.sub(r"【[^】]+】", "", value)
        value = re.sub(r"\s+", " ", value).strip("：:；;，, ")
        return _strip_unverified_standard_language(value)

    subject = source_field("主题对象/部位")
    phenomenon = source_field("主题异常现象")
    method = source_field("主题解题方式")
    user_judgment_query = {
        "产品类型": _clean_text(topic.get("适用范围")),
        "对象/部位": subject,
        "异常现象": phenomenon,
        "解题方式": method,
        "核心问题": _safe_join(
            [
                _clean_text(topic.get("核心问题")),
                _clean_text(topic.get("主标题")),
            ],
            "；",
        ),
        "人工判定结论": _clean_text(topic.get("人工判定结论")),
        "判定依据": _clean_text(topic.get("判定依据")),
        "历史实际回复": _clean_text(topic.get("历史实际回复")),
        "聊天内容": _clean_text(topic.get("聊天内容")),
    }
    user_judgment_content = _failed_topic_source_knowledge_content(
        user_judgment_query
    )
    if (
        "用户判断"
        in " ".join(
            _clean_text(user_judgment_query.get(field))
            for field in (
                "核心问题",
                "异常现象",
                "聊天内容",
                "历史实际回复",
            )
        )
        and "不能仅凭该提示直接勾选" in user_judgment_content
    ):
        return user_judgment_content
    source_rule_content = _source_rule_content_from_topic_evidence(topic)
    if source_rule_content:
        return source_rule_content
    points = [
        "当前标准引用已撤销，不能按原标准路径或候选项直接处理。",
    ]
    condition = _safe_join([subject, phenomenon], " / ")
    if condition:
        points.append(f"请先根据来源事实核对{condition}是否一致。")
    if method:
        points.append(f"核验时，{method}。")
    points.append("来源证据不足、对象不一致或无法复现时，补充证据后转人工审核。")
    return "\n".join(
        f"{index}. {point}"
        for index, point in enumerate(
            list(dict.fromkeys(point for point in points if point)),
            start=1,
        )
    )


def _enforce_standard_reference_consistency(
    topic: dict[str, Any],
    *,
    use_standard_references: bool,
) -> dict[str, Any]:
    """Apply the final standard-reference consistency gate before export."""
    if not use_standard_references:
        return topic
    updated = dict(topic)
    association = _clean_text(updated.get("关联标准项"))
    label = _clean_text(updated.get("标准引用标签"))
    gate = _clean_text(updated.get("标准引用门禁状态"))
    blocking_reason = _standard_mapping_quality_failure(updated)
    if label == "已引用标准知识点" and not association:
        blocking_reason = _safe_join(
            [
                blocking_reason,
                "标准引用标签为已引用，但关联标准项为空，已改为人工重点复核。",
            ],
            "；",
        )
    if blocking_reason:
        retrieved_before_rejection = bool(association)
        updated["标准引用标签"] = "未引用标准-人工重点复核"
        updated["标准引用门禁状态"] = (
            "retrieved_mapping_rejected"
            if retrieved_before_rejection
            else "rejected_or_missing"
        )
        updated["关联标准项"] = ""
        updated["候选项/处理项"] = ""
        updated["主题标准版本"] = ""
        updated["来源版本"] = ""
        updated["知识来源"] = "方向二经验补充候选"
        rebuilt_title = _revoked_standard_source_title(updated)
        if rebuilt_title:
            updated["主标题"] = rebuilt_title
        revoked_content = _standard_reference_revoked_content(updated)
        updated["知识内容"] = revoked_content
        updated["副标题"] = _finalize_topic_subtitles(
            [],
            _clean_text(updated.get("主标题")),
            revoked_content,
            _clean_text(updated.get("正文类型")) or CONTENT_TYPE_VERIFICATION,
        )
        source_handling_options = _extract_handling_options_from_text(
            revoked_content
        )
        if source_handling_options:
            updated["候选项/处理项"] = "\n".join(source_handling_options)
        updated["推荐回复"] = (
            _recommended_reply_from_final_content(revoked_content)
            if "当前标准引用已撤销" not in revoked_content
            else ""
        )
        updated["校验备注"] = _safe_join(
            [_clean_text(updated.get("校验备注")), blocking_reason],
            "；",
        )
    elif label == "已引用标准知识点" and association:
        updated["标准引用门禁状态"] = gate or "accepted"
    if not _clean_text(updated.get("关联标准项")):
        content_before_cleanup = _clean_text(updated.get("知识内容"))
        reply_before_cleanup = _clean_text(updated.get("推荐回复"))
        updated["知识内容"] = _compact_knowledge_content(
            _strip_unverified_standard_language(content_before_cleanup)
        )
        if reply_before_cleanup:
            updated["推荐回复"] = _compact_recommended_reply(
                _strip_unverified_standard_language(reply_before_cleanup)
            )
        if (
            updated["知识内容"] != content_before_cleanup
            or _clean_text(updated.get("推荐回复")) != reply_before_cleanup
        ):
            updated["校验备注"] = _safe_join(
                [
                    _clean_text(updated.get("校验备注")),
                    "未引用标准，已清理正文和推荐回复中的标准声明与重复编号。",
                ],
                "；",
            )
    return updated


def _retarget_battery_user_judgment_query(
    query: dict[str, Any],
) -> dict[str, Any]:
    """Correct an upstream health-field mislabel for battery tool results."""
    product_type = canonical_product_name(
        _clean_text(query.get("产品类型")),
        unknown=_clean_text(query.get("产品类型")),
    )
    if product_type != "平板电脑":
        return query
    source_text = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "人工判定结论",
            "判定依据",
            "历史实际回复",
            "异常现象",
            "解题方式",
            "聊天内容",
        )
    )
    if not (
        "电池" in source_text
        and "用户判断" in source_text
        and (
            any(
                marker in source_text
                for marker in (
                    "一根线",
                    "验机工具",
                    "验机侠",
                    "工具读出",
                    "序列号",
                )
            )
            or (
                _clean_text(query.get("产品类型")) == "平板电脑"
                and "质检选项" in source_text
            )
        )
    ):
        return query
    corrected = dict(query)
    corrected.update(
        {
            "一级分类": "拆修及浸液情况",
            "二级分类": "电池拆修",
            "问题意图": "标准判定",
            "对象/部位": "电池",
            "异常现象": "验机工具读出用户判断",
            "核心问题": "平板电池验机工具显示用户判断时如何处理",
            "解题方式": "核对工具报告和对应电池拆修结论",
        }
    )
    return corrected


def _is_single_battery_health_observation_topic(
    query: dict[str, Any],
) -> bool:
    """A one-off percentage selects a case option but is not reusable knowledge."""
    if _clean_text(query.get("产品类型")) != "平板电脑":
        return False
    text = " ".join(
        _clean_text(query.get(field))
        for field in (
            "核心问题",
            "人工核心问题",
            "人工判定结论",
            "判定依据",
            "历史实际回复",
            "异常现象",
            "解题方式",
        )
    )
    if "电池健康度" not in text or not re.search(
        r"\d+(?:\.\d+)?\s*(?:%|％)",
        text,
    ):
        return False
    return not any(
        marker in text
        for marker in (
            "无法读取",
            "读不出",
            "读取不到",
            "无法获取",
            "无法检测",
            "支持APP",
            "支持 App",
            "优先级",
            "分级",
            "范围",
        )
    )


def _failed_topic_transcription_row(
    topic_id: str,
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    classification: dict[str, Any],
    *,
    provider: str,
    model_name: str,
    prompt_version: str,
    model_run_id: str,
    transcription_status: str,
    model_call_status: str,
    error: str,
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool,
) -> dict[str, Any]:
    topic = _untranscribed_topic_candidate_row(
        topic_id,
        key,
        rows,
        classification,
    )
    failed_query = _retarget_battery_user_judgment_query(_topic_query(rows))
    # 模型输出校验失败时仍保留可追溯的来源事实，供人工复核；正文必须
    # 是可阅读的业务知识，而不是“关键事实/匹配口径/老师回复”的案例分析。
    evidence_content = _failed_topic_source_knowledge_content(failed_query)
    if not evidence_content:
        evidence_content = (
            "当前模型未形成合格知识草稿；请根据主题来源事实、人工判定结论和"
            "判定依据补充可复用规则后再审核。"
        )
    failed_title = _rebuild_title_from_structured_fields(failed_query)
    if not failed_title:
        product = _topic_product_type(failed_query, rows)
        subject = (
            _clean_text(failed_query.get("二级分类"))
            or _clean_text(failed_query.get("一级分类"))
            or "当前问题"
        )
        failed_title = f"{product}{subject}应如何核验？"
    topic["主标题"] = failed_title
    reason = _safe_join(
        [
            "主题模型未形成通过业务校验的知识草稿，已阻止规则模板继续生成正文",
            error,
        ],
        "；",
    )
    topic.update(
        {
            "主题状态": "transcription_review_pending",
            "主题模型提供方": provider,
            "主题模型名称": model_name,
            "主题Prompt版本": prompt_version,
            "主题模型运行ID": model_run_id,
            "模型调用状态": model_call_status,
            "模型输出校验状态": "failed",
            "模型质量状态": "failed",
            "知识草稿状态": "blocked",
            "标准引用门禁状态": (
                "accepted" if matches else "rejected_or_missing"
            ),
            "标准引用标签": (
                "已引用标准知识点"
                if matches
                else "未引用标准-人工重点复核"
                if use_standard_references
                else ""
            ),
            "图片证据门禁状态": _topic_image_measurement_gate(rows)["status"],
            "模型初标结论": "未执行",
            "模型初标是否值得沉淀": _clean_text(
                classification.get("knowledge_value")
            ),
            "模型初标错误类型": "",
            "模型初标原因": reason,
            "模型初标标准一致性": "",
            "模型初标证据充分性": "",
            "模型初标内容一致性": "",
            "模型初标图片必要性": "",
            "模型初标标题质量": "",
            "模型初标置信度": "",
            "模型初标重点复核": "是",
            "模型初标提供方": "未执行",
            "模型初标模型名称": "",
            "模型初标Prompt版本": "",
            "模型初标运行ID": "",
            "模型初标状态": "topic_initial_review_skipped_transcription_failed",
            "知识内容": evidence_content,
            # 模型转写失败仍会进入候选价值复核；不能因为草稿被拦截就丢失
            # 推荐回复。这里仅从来源事实/历史实际回复生成保守话术，供人工修改。
            "推荐回复": _recommended_reply(
                failed_title,
                evidence_content,
                existing_reply=_clean_text(
                    failed_query.get("历史实际回复")
                ),
                use_standard_references=use_standard_references,
            ),
            "校验备注": reason,
            "流程状态": "review_pending",
            "模型阶段状态": transcription_status,
        }
    )
    return topic


def _topic_source_mapping_rows(
    topic_id: str,
    rows: list[dict[str, Any]],
    topic: dict[str, Any],
    model_run_id: str,
) -> list[dict[str, Any]]:
    mapping_rows: list[dict[str, Any]] = []
    evidence_package = _topic_evidence_package(rows)
    facts = evidence_package.get("facts") or []
    representative_by_id = {
        fact.get("fact_id"): fact
        for fact in evidence_package.get("representative_facts") or []
    }
    for index, row in enumerate(rows):
        evidence_level, _eligible, reason = _topic_evidence(row)
        fact = facts[index] if index < len(facts) else {}
        representative_fact = representative_by_id.get(
            fact.get("fact_id")
        )
        mapping_rows.append(
            {
                "主题ID": topic_id,
                "事实ID": _clean_text(fact.get("fact_id")),
                "事实引用": (
                    f"[{fact.get('fact_id')}] "
                    f"来源记录={fact.get('source_record_id')}"
                ),
                "是否代表性证据": (
                    "是" if representative_fact else "否"
                ),
                "代表性选择原因": _clean_text(
                    (representative_fact or {}).get(
                        "selection_reason"
                    )
                ),
                "来源记录ID": _clean_text(fact.get("source_record_id")),
                "工单ID": _clean_text(row.get("工单ID")),
                "原始工单ID": _original_work_order_id_for_row(row),
                "核心问题": _clean_text(row.get("核心问题")),
                "人工核心问题": _clean_text(
                    row.get("原始核心问题") or row.get("核心问题")
                ),
                "人工判定结论": _clean_text(
                    row.get("原始判定结论") or row.get("判定结论")
                ),
                "聊天内容": _clean_text(row.get("聊天内容")),
                "历史实际回复": _historical_actual_reply(row),
                "图片链接": _clean_text(row.get("图片链接")),
                "案例图引用": "\n".join(
                    f"[{fact.get('fact_id')}] {url}"
                    for url in fact.get("image_urls") or []
                ),
                "视频链接": _clean_text(row.get("视频链接")),
                "图片处理状态": _clean_text(row.get("图片处理状态")),
                "视频处理状态": _clean_text(row.get("视频处理状态")),
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
                "历史主题处理结果": _clean_text(
                    topic.get("历史主题处理结果")
                ),
                "历史主题匹配ID": _clean_text(
                    topic.get("历史主题匹配ID")
                ),
                "历史主题匹配置信度": topic.get(
                    "历史主题匹配置信度",
                    "",
                ),
                "历史主题匹配原因": _clean_text(
                    topic.get("历史主题匹配原因")
                ),
                "主题证据版本": topic.get("主题证据版本", ""),
                "聚类准入状态": _clean_text(topic.get("聚类准入状态")),
                "聚类准入置信度": topic.get("聚类准入置信度", ""),
                "聚类准入原因": _clean_text(topic.get("聚类准入原因")),
                "问题意图": _clean_text(row.get("问题意图")),
                "对象/部位": _clean_text(row.get("对象/部位")),
                "异常现象": _clean_text(row.get("异常现象")),
                "解题方式": _clean_text(row.get("解题方式")),
                "关联标准项": _clean_text(topic.get("关联标准项")),
                "模型运行ID": model_run_id,
            }
        )
    return mapping_rows


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
    standard_basis_source: str = "",
) -> dict[str, Any]:
    evidence_package = _topic_evidence_package(rows)
    query = _retarget_battery_user_judgment_query(
        _topic_query(rows, evidence_package)
    )
    content_rebuild_note = ""
    content_type_note = ""
    separated_content, embedded_reply = _split_embedded_recommended_reply(
        candidate.get("content"),
        candidate.get("recommended_reply"),
    )
    if separated_content != _clean_text(candidate.get("content")) or embedded_reply:
        candidate = dict(candidate)
        candidate["content"] = separated_content
        if embedded_reply:
            candidate["recommended_reply"] = embedded_reply
        content_rebuild_note = "已将正文中的【推荐回复】内容移入推荐回复列。"
    if _topic_requires_process(
        rows,
        matches,
        candidate,
        use_standard_references=use_standard_references,
    ) and not (
        not use_standard_references
        and _topic_draft_has_source_specific_content(candidate, rows)
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
    if (
        _topic_content_uses_internal_analysis_labels(candidate.get("content"))
        and not (use_standard_references and matches)
    ):
        candidate = dict(candidate)
        candidate["content"] = _failed_topic_source_knowledge_content(query)
        candidate["recommended_reply"] = ""
        candidate["needs_human_review"] = True
        content_rebuild_note = (
            "知识正文包含内部分析标签，已按对象、工具结果、明确选项和人工边界重建。"
        )
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
    local_standard_basis = standard_basis_source == "local_quality_standard"
    model_standard_refs = (
        _format_model_refs(candidate.get("standard_refs", []), matches)
        if use_standard_references
        else ""
    )
    if use_standard_references and matches and not model_standard_refs:
        model_standard_refs = _format_model_refs(
            [_standard_reference(matches[0][0])],
            matches,
        )
    standard_refs = (
        _merge_unique_text(
            [preserved_standard_refs, model_standard_refs],
            separator="；",
        )
        if use_standard_references
        else preserved_standard_refs
    )
    standard = matches[0][0] if use_standard_references and matches else None
    has_authoritative_standard = bool(standard)
    matched_existing = bool(standard and standard.knowledge_type == "已有知识")
    source_ids = list(
        dict.fromkeys(
            _clean_text(fact.get("source_record_id"))
            for fact in evidence_package.get("facts") or []
            if _clean_text(fact.get("source_record_id"))
        )
    )
    work_order_ids = list(
        dict.fromkeys(
            _original_work_order_id_for_row(row)
            for row in rows
            if _original_work_order_id_for_row(row)
        )
    )
    image_links = _topic_image_links(rows, evidence_package)
    image_source_trace = _topic_image_source_trace(
        rows,
        evidence_package,
    )
    video_links = _topic_video_links(rows, evidence_package)
    video_source_trace = _topic_video_source_trace(
        rows,
        evidence_package,
    )
    requires_images = _topic_needs_images(rows, candidate)
    image_measurement_gate = _topic_image_measurement_gate(rows, candidate)
    image_note = (
        _clean_text(candidate.get("image_usage_instruction"))
        or (
            (
                "保留现场图片，作为部位、现象和标准边界的辅助说明。"
                if use_standard_references
                else "保留脱敏案例图，作为问题部位、现象和处理情形的辅助说明。"
            )
            if requires_images
            else ""
        )
    )
    if not requires_images and image_links:
        image_note = (
            "保留代表性来源案例图作为辅助图例；"
            "图片不作为知识结论的唯一依据。"
        )
    elif not requires_images and not image_links:
        image_note = "来源没有可用案例图，文字证据用于表达处理方法。"
    levels = list(dict.fromkeys(_topic_evidence(row)[0] for row in rows))
    confidence = float(candidate.get("confidence", 0.0))
    needs_review = (
        candidate.get("needs_human_review", False)
        or any(bool(row.get("_原子需要复核")) for row in rows)
        or any(bool(row.get("_聚类需要复核")) for row in rows)
        or (use_standard_references and not matches)
        or local_standard_basis
        or (use_standard_references and not candidate.get("standard_refs"))
        or confidence < min_confidence
        or candidate.get("knowledge_form") != "具体判定"
    )
    product_type = _topic_product_type(query, rows)
    content = _compact_knowledge_content(
        standard.response_snippet if matched_existing and standard else candidate.get("content")
    )
    content_type = _classify_topic_content_type(query, rows, matches)
    # Normalize old cached/model bodies before export.  Standard-backed topics
    # use the authoritative standard snippet; experience-only topics retain
    # only auditable numbered points.  This prevents the historical
    # three-section body from returning through an old cache or mock response.
    compact_standard = _build_compact_standard_content(
        standard,
        content_type,
        query=query,
    )
    if compact_standard and standard and not matched_existing:
        content = compact_standard
        content_rebuild_note = _safe_join(
            [content_rebuild_note, "已按引用标准重建为简洁编号正文。"],
            "；",
        )
    elif content:
        normalized_points = _compact_standard_rule_points(content, content_type)
        if normalized_points and (
            not re.search(r"(?:^|\n)\s*\d+[.、]\s*\S+", content)
            or any(
                marker in content
                for marker in (
                    "判定规则：",
                    "处理步骤：",
                    "例外与边界：",
                    "判定标准：",
                    "核验方法：",
                    "处理边界：",
                )
            )):
            content = "\n".join(
                f"{index}. {point}"
                for index, point in enumerate(normalized_points, start=1)
            )
            content_rebuild_note = _safe_join(
                [content_rebuild_note, "已将历史三段式正文归一化为简洁编号正文。"],
                "；",
            )

    if use_standard_references and not has_authoritative_standard:
        # 标准模式下没有真实命中时：优先用来源案例事实重建正文，保留来源中
        # 明确记录的对象、核验方式和处理方向；但来源里的数值阈值只能证明
        # 该案例曾这样处理，不能转写成可复用规则，所以走保守模式剥离阈值。
        # 候选必须标记人工重点复核，不能把来源明确的判定选项剥掉。
        source_fact_content = _failed_topic_source_knowledge_content(query)
        source_points = _compact_standard_rule_points(
            source_fact_content,
            content_type,
        )
        if source_points and _topic_source_fact_has_explicit_conclusion(query):
            content = chr(10).join(
                f"{index}. {point}"
                for index, point in enumerate(source_points, start=1)
            )
            content_rebuild_note = _safe_join(
                [
                    content_rebuild_note,
                    "未命中真实标准，已按来源案例事实重建为可复用正文，"
                    "已剥离无标准支撑的数值阈值，保留来源明确处理方向；"
                    "候选标记人工重点复核。",
                ],
                "；",
            )
        else:
            content = _build_experience_review_content(query)
            content_rebuild_note = _safe_join(
                [
                    content_rebuild_note,
                    "未命中真实标准且来源事实不足，已重建为经验补充人工复核内容。",
                ],
                "；",
            )
        content = _strip_unverified_standard_language(content)
    model_content_type = _clean_text(candidate.get("content_type"))
    if model_content_type and model_content_type != content_type:
        needs_review = True
        content_type_note = (
            f"正文类型已按来源规则从{model_content_type}调整为{content_type}，"
            "正文格式需人工复核。"
        )
    title = (
        _clean_text(standard.title)
        if matched_existing and standard and _clean_text(standard.title)
        else _clean_text(candidate.get("title"))
    )
    title_rebuild_note = ""
    title_structure_issue = _natural_question_title_issue(title)
    source_title = _natural_topic_title_from_source(query, rows, standard)
    structured_title = _rebuild_title_from_structured_fields(query, standard)
    normalized_title = _as_natural_question_title(title)
    user_judgment_repair_title = (
        structured_title
        == "平板电脑电池拆修检测显示“用户判断”时如何处理？"
    )
    if user_judgment_repair_title:
        title = structured_title
        needs_review = True
        title_rebuild_note = (
            "“用户判断”属于电池拆修检测待核验状态，"
            "主标题已按拆修检测问题重建。"
        )
    elif (
        title
        and normalized_title
        and not _title_requires_structured_rebuild(title)
        and not _has_internal_title_tag(title)
    ):
        title = normalized_title
        needs_review = True
        title_rebuild_note = "主标题已改写为自然问句。"
    elif title_structure_issue or _title_requires_structured_rebuild(title):
        rebuilt_source_title = (
            source_title
            if source_title and not _title_requires_structured_rebuild(source_title)
            else ""
        )
        title = rebuilt_source_title or structured_title
        needs_review = True
        title_rebuild_note = (
            (
                f"主标题存在{title_structure_issue}，"
                "已按结构化字段改写为自然标题。"
                if title_structure_issue
                else "主标题包含案例叙述、多问题或过长内容，已按结构化字段改写为自然标题。"
            )
        )
    if not title and structured_title:
        title = structured_title
        needs_review = True
        title_rebuild_note = _safe_join(
            [title_rebuild_note, "主标题为空，已按结构化字段重建。"],
            "；",
        )
    applicable_scope = _normalized_applicable_scope(
        product_type,
        standard.scope if matched_existing and standard else candidate.get("applicable_scope"),
        rows,
        trusted_scope=matched_existing,
    )
    applicable_brands = _supported_topic_applicability_values(
        candidate.get("applicable_brands"),
        rows,
        "适用品牌",
        product_type,
    )
    applicable_models = _supported_topic_applicability_values(
        candidate.get("applicable_models"),
        rows,
        "适用机型",
        product_type,
    )
    source_model_values = _source_specific_model_values(
        [
            _clean_text(query.get("核心问题")),
            _clean_text(query.get("人工核心问题")),
            _clean_text(query.get("人工判定结论")),
            _clean_text(query.get("历史实际回复")),
        ]
    )
    visible_models = list(dict.fromkeys([*applicable_models, *source_model_values]))
    stripped_title = _strip_specific_models_from_text(title, visible_models)
    if stripped_title != title:
        title = stripped_title
        needs_review = True
        title_rebuild_note = _safe_join(
            [title_rebuild_note, "主标题已去除具体机型，机型保留在适用机型字段。"],
            "；",
        )
    product_title_aliases = {
        product_type,
        re.sub(r"电脑$", "", product_type),
    }
    if title and product_type and not any(
        alias and alias in title for alias in product_title_aliases
    ):
        title = f"{product_type}{title}"[:120]
    specific_model = (
        applicable_models[0]
        if len(applicable_models) == 1
        else ""
    )
    specific_model_conclusion = (
        _topic_specific_model_conclusion(query, specific_model)
        if specific_model
        else ""
    )
    specific_model_note = ""
    if specific_model:
        # 机型只保留在“适用机型”，主标题保持品类级，避免把单案例
        # 误包装成机型标题或让标题无法检索同类问题。
        title = _strip_specific_models_from_text(title, applicable_models)
        needs_review = True
        specific_model_note = (
            "来源明确限制单一机型，具体机型已保留在适用机型字段，标题保持品类级。"
        )
    if (
        specific_model_conclusion
        and not (use_standard_references and not has_authoritative_standard)
        and (
            _normalized_topic_claim(specific_model)
            not in _normalized_topic_claim(content)
            or not _topic_text_keeps_specific_conclusion(
                content,
                specific_model_conclusion,
            )
        )
    ):
        content = _compact_knowledge_content(
            _build_topic_source_fact_content(
                query,
                use_standard_references=use_standard_references,
            )
        )
        needs_review = True
        specific_model_note = _safe_join(
            [
                specific_model_note,
                "来源已给出单机型结论，已重建正文以避免泛化到整个品类。",
            ],
            "；",
        )
    # The transcription contract already selects a compact body shape. Do not
    # force every candidate back into the historical three-section template.
    content = _compact_knowledge_content(content)
    if (
        image_measurement_gate["status"] in {"required_missing", "unusable"}
        and not (
            standard
            and compact_standard
            and content == _compact_knowledge_content(compact_standard)
        )
    ):
        content = _append_measurement_evidence_boundary(
            content,
            query=query,
        )
        needs_review = True
        content_rebuild_note = _safe_join(
            [
                content_rebuild_note,
                "图片缺少尺寸证据，已保留来源规则并阻止对当前案例直接下尺寸、档位或确定结论。",
            ],
            "；",
        )
    content_structure_issues: tuple[str, ...] = ()
    raw_recommended_reply = _clean_text(candidate.get("recommended_reply"))
    recommended_reply = _recommended_reply_from_final_content(
        content,
        evidence_status=image_measurement_gate["status"],
    )
    reply_quality_issues = _recommended_reply_quality_issues(
        raw_recommended_reply,
        title=title,
        content=content,
    )
    if not reply_quality_issues and recommended_reply:
        reply_quality_issues = _recommended_reply_quality_issues(
            recommended_reply,
            title=title,
            content=content,
        )
    reply_rebuild_note = ""
    if reply_quality_issues:
        needs_review = True
        reply_rebuild_note = (
            "推荐回复包含主题外内容，已按当前主题正文重建"
            f"（检测到：{'、'.join(reply_quality_issues)}）。"
        )
    if (
        specific_model_conclusion
        and (
            _normalized_topic_claim(specific_model)
            not in _normalized_topic_claim(recommended_reply)
            or "是否支持" in recommended_reply
            or "查询官网" in recommended_reply
            or not _topic_text_keeps_specific_conclusion(
                recommended_reply,
                specific_model_conclusion,
            )
        )
    ):
        recommended_reply = _recommended_reply_from_final_content(
            content,
            evidence_status=image_measurement_gate["status"],
        )

    cleaned_reply = _strip_specific_models_from_text(recommended_reply, visible_models)
    if cleaned_reply != recommended_reply:
        recommended_reply = cleaned_reply
        needs_review = True
        reply_rebuild_note = _safe_join(
            [reply_rebuild_note, "推荐回复已去除具体机型，避免单案例口径外溢。"],
            "；",
        )
    final_reply_quality_issues = _recommended_reply_quality_issues(
        recommended_reply,
        title=title,
        content=content,
    )
    if final_reply_quality_issues:
        needs_review = True
        reply_rebuild_note = _safe_join(
            [
                reply_rebuild_note,
                (
                    "重建后的推荐回复仍存在质量问题："
                    + "、".join(final_reply_quality_issues)
                ),
            ],
            "；",
        )
    unsupported_claims = _topic_unsupported_source_claims(
        {
            "content": content,
            "recommended_reply": recommended_reply,
        },
        evidence_package,
        matches if use_standard_references else [],
    )
    if unsupported_claims:
        needs_review = True
    quality_issues = list(
        dict.fromkeys(
            [
                *final_reply_quality_issues,
                *(["标题为空"] if not title else []),
                *(["图片证据不足"] if image_measurement_gate["status"] in {"required_missing", "unusable"} else []),
                *(["来源事实不支持"] if unsupported_claims else []),
            ]
        )
    )
    model_call_status, draft_status = _draft_status_for_model_result(
        model_error=model_error,
        quality_issues=quality_issues,
        has_standard=has_authoritative_standard,
        model_call_failed=stage_status
        not in {
            "topic_model_labeled",
            "topic_model_rewritten_for_evidence",
        },
    )
    model_output_validation_status = (
        "passed"
        if stage_status
        in {
            "topic_model_labeled",
            "topic_model_rewritten_for_evidence",
        }
        else "not_run"
    )
    model_quality_status = "failed" if quality_issues else "passed"
    if quality_issues:
        recommended_reply = ""
    standard_gate_status = "accepted" if matches else "rejected_or_missing"
    standard_reference_label = (
        "已引用标准知识点"
        if has_authoritative_standard
        else "未引用标准-人工重点复核"
        if use_standard_references
        else ""
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
    illustration = "\n".join(image_links)
    return {
        "主题ID": topic_id,
        "知识ID": topic_id,
        "主题状态": "review_pending",
        "主题样本数": len(rows),
        "回收业务层级": _business_line_for_row(rows[0]),
        "主题来源记录ID": "\n".join(source_ids),
        "主题代表性记录ID": "\n".join(
            evidence_package.get("representative_source_ids") or []
        ),
        "主题工单ID": "\n".join(work_order_ids),
        "主题聚类键": " | ".join(key),
        "主题问题意图": _clean_text(query.get("问题意图")),
        "主题对象/部位": _clean_text(query.get("对象/部位")),
        "主题异常现象": _clean_text(query.get("异常现象")),
        "主题解题方式": _clean_text(query.get("解题方式")),
        "主题证据等级": "、".join(levels),
        "主题证据摘要": _topic_evidence_summary(
            rows,
            evidence_package,
        ),
        "主题事实引用": _topic_fact_references(evidence_package),
        "主题事实证据包": _topic_evidence_package_json(
            evidence_package
        ),
        "主题无来源内容": "\n".join(unsupported_claims),
        "主题图例来源": image_source_trace,
        "主题视频来源": video_source_trace,
        "主题图片链接": illustration,
        "主题视频链接": "\n".join(video_links),
        "主题图片必要性": (
            "需要保留"
            if requires_images
            else "辅助图例"
            if image_links
            else "无案例图"
        ),
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
        "模型调用状态": model_call_status,
        "模型输出校验状态": model_output_validation_status,
        "模型质量状态": model_quality_status,
        "知识草稿状态": draft_status,
        "标准引用门禁状态": standard_gate_status,
        "图片证据门禁状态": image_measurement_gate["status"],
        "人工主题问题分类": "",
        "主标题": title,
        "副标题": _finalize_topic_subtitles(
            candidate.get("subtitles", []),
            title,
            content,
            content_type,
            visible_models,
        ),
        "知识内容": content,
        "正文类型": content_type,
        "图例": illustration,
        "知识分类": knowledge_category_from_topic_stage(
            candidate.get("knowledge_category"),
            candidate.get("knowledge_form"),
        ),
        "知识来源": (
            "已有知识优先匹配"
            if matched_existing
            else "方向二总部标准候选"
            if use_standard_references
            and matches
            and standard_basis_source == "headquarters_standard"
            else "方向二本地质检标准候选"
            if use_standard_references and matches and local_standard_basis
            else "方向二经验补充候选"
            if use_standard_references
            else "方向二案例沉淀"
        ),
        "关联标准项": standard_refs,
        "候选项/处理项": (
            "\n".join(_standard_handling_options(standard, query))
            if standard and standard.response_snippet
            else "\n".join(
                _extract_handling_options_from_text(
                    _safe_join(
                        [
                            _clean_text(query.get("人工判定结论")),
                            _clean_text(query.get("判定结论")),
                            _clean_text(query.get("历史实际回复")),
                        ],
                        "；",
                    )
                )
            )
        ),
        "适用范围": applicable_scope,
        "适用品牌": "\n".join(applicable_brands),
        "适用机型": "\n".join(applicable_models),
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
                f"正文类型：{content_type}",
                f"已有知识优先匹配：{standard.title}" if matched_existing and standard else "",
                _clean_text(candidate.get("reasoning_summary")),
                "未命中总部标准，作为经验补充候选，需人工确认是否补充标准。"
                if use_standard_references and not matches
                else "无标准引用模式：仅依据第二部分案例证据生成。"
                if not use_standard_references
                else "",
                (
                    "来源事实不支持："
                    + "；".join(unsupported_claims)
                    if unsupported_claims
                    else ""
                ),
                reply_rebuild_note,
                title_rebuild_note,
                content_rebuild_note,
                content_type_note,
                "；".join(content_structure_issues),
                specific_model_note,
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
        "模型调用状态": model_call_status,
        "模型输出校验状态": model_output_validation_status,
        "模型质量状态": model_quality_status,
        "知识草稿状态": draft_status,
        "标准引用门禁状态": standard_gate_status,
        "标准引用标签": standard_reference_label,
        "图片证据门禁状态": image_measurement_gate["status"],
        "流程状态": "review_pending",
        "模型阶段状态": stage_status,
    }


def _topic_content_has_complete_short_structure(
    content: str,
    content_type: str = "",
) -> bool:
    numbered_steps = re.findall(
        r"(?:^|\n)\s*\d+[.、]\s*\S+",
        content,
    )
    expected_counts = {
        CONTENT_TYPE_DEFINITION: (1, 2),
        CONTENT_TYPE_THRESHOLD: (1, 3),
        CONTENT_TYPE_VERIFICATION: (1, 4),
        CONTENT_TYPE_DISTINCTION: (2, 5),
    }
    if content_type in expected_counts:
        minimum, maximum = expected_counts[content_type]
        return minimum <= len(numbered_steps) <= maximum
    if len(numbered_steps) >= 3:
        return True
    section_markers = (
        "判定规则：",
        "处理步骤：",
        "例外与边界：",
        "适用情形：",
        "核验要点：",
        "核验方法：",
        "核验依据：",
        "检查条件：",
        "触发条件：",
        "处理结论：",
        "处理方式：",
        "适用边界：",
        "例外：",
    )
    return sum(marker in content for marker in section_markers) >= 3


_TOPIC_SOURCE_ASSERTION_FIELDS = (
    "human_judgment_conclusion",
    "judgment_basis",
    "historical_actual_reply",
    "source_supported_threshold_or_exception",
)

_TOPIC_NUMERIC_CLAIM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*"
    r"(?:秒|分钟|小时|天|次|个|件|台|套|张|条|%"
    r"|％|毫米|厘米|米|mm|cm|px|级|档|元|℃|度)",
    re.IGNORECASE,
)

_TOPIC_ACTION_CLAIM_GROUPS = (
    ("拆机", "拆开", "拆卸"),
    ("检查", "检测", "核验"),
    ("测试", "复测"),
    ("重启", "重新启动"),
    ("更换", "替换"),
    ("维修", "返修", "送修"),
    ("升级", "更新"),
    ("清洁", "擦拭", "清理"),
    ("拍摄", "拍照"),
    ("上传", "提交"),
    ("联系", "反馈"),
    ("记录", "登记"),
    ("安装",),
    ("卸载",),
    ("恢复", "重置"),
    ("校准",),
    ("测量",),
    ("观察",),
)

_TOPIC_ENTITY_GROUPS = (
    ("屏幕", "显示屏"),
    ("主板", "逻辑板", "电路板"),
    ("转轴", "铰链"),
    ("电池", "电芯"),
    ("摄像头", "镜头", "相机"),
    ("键盘",),
    ("触控板", "触摸板"),
    ("麦克风", "话筒"),
    ("扬声器", "喇叭"),
    ("充电口", "充电接口"),
    ("接口", "端口"),
    ("外壳", "机壳", "后壳"),
    ("后盖",),
    ("边框",),
    ("传感器",),
    ("芯片",),
    ("内存",),
    ("硬盘", "固态硬盘"),
    ("耳机",),
    ("手机",),
    ("笔记本",),
    ("平板",),
    ("手表",),
)

_TOPIC_EVIDENCE_GAP_MARKERS = (
    "来源未说明",
    "来源没有说明",
    "来源事实未说明",
    "其他情形需要补充",
    "其他情形需补充",
    "需补充对应来源事实",
    "需要补充对应来源事实",
    "不得直接套用",
    "不能直接套用",
    "不得直接外推",
    "不能直接外推",
    "待补充来源事实",
)


def _topic_claim_is_evidence_gap(value: Any) -> bool:
    text = _normalized_topic_claim(value)
    explicit_gap = any(
        marker in text
        for marker in _TOPIC_EVIDENCE_GAP_MARKERS
    )
    missing_context = any(
        marker in text
        for marker in (
            "无法确认",
            "不能确认",
            "尚未确认",
            "未能确认",
            "信息不足",
            "证据不足",
        )
    )
    conservative_action = any(
        marker in text
        for marker in (
            "不沿用",
            "不套用",
            "补充信息",
            "补充证据",
            "再确认",
            "再判定",
            "人工复核",
            "转人工审核",
            "转人工",
        )
    )
    if not (
        explicit_gap
        or (missing_context and conservative_action)
    ):
        return False
    if _topic_action_claim_groups(text):
        return False
    if _topic_causality_markers(text):
        return False
    if any(
        marker in text
        for marker in (
            "属于",
            "不属于",
            "判定为",
            "选择",
            "维修",
            "更换",
        )
    ):
        return False
    has_strong_requirement = any(
        marker in text
        for marker in ("必须", "应当", "需要")
    )
    allowed_requirement = any(
        marker in text
        for marker in (
            "补充来源",
            "补充对应来源",
            "补充事实",
            "补充证据",
            "补充信息",
            "人工复核",
            "再确认",
        )
    )
    return not has_strong_requirement or allowed_requirement


def _topic_source_segment_is_question(value: Any) -> bool:
    text = _clean_text(value)
    if re.search(
        r"(?:先|再|需要|应当|应|需|必须).{0,40}"
        r"(?:确认|核验|检查|检测|记录).{0,40}(?:是否|能否)",
        text,
    ):
        return False
    return any(
        marker in text
        for marker in (
            "是否",
            "怎么",
            "如何",
            "能否",
            "可否",
            "吗",
            "么",
            "?",
            "？",
        )
    )


def _topic_source_assertion_segments(
    fact: dict[str, Any],
) -> list[str]:
    values = [
        _clean_text(fact.get(field))
        for field in _TOPIC_SOURCE_ASSERTION_FIELDS
        if _clean_text(fact.get(field))
    ]
    values.append(_clean_text(fact.get("conversation_excerpt")))
    segments = [
        _clean_text(segment)
        for value in values
        for segment in re.split(
            r"[\n，,。；;！？!?]+",
            value,
        )
        if _clean_text(segment)
    ]
    return list(
        dict.fromkeys(
            segment
            for segment in segments
            if not _topic_source_segment_is_question(segment)
        )
    )


def _topic_source_claim_texts(
    evidence_package: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]] | None = None,
) -> list[str]:
    texts = []
    for fact in evidence_package.get("facts") or []:
        assertion_text = "\n".join(
            _topic_source_assertion_segments(fact)
        )
        if assertion_text:
            texts.append(assertion_text)
    for standard, _score in matches or []:
        standard_text = "\n".join(
            value
            for value in (
                _clean_text(standard.title),
                _clean_text(standard.standard_path),
                _clean_text(standard.scope),
                _clean_text(standard.response_snippet),
                *(_clean_text(keyword) for keyword in standard.keywords),
            )
            if value
        )
        if standard_text:
            texts.append(standard_text)
    return list(dict.fromkeys(text for text in texts if text))


def _normalized_topic_claim(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(
        r"^\s*(?:[-•]|\d+\s*[.、)）])\s*",
        "",
        text,
    )
    text = re.sub(
        r"^(?:您好[，,]?)?(?:问题背景|判断对象|来源现象|来源核验依据|"
        r"核验依据|核验流程|功能核验流程|检查条件|触发条件|"
        r"新增阈值|案例结论|人工处理结论|处理结论|处理方式|"
        r"适用主题|适用边界|例外)[：:]\s*",
        "",
        text,
    )
    normalized = re.sub(
        r"[\s，,。；;：:！？!?（）()\[\]【】“”\"'、]+",
        "",
        text,
    ).lower()
    normalized = normalized.replace("可以", "可")
    normalized = re.sub(
        r"直接(?:进入|进行|继续)",
        "直接执行",
        normalized,
    )
    return normalized


def _topic_numeric_claim_tokens(value: Any) -> set[str]:
    return {
        re.sub(r"\s+", "", token).replace("％", "%").lower()
        for token in _TOPIC_NUMERIC_CLAIM_PATTERN.findall(
            _clean_text(value)
        )
    }


def _topic_membership_polarities(value: Any) -> set[str]:
    text = _clean_text(value)
    polarities = set()
    if "不属于" in text:
        polarities.add("negative")
    if "属于" in text.replace("不属于", ""):
        polarities.add("positive")
    return polarities


def _topic_obligation_polarities(value: Any) -> set[str]:
    text = _clean_text(value)
    polarities = set()
    negative_markers = (
        "不得",
        "禁止",
        "不能",
        "不应",
        "无需",
        "不需要",
        "不要求",
    )
    if any(marker in text for marker in negative_markers):
        polarities.add("prohibit")
    positive_text = text
    for marker in negative_markers:
        positive_text = positive_text.replace(marker, "")
    if any(
        marker in positive_text
        for marker in ("必须", "应当", "需要")
    ):
        polarities.add("require")
    return polarities


def _topic_threshold_relations(value: Any) -> set[str]:
    text = _clean_text(value)
    relations = set()
    patterns = (
        ("less_equal", ("不超过", "至多")),
        ("greater_equal", ("至少", "不少于")),
        ("greater", ("超过", "大于", "高于")),
        ("less", ("小于", "低于")),
    )
    remaining = text
    for relation, markers in patterns:
        if any(marker in remaining for marker in markers):
            relations.add(relation)
        for marker in markers:
            remaining = remaining.replace(marker, "")
    return relations


def _topic_absolute_scope_markers(value: Any) -> set[str]:
    text = _clean_text(value)
    return {
        marker
        for marker in ("所有", "一律", "全部", "任何", "无论")
        if marker in text
    }


def _topic_causality_markers(value: Any) -> set[str]:
    text = _clean_text(value)
    return {
        marker
        for marker in (
            "原因是",
            "原因为",
            "由于",
            "导致",
            "造成",
            "引起",
        )
        if marker in text
    }


def _topic_severity_markers(value: Any) -> set[str]:
    text = _clean_text(value)
    markers = set()
    if any(
        marker in text
        for marker in ("严重", "重度", "明显")
    ):
        markers.add("severe")
    if any(
        marker in text
        for marker in ("轻微", "轻度", "不明显")
    ):
        markers.add("mild")
    return markers


def _topic_action_claim_groups(value: Any) -> set[int]:
    text = _clean_text(value)
    return {
        index
        for index, markers in enumerate(_TOPIC_ACTION_CLAIM_GROUPS)
        if any(marker in text for marker in markers)
    }


def _topic_entity_groups(value: Any) -> set[int]:
    text = _clean_text(value).lower()
    return {
        index
        for index, markers in enumerate(_TOPIC_ENTITY_GROUPS)
        if any(marker.lower() in text for marker in markers)
    }


def _topic_claim_relations_are_supported(
    claim: str,
    source: str,
) -> bool:
    claim_relations = (
        _topic_membership_polarities(claim),
        _topic_obligation_polarities(claim),
        _topic_threshold_relations(claim),
    )
    source_relations = (
        _topic_membership_polarities(source),
        _topic_obligation_polarities(source),
        _topic_threshold_relations(source),
    )
    for expected, actual in zip(claim_relations, source_relations):
        if expected and not expected.issubset(actual):
            return False
    claim_scope = _topic_absolute_scope_markers(claim)
    if claim_scope and not _topic_absolute_scope_markers(source):
        return False
    claim_causality = _topic_causality_markers(claim)
    if claim_causality and not _topic_causality_markers(source):
        return False
    claim_severity = _topic_severity_markers(claim)
    if claim_severity and not claim_severity.issubset(
        _topic_severity_markers(source)
    ):
        return False
    claim_actions = _topic_action_claim_groups(claim)
    if claim_actions and not claim_actions.issubset(
        _topic_action_claim_groups(source)
    ):
        return False
    claim_entities = _topic_entity_groups(claim)
    if claim_entities and not claim_entities.issubset(
        _topic_entity_groups(source)
    ):
        return False
    return True


def _topic_claim_comparison_variants(
    normalized_claim: str,
    normalized_source: str,
) -> set[str]:
    variants = {normalized_claim}
    if "确认设备代际为" in normalized_claim:
        variants.add(
            normalized_claim.replace("确认设备代际为", "", 1)
        )
    if (
        normalized_claim.startswith("确认代际后")
        and re.search(r"(?:第)?[一二三四五六七八九十\d]+代", normalized_source)
    ):
        variants.add(normalized_claim.removeprefix("确认代际后"))
    return {variant for variant in variants if variant}


def _topic_claim_anchors_are_supported(
    normalized_claim: str,
    normalized_source: str,
) -> bool:
    anchors = [
        anchor
        for anchor in re.split(
            r"(?:之后|以后|然后|同时|并且|并|且|再|后|时)",
            normalized_claim,
        )
        if len(anchor) >= 4
    ]
    return (
        len(anchors) >= 2
        and all(anchor in normalized_source for anchor in anchors)
    )


def _topic_claim_is_source_supported(
    claim: str,
    source_texts: list[str],
) -> bool:
    claim_numbers = _topic_numeric_claim_tokens(claim)
    normalized_claim = _normalized_topic_claim(claim)
    if not normalized_claim:
        return True
    for source_text in source_texts:
        if claim_numbers - _topic_numeric_claim_tokens(source_text):
            continue
        if not _topic_claim_relations_are_supported(
            normalized_claim,
            source_text,
        ):
            continue
        source_segments = [
            source_text,
            *[
                segment
                for segment in re.split(
                    r"[\n，,。；;：:！？!?（）()“”\"'、]+",
                    source_text,
                )
                if _clean_text(segment)
            ],
        ]
        for source in source_segments:
            normalized_source = _normalized_topic_claim(source)
            if len(normalized_source) < 4:
                continue
            for claim_variant in _topic_claim_comparison_variants(
                normalized_claim,
                normalized_source,
            ):
                if claim_variant in normalized_source:
                    return True
                if (
                    len(normalized_source) >= 8
                    and normalized_source in claim_variant
                ):
                    return True
                if _topic_claim_anchors_are_supported(
                    claim_variant,
                    normalized_source,
                ):
                    return True
                if (
                    SequenceMatcher(
                        None,
                        claim_variant,
                        normalized_source,
                    ).ratio()
                    >= 0.65
                ):
                    return True
    return False


def _topic_unsupported_source_claims(
    candidate: dict[str, Any],
    evidence_package: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]] | None = None,
) -> list[str]:
    source_texts = _topic_source_claim_texts(
        evidence_package,
        matches,
    )
    claims = [
        _clean_text(claim)
        for draft in (
            candidate.get("content"),
            candidate.get("recommended_reply"),
        )
        for claim in re.split(
            r"[\n。；;！？!?]+",
            _clean_text(draft),
        )
        if _clean_text(claim)
        and not _KNOWLEDGE_CONTENT_SECTION_LINE_PATTERN.fullmatch(
            _clean_text(claim)
        )
    ]
    unsupported: list[str] = []
    seen: set[str] = set()
    for claim in claims:
        normalized_claim = _normalized_topic_claim(claim)
        if not normalized_claim or normalized_claim in seen:
            continue
        seen.add(normalized_claim)
        if any(
            marker in normalized_claim
            for marker in (
                "补充证据后再判定",
                "补充信息后再处理",
                "证据完整后由人工按适用口径判断",
                "标准未提供可复用的处理步骤",
                "标准未提供可复用的例外与边界",
                "需人工补充后再审核",
            )
        ):
            continue
        if _topic_claim_is_evidence_gap(normalized_claim):
            continue
        if not _topic_claim_is_source_supported(claim, source_texts):
            unsupported.append(claim)
    return unsupported


_TOPIC_POLICY_TEMPLATE_LABELS = (
    "适用对象",
    "处理原则",
    "记录要求",
    "边界说明",
)
_TOPIC_POLICY_LINE_PREFIX_PATTERN = re.compile(
    r"^(?:>\s*|#{1,6}\s*|[-+*]\s+|"
    r"\d+\s*(?:[.、)]|）)\s*|[（(]\d+[）)]\s*)"
)
_TOPIC_POLICY_LINE_SUFFIX_PATTERN = re.compile(r"\s+#{1,6}\s*$")


def _topic_policy_line_label(value: Any) -> str:
    line = _clean_text(value)
    for _attempt in range(6):
        without_prefix = _TOPIC_POLICY_LINE_PREFIX_PATTERN.sub(
            "",
            line,
            count=1,
        ).strip()
        if without_prefix == line:
            break
        line = without_prefix
    line = _TOPIC_POLICY_LINE_SUFFIX_PATTERN.sub("", line).strip()
    for label in _TOPIC_POLICY_TEMPLATE_LABELS:
        if re.fullmatch(
            r"(?:(?:\*{1,2})|(?:_{1,2}))?"
            + re.escape(label)
            + r"(?:(?:\*{1,2})|(?:_{1,2}))?"
            r"(?:\s*[：:].*)?",
            line,
        ):
            return label
    return ""


def _topic_policy_template_markers(value: Any) -> list[str]:
    found = {
        label
        for line in _clean_text(value).splitlines()
        if (label := _topic_policy_line_label(line))
    }
    return [
        label
        for label in _TOPIC_POLICY_TEMPLATE_LABELS
        if label in found
    ]


def _rule_topic_initial_review(
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool = True,
) -> dict[str, Any]:
    title = _clean_text(topic.get("主标题"))
    content = _clean_text(topic.get("知识内容"))
    recommended_reply = _clean_text(topic.get("推荐回复"))
    refs = _clean_text(topic.get("关联标准项"))
    local_standard_basis = (
        _clean_text(topic.get("主题标准检索来源"))
        == "local_quality_standard"
    )
    title_structure_issue = candidate_title_structure_issue(title)
    if title_structure_issue:
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "标题不准",
            "reason": (
                f"主标题存在{title_structure_issue}，"
                "不适合作为可直接使用的自然知识标题。"
            ),
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "需修改",
            "confidence": 0.92,
            "priority_review": True,
        }
    title_style_issue = candidate_title_style_issue(title)
    if title_style_issue:
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "标题不准",
            "reason": (
                f"主标题包含“{title_style_issue}”这类公文式元描述，"
                "不能直接作为用户检索的知识标题。"
            ),
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "需修改",
            "confidence": 0.94,
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
            "decision": "需修改",
            "knowledge_value": "待确认",
            "error_type": "标准未覆盖/标准召回不足",
            "reason": (
                "未命中可追溯的总部或本地标准，当前只能作为经验补充候选"
                "进入人工复核；不得作为正式知识、训练样本或自动送审候选。"
            ),
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "一致",
            "title_quality": "清晰",
            "confidence": 0.9,
            "priority_review": True,
        }
    if (
        _clean_text(topic.get("主题图片必要性")) == "需要保留"
        and not _clean_text(topic.get("主题图片链接"))
    ):
        return {
            "decision": "证据不足待补充",
            "knowledge_value": "待确认",
            "error_type": "图片判断失误",
            "reason": "草稿内容依赖视觉差异，但候选中没有保留可用图片。",
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "不足",
            "content_consistency": "部分一致",
            "image_necessity": "图片不足",
            "title_quality": "清晰",
            "confidence": 0.91,
            "priority_review": True,
        }
    unsupported_claims = _clean_text(topic.get("主题无来源内容"))
    if unsupported_claims:
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "场景理解错",
            "reason": (
                "转写草稿包含来源事实不支持的阈值、范围或判定内容："
                f"{unsupported_claims}"
            ),
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "不一致",
            "title_quality": "清晰",
            "confidence": 0.95,
            "priority_review": True,
        }
    if (
        use_standard_references
        and _topic_draft_is_case_analysis(
            {
                "title": title,
                "content": content,
                "recommended_reply": recommended_reply,
            }
        )
    ):
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "话术不合适",
            "reason": (
                "知识草稿仍在复述单个案例或本次会话；"
                "请依据已命中标准改写为可复用的标准条件、核验方法和判定边界。"
            ),
            "standard_consistency": "部分一致" if matches else "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "需修改",
            "confidence": 0.96,
            "priority_review": True,
        }
    if (
        not use_standard_references
        and _topic_content_uses_internal_analysis_labels(content)
    ):
        unsupported_note = _clean_text(topic.get("主题无来源内容"))
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "话术不合适",
            "reason": (
                "知识正文使用了问题背景、判断对象或来源核验依据等内部分析标签；"
                "请改为可直接使用的适用情形、核验要点、处理结论和适用边界。"
                + (
                    "来源事实不支持：" + unsupported_note
                    if unsupported_note
                    else ""
                )
            ),
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.94,
            "priority_review": True,
        }
    reply_quality_issues = _recommended_reply_quality_issues(
        recommended_reply,
        title=title,
        content=content,
    )
    if reply_quality_issues:
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "话术不合适",
            "reason": (
                "推荐回复未保持当前主题纯度或存在病句："
                + "、".join(reply_quality_issues)
            ),
            "standard_consistency": "一致" if matches else "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "不一致",
            "title_quality": "清晰",
            "confidence": 0.96,
            "priority_review": True,
        }
    if use_standard_references and matches and not refs:
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
    policy_template_markers = _topic_policy_template_markers(content)
    if (
        not use_standard_references
        and set(policy_template_markers)
        == set(_TOPIC_POLICY_TEMPLATE_LABELS)
    ):
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "话术不合适",
            "reason": (
                "无标准案例模式的正文使用了"
                f"“{'、'.join(policy_template_markers)}”等制度条款模板，"
                "容易把单个案例包装成通用规则；请改为来源对象、"
                "核验依据、当前处理结论和证据边界。"
            ),
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.93,
            "priority_review": True,
        }
    content_type = _clean_text(topic.get("正文类型"))
    minimum_content_lengths = {
        CONTENT_TYPE_DEFINITION: 18,
        CONTENT_TYPE_THRESHOLD: 18,
        CONTENT_TYPE_VERIFICATION: 24,
        CONTENT_TYPE_DISTINCTION: 36,
    }
    if (
        not use_standard_references
        and len(numbered_steps := re.findall(
            r"(?:^|\n)\s*\d+[.、]\s*\S+",
            content,
        )) == 1
        and len(content) < 40
    ):
        return {
            "decision": "需修改",
            "knowledge_value": "值得沉淀",
            "error_type": "话术不合适",
            "reason": "知识正文过短，只有孤立结论，未形成可审核的来源边界或判定依据。",
            "standard_consistency": "无可信标准",
            "evidence_sufficiency": "部分充分",
            "content_consistency": "部分一致",
            "title_quality": "清晰",
            "confidence": 0.9,
            "priority_review": True,
        }
    if (
        len(content) < minimum_content_lengths.get(content_type, 60)
        and not _topic_content_has_complete_short_structure(content, content_type)
    ):
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
            and matches
            and not local_standard_basis
            else "转写草稿依据本地质检标准生成，需人工确认标准仍有效后再送审。"
            if use_standard_references
            and matches
            and local_standard_basis
            else "转写草稿具备主题级标题、正文和来源事实追溯；未命中总部标准，作为经验补充候选进入人工价值复核。"
            if use_standard_references
            else "转写草稿具备主题级标题、正文、分类、案例证据和来源追溯，可进入人工复标。"
        ),
        "standard_consistency": "一致" if matches else "无可信标准",
        "evidence_sufficiency": "充分" if _clean_text(topic.get("主题证据等级")) == "完整会话" else "部分充分",
        "confidence": 0.76,
        "priority_review": (
            _clean_text(topic.get("是否重点复核")) == "是"
            or (use_standard_references and not matches)
            or local_standard_basis
        ),
    }


def _apply_topic_initial_review_guard(
    review: dict[str, Any],
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    use_standard_references: bool = True,
) -> dict[str, Any]:
    guarded = dict(review)
    local_standard_basis = (
        _clean_text(topic.get("主题标准检索来源"))
        == "local_quality_standard"
    )
    deterministic_review = _rule_topic_initial_review(
        topic,
        matches,
        use_standard_references=use_standard_references,
    )
    if use_standard_references and not matches:
        model_decision = _clean_text(guarded.get("decision"))
        deterministic_review["reason"] = _safe_join(
            [
                _clean_text(deterministic_review.get("reason")),
                (
                    f"模型初审原结论为“{model_decision}”，"
                    "标准覆盖门禁不允许放行。"
                )
                if model_decision
                else "",
            ],
            "；",
        )
        return deterministic_review
    decision_severity = {
        "通过": 0,
        "需修改": 1,
        "证据不足待补充": 2,
        "驳回": 3,
    }
    if not use_standard_references:
        standard_error_types = {
            "标准未覆盖/标准召回不足",
            "标准项映射错",
        }
        if guarded.get("error_type") in standard_error_types:
            deterministic_review["reason"] = _safe_join(
                [
                    _clean_text(deterministic_review.get("reason")),
                    "本模式不使用标准引用，已忽略模型提出的标准补充要求。",
                ],
                "；",
            )
            return deterministic_review
    deterministic_decision = _clean_text(
        deterministic_review.get("decision")
    )
    model_decision = _clean_text(guarded.get("decision"))
    if decision_severity.get(deterministic_decision, 1) > (
        decision_severity.get(model_decision, 0)
    ):
        deterministic_review["reason"] = _safe_join(
            [
                _clean_text(deterministic_review.get("reason")),
                (
                    f"模型初审原结论为“{model_decision}”，"
                    "确定性内容门禁不允许覆盖。"
                )
                if model_decision
                else "",
            ],
            "；",
        )
        return deterministic_review
    if not use_standard_references:
        guarded["standard_consistency"] = "无可信标准"
        guarded["priority_review"] = bool(
            guarded.get("priority_review")
            or deterministic_review.get("priority_review")
        )
        return guarded
    if (
        not _clean_text(topic.get("关联标准项"))
        and guarded.get("decision") == "通过"
    ):
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
    elif local_standard_basis:
        guarded.update(
            {
                "priority_review": True,
                "reason": _safe_join(
                    [
                        _clean_text(guarded.get("reason")),
                        "本次依据本地质检标准，已保留标准引用，需人工复核。",
                    ],
                    "；",
                ),
            }
        )
    guarded["priority_review"] = bool(
        guarded.get("priority_review")
        or deterministic_review.get("priority_review")
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


def _member_cluster_question(row: dict[str, Any]) -> str:
    atomic_issue = _clean_text(row.get("核心问题"))
    upstream_core_problem = _clean_text(
        row.get("原始核心问题")
    )
    upstream_judgment = _clean_text(
        row.get("原始判定结论") or row.get("判定结论")
    )
    parts = []
    if atomic_issue:
        parts.append(f"原子问题：{atomic_issue}")
    if upstream_core_problem and upstream_core_problem != atomic_issue:
        parts.append(f"上游核心问题：{upstream_core_problem}")
    if upstream_judgment:
        parts.append(f"上游判定结论：{upstream_judgment}")
    return "\n".join(parts)


def _confidence_01(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if 0 <= confidence <= 1 else None


def _cluster_topic_admission(
    rows: list[dict[str, Any]],
    clustering_meta: dict[str, Any],
    *,
    enabled: bool,
    min_confidence: float,
) -> dict[str, Any]:
    atomic_confidences = [
        value
        for row in rows
        if (
            value := _confidence_01(row.get("语义标注置信度"))
        )
        is not None
    ]
    cluster_confidences = [
        value
        for row in rows
        if (
            value := _confidence_01(row.get("_聚类裁决置信度"))
        )
        is not None
    ]
    confidence_candidates: list[float] = []
    if atomic_confidences:
        confidence_candidates.append(min(atomic_confidences))
    if cluster_confidences:
        confidence_candidates.append(min(cluster_confidences))
    admission_confidence = (
        min(confidence_candidates)
        if confidence_candidates
        else 0.0
    )
    if not enabled:
        return {
            "admitted": True,
            "status": "未启用准入门禁",
            "confidence": round(admission_confidence, 4),
            "reason": "当前调用方未启用聚类准入门禁。",
        }

    threshold = max(0.0, min(float(min_confidence), 1.0))
    reasons: list[str] = []
    requested_mode = _clean_text(
        clustering_meta.get("requested_mode")
    ).lower()
    effective_mode = _clean_text(
        clustering_meta.get("effective_mode")
    ).lower()
    if requested_mode != "direct_mimo":
        reasons.append(
            "当前聚类模式不是已验证的 direct_mimo，不能自动进入后续流程"
        )
    elif effective_mode != "direct_mimo":
        reasons.append(
            "direct_mimo 未成功生效，当前结果属于规则或其他模式降级"
        )

    business_lines = {
        _business_line_for_row(row)
        for row in rows
        if _business_line_for_row(row)
    }
    resolved_product_types = [_resolved_product_type_for_row(row) for row in rows]
    product_types = {
        product_type
        for product_type in resolved_product_types
        if product_type
    }
    hard_reasons: list[str] = []
    if len(business_lines) != 1:
        hard_reasons.append("主题成员的回收业务层级不唯一")
    elif UNKNOWN_BUSINESS_LINE_NAME in business_lines:
        hard_reasons.append("回收业务层级待确认")
    if any(not product_type for product_type in resolved_product_types):
        hard_reasons.append("主题成员包含未识别产品品类")
    elif len(product_types) != 1:
        hard_reasons.append("主题成员的产品品类不唯一")
    if any(bool(row.get("_原子品类冲突")) for row in rows):
        hard_reasons.append(
            "主题成员存在模型识别品类与源数据品类冲突，禁止进入正式主题候选"
        )
    reasons.extend(hard_reasons)

    if len(atomic_confidences) != len(rows):
        reasons.append("部分成员缺少有效的原子问题置信度")
    if len(rows) > 1 and not cluster_confidences:
        reasons.append("多成员主题缺少独立的聚类置信度")
    if admission_confidence < threshold:
        reasons.append(
            "聚类准入置信度"
            f" {admission_confidence:.3f} 低于自动放行阈值 {threshold:.3f}"
        )

    if any(bool(row.get("_原子需要复核")) for row in rows):
        reasons.append("成员原子问题需要人工复核")
    if any(bool(row.get("_聚类需要复核")) for row in rows):
        reasons.append("聚类模型要求人工复核")
    priority_reasons = [
        _clean_text(row.get("人工优先复核原因"))
        for row in rows
        if _clean_text(row.get("人工优先复核原因"))
    ]
    if priority_reasons:
        reasons.append(
            "存在人工优先复核原因："
            + "；".join(dict.fromkeys(priority_reasons))
        )

    unsafe_provider_markers = (
        "failed",
        "post-guard",
        "split",
        "mimo-direct-guard",
        "mimo-direct-review",
    )
    unsafe_decision_markers = (
        "聚类失败",
        "程序门禁拆分",
        "继续拆分",
        "未分配",
        "复核标记",
    )
    providers = {
        _clean_text(row.get("_聚类裁决提供方")).lower()
        for row in rows
        if _clean_text(row.get("_聚类裁决提供方"))
    }
    decisions = {
        _clean_text(row.get("_聚类决策"))
        for row in rows
        if _clean_text(row.get("_聚类决策"))
    }
    if any(
        marker in provider
        for provider in providers
        for marker in unsafe_provider_markers
    ):
        reasons.append("聚类结果包含失败、冲突拆分或待复核状态")
    if any(
        marker in decision
        for decision in decisions
        for marker in unsafe_decision_markers
    ):
        reasons.append("聚类决策不是可自动放行的稳定结果")

    unique_reasons = list(dict.fromkeys(reasons))
    admitted = not unique_reasons
    return {
        "admitted": admitted,
        "status": "已自动放行" if admitted else "待人工聚类复核",
        "confidence": round(admission_confidence, 4),
        "hard_blocked": bool(hard_reasons),
        "provisional": bool(unique_reasons) and not hard_reasons,
        "reason": (
            "direct_mimo 成功，原子问题与聚类置信度达到阈值且无风险标记。"
            if admitted
            else "；".join(unique_reasons)
        ),
    }


def _attach_cluster_admission(
    topic: dict[str, Any],
    admission: dict[str, Any],
) -> None:
    topic.update(
        {
            "聚类准入状态": _clean_text(admission.get("status")),
            "聚类准入置信度": admission.get("confidence", ""),
            "聚类准入原因": _clean_text(admission.get("reason")),
        }
    )


def _provisional_singleton_admission(
    admission: dict[str, Any],
) -> dict[str, Any]:
    reason = _safe_join(
        [
            _clean_text(admission.get("reason")),
            "未自动合并，已按单个原子问题生成暂定候选并强制人工价值复核。",
        ],
        "；",
    )
    return {
        **admission,
        "status": "暂定单主题候选",
        "provisional": True,
        "reason": reason,
    }


def _provisional_singleton_key(
    key: tuple[str, ...],
    row: dict[str, Any],
    index: int,
) -> tuple[str, ...]:
    source_id = _clean_text(row.get("数据ID")) or _clean_text(
        row.get("工单ID")
    )
    return (*key, f"暂定单主题:{source_id or index}")


def _attach_provisional_singleton_candidate(
    topic: dict[str, Any],
    admission: dict[str, Any],
) -> None:
    note = _clean_text(admission.get("reason"))
    topic.update(
        {
            "主题状态": "provisional_singleton_review_pending",
            "是否重点复核": "是",
            "校验备注": _safe_join(
                [topic.get("校验备注"), note],
                "；",
            ),
        }
    )


def _attach_incremental_topic(
    topic: dict[str, Any],
    resolution: TopicResolution | None,
) -> None:
    if resolution is None:
        return
    topic.update(
        {
            "历史主题处理结果": resolution.decision,
            "历史主题匹配ID": resolution.historical_topic_id,
            "历史主题匹配置信度": resolution.confidence,
            "历史主题匹配原因": resolution.reason,
            "主题证据版本": resolution.evidence_version,
            "本次新增证据数": resolution.added_member_count,
            "本次重复证据数": resolution.duplicate_member_count,
        }
    )


def _cluster_only_topic_row(
    topic_id: str,
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = _topic_query(rows)
    source_ids = list(
        dict.fromkeys(
            _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
            for row in rows
            if _clean_text(row.get("数据ID")) or _clean_text(row.get("工单ID"))
        )
    )
    work_order_ids = list(
        dict.fromkeys(
            _original_work_order_id_for_row(row)
            for row in rows
            if _original_work_order_id_for_row(row)
        )
    )
    core_questions = list(
        dict.fromkeys(
            _member_cluster_question(row)
            for row in rows
            if _member_cluster_question(row)
        )
    )
    decisions = list(
        dict.fromkeys(
            _clean_text(row.get("_聚类决策"))
            for row in rows
            if _clean_text(row.get("_聚类决策"))
        )
    )
    providers = list(
        dict.fromkeys(
            _clean_text(row.get("_聚类裁决提供方"))
            for row in rows
            if _clean_text(row.get("_聚类裁决提供方"))
        )
    )
    reasons = list(
        dict.fromkeys(
            _clean_text(row.get("_聚类裁决原因"))
            for row in rows
            if _clean_text(row.get("_聚类裁决原因"))
        )
    )
    raw_title = next(
        (
            _clean_text(row.get("_聚类主题标题"))
            for row in rows
            if _clean_text(row.get("_聚类主题标题"))
        ),
        "",
    )
    # A source row may contain multiple human questions.  A model cluster name
    # can then leak the sibling question into this atomic topic (for example,
    # using “更换表带” for the separate “充电线缺失” topic).  Prefer the
    # current atomic object's structured title whenever the source is explicitly
    # multi-topic; single-topic rows keep the existing model-title path.
    structured_title = _structured_topic_question_title(query, rows)
    # The clustering review sheet is an operational result, not a place to
    # preserve a model's conversational label.  Build its visible title from
    # the resolved business fields first so first-round prompts such as
    # “看一下后摄” or “无帮助” cannot become topic names.  Keep the model
    # title only as a fallback for rows whose structured fields are incomplete.
    title = structured_title or (
        raw_title
        if not _topic_has_multiple_targets(query)
        and _cluster_title_matches_topic(raw_title, query)
        else _untranscribed_topic_title(query, rows)
    )
    return {
        "聚类主题ID": topic_id,
        "聚类主题": title,
        "主题样本数": len(rows),
        "回收业务层级": _business_line_for_row(rows[0]),
        "产品类型": _topic_product_type(query, rows),
        "主题对象/部位": _clean_text(query.get("对象/部位")),
        "主题异常现象": _clean_text(query.get("异常现象")),
        "主题解题方式": _clean_text(query.get("解题方式")),
        "主题来源记录ID": "\n".join(source_ids),
        "主题工单ID": "\n".join(work_order_ids),
        "成员核心问题": "\n\n".join(core_questions),
        "主题聚类键": " | ".join(key),
        "聚类决策": "；".join(decisions),
        "聚类提供方": "；".join(providers),
        "聚类原因": "；".join(reasons),
        "聚类准入状态": _clean_text((admission or {}).get("status")),
        "聚类准入置信度": (admission or {}).get("confidence", ""),
        "聚类准入原因": _clean_text((admission or {}).get("reason")),
        "是否重点复核": (
            "是"
            if len(rows) == 1
            or any(
                bool(row.get("_原子需要复核"))
                or bool(row.get("_聚类需要复核"))
                or bool(_clean_text(row.get("人工优先复核原因")))
                for row in rows
            )
            else "否"
        ),
        "人工聚类判断": "",
        "人工备注": "",
    }


def build_topic_review_rows(
    feature_rows: list[dict[str, Any]],
    standard_catalog: list[StandardCatalogItem] | None = None,
    classification_catalog: (
        Iterable[ClassificationCatalogItem] | None
    ) = None,
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
    topic_model_call_limit: int | None = None,
    direct_mimo_progress_path: Path | None = None,
    topic_progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    topic_standard_retriever: Callable[
        [str, list[dict[str, Any]], dict[str, Any]],
        tuple[list[tuple[StandardCatalogItem, float]], dict[str, Any]],
    ] | None = None,
    require_standard_match: bool = False,
    transcribe_all_admitted_topics: bool = False,
    cluster_only: bool = False,
    enforce_cluster_admission: bool = False,
    cluster_admission_min_confidence: float = DEFAULT_CLUSTER_ADMISSION_MIN_CONFIDENCE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Cluster record features, then generate one auditable draft per topic."""
    if classification_catalog is None:
        classification_catalog_path = _clean_text(
            os.getenv("ANSWER_HUB_CLUSTERING_CLASSIFICATION_CATALOG")
        )
        classification_catalog_items = (
            load_classification_catalog(classification_catalog_path)
            if classification_catalog_path
            else ()
        )
    else:
        classification_catalog_items = tuple(classification_catalog)
    evidence_gap_rows: list[dict[str, Any]] = []
    eligible_topic_rows: list[dict[str, Any]] = []
    if topic_model_call_limit is None:
        try:
            topic_model_call_limit = int(
                os.getenv(
                    "ANSWER_HUB_TOPIC_MODEL_CALL_LIMIT",
                    str(DEFAULT_TOPIC_MODEL_CALL_LIMIT),
                )
            )
        except ValueError:
            topic_model_call_limit = DEFAULT_TOPIC_MODEL_CALL_LIMIT
    topic_model_call_limit = max(0, topic_model_call_limit)
    topic_model_calls = 0
    topic_model_budget_skipped = 0

    def reserve_topic_model_call() -> bool:
        nonlocal topic_model_calls
        if topic_model_calls >= topic_model_call_limit:
            return False
        topic_model_calls += 1
        return True

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
                classification_catalog=classification_catalog_items,
                progress_path=direct_mimo_progress_path,
                progress_callback=topic_progress_callback,
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

    if cluster_only:
        cluster_rows = []
        for key, rows in topic_groups:
            admission = _cluster_topic_admission(
                rows,
                meta,
                enabled=True,
                min_confidence=cluster_admission_min_confidence,
            )
            cluster_rows.append(
                _cluster_only_topic_row(
                    _topic_id(key),
                    key,
                    rows,
                    admission,
                )
            )
        if clustering_meta is not None:
            clustering_meta.update(
                {
                    "cluster_only": True,
                    "topic_model_call_limit": 0,
                    "topic_model_calls": 0,
                    "topic_model_budget_skipped": 0,
                    "cluster_admission_enforced": False,
                    "cluster_admission_policy_version": (
                        CLUSTER_ADMISSION_POLICY_VERSION
                    ),
                    "cluster_admission_min_confidence": (
                        cluster_admission_min_confidence
                    ),
                }
            )
        return cluster_rows, [], evidence_gap_rows, []

    topic_rows: list[dict[str, Any]] = []
    source_mapping_rows: list[dict[str, Any]] = []
    pending_cluster_rows: list[dict[str, Any]] = []
    product_conflict_groups = [
        (key, rows)
        for key, rows in topic_groups
        if any(bool(row.get("_原子品类冲突")) for row in rows)
    ]
    if product_conflict_groups:
        topic_groups = [
            (key, rows)
            for key, rows in topic_groups
            if not any(bool(row.get("_原子品类冲突")) for row in rows)
        ]
        for key, rows in product_conflict_groups:
            pending_cluster_rows.extend(
                _pending_cluster_source_rows(
                    _topic_id(key),
                    key,
                    rows,
                    (
                        "模型识别品类与源数据品类冲突；"
                        "该原子问题不得进入当前品类的候选价值复核队列。"
                    ),
                    status="pending_product_conflict_review",
                )
            )
        meta["export_blocked_product_conflict_topics"] = len(
            product_conflict_groups
        )
        meta["export_blocked_product_conflict_rows"] = sum(
            len(rows) for _key, rows in product_conflict_groups
        )
    catalog = standard_catalog or []
    client = mimo_client if use_mimo else None
    if client is None and use_mimo:
        client = MimoClient.from_env()

    def resolve_topic_standard_matches(
        topic_id: str,
        rows: list[dict[str, Any]],
        query: dict[str, Any],
    ) -> tuple[list[tuple[StandardCatalogItem, float]], dict[str, Any]]:
        def usable_matches(
            candidates: list[tuple[StandardCatalogItem, float]],
        ) -> list[tuple[StandardCatalogItem, float]]:
            return [
                (item, score)
                for item, score in candidates
                if is_active_standard(item.status)
                and all(
                    _clean_text(value)
                    for value in (
                        item.standard_id,
                        item.title,
                        item.standard_path,
                        item.scope,
                        item.response_snippet,
                    )
                )
            ]

        headquarters_matches: list[tuple[StandardCatalogItem, float]] = []
        headquarters_audit: dict[str, Any] = {}
        if topic_standard_retriever is not None:
            try:
                headquarters_matches, headquarters_audit = topic_standard_retriever(
                    topic_id,
                    rows,
                    query,
                )
                headquarters_matches = usable_matches(headquarters_matches)
            except Exception as exc:
                headquarters_audit = {
                    "source": "headquarters_standard",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            headquarters_matches, rejected_headquarters = _validated_standard_matches(
                query,
                headquarters_matches,
            )
            if rejected_headquarters:
                headquarters_audit = {
                    **headquarters_audit,
                    "rejected_standard_matches": rejected_headquarters,
                    "standard_gate_status": "rejected_candidates",
                }
            if headquarters_matches:
                return headquarters_matches, headquarters_audit

        business_line = business_line_from_record(rows[0]) if rows else None
        if business_line and not business_line.product_categories_configured:
            return [], {
                **headquarters_audit,
                "source": "aggregate_product_no_local_standard",
                "status": "local_catalog_not_applicable",
            }

        local_matches = (
            retrieve_standard_matches(query, catalog, top_k=20)
            if catalog
            else []
        )
        local_matches = usable_matches(local_matches)
        local_matches, rejected_local = _validated_standard_matches(
            query,
            local_matches,
        )
        local_matches = local_matches[:5]
        if local_matches:
            local_versions = _merge_unique_text(
                [item.version for item, _score in local_matches],
                separator="；",
            )
            return local_matches, {
                "source": "local_quality_standard",
                "status": (
                    "fallback_match"
                    if topic_standard_retriever is not None
                    else "success"
                ),
                "knowledge_version": local_versions,
                "headquarters_status": _clean_text(
                    headquarters_audit.get("status")
                ),
                "headquarters_knowledge_version": _clean_text(
                    headquarters_audit.get("knowledge_version")
                ),
                "headquarters_error": _clean_text(
                    headquarters_audit.get("error")
                ),
                "rejected_standard_matches": (
                    rejected_local
                    + list(headquarters_audit.get("rejected_standard_matches") or [])
                ),
                "standard_gate_status": "accepted",
            }
        if topic_standard_retriever is not None:
            return [], {
                **headquarters_audit,
                "rejected_standard_matches": (
                    list(headquarters_audit.get("rejected_standard_matches") or [])
                    + rejected_local
                ),
                "standard_gate_status": "rejected_all_candidates"
                if rejected_local
                or headquarters_audit.get("rejected_standard_matches")
                else _clean_text(headquarters_audit.get("standard_gate_status")),
            }
        return [], {
            "source": "local_quality_standard",
            "status": "no_match",
            "rejected_standard_matches": rejected_local,
            "standard_gate_status": "rejected_all_candidates"
            if rejected_local
            else "no_candidate",
        }

    auto_review_policy = AutoReviewPolicy.from_env()
    cluster_admission_admitted_topics = 0
    cluster_admission_pending_topics = 0
    cluster_admission_provisional_topics = 0
    cluster_admission_provisional_candidates = 0
    cluster_admission_admitted_source_rows = 0
    cluster_admission_pending_source_rows = 0
    incremental_registry = (
        TopicRegistry(audit_store)
        if audit_store and enforce_cluster_admission
        else None
    )
    incremental_created_topics = 0
    incremental_merged_topics = 0
    incremental_pending_topics = 0
    incremental_added_members = 0
    incremental_duplicate_members = 0
    topic_groups_before_known_equivalence_merge = len(topic_groups)
    topic_groups = _merge_known_equivalent_topic_groups(topic_groups)
    meta["known_equivalent_topic_merges"] = (
        topic_groups_before_known_equivalence_merge - len(topic_groups)
    )
    execution_groups: list[
        tuple[tuple[str, ...], list[dict[str, Any]], dict[str, Any], bool]
    ] = []
    for key, rows in topic_groups:
        admission = _cluster_topic_admission(
            rows,
            meta,
            enabled=enforce_cluster_admission,
            min_confidence=cluster_admission_min_confidence,
        )
        if admission.get("provisional"):
            cluster_admission_provisional_topics += 1
            provisional_admission = _provisional_singleton_admission(admission)
            for row_index, row in enumerate(rows, start=1):
                execution_groups.append(
                    (
                        _provisional_singleton_key(key, row, row_index),
                        [row],
                        provisional_admission,
                        True,
                    )
                )
            continue
        execution_groups.append((key, rows, admission, False))
    topic_group_count = len(execution_groups)
    transcription_topics_started = 0

    if topic_progress_callback:
        topic_progress_callback(
            "主题聚类完成，正在执行聚类准入、历史主题归并与价值分类。",
            {
                "pipeline_phase": "topic_enrichment",
                "topic_groups_completed": 0,
                "topic_groups_total": topic_group_count,
            },
        )

    for topic_group_index, (
        key,
        rows,
        cluster_admission,
        provisional_singleton,
    ) in enumerate(execution_groups, start=1):
        if topic_progress_callback:
            topic_progress_callback(
                "正在执行聚类准入、历史主题归并与价值分类。",
                {
                    "pipeline_phase": "topic_enrichment",
                    "topic_groups_completed": topic_group_index - 1,
                    "topic_groups_total": topic_group_count,
                    "transcription_topics_started": (
                        transcription_topics_started
                    ),
                },
            )
        topic_id = _topic_id(key)
        if not cluster_admission["admitted"] and not provisional_singleton:
            cluster_admission_pending_topics += 1
            cluster_admission_pending_source_rows += len(rows)
            pending_cluster_rows.extend(
                _pending_cluster_source_rows(
                    topic_id,
                    key,
                    rows,
                    _clean_text(cluster_admission.get("reason")),
                    status="pending_cluster_review",
                    admission=cluster_admission,
                )
            )
            continue
        if provisional_singleton:
            cluster_admission_provisional_candidates += 1
        else:
            cluster_admission_admitted_topics += 1
            cluster_admission_admitted_source_rows += len(rows)
        incremental_resolution: TopicResolution | None = None
        if incremental_registry is not None and not provisional_singleton:
            try:
                incremental_resolution = incremental_registry.integrate(
                    proposed_topic_id=topic_id,
                    topic_key=key,
                    rows=rows,
                    run_id=run_id or "",
                )
            except ValueError as exc:
                incremental_pending_topics += 1
                pending_cluster_rows.extend(
                    _pending_cluster_source_rows(
                        topic_id,
                        key,
                        rows,
                        f"历史主题归并失败：{exc}",
                        status="pending_historical_topic_review",
                        admission=cluster_admission,
                    )
                )
                continue
            if incremental_resolution.requires_review:
                incremental_pending_topics += 1
                historical_pending_rows = _pending_cluster_source_rows(
                    topic_id,
                    key,
                    rows,
                    incremental_resolution.reason,
                    status="pending_historical_topic_review",
                    admission=cluster_admission,
                )
                for pending_row in historical_pending_rows:
                    pending_row.update(
                        {
                            "历史主题匹配ID": (
                                incremental_resolution.historical_topic_id
                            ),
                            "历史主题匹配置信度": (
                                incremental_resolution.confidence
                            ),
                            "历史主题匹配原因": (
                                incremental_resolution.reason
                            ),
                        }
                    )
                pending_cluster_rows.extend(historical_pending_rows)
                continue
            topic_id = incremental_resolution.topic_id
            key = incremental_resolution.topic_key
            rows = incremental_resolution.rows
            incremental_added_members += (
                incremental_resolution.added_member_count
            )
            incremental_duplicate_members += (
                incremental_resolution.duplicate_member_count
            )
            if incremental_resolution.matched_existing:
                incremental_merged_topics += 1
            else:
                incremental_created_topics += 1
        evidence_package = _topic_evidence_package(rows)
        model_evidence_package = _topic_model_evidence_package(
            evidence_package
        )
        representative_facts = evidence_package.get(
            "representative_facts"
        ) or []
        query = _retarget_battery_user_judgment_query(
            _topic_query(rows, evidence_package)
        )
        topic_stage_input = _topic_stage_payload(
            topic_id,
            rows,
            use_standard_references=use_standard_references,
        )
        topic_stage = _rule_topic_stage_classification(topic_stage_input)
        topic_stage_provider = "stage-rule"
        topic_stage_model = "topic-stage-rule-v1"
        topic_stage_prompt = ""
        topic_stage_status = "topic_stage_classified_rule"
        topic_stage_error = ""
        topic_stage_run_id = uuid.uuid4().hex
        topic_stage_request_audit: dict[str, Any] = {
            "topic_id": topic_id,
            "topic": topic_stage_input,
        }
        topic_stage_response_audit: dict[str, Any] = {}
        apply_stage_guard = True
        if client and hasattr(client, "classify_topic_stage"):
            topic_stage_provider = "mimo"
            topic_stage_model = client.config.model
            topic_stage_prompt = TOPIC_STAGE_PROMPT_VERSION
            if reserve_topic_model_call():
                try:
                    topic_stage_result = client.classify_topic_stage(topic_stage_input)
                    topic_stage = topic_stage_result.candidate
                    topic_stage_request_audit = topic_stage_result.request_audit
                    topic_stage_response_audit = topic_stage_result.response_audit
                    topic_stage_status = "topic_stage_classified_model"
                except Exception as exc:
                    topic_stage_status = "topic_stage_classification_failed"
                    topic_stage_error = f"{type(exc).__name__}: {exc}"
            else:
                topic_model_budget_skipped += 1
                topic_stage = _topic_model_budget_pending_stage(
                    topic_stage,
                    topic_model_call_limit,
                )
                topic_stage_provider = "model-budget"
                topic_stage_status = "topic_stage_skipped_model_budget"
                topic_stage_error = (
                    f"主题模型调用达到上限 {topic_model_call_limit}，"
                    "已转人工优先审核。"
                )
        elif client:
            apply_stage_guard = False
            topic_stage.update(
                {
                    "knowledge_value": "值得沉淀",
                    "value_reason": "当前调用方未提供主题价值分类能力，按兼容模式继续转写并强制人工复核。",
                    "needs_human_review": True,
                }
            )
            topic_stage_provider = "legacy-compatible"
            topic_stage_model = _clean_text(getattr(client.config, "model", ""))
            topic_stage_status = "topic_stage_legacy_compatible"
        matches: list[tuple[StandardCatalogItem, float]] = []
        standard_retrieval_audit: dict[str, Any] = {}
        standard_retrieval_attempted = False
        if use_standard_references:
            standard_retrieval_attempted = True
            matches, standard_retrieval_audit = resolve_topic_standard_matches(
                topic_id,
                rows,
                query,
            )
        standard_missing_requires_review = bool(
            standard_retrieval_attempted and not matches
        )
        single_battery_health_observation = (
            _is_single_battery_health_observation_topic(query)
        )
        if apply_stage_guard:
            topic_stage = _apply_topic_stage_guard(
                topic_stage_input,
                topic_stage,
                has_authoritative_standard=bool(matches),
            )
        if single_battery_health_observation:
            topic_stage = {
                **topic_stage,
                "knowledge_value": "不值得沉淀",
                "value_reason": _safe_join(
                    [
                        _clean_text(topic_stage.get("value_reason")),
                        (
                            "仅包含单次电池健康度数值，"
                            "只能用于当前案例选档，未提供新的通用步骤、边界或例外，"
                            "不生成可复用知识。"
                        ),
                    ],
                    "；",
                ),
                "reusable_knowledge": (
                    "单次电池健康度数值属于案例观测，不作为独立知识沉淀。"
                ),
                "needs_human_review": True,
            }
        if (
            (transcribe_all_admitted_topics or provisional_singleton)
            and _clean_text(topic_stage.get("knowledge_value")) != "值得沉淀"
            and not single_battery_health_observation
        ):
            topic_stage = {
                **topic_stage,
                "needs_human_review": True,
                "value_reason": _safe_join(
                    [
                        _clean_text(topic_stage.get("value_reason")),
                        (
                            "暂定单主题候选仍生成知识草稿，"
                            "交由人工价值复核决定是否沉淀。"
                            if provisional_singleton
                            else "仍生成知识候选，交由人工价值复核决定是否沉淀。"
                        ),
                    ],
                    "；",
                ),
            }
        elif provisional_singleton:
            topic_stage = {
                **topic_stage,
                "needs_human_review": True,
                "value_reason": _safe_join(
                    [
                        _clean_text(topic_stage.get("value_reason")),
                        "暂定单主题候选，禁止自动合并，必须人工价值复核。",
                    ],
                    "；",
                ),
            }
        incubate_single_case = _should_incubate_single_case_topic(
            topic_stage_input,
            topic_stage,
            use_standard_references=use_standard_references,
        )

        if (
            _clean_text(topic_stage.get("knowledge_value")) != "值得沉淀"
            and not standard_missing_requires_review
            and (
                not transcribe_all_admitted_topics
                or single_battery_health_observation
            )
            and not provisional_singleton
        ):
            topic = _untranscribed_topic_candidate_row(
                topic_id,
                key,
                rows,
                topic_stage,
            )
            _attach_incremental_topic(topic, incremental_resolution)
            _attach_cluster_admission(topic, cluster_admission)
            _attach_topic_stage_classification(
                topic,
                topic_stage,
                provider=topic_stage_provider,
                model_name=topic_stage_model,
                prompt_version=topic_stage_prompt,
                model_run_id=topic_stage_run_id,
                status=topic_stage_status,
                error=topic_stage_error,
                transcription_status="skipped_not_worthy",
            )
            if incubate_single_case:
                pending_reason = _single_case_pending_cluster_reason(topic_stage)
                topic["主题状态"] = "incubating_pending_cluster"
                topic["模型初标原因"] = _safe_join(
                    [topic.get("模型初标原因"), pending_reason],
                    "；",
                )
                topic["校验备注"] = _safe_join(
                    [topic.get("校验备注"), pending_reason],
                    "；",
                )
                pending_cluster_rows.extend(
                    _pending_cluster_source_rows(
                        topic_id,
                        key,
                        rows,
                        pending_reason,
                        admission=cluster_admission,
                    )
                )
            apply_auto_review_annotation(topic, auto_review_policy)
            topic_rows.append(topic)
            source_mapping_rows.extend(
                _topic_source_mapping_rows(topic_id, rows, topic, "")
            )
            if audit_store:
                audit_store.record_model_run(
                    model_run_id=topic_stage_run_id,
                    run_id=run_id or "",
                    record_id=topic_id,
                    provider=topic_stage_provider,
                    model_name=topic_stage_model,
                    prompt_version=topic_stage_prompt,
                    status=topic_stage_status,
                    retrieved_standards=[],
                    request_audit=topic_stage_request_audit,
                    response_audit=topic_stage_response_audit,
                    error=topic_stage_error,
                )
                audit_store.save_candidate(
                    topic_stage_run_id,
                    run_id or "",
                    topic_id,
                    topic,
                )
            continue

        transcription_topics_started += 1
        if topic_progress_callback:
            topic_progress_callback(
                "正在进行知识转写与内容初审。",
                {
                    "pipeline_phase": "knowledge_transcription",
                    "topic_groups_completed": topic_group_index - 1,
                    "topic_groups_total": topic_group_count,
                    "transcription_topics_started": (
                        transcription_topics_started
                    ),
                },
            )
        if require_standard_match and not matches:
            topic_stage = {
                **topic_stage,
                "needs_human_review": True,
                "value_reason": _safe_join(
                    [
                        _clean_text(topic_stage.get("value_reason")),
                        "未命中总部标准，仍转写为经验补充候选并进入人工价值复核。",
                    ],
                    "；",
                ),
            }
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
        failed_model_call_status = ""
        model_run_id = uuid.uuid4().hex
        request_audit: dict[str, Any] = {
            "topic_id": topic_id,
            "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
            "topic_features": query,
            "evidence_summary": _topic_evidence_summary(
                rows,
                evidence_package,
            ),
            "evidence_package": evidence_package,
            "retrieved_standards": _retrieved_standard_rows(matches),
            "standard_retrieval": standard_retrieval_audit,
        }
        response_audit: dict[str, Any] = {}
        if client and hasattr(client, "label_topic"):
            provider = "mimo"
            model_name = client.config.model
            prompt_version = PROMPT_VERSION
            if reserve_topic_model_call():
                try:
                    label_topic = client.label_topic
                    topic_payload = {
                        "topic_id": topic_id,
                        "sample_count": len(rows),
                        "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
                        "features": query,
                        "evidence_summary": _topic_evidence_summary(
                            rows,
                            evidence_package,
                        ),
                        "evidence_package": model_evidence_package,
                        "source_fact_refs": evidence_package.get(
                            "source_fact_refs",
                            [],
                        ),
                        "representative_source_ids": evidence_package.get(
                            "representative_source_ids",
                            [],
                        ),
                        "topic_stage": _clean_text(topic_stage.get("topic_stage")),
                        "knowledge_value": _clean_text(
                            topic_stage.get("knowledge_value")
                        ),
                        "标准依据来源": _clean_text(
                            standard_retrieval_audit.get("source")
                        ),
                        "stage_reason": _clean_text(topic_stage.get("stage_reason")),
                        "value_reason": _clean_text(topic_stage.get("value_reason")),
                        "reusable_knowledge": _clean_text(
                            topic_stage.get("reusable_knowledge")
                        ),
                        "source_conversations": [
                            fact.get("conversation_excerpt")
                            for fact in representative_facts
                            if fact.get("conversation_excerpt")
                        ],
                        "historical_actual_replies": [
                            fact.get("historical_actual_reply")
                            for fact in representative_facts
                            if fact.get("historical_actual_reply")
                        ],
                        "source_conclusions": [
                            fact.get("human_judgment_conclusion")
                            for fact in representative_facts
                            if fact.get("human_judgment_conclusion")
                        ],
                        "human_core_problems": [
                            fact.get("human_core_problem")
                            for fact in representative_facts
                            if fact.get("human_core_problem")
                        ],
                        "case_images": _topic_case_images(
                            rows,
                            evidence_package,
                        ),
                    }
                    label_parameters = inspect.signature(label_topic).parameters
                    if "use_standard_references" in label_parameters:
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
                    request_audit = {
                        **result.request_audit,
                        "standard_retrieval": standard_retrieval_audit,
                    }
                    response_audit = result.response_audit
                    stage_status = "topic_model_labeled"
                    is_generic_case_draft = (
                        not use_standard_references
                        and _topic_draft_is_generic(candidate, rows)
                    )
                    is_standard_case_analysis_draft = (
                        use_standard_references
                        and _topic_draft_is_case_analysis(candidate)
                    )
                    if (
                        (is_generic_case_draft or is_standard_case_analysis_draft)
                        and "retry_reason" in label_parameters
                    ):
                        retry_reason = (
                            "上一版草稿是对单个案例或本次会话的案例分析，"
                            "不是可复用知识。请依据本次选用的标准依据重写："
                            "标题改为可检索的判定主题；正文以适用范围、标准条件、"
                            "核验方法和判定/处理边界为主；推荐回复按该规则答疑。"
                            "不得复述回收师、用户、本次会话、现场图片或处理结论。"
                            "案例事实只能作为审核追溯证据。"
                            if is_standard_case_analysis_draft
                            else (
                                "上一版正文只有通用核验模板，未总结来源聊天、"
                                "历史实际回复或判定结论。请重写为案例内容分析："
                                "必须写明具体对象/代际/来源问题、检查项或触发条件、"
                                "实际结论或处理方式，以及不能适用时的边界。"
                            )
                        )
                        retry_kwargs: dict[str, Any] = {
                            "retry_reason": retry_reason,
                        }
                        if "use_standard_references" in label_parameters:
                            retry_kwargs["use_standard_references"] = (
                                use_standard_references
                            )
                        if reserve_topic_model_call():
                            retry_result = label_topic(
                                topic_payload,
                                matches,
                                **retry_kwargs,
                            )
                            retry_candidate = retry_result.candidate
                            if (
                                not use_standard_references
                                and _candidate_contains_standard_reference(retry_candidate)
                            ):
                                raise MimoError(
                                    "无标准引用模式检测到重写草稿包含标准引用"
                                )
                            candidate = retry_candidate
                            request_audit = {
                                **retry_result.request_audit,
                                "standard_retrieval": standard_retrieval_audit,
                            }
                            response_audit = retry_result.response_audit
                            stage_status = "topic_model_rewritten_for_evidence"
                        else:
                            topic_model_budget_skipped += 1
                            stage_status = "topic_model_rewrite_skipped_budget"
                            model_error = (
                                f"主题模型调用达到上限 {topic_model_call_limit}，"
                                "未执行通用草稿重写，转人工优先审核。"
                            )
                except Exception as exc:
                    (
                        stage_status,
                        failed_model_call_status,
                    ) = _topic_model_failure_status(exc)
                    model_error = f"{type(exc).__name__}: {exc}"
            else:
                topic_model_budget_skipped += 1
                stage_status = "topic_model_skipped_budget"
                model_error = (
                    f"主题模型调用达到上限 {topic_model_call_limit}，"
                    "已使用规则草稿并转人工优先审核。"
                )
        else:
            model_error = "未配置 MiMo，使用主题级规则草稿。"

        if stage_status == "topic_model_validation_failed" or (
            stage_status == "topic_model_call_failed"
            and not matches
        ):
            topic = _failed_topic_transcription_row(
                topic_id,
                key,
                rows,
                topic_stage,
                provider=provider,
                model_name=model_name,
                prompt_version=prompt_version,
                model_run_id=model_run_id,
                transcription_status=stage_status,
                model_call_status=failed_model_call_status,
                error=model_error,
                matches=matches,
                use_standard_references=use_standard_references,
            )
            _attach_incremental_topic(topic, incremental_resolution)
            _attach_cluster_admission(topic, cluster_admission)
            _attach_topic_stage_classification(
                topic,
                topic_stage,
                provider=topic_stage_provider,
                model_name=topic_stage_model,
                prompt_version=topic_stage_prompt,
                model_run_id=topic_stage_run_id,
                status=topic_stage_status,
                error=topic_stage_error,
                transcription_status=stage_status,
            )
            topic = _enforce_standard_reference_consistency(
                topic,
                use_standard_references=use_standard_references,
            )
            apply_auto_review_annotation(topic, auto_review_policy)
            topic_rows.append(topic)
            source_mapping_rows.extend(
                _topic_source_mapping_rows(
                    topic_id,
                    rows,
                    topic,
                    model_run_id,
                )
            )
            if audit_store:
                audit_store.record_model_run(
                    model_run_id=topic_stage_run_id,
                    run_id=run_id or "",
                    record_id=topic_id,
                    provider=topic_stage_provider,
                    model_name=topic_stage_model,
                    prompt_version=topic_stage_prompt,
                    status=topic_stage_status,
                    retrieved_standards=[],
                    request_audit=topic_stage_request_audit,
                    response_audit=topic_stage_response_audit,
                    error=topic_stage_error,
                )
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
                audit_store.save_candidate(
                    model_run_id,
                    run_id or "",
                    topic_id,
                    topic,
                )
            continue

        if (
            not use_standard_references
            and _topic_draft_is_generic(candidate, rows)
            and not _topic_has_unavailable_required_images(rows)
            and not provisional_singleton
        ):
            topic_stage = _manual_pending_topic_stage(topic_stage)
            topic = _untranscribed_topic_candidate_row(
                topic_id,
                key,
                rows,
                topic_stage,
            )
            _attach_incremental_topic(topic, incremental_resolution)
            _attach_cluster_admission(topic, cluster_admission)
            _attach_topic_stage_classification(
                topic,
                topic_stage,
                provider=topic_stage_provider,
                model_name=topic_stage_model,
                prompt_version=topic_stage_prompt,
                model_run_id=topic_stage_run_id,
                status=topic_stage_status,
                error=topic_stage_error,
                transcription_status="skipped_generic_draft",
            )
            apply_auto_review_annotation(topic, auto_review_policy)
            topic_rows.append(topic)
            source_mapping_rows.extend(
                _topic_source_mapping_rows(
                    topic_id,
                    rows,
                    topic,
                    model_run_id,
                )
            )
            if audit_store:
                audit_store.record_model_run(
                    model_run_id=topic_stage_run_id,
                    run_id=run_id or "",
                    record_id=topic_id,
                    provider=topic_stage_provider,
                    model_name=topic_stage_model,
                    prompt_version=topic_stage_prompt,
                    status=topic_stage_status,
                    retrieved_standards=[],
                    request_audit=topic_stage_request_audit,
                    response_audit=topic_stage_response_audit,
                    error=topic_stage_error,
                )
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
                    error=_safe_join(
                        [
                            model_error,
                            "转写正文只有通用模板，已转为人工价值复核。",
                        ],
                        "；",
                    ),
                )
                audit_store.save_candidate(
                    model_run_id,
                    run_id or "",
                    topic_id,
                    topic,
                )
            continue

        topic = _topic_candidate_row(
            topic_id, key, rows, matches, candidate, provider, model_name, prompt_version, model_run_id,
            model_error, stage_status, min_confidence,
            use_standard_references=use_standard_references,
            standard_basis_source=_clean_text(
                standard_retrieval_audit.get("source")
            ),
        )
        if (
            _clean_text(topic.get("模型调用状态")) == "model_success"
            and _clean_text(topic.get("模型输出校验状态")) == "passed"
            and _clean_text(topic.get("模型质量状态")) == "failed"
        ):
            stage_status = "topic_model_quality_failed"
            topic["模型阶段状态"] = stage_status
        _attach_incremental_topic(topic, incremental_resolution)
        _attach_cluster_admission(topic, cluster_admission)
        if provisional_singleton:
            _attach_provisional_singleton_candidate(
                topic,
                cluster_admission,
            )
        _attach_topic_stage_classification(
            topic,
            topic_stage,
            provider=topic_stage_provider,
            model_name=topic_stage_model,
            prompt_version=topic_stage_prompt,
            model_run_id=topic_stage_run_id,
            status=topic_stage_status,
            error=topic_stage_error,
            transcription_status=stage_status,
        )
        topic["主题标准检索来源"] = _clean_text(
            standard_retrieval_audit.get("source")
        )
        topic["主题标准检索状态"] = _clean_text(
            standard_retrieval_audit.get("status")
        )
        topic["主题标准快照版本"] = _clean_text(
            standard_retrieval_audit.get("knowledge_version")
        )
        topic["主题标准检索错误"] = _clean_text(
            standard_retrieval_audit.get("error")
            or standard_retrieval_audit.get("headquarters_error")
        )
        topic["知识分类"] = knowledge_category_from_topic_stage(
            topic.get("主题问题分类"),
            candidate.get("knowledge_form"),
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
            matches
            if topic_standard_retriever is not None
            else (
                retrieve_standard_matches(review_query, catalog, top_k=5)
                if use_standard_references
                else []
            )
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
            "standard_retrieval": standard_retrieval_audit,
        }
        review_response_audit: dict[str, Any] = {}
        if client and hasattr(client, "review_topic"):
            initial_review_provider = "mimo"
            initial_review_model = client.config.model
            initial_review_prompt = TOPIC_REVIEW_PROMPT_VERSION
            if reserve_topic_model_call():
                try:
                    review_topic = client.review_topic
                    review_args = [
                        {
                            "topic_id": topic_id,
                            "sample_count": len(rows),
                            "source_record_ids": [_clean_text(row.get("数据ID")) for row in rows],
                            "features": query,
                            "evidence_summary": _topic_evidence_summary(
                                rows,
                                evidence_package,
                            ),
                            "evidence_package": model_evidence_package,
                            "source_fact_refs": evidence_package.get(
                                "source_fact_refs",
                                [],
                            ),
                            "representative_source_ids": evidence_package.get(
                                "representative_source_ids",
                                [],
                            ),
                            "case_images": _topic_case_images(
                                rows,
                                evidence_package,
                            ),
                            "topic_stage": _clean_text(topic_stage.get("topic_stage")),
                            "knowledge_value": _clean_text(
                                topic_stage.get("knowledge_value")
                            ),
                            "stage_reason": _clean_text(
                                topic_stage.get("stage_reason")
                            ),
                            "value_reason": _clean_text(
                                topic_stage.get("value_reason")
                            ),
                            "reusable_knowledge": _clean_text(
                                topic_stage.get("reusable_knowledge")
                            ),
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
                    review_request_audit = {
                        **review_result.request_audit,
                        "standard_retrieval": standard_retrieval_audit,
                    }
                    review_response_audit = review_result.response_audit
                    initial_review_status = "topic_initial_reviewed_model"
                except Exception as exc:
                    initial_review_status = "topic_initial_review_failed"
                    initial_review_error = f"{type(exc).__name__}: {exc}"
            else:
                topic_model_budget_skipped += 1
                initial_review_provider = "model-budget"
                initial_review_status = "topic_initial_review_skipped_budget"
                initial_review_error = (
                    f"主题模型调用达到上限 {topic_model_call_limit}，"
                    "已使用规则初标并转人工优先审核。"
                )
        else:
            initial_review_error = "未配置支持主题初标的 MiMo，使用规则模型初标。"
        initial_review = {
            **initial_review,
            "knowledge_value": _clean_text(topic_stage.get("knowledge_value"))
            or "值得沉淀",
        }
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
        topic = _enforce_standard_reference_consistency(
            topic,
            use_standard_references=use_standard_references,
        )
        apply_auto_review_annotation(topic, auto_review_policy)
        topic_rows.append(topic)
        if audit_store:
            audit_store.record_model_run(
                model_run_id=topic_stage_run_id,
                run_id=run_id or "",
                record_id=topic_id,
                provider=topic_stage_provider,
                model_name=topic_stage_model,
                prompt_version=topic_stage_prompt,
                status=topic_stage_status,
                retrieved_standards=[],
                request_audit=topic_stage_request_audit,
                response_audit=topic_stage_response_audit,
                error=topic_stage_error,
            )
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

        source_mapping_rows.extend(
            _topic_source_mapping_rows(
                topic_id,
                rows,
                topic,
                model_run_id,
            )
        )
    if clustering_meta is not None:
        clustering_meta.update(
            {
                "topic_model_call_limit": topic_model_call_limit,
                "topic_model_calls": topic_model_calls,
                "topic_model_budget_skipped": topic_model_budget_skipped,
                "cluster_admission_enforced": enforce_cluster_admission,
                "cluster_admission_policy_version": (
                    CLUSTER_ADMISSION_POLICY_VERSION
                ),
                "cluster_admission_min_confidence": (
                    cluster_admission_min_confidence
                ),
                "cluster_admission_admitted_topics": (
                    cluster_admission_admitted_topics
                ),
                "cluster_admission_pending_topics": (
                    cluster_admission_pending_topics
                ),
                "cluster_admission_provisional_topics": (
                    cluster_admission_provisional_topics
                ),
                "cluster_admission_provisional_candidates": (
                    cluster_admission_provisional_candidates
                ),
                "cluster_admission_admitted_source_rows": (
                    cluster_admission_admitted_source_rows
                ),
                "cluster_admission_pending_source_rows": (
                    cluster_admission_pending_source_rows
                ),
                "incremental_topic_registry_enabled": bool(
                    incremental_registry
                ),
                "incremental_created_topics": incremental_created_topics,
                "incremental_merged_topics": incremental_merged_topics,
                "incremental_pending_topics": incremental_pending_topics,
                "incremental_added_members": incremental_added_members,
                "incremental_duplicate_members": (
                    incremental_duplicate_members
                ),
            }
        )
    return topic_rows, source_mapping_rows, evidence_gap_rows, pending_cluster_rows


def _topic_guide_sheet() -> tuple[list[str], list[dict[str, Any]]]:
    rows = [
        {"说明": "topic_review_queue 是 1～N 个原子问题形成的主题级候选，不按固定两两配对。"},
        {"说明": "单主题会话只保留1个原子问题；只有明确多主题会话才拆成2～3个；不确定会话最多保留1个暂定原子问题。"},
        {"说明": "完整会话或有可用现场图片的记录可形成主题候选；证据不足的记录进入 evidence_gap_rows。"},
        {"说明": "主题聚类后先执行准入门禁；仅高置信、无风险且业务层级和产品品类一致的聚类允许自动合并。"},
        {"说明": "业务层级或产品品类冲突、未识别的主题进入 pending_cluster_rows；低置信、模型要求复核、规则降级或冲突拆分时，按原子问题生成暂定单主题候选并继续转写。"},
        {"说明": "新批次会先匹配同一回收业务层级、同一产品品类的历史主题；高置信匹配追加原工单和证据并复用原主题ID，模糊匹配进入人工确认。"},
        {"说明": "清晰的单成员主题允许通过聚类准入；单成员本身不是拦截条件。"},
        {"说明": "聚类准入后再标注问题分类和是否值得沉淀；所有通过聚类准入的主题都会生成候选草稿，沉淀价值由人工复核决定。"},
        {"说明": "topic_model_drafts 保存所有通过聚类准入或暂定单主题候选的转写草稿、案例图和推荐回复；未命中总部标准的草稿标记为经验补充候选并重点复核。"},
        {"说明": "知识转写后再执行模型内容质量初标；内容质量初标不得重新修改主题沉淀价值。"},
        {"说明": "发给组员时复核“人工主题问题分类、是否值得沉淀、是否可用、如何修改、问题反馈”；不值得沉淀的主题不进入批量送审。"},
        {"说明": "验证模式下组员标注用于计算准确率；生产自动审核启用后，模型通过候选替代第三部分人工复标，风险候选进入人工例外队列。"},
        {"说明": "配置 CZ 标准检索后，实际引用的总部或本地质检标准会写入关联标准项并保留版本；未命中时仍按来源事实生成经验补充候选，关联标准项保持为空并重点复核。"},
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
    direct_mimo_progress_path: Path | None = None,
    topic_progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    topic_standard_retriever: Callable[
        [str, list[dict[str, Any]], dict[str, Any]],
        tuple[list[tuple[StandardCatalogItem, float]], dict[str, Any]],
    ] | None = None,
    require_standard_match: bool = False,
    transcribe_all_admitted_topics: bool = False,
    enforce_cluster_admission: bool = False,
    cluster_admission_min_confidence: float = DEFAULT_CLUSTER_ADMISSION_MIN_CONFIDENCE,
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
        direct_mimo_progress_path=direct_mimo_progress_path,
        topic_progress_callback=topic_progress_callback,
        topic_standard_retriever=topic_standard_retriever,
        require_standard_match=require_standard_match,
        transcribe_all_admitted_topics=transcribe_all_admitted_topics,
        enforce_cluster_admission=enforce_cluster_admission,
        cluster_admission_min_confidence=cluster_admission_min_confidence,
    )
    model_draft_rows = [
        {
            "主题ID": _clean_text(topic.get("主题ID")),
            "主题问题分类": _clean_text(topic.get("主题问题分类")),
            "主题沉淀价值": _clean_text(topic.get("主题沉淀价值")),
            "转写状态": _clean_text(topic.get("主题转写状态")),
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
                SOURCE_COLUMNS
                + PREPROCESS_COLUMNS
                + TOPIC_FEATURE_COLUMNS
                + [
                    "主题ID",
                    "主题聚类键",
                    "待聚合状态",
                    "待聚合原因",
                    "历史主题匹配ID",
                    "历史主题匹配置信度",
                    "历史主题匹配原因",
                    *CLUSTER_ADMISSION_COLUMNS,
                    "人工聚类判断",
                    "人工备注",
                ],
                pending_cluster_rows,
            ),
            "excluded_rows": (SOURCE_COLUMNS + ["排除原因"], excluded_rows),
            "guide": (guide_columns, guide_rows),
        },
        workbook_path,
    )
    return {
        "topic_rows": len(topic_rows),
        "ai_result_rows": sum(
            bool(_clean_text(row.get("ai_result")))
            for row in preprocessed_rows
        ),
        "ai_result_parsed_rows": sum(
            _clean_text(row.get("AI结果解析状态")) == "已解析"
            for row in preprocessed_rows
        ),
        "ai_result_unrecognized_rows": sum(
            _clean_text(row.get("AI结果解析状态")) == "未识别"
            for row in preprocessed_rows
        ),
        "ai_result_conflict_rows": sum(
            bool(_clean_text(row.get("AI结果冲突字段")))
            for row in preprocessed_rows
        ),
        "ai_result_conflict_fields": dict(
            Counter(
                field
                for row in preprocessed_rows
                for field in _clean_text(row.get("AI结果冲突字段")).split("、")
                if field
            )
        ),
        "topic_stage_classified_rows": sum(
            bool(_clean_text(row.get("主题问题分类")))
            for row in topic_rows
        ),
        "topic_worthy_rows": sum(
            _clean_text(row.get("主题沉淀价值")) == "值得沉淀"
            for row in topic_rows
        ),
        "topic_unworthy_rows": sum(
            _clean_text(row.get("主题沉淀价值")) == "不值得沉淀"
            for row in topic_rows
        ),
        "topic_transcribed_rows": sum(
            not _topic_transcription_is_skipped(row.get("主题转写状态"))
            for row in topic_rows
        ),
        "topic_transcription_skipped_rows": sum(
            _topic_transcription_is_skipped(row.get("主题转写状态"))
            for row in topic_rows
        ),
        "topic_source_rows": len(mapping_rows),
        "evidence_gap_rows": len(evidence_gap_rows),
        "pending_cluster_rows": len(pending_cluster_rows),
        "cluster_admission_enforced": clustering_meta.get(
            "cluster_admission_enforced",
            False,
        ),
        "cluster_admission_policy_version": clustering_meta.get(
            "cluster_admission_policy_version",
            CLUSTER_ADMISSION_POLICY_VERSION,
        ),
        "cluster_admission_min_confidence": clustering_meta.get(
            "cluster_admission_min_confidence",
            cluster_admission_min_confidence,
        ),
        "cluster_admission_admitted_topics": clustering_meta.get(
            "cluster_admission_admitted_topics",
            0,
        ),
        "cluster_admission_pending_topics": clustering_meta.get(
            "cluster_admission_pending_topics",
            0,
        ),
        "cluster_admission_admitted_source_rows": clustering_meta.get(
            "cluster_admission_admitted_source_rows",
            0,
        ),
        "cluster_admission_pending_source_rows": clustering_meta.get(
            "cluster_admission_pending_source_rows",
            0,
        ),
        "incremental_topic_registry_enabled": clustering_meta.get(
            "incremental_topic_registry_enabled",
            False,
        ),
        "incremental_created_topics": clustering_meta.get(
            "incremental_created_topics",
            0,
        ),
        "incremental_merged_topics": clustering_meta.get(
            "incremental_merged_topics",
            0,
        ),
        "incremental_pending_topics": clustering_meta.get(
            "incremental_pending_topics",
            0,
        ),
        "incremental_added_members": clustering_meta.get(
            "incremental_added_members",
            0,
        ),
        "incremental_duplicate_members": clustering_meta.get(
            "incremental_duplicate_members",
            0,
        ),
        "topic_signal_labeled_rows": sum(
            _clean_text(row.get("语义标注状态")) == "topic_signal_labeled"
            for row in feature_rows
        ),
        "topic_signal_fallback_rows": sum(
            _clean_text(row.get("语义标注状态")) != "topic_signal_labeled"
            for row in feature_rows
        ),
        "topic_model_call_limit": clustering_meta.get(
            "topic_model_call_limit",
            "",
        ),
        "topic_model_calls": clustering_meta.get("topic_model_calls", 0),
        "topic_model_budget_skipped": clustering_meta.get(
            "topic_model_budget_skipped",
            0,
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
        "atomic_extraction_request_jobs": clustering_meta.get(
            "atomic_extraction_request_jobs",
            0,
        ),
        "atomic_extraction_model_requests": clustering_meta.get(
            "atomic_extraction_model_requests",
            0,
        ),
        "atomic_extraction_batch_calls": clustering_meta.get(
            "atomic_extraction_batch_calls",
            0,
        ),
        "atomic_extraction_batch_splits": clustering_meta.get(
            "atomic_extraction_batch_splits",
            0,
        ),
        "atomic_extraction_batch_size": clustering_meta.get(
            "atomic_extraction_batch_size",
            1,
        ),
        "atomic_extraction_batch_max_chars": clustering_meta.get(
            "atomic_extraction_batch_max_chars",
            0,
        ),
        "atomic_extraction_failed": clustering_meta.get("atomic_extraction_failed", 0),
        "atomic_extraction_cache_hits": clustering_meta.get(
            "atomic_extraction_cache_hits",
            0,
        ),
        "atomic_extraction_workers": clustering_meta.get("max_workers", 0),
        "atomic_unit_count": clustering_meta.get("atomic_unit_count", 0),
        "atomic_product_conflicts": clustering_meta.get(
            "atomic_product_conflicts",
            0,
        ),
        "direct_cluster_calls": clustering_meta.get("direct_cluster_calls", 0),
        "direct_cluster_cache_hits": clustering_meta.get(
            "direct_cluster_cache_hits",
            0,
        ),
        "direct_cluster_max_workers": clustering_meta.get(
            "direct_cluster_max_workers",
            0,
        ),
        "direct_cluster_batches_total": clustering_meta.get(
            "direct_cluster_batches_total",
            0,
        ),
        "direct_cluster_failed": clustering_meta.get("direct_cluster_failed", 0),
        "direct_cluster_last_error": clustering_meta.get(
            "direct_cluster_last_error",
            "",
        ),
        "direct_cluster_failure_reasons": clustering_meta.get(
            "direct_cluster_failure_reasons",
            [],
        ),
        "atomic_extraction_failure_reasons": clustering_meta.get(
            "atomic_extraction_failure_reasons",
            [],
        ),
        "direct_review_singletons": clustering_meta.get("direct_review_singletons", 0),
        "direct_reconcile_floor": clustering_meta.get("direct_reconcile_floor", ""),
        "direct_reconcile_model_floor": clustering_meta.get(
            "direct_reconcile_model_floor",
            "",
        ),
        "direct_reconcile_limit": clustering_meta.get(
            "direct_reconcile_limit",
            0,
        ),
        "direct_reconcile_candidates": clustering_meta.get("direct_reconcile_candidates", 0),
        "direct_reconcile_calls": clustering_meta.get("direct_reconcile_calls", 0),
        "direct_reconcile_cache_hits": clustering_meta.get(
            "direct_reconcile_cache_hits",
            0,
        ),
        "direct_reconcile_model_floor_skipped": clustering_meta.get(
            "direct_reconcile_model_floor_skipped",
            0,
        ),
        "direct_reconcile_rule_approved": clustering_meta.get(
            "direct_reconcile_rule_approved",
            0,
        ),
        "direct_reconcile_approved": clustering_meta.get("direct_reconcile_approved", 0),
        "direct_reconcile_rejected": clustering_meta.get("direct_reconcile_rejected", 0),
        "direct_reconcile_uncertain": clustering_meta.get("direct_reconcile_uncertain", 0),
        "direct_reconcile_failed": clustering_meta.get("direct_reconcile_failed", 0),
        "direct_reconcile_hard_rejected": clustering_meta.get(
            "direct_reconcile_hard_rejected",
            0,
        ),
        "direct_reconcile_limit_reached": clustering_meta.get(
            "direct_reconcile_limit_reached",
            0,
        ),
        "standard_references_enabled": use_standard_references,
    }


def write_cluster_only_workbook(
    feature_rows: list[dict[str, Any]],
    workbook_path: str | Path,
    *,
    use_mimo: bool = True,
    mimo_client: MimoClient | None = None,
    clustering_mode: str = "direct_mimo",
    semantic_threshold: float = 0.84,
    cluster_review_floor: float = DEFAULT_CLUSTER_REVIEW_FLOOR,
    cluster_auto_merge_threshold: float = DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    cluster_review_limit: int = DEFAULT_CLUSTER_REVIEW_LIMIT,
    embedding_client: EmbeddingClient | None = None,
    direct_mimo_progress_path: Path | None = None,
    topic_progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Export one clustering-only sheet without downstream knowledge generation."""
    clustering_meta: dict[str, Any] = {}
    cluster_rows, _mapping_rows, evidence_gap_rows, _pending_rows = build_topic_review_rows(
        feature_rows,
        use_mimo=use_mimo,
        mimo_client=mimo_client,
        clustering_mode=clustering_mode,
        semantic_threshold=semantic_threshold,
        cluster_review_floor=cluster_review_floor,
        cluster_auto_merge_threshold=cluster_auto_merge_threshold,
        cluster_review_limit=cluster_review_limit,
        embedding_client=embedding_client,
        clustering_meta=clustering_meta,
        use_standard_references=False,
        topic_model_call_limit=0,
        direct_mimo_progress_path=direct_mimo_progress_path,
        topic_progress_callback=topic_progress_callback,
        cluster_only=True,
    )
    write_rows_to_workbook(
        {"聚类结果": (CLUSTER_ONLY_COLUMNS, cluster_rows)},
        workbook_path,
    )
    return {
        "cluster_only": True,
        "cluster_rows": len(cluster_rows),
        "evidence_gap_rows": len(evidence_gap_rows),
        **clustering_meta,
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
            note_parts.append(
                f"来源记录ID：{'、'.join(unique_source_ids)}"
            )
        merged_note = _merge_unique_text(notes, separator="；")
        if merged_note:
            note_parts.append(merged_note)
        base["副标题"] = _merge_unique_text([row.get("副标题") for row in rows]) or base.get("副标题", "")
        base["知识分类"] = knowledge_category_from_topic_stage(
            base.get("知识分类") or base.get("主题问题分类"),
            base.get("候选知识形态") or base.get("模型知识形态"),
        )
        base["适用范围"] = _canonical_export_applicable_scope(base)
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
        content, recommended_reply = _split_embedded_recommended_reply(
            row.get("知识内容"),
            row.get("推荐回复"),
        )
        item = {
            "知识ID": _clean_text(row.get("知识ID"))
            or _clean_text(row.get("主题ID"))
            or _clean_text(row.get("候选ID")),
            "主标题": _clean_text(row.get("主标题")),
            "副标题": _clean_text(row.get("副标题")),
            "知识内容": content,
            "图例": _clean_text(row.get("图例")) or _clean_text(row.get("主题图片链接")),
            "推荐回复": recommended_reply,
            "知识分类": knowledge_category_from_topic_stage(
                row.get("知识分类") or row.get("主题问题分类"),
                row.get("候选知识形态") or row.get("模型知识形态"),
            ),
            "关联标准项": _clean_text(row.get("关联标准项")),
            "适用范围": _canonical_export_applicable_scope(row),
            "适用品牌": _clean_text(row.get("适用品牌")),
            "适用机型": _clean_text(row.get("适用机型")),
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


def _topic_candidate_export_gate_issues(
    row: dict[str, Any],
    *,
    use_standard_references: bool,
) -> list[str]:
    """Return hard blockers for the formal candidate workbook.

    ``topic_review_queue.xlsx`` remains the complete review draft.  The formal
    candidate workbook is intentionally stricter: it must have an explicit
    human approval, a real standard reference in standard mode, and no known
    evidence/content blockers.
    """
    issues: list[str] = []
    approval_values = [
        _clean_text(row.get(field))
        for field in ("审核结论", "CZ复核结论", "是否可用")
        if _clean_text(row.get(field))
    ]
    if not any(value in {"通过", "修改后通过", "是", "可用"} for value in approval_values):
        issues.append("未获得明确人工通过")
    if any(
        marker in value
        for value in approval_values
        for marker in ("驳回", "Bad Case", "不通过", "不可用", "待确认", "需修改")
    ):
        issues.append("人工复核未通过")
    if _clean_text(row.get("主题沉淀价值")) != "值得沉淀":
        issues.append("主题沉淀价值未通过")
    if _clean_text(row.get("主题转写状态")) not in {
        "topic_model_labeled",
        "topic_model_rewritten_for_evidence",
    }:
        issues.append("主题转写未成功")
    if _clean_text(row.get("模型调用状态")) != "model_success":
        issues.append("模型调用未成功")
    if _clean_text(row.get("模型输出校验状态")) != "passed":
        issues.append("模型输出校验未通过")
    if _clean_text(row.get("模型质量状态")) != "passed":
        issues.append("模型业务质量未通过")
    if _clean_text(row.get("知识草稿状态")) != "ready_for_human_review":
        issues.append("知识草稿未就绪")
    if _clean_text(row.get("模型初标结论")) != "通过":
        issues.append("模型初标未通过")
    if use_standard_references and not _clean_text(row.get("关联标准项")):
        issues.append("缺少真实标准引用")
    if _clean_text(row.get("主题无来源内容")):
        issues.append("存在无来源内容")
    if _clean_text(row.get("模型初标错误类型")):
        issues.append("模型初标存在错误类型")
    if "标准项映射错" in _clean_text(row.get("模型初标错误类型")):
        issues.append("模型初标标准映射错误")
    title = _clean_text(row.get("主标题"))
    if not title:
        issues.append("主标题为空")
    elif _natural_question_title_issue(title):
        issues.append("主标题不是自然问句")
    content, reply = _split_embedded_recommended_reply(
        row.get("知识内容"),
        row.get("推荐回复"),
    )
    if not content:
        issues.append("知识内容为空")
    if _topic_draft_is_case_analysis({"content": content}):
        issues.append("知识内容仍是案例分析")
    reply_issues = _recommended_reply_quality_issues(
        reply,
        title=title,
        content=content,
    )
    if reply_issues:
        issues.append("推荐回复质量门禁失败")
    if (
        _clean_text(row.get("主题图片必要性")).startswith("需要")
        and not _clean_text(row.get("图例"))
        and not _clean_text(row.get("主题图片链接"))
    ):
        issues.append("要求图片证据但没有图例")
    return issues


def write_topic_candidate_knowledge_workbook(
    topic_rows: list[dict[str, Any]],
    workbook_path: str | Path,
    *,
    use_standard_references: bool = True,
) -> None:
    """Export either the legacy standard-aware contract or the case-only contract."""
    exported_topic_rows: list[dict[str, Any]] = []
    for row in topic_rows:
        status = _clean_text(row.get("主题转写状态"))
        if _topic_transcription_is_skipped(status):
            continue
        gate_issues = _topic_candidate_export_gate_issues(
            row,
            use_standard_references=use_standard_references,
        )
        if gate_issues:
            continue
        exported_topic_rows.append(row)
    if not use_standard_references:
        write_rows_to_workbook(
            {
                "候选知识": (
                    CASE_KNOWLEDGE_COLUMNS,
                    build_case_knowledge_rows(exported_topic_rows),
                )
            },
            workbook_path,
        )
        return
    exported_rows: list[dict[str, Any]] = []
    for row in exported_topic_rows:
        content, recommended_reply = _split_embedded_recommended_reply(
            row.get("知识内容"),
            row.get("推荐回复"),
        )
        exported_rows.append(
            {
                **{
                    column: _clean_text(row.get(column))
                    for column in KNOWLEDGE_MASTER_COLUMNS
                    + KNOWLEDGE_REVIEW_EXTENSION_COLUMNS
                },
                "知识内容": content,
                "推荐回复": recommended_reply,
            }
        )
    write_rows_to_workbook(
        {
            "候选知识": (
                KNOWLEDGE_MASTER_COLUMNS + KNOWLEDGE_REVIEW_EXTENSION_COLUMNS,
                exported_rows,
            )
        },
        workbook_path,
    )


def _workflow_checkpoint_path(output_path: Path) -> Path:
    return output_path / "workflow_checkpoint.json"


def _write_workflow_checkpoint(
    output_path: Path,
    stage: str,
    payload: dict[str, Any],
) -> None:
    checkpoint_path = _workflow_checkpoint_path(output_path)
    temporary_path = checkpoint_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "version": 1,
                "stage": stage,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                **payload,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(checkpoint_path)


def _load_workflow_checkpoint(output_path: Path) -> dict[str, Any]:
    checkpoint_path = _workflow_checkpoint_path(output_path)
    if not checkpoint_path.is_file():
        return {}
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    resume: bool = False,
    cluster_only: bool = False,
    source_row_limit: int | None = None,
    direct_mimo_progress_path: Path | None = None,
    cluster_media_policy: str | None = None,
    enforce_cluster_admission: bool | None = None,
    cluster_admission_min_confidence: float | None = None,
) -> dict[str, Any]:
    if source_row_limit is not None:
        try:
            source_row_limit = int(source_row_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("source_row_limit 必须是正整数") from exc
        if source_row_limit < 1:
            raise ValueError("source_row_limit 必须是正整数")
        if not cluster_only:
            raise ValueError("source_row_limit 仅允许用于 --cluster-only 小样本验证")
    terminology = ensure_terminology_loaded()
    effective_cluster_media_policy = resolve_cluster_media_policy(
        cluster_media_policy,
        cluster_only=cluster_only,
        clustering_mode=clustering_mode,
    )
    effective_cluster_admission = (
        bool(enforce_cluster_admission)
        if enforce_cluster_admission is not None
        else bool(
            use_mimo
            and clustering_mode.strip().lower() == "direct_mimo"
        )
    )
    if cluster_admission_min_confidence is None:
        try:
            effective_cluster_admission_min_confidence = float(
                os.getenv(
                    "ANSWER_HUB_CLUSTER_ADMISSION_MIN_CONFIDENCE",
                    str(DEFAULT_CLUSTER_ADMISSION_MIN_CONFIDENCE),
                )
            )
        except ValueError:
            effective_cluster_admission_min_confidence = (
                DEFAULT_CLUSTER_ADMISSION_MIN_CONFIDENCE
            )
    else:
        effective_cluster_admission_min_confidence = float(
            cluster_admission_min_confidence
        )
    effective_cluster_admission_min_confidence = max(
        0.0,
        min(effective_cluster_admission_min_confidence, 1.0),
    )

    def report(
        stage_id: str,
        status: str,
        detail: str,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        if progress_callback:
            progress_callback(stage_id, status, detail, metrics or {})

    cz_standard_adapter = CzIntegrationAdapter()
    cz_standard_retrieval_enabled = bool(
        use_standard_references is not False
        and cz_standard_adapter.can_search_headquarters_standards()
    )
    # 默认保持“标准引用模式”开启，即使总部接口和本地目录都暂不可用；
    # 这样无标准主题仍会进入经验补充人工复核，而不会降级成可正式导出的案例知识。
    standards_enabled = (
        use_standard_references is not False
        if use_standard_references is None
        else bool(use_standard_references)
    )

    def topic_standard_retriever(
        _topic_id: str,
        rows: list[dict[str, Any]],
        query: dict[str, Any],
    ) -> tuple[list[tuple[StandardCatalogItem, float]], dict[str, Any]]:
        source_record_id = ""
        for row in rows:
            for field in ("工单ID", "原始工单ID", "数据ID"):
                candidate = _clean_text(row.get(field))
                if re.fullmatch(r"[0-9]{1,64}", candidate):
                    source_record_id = candidate
                    break
            if source_record_id:
                break
        if not source_record_id:
            return [], {
                "source": "headquarters_standard",
                "status": "error",
                "error": "主题没有可用于 CZ 标准检索的数字工单ID。",
            }
        business_line = business_line_from_record(rows[0]) if rows else None
        business_type = {
            SELF_OPERATED_BUSINESS_LINE_CODE: "self_operated",
            AGGREGATE_BUSINESS_LINE_CODE: "aggregated",
        }.get(business_line.code if business_line else "")
        if not business_type:
            return [], {
                "source": "headquarters_standard",
                "status": "error",
                "error": "主题缺少可映射的回收业务层级，无法检索 CZ 标准。",
            }
        normalized_question = _safe_join(
            [
                _clean_text(query.get("核心问题")),
                _clean_text(query.get("人工核心问题")),
                _clean_text(query.get("人工判定结论")),
                _clean_text(query.get("对象/部位")),
                _clean_text(query.get("异常现象")),
            ],
            "；",
        )
        model = _merge_unique_text(
            [row.get("适用机型") or row.get("机型") for row in rows],
            separator="；",
        )
        return cz_standard_adapter.search_headquarters_standards(
            conversation_id=source_record_id,
            normalized_question=normalized_question,
            business_type=business_type,
            product_type=_clean_text(query.get("产品类型")),
            model=model,
        )

    active_topic_standard_retriever = (
        topic_standard_retriever if cz_standard_retrieval_enabled else None
    )
    output_path = _ensure_output_dir(output_dir)
    checkpoint = _load_workflow_checkpoint(output_path) if resume else {}
    checkpoint_stage = _clean_text(checkpoint.get("stage"))
    stage_rank = {
        "": 0,
        "preprocess": 1,
        "semantic_label": 2,
        "topic_build": 3,
        "export_review": 4,
    }
    checkpoint_topic_summary = dict(
        checkpoint.get("topic_summary") or {}
    )
    checkpoint_admission_threshold = _confidence_01(
        checkpoint_topic_summary.get(
            "cluster_admission_min_confidence"
        )
    )
    checkpoint_cluster_admission_compatible = bool(
        not effective_cluster_admission
        or (
            checkpoint_topic_summary.get(
                "cluster_admission_enforced"
            )
            and _clean_text(
                checkpoint_topic_summary.get(
                    "cluster_admission_policy_version"
                )
            )
            == CLUSTER_ADMISSION_POLICY_VERSION
            and checkpoint_admission_threshold is not None
            and abs(
                checkpoint_admission_threshold
                - effective_cluster_admission_min_confidence
            )
            <= 1e-9
            and _clean_text(
                checkpoint_topic_summary.get(
                    "clustering_requested_mode"
                )
            ).lower()
            == clustering_mode.strip().lower()
        )
    )
    if (
        resume
        and effective_cluster_admission
        and stage_rank.get(checkpoint_stage, 0) >= stage_rank["topic_build"]
        and not checkpoint_cluster_admission_compatible
        and not checkpoint.get("feature_rows")
    ):
        checkpoint = {}
        checkpoint_stage = ""
        checkpoint_topic_summary = {}
        checkpoint_cluster_admission_compatible = False
    report(
        "load_input",
        "running",
        "正在读取会话数据；CZ 标准将在主题转写阶段按需检索。"
        if cz_standard_retrieval_enabled
        else (
            "正在读取会话数据与标准目录。"
            if standards_enabled
            else "正在读取会话数据；本次不使用标准引用。"
        ),
    )
    loaded_standard_catalog = (
        load_standard_catalog(standards_path) if standards_path else []
    )
    incomplete_standard_items = [
        item
        for item in loaded_standard_catalog
        if not all(
            _clean_text(value)
            for value in (
                item.standard_id,
                item.title,
                item.standard_path,
                item.scope,
                item.response_snippet,
            )
        )
    ]
    standard_catalog = [
        item for item in loaded_standard_catalog if item not in incomplete_standard_items
    ]
    if standards_path and not standard_catalog:
        if incomplete_standard_items:
            raise ValueError(
                "标准文件存在字段不完整的生效标准，且没有可用的标准正文；"
                "每条可引用标准必须包含标准ID、标题、标准路径、适用品类和标准正文。"
            )
        raise ValueError(
            "标准文件已配置但没有读取到有效的生效标准；"
            "请检查文件格式、首个工作表表头、生效状态和标准正文。"
        )
    source_rows = _read_source_rows(source_path)
    source_available_rows = len(source_rows)
    if source_row_limit is not None:
        source_rows = source_rows[:source_row_limit]
    source_total_rows = len(source_rows)
    source_rows, redaction_excluded_rows, redaction_audit = partition_redaction_rows(
        source_rows
    )
    source_product_categories = {
        canonical_product_name(
            row.get("类目")
            or row.get("产品类型编码")
            or row.get("产品类型"),
            unknown=_clean_text(row.get("产品类型")),
        )
        for row in source_rows
        if _clean_text(
            row.get("类目")
            or row.get("产品类型编码")
            or row.get("产品类型")
        )
    }
    standard_product_categories = {
        canonical_product_name(
            _clean_text(item.scope).split("-", 1)[0],
            unknown=_clean_text(item.scope),
        )
        for item in standard_catalog
        if _clean_text(item.scope)
    }
    missing_standard_product_categories = sorted(
        category
        for category in source_product_categories - standard_product_categories
        if category
    )
    report(
        "load_input",
        "completed",
        "输入文件读取完成。",
        {
            "source_rows": source_total_rows,
            "redaction_safe_rows": len(source_rows),
            "redaction_skipped_rows": len(redaction_excluded_rows),
            "source_available_rows": source_available_rows,
            "source_row_limit": source_row_limit or 0,
            "standards": len(standard_catalog),
            "standard_product_categories": len(standard_product_categories),
            "standard_missing_product_categories": "\n".join(
                missing_standard_product_categories
            ),
            "standard_incomplete_records": len(incomplete_standard_items),
            "redaction_blocking_findings": redaction_audit["blocking_count"],
            "redaction_warning_findings": redaction_audit["warning_count"],
        },
    )

    if stage_rank.get(checkpoint_stage, 0) >= stage_rank["preprocess"]:
        report("preprocess", "running", "正在从运行检查点恢复清洗结果。")
        preprocessed_rows = list(checkpoint.get("preprocessed_rows") or [])
        eligible_rows = list(checkpoint.get("eligible_rows") or [])
        eligible_raw_rows = list(checkpoint.get("eligible_raw_rows") or [])
        excluded_rows = redaction_excluded_rows + list(
            checkpoint.get("excluded_rows") or []
        )
        selected_rows = list(checkpoint.get("selected_rows") or [])
        if _checkpoint_needs_ai_result_reprocessing(preprocessed_rows):
            selected_rows, checkpoint_excluded_rows = filter_source_rows_by_product_type(
                source_rows,
                product_type,
            )
            excluded_rows = redaction_excluded_rows + checkpoint_excluded_rows
            preprocessed_rows = preprocess_source_rows(selected_rows)
            eligible_rows, validation_excluded_rows = filter_preprocessed_rows_for_model(
                preprocessed_rows
            )
            excluded_rows.extend(validation_excluded_rows)
            eligible_raw_rows = [
                source_row
                for source_row, preprocessed_row in zip(selected_rows, preprocessed_rows)
                if _clean_text(preprocessed_row.get("可进入模型初标")) == "是"
            ]
            checkpoint_stage = "preprocess"
            checkpoint["stage"] = checkpoint_stage
            checkpoint.pop("feature_rows", None)
            checkpoint.pop("topic_summary", None)
            _write_workflow_checkpoint(
                output_path,
                checkpoint_stage,
                {
                    "run_id": _clean_text(checkpoint.get("run_id")),
                    "selected_rows": selected_rows,
                    "preprocessed_rows": preprocessed_rows,
                    "eligible_rows": eligible_rows,
                    "eligible_raw_rows": eligible_raw_rows,
                    "excluded_rows": excluded_rows,
                },
            )
            preprocess_detail = (
                "检测到旧检查点未解析 ai_result；已重新清洗，"
                "后续语义标注与聚类将重新执行。"
            )
        else:
            preprocess_detail = "已从检查点恢复清洗与证据分流结果。"
    else:
        report("preprocess", "running", "正在执行品类筛选、字段清洗和证据校验。")
        selected_rows, excluded_rows = filter_source_rows_by_product_type(source_rows, product_type)
        excluded_rows = redaction_excluded_rows + excluded_rows
        preprocessed_rows = preprocess_source_rows(selected_rows)
        eligible_rows, validation_excluded_rows = filter_preprocessed_rows_for_model(preprocessed_rows)
        excluded_rows.extend(validation_excluded_rows)
        eligible_raw_rows = [
            source_row
            for source_row, preprocessed_row in zip(selected_rows, preprocessed_rows)
            if _clean_text(preprocessed_row.get("可进入模型初标")) == "是"
        ]
        _write_workflow_checkpoint(
            output_path,
            "preprocess",
            {
                "selected_rows": selected_rows,
                "preprocessed_rows": preprocessed_rows,
                "eligible_rows": eligible_rows,
                "eligible_raw_rows": eligible_raw_rows,
                "excluded_rows": excluded_rows,
            },
        )
        preprocess_detail = "清洗与证据分流完成。"
    report(
        "preprocess",
        "completed",
        preprocess_detail,
        {
            "selected_rows": len(selected_rows),
            "eligible_rows": len(eligible_rows),
            "excluded_rows": len(excluded_rows),
        },
    )

    audit_store = AuditStore.from_env(audit_db_path)
    active_run_id = _clean_text(checkpoint.get("run_id")) or uuid.uuid4().hex
    for index, row in enumerate(excluded_rows, start=1):
        audit_store.record_excluded(
            active_run_id,
            _record_id_for_row(row, index),
            row,
            _clean_text(row.get("排除原因")) or "未通过候选生成校验",
        )
    mimo_client = MimoClient.from_env() if use_mimo else None
    if mimo_client is not None and effective_cluster_media_policy:
        mimo_client.config = replace(
            mimo_client.config,
            cluster_media_policy=effective_cluster_media_policy,
        )
    if cluster_only and clustering_mode.strip().lower() == "direct_mimo":
        feature_rows = list(eligible_rows)
        run_id = active_run_id
        semantic_detail = "仅聚类模式跳过重复语义标注，直接使用原子问题拆分缓存。"
    elif stage_rank.get(checkpoint_stage, 0) >= stage_rank["semantic_label"]:
        report("semantic_label", "running", "正在从运行检查点恢复会话语义标注结果。")
        feature_rows = list(checkpoint.get("feature_rows") or [])
        run_id = active_run_id
        semantic_detail = "已从检查点恢复会话语义标注结果。"
    else:
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
            mimo_client=mimo_client,
            audit_store=audit_store,
            run_id=active_run_id,
            use_standard_references=standards_enabled,
        )
        _write_workflow_checkpoint(
            output_path,
            "semantic_label",
            {
                "selected_rows": selected_rows,
                "preprocessed_rows": preprocessed_rows,
                "eligible_rows": eligible_rows,
                "eligible_raw_rows": eligible_raw_rows,
                "excluded_rows": excluded_rows,
                "feature_rows": feature_rows,
                "run_id": run_id,
            },
        )
        semantic_detail = "会话语义标注完成。"
    report(
        "semantic_label",
        "completed",
        semantic_detail,
        {
            "feature_rows": len(feature_rows),
            "model_labeled_rows": sum(
                _clean_text(row.get("语义标注状态")) == "topic_signal_labeled"
                for row in feature_rows
            ),
            "skipped_for_cluster_only": bool(
                cluster_only and clustering_mode.strip().lower() == "direct_mimo"
            ),
        },
    )

    if cluster_only:
        cluster_workbook_path = output_path / "cluster_result.xlsx"
        report("topic_build", "running", "正在执行仅聚类验证。")
        cluster_summary = write_cluster_only_workbook(
            feature_rows,
            cluster_workbook_path,
            use_mimo=use_mimo,
            mimo_client=mimo_client,
            clustering_mode=clustering_mode,
            semantic_threshold=semantic_threshold,
            cluster_review_floor=cluster_review_floor,
            cluster_auto_merge_threshold=cluster_auto_merge_threshold,
            cluster_review_limit=cluster_review_limit,
            embedding_client=embedding_client,
            direct_mimo_progress_path=(
                direct_mimo_progress_path
                or output_path / "direct_mimo_progress.json"
            ),
            topic_progress_callback=lambda detail, metrics: report(
                "topic_build",
                "running",
                detail,
                metrics,
            ),
        )
        _write_workflow_checkpoint(
            output_path,
            "topic_build",
            {
                "selected_rows": selected_rows,
                "preprocessed_rows": preprocessed_rows,
                "eligible_rows": eligible_rows,
                "eligible_raw_rows": eligible_raw_rows,
                "excluded_rows": excluded_rows,
                "feature_rows": feature_rows,
                "run_id": run_id,
                "cluster_summary": cluster_summary,
                "cluster_only": True,
            },
        )
        report(
            "topic_build",
            "completed",
            "仅聚类验证完成，未执行主题价值、知识转写或内容初审。",
            {
                "cluster_rows": cluster_summary.get("cluster_rows", 0),
                "evidence_gap_rows": cluster_summary.get(
                    "evidence_gap_rows",
                    0,
                ),
            },
        )
        report("export_review", "running", "正在生成单 sheet 聚类结果。")
        report(
            "export_review",
            "completed",
            "聚类结果已生成，自动化流程进入人工聚类审核阶段。",
            {
                "cluster_file": str(cluster_workbook_path),
            },
        )
        summary = _summary_for_preprocessed_rows(preprocessed_rows)
        summary.update(_summary_for_labeled_rows(feature_rows))
        summary.update(
            {
                "source_file": str(Path(source_path)),
                "standard_file": "",
                "output_file": str(cluster_workbook_path),
                "topic_review_file": str(cluster_workbook_path),
                "candidate_output_file": "",
                "product_type": _clean_text(product_type),
                "source_total_rows": source_total_rows,
                "source_available_rows": source_available_rows,
                "source_row_limit": source_row_limit or 0,
                "excluded_rows": len(excluded_rows),
                "eligible_rows": len(eligible_rows),
                "redaction_safe_rows": len(source_rows),
                "redaction_skipped_rows": len(redaction_excluded_rows),
                "run_id": run_id,
                "audit_db": str(audit_store.path),
                "mimo_configured": bool(mimo_client),
                "terminology": terminology,
                "quality_clustering_rules": clustering_rules_metadata(),
                "cluster_media_policy": (
                    effective_cluster_media_policy
                    or (
                        mimo_client.config.cluster_media_policy
                        if mimo_client
                        else ""
                    )
                ),
                "standard_references_enabled": False,
                "redaction_audit": redaction_audit,
                "resumed_from_checkpoint": bool(checkpoint),
                "cluster_only": True,
            }
        )
        summary.update(cluster_summary)
        if mimo_client:
            summary.update(mimo_client.metrics_snapshot())
        (output_path / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_workflow_checkpoint(
            output_path,
            "export_review",
            {
                "run_id": run_id,
                "summary": summary,
                "cluster_only": True,
            },
        )
        return summary

    workbook_path = output_path / "review_queue.xlsx"
    topic_workbook_path = output_path / "topic_review_queue.xlsx"
    candidate_workbook_path = output_path / "candidate_knowledge.xlsx"
    write_review_workbook(preprocessed_rows, feature_rows, excluded_rows, workbook_path)
    if (
        stage_rank.get(checkpoint_stage, 0) >= stage_rank["topic_build"]
        and topic_workbook_path.is_file()
        and checkpoint_cluster_admission_compatible
    ):
        report("topic_build", "running", "正在从运行检查点恢复主题候选结果。")
        topic_summary = checkpoint_topic_summary
        topic_detail = "已从检查点恢复主题聚类、知识转写和模型初标结果。"
    else:
        report(
            "topic_build",
            "running",
            "正在聚类主题、执行聚类准入、转写知识并执行模型初标。",
        )
        topic_summary = write_topic_review_workbook(
            preprocessed_rows,
            feature_rows,
            excluded_rows,
            topic_workbook_path,
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
            use_standard_references=standards_enabled,
            topic_standard_retriever=active_topic_standard_retriever,
            require_standard_match=cz_standard_retrieval_enabled,
            transcribe_all_admitted_topics=True,
            direct_mimo_progress_path=output_path / "direct_mimo_progress.json",
            topic_progress_callback=lambda detail, metrics: report(
                "topic_build",
                "running",
                detail,
                metrics,
            ),
            enforce_cluster_admission=effective_cluster_admission,
            cluster_admission_min_confidence=(
                effective_cluster_admission_min_confidence
            ),
        )
        _write_workflow_checkpoint(
            output_path,
            "topic_build",
            {
                "selected_rows": selected_rows,
                "preprocessed_rows": preprocessed_rows,
                "eligible_rows": eligible_rows,
                "eligible_raw_rows": eligible_raw_rows,
                "excluded_rows": excluded_rows,
                "feature_rows": feature_rows,
                "run_id": run_id,
                "topic_summary": topic_summary,
            },
        )
        topic_detail = "主题聚类、准入分流与知识转写完成。"
    report(
        "topic_build",
        "completed",
        topic_detail,
        {
            "topic_rows": topic_summary.get("topic_rows", 0),
            "topic_stage_classified_rows": topic_summary.get(
                "topic_stage_classified_rows",
                0,
            ),
            "topic_worthy_rows": topic_summary.get("topic_worthy_rows", 0),
            "topic_unworthy_rows": topic_summary.get("topic_unworthy_rows", 0),
            "topic_transcribed_rows": topic_summary.get(
                "topic_transcribed_rows",
                0,
            ),
            "topic_transcription_skipped_rows": topic_summary.get(
                "topic_transcription_skipped_rows",
                0,
            ),
            "evidence_gap_rows": topic_summary.get("evidence_gap_rows", 0),
            "pending_cluster_rows": topic_summary.get("pending_cluster_rows", 0),
            "cluster_admission_admitted_topics": topic_summary.get(
                "cluster_admission_admitted_topics",
                0,
            ),
            "cluster_admission_pending_topics": topic_summary.get(
                "cluster_admission_pending_topics",
                0,
            ),
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
                "source_total_rows": source_total_rows,
            "source_available_rows": source_available_rows,
            "source_row_limit": source_row_limit or 0,
            "excluded_rows": len(excluded_rows),
            "eligible_rows": len(eligible_rows),
            "redaction_safe_rows": len(source_rows),
            "redaction_skipped_rows": len(redaction_excluded_rows),
            "run_id": run_id,
            "audit_db": str(audit_store.path),
            "mimo_configured": bool(mimo_client),
            "terminology": terminology,
            "quality_clustering_rules": clustering_rules_metadata(),
            "standard_references_enabled": standards_enabled,
            "cz_headquarters_standard_retrieval_enabled": (
                cz_standard_retrieval_enabled
            ),
            "redaction_audit": redaction_audit,
            "resumed_from_checkpoint": bool(checkpoint),
        }
    )
    summary.update(topic_summary)
    if mimo_client:
        summary.update(mimo_client.metrics_snapshot())
    (output_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_workflow_checkpoint(
        output_path,
        "export_review",
        {
            "selected_rows": selected_rows,
            "preprocessed_rows": preprocessed_rows,
            "eligible_rows": eligible_rows,
            "eligible_raw_rows": eligible_raw_rows,
            "excluded_rows": excluded_rows,
            "feature_rows": feature_rows,
            "run_id": run_id,
            "topic_summary": topic_summary,
            "summary": summary,
        },
    )
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
