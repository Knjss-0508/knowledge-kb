from __future__ import annotations

"""Small OpenAI-compatible client for the multi-category knowledge workflow.

The client intentionally uses only the Python standard library: the project can
run with the dependencies that are already declared in ``pyproject.toml`` and
the API key never needs to be sent to the browser.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import copy
import json
import os
import re
import threading
import time

from .business_taxonomy import (
    SELF_OPERATED_BUSINESS_LINE_NAME,
    business_line_from_record,
)
from .catalog import StandardCatalogItem
from .clustering_rules import build_clustering_rules_prompt_block
from .images import ImageEvidence, split_image_urls
from .knowledge_categories import SUPPORTED_KNOWLEDGE_CATEGORIES, UNCERTAIN_CATEGORY
from .product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    canonical_product_name,
    configured_product_names,
    product_category_prompt,
    resolve_product_category,
)
from .terminology import (
    build_terminology_prompt_block,
    ensure_terminology_loaded,
)


PROMPT_VERSION = "multi-category-topic-transcription-v13-recall-subtitles"
TOPIC_REVIEW_PROMPT_VERSION = "multi-category-topic-content-quality-review-v9-12-category-taxonomy"
TOPIC_STAGE_PROMPT_VERSION = "multi-category-topic-stage-value-v8-12-category-taxonomy"
TOPIC_DISPLAY_QUESTION_PROMPT_VERSION = "topic-display-question-v3-12-category-taxonomy"
CLUSTER_PAIR_REVIEW_PROMPT_VERSION = (
    "knowledge-cluster-membership-review-v16-12-category-taxonomy"
)
TOPIC_SIGNAL_PROMPT_VERSION = "multi-category-conversation-topic-signal-v9-12-category-taxonomy"
CLUSTER_UNIT_PROMPT_VERSION = "multi-category-conversation-cluster-units-v29-12-category-taxonomy"
CLUSTER_FUSION_PROMPT_VERSION = "multi-category-conversation-cluster-fusion-v9-12-category-taxonomy"
ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION = (
    "atomic-knowledge-topic-clustering-v16-classification-catalog-reviewable-recall"
)

CONTENT_TYPES = frozenset({"定义型", "阈值型", "核验型", "区分型"})

_API_KEY_SEPARATOR_RE = re.compile(r"[,;\r\n]+")
_META_DOCUMENT_TITLE_PATTERN = re.compile(
    r"(质检记录规则|工单记录规则|知识记录规则|"
    r"质检记录规范|工单记录规范|审核记录规范|管理规定)"
    r"(?:\s*(?:[。.!！?？]+|[（(][^）)\n]{1,12}[）)]))*\s*$"
)
_API_KEY_ROTATION_MARKERS = (
    "insufficient balance",
    "balance is insufficient",
    "insufficient funds",
    "payment required",
    "credit balance",
    "account balance",
    "quota exhausted",
    "quota exceeded",
    "billing hard limit",
    "余额不足",
    "余额已耗尽",
    "额度不足",
    "额度耗尽",
    "欠费",
    "请充值",
)


def candidate_title_style_issue(value: Any) -> str:
    title = str(value or "").strip()
    match = _META_DOCUMENT_TITLE_PATTERN.search(title)
    return match.group(1) if match else ""


def candidate_title_structure_issue(value: Any) -> str:
    title = str(value or "").strip()
    if "｜" in title or "|" in title:
        return "竖线分割"
    if "；" in title or ";" in title:
        return "分号拼接"
    if title.endswith(("、", "：", ":", "，", ",")):
        return "尾部残缺"
    if "/" in title or "／" in title or title.count("、") >= 2:
        return "关键词堆砌"
    return ""


class MimoError(RuntimeError):
    """The configured MiMo endpoint could not produce a valid candidate."""


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overwriting actual environment vars."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class MimoConfig:
    api_key: str
    base_url: str
    model: str
    api_keys: tuple[str, ...] = ()
    media_model: str = ""
    timeout_seconds: int = 60
    response_read_timeout_seconds: float = 90.0
    response_timeout_circuit_breaker_failures: int = 2
    thinking_type: str = "disabled"
    max_completion_tokens: int = 2048
    max_retries: int = 2
    retry_backoff_seconds: float = 0.75
    max_requests_per_second: float = 2.0
    cluster_media_policy: str = "on_demand"
    cluster_media_min_text_chars: int = 220
    input_cost_per_million_tokens: float = 0.0
    output_cost_per_million_tokens: float = 0.0

    @classmethod
    def from_env(cls) -> "MimoConfig | None":
        load_dotenv()
        api_key = os.getenv("MIMO_API_KEY", "").strip()
        api_keys = tuple(
            key
            for key in (
                item.strip()
                for item in _API_KEY_SEPARATOR_RE.split(
                    os.getenv("MIMO_API_KEYS", "")
                )
            )
            if key
        )
        base_url = os.getenv("MIMO_BASE_URL", "").strip()
        model = os.getenv("MIMO_MODEL", "").strip()
        media_model = os.getenv("MIMO_MEDIA_MODEL", "").strip()
        if not (api_key and base_url and model):
            return None
        if not media_model:
            media_model = "mimo-v2.5" if model.startswith("mimo-v2.5") else model
        cluster_media_policy = (
            os.getenv("MIMO_CLUSTER_MEDIA_POLICY", "on_demand")
            .strip()
            .lower()
        )
        if cluster_media_policy not in {"always", "on_demand", "never"}:
            cluster_media_policy = "on_demand"
        try:
            cluster_media_min_text_chars = max(
                80,
                min(
                    int(
                        os.getenv(
                            "MIMO_CLUSTER_MEDIA_MIN_TEXT_CHARS",
                            "220",
                        )
                    ),
                    2000,
                ),
            )
        except ValueError:
            cluster_media_min_text_chars = 220
        try:
            timeout = max(10, min(int(os.getenv("MIMO_TIMEOUT_SECONDS", "60")), 180))
        except ValueError:
            timeout = 60
        try:
            response_read_timeout = max(
                10.0,
                min(float(os.getenv("MIMO_RESPONSE_READ_TIMEOUT_SECONDS", "90")), 300.0),
            )
        except ValueError:
            response_read_timeout = 90.0
        try:
            response_timeout_circuit_breaker_failures = max(
                1,
                min(
                    int(
                        os.getenv(
                            "MIMO_RESPONSE_TIMEOUT_CIRCUIT_BREAKER_FAILURES",
                            "2",
                        )
                    ),
                    4,
                ),
            )
        except ValueError:
            response_timeout_circuit_breaker_failures = 2
        thinking_type = os.getenv("MIMO_THINKING_TYPE", "disabled").strip().lower()
        if thinking_type not in {"enabled", "disabled"}:
            thinking_type = "disabled"
        try:
            max_completion_tokens = max(
                256,
                min(
                    int(os.getenv("MIMO_MAX_COMPLETION_TOKENS", "2048")),
                    8192,
                ),
            )
        except ValueError:
            max_completion_tokens = 2048
        try:
            max_retries = max(0, min(int(os.getenv("MIMO_MAX_RETRIES", "2")), 6))
        except ValueError:
            max_retries = 2
        try:
            retry_backoff = max(
                0.1,
                min(float(os.getenv("MIMO_RETRY_BACKOFF_SECONDS", "0.75")), 30.0),
            )
        except ValueError:
            retry_backoff = 0.75
        try:
            max_rps = max(
                0.1,
                min(float(os.getenv("MIMO_MAX_REQUESTS_PER_SECOND", "2")), 50.0),
            )
        except ValueError:
            max_rps = 2.0
        try:
            input_cost = max(
                0.0,
                float(os.getenv("MIMO_INPUT_COST_PER_MILLION_TOKENS", "0")),
            )
        except ValueError:
            input_cost = 0.0
        try:
            output_cost = max(
                0.0,
                float(os.getenv("MIMO_OUTPUT_COST_PER_MILLION_TOKENS", "0")),
            )
        except ValueError:
            output_cost = 0.0
        return cls(
            api_key=api_key,
            api_keys=api_keys,
            base_url=base_url,
            model=model,
            media_model=media_model,
            timeout_seconds=timeout,
            response_read_timeout_seconds=response_read_timeout,
            response_timeout_circuit_breaker_failures=(
                response_timeout_circuit_breaker_failures
            ),
            thinking_type=thinking_type,
            max_completion_tokens=max_completion_tokens,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff,
            max_requests_per_second=max_rps,
            cluster_media_policy=cluster_media_policy,
            cluster_media_min_text_chars=cluster_media_min_text_chars,
            input_cost_per_million_tokens=input_cost,
            output_cost_per_million_tokens=output_cost,
        )

    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def all_api_keys(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                key
                for key in (self.api_key, *self.api_keys)
                if key
            )
        )


@dataclass(frozen=True)
class MimoLabelResult:
    candidate: dict[str, Any]
    request_audit: dict[str, Any]
    response_audit: dict[str, Any]


def _text(value: Any, limit: int = 7000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _terminology_product_categories(*values: Any) -> tuple[str, ...]:
    categories: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key in (
                "产品类型",
                "product_type",
                "product_category",
                "product_categories",
            ):
                collect(value.get(key))
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                collect(item)
            return
        if not isinstance(value, str):
            return
        for category in re.split(r"[,，、|/]+", value):
            normalized = category.strip()
            if normalized and normalized not in categories:
                categories.append(normalized)

    for value in values:
        collect(value)
    return tuple(categories)


def _terminology_prompt_block(*values: Any) -> str:
    ensure_terminology_loaded()
    product_categories = _terminology_product_categories(*values)
    return build_terminology_prompt_block(product_categories or None)


def _clustering_rules_prompt_block(*values: Any) -> str:
    business_lines: set[str] = set()

    def collect_business_line(value: Any) -> None:
        if isinstance(value, dict):
            line = business_line_from_record(value)
            if line:
                business_lines.add(line.name)
            for item in value.values():
                if isinstance(item, (dict, list, tuple, set, frozenset)):
                    collect_business_line(item)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                collect_business_line(item)

    for value in values:
        collect_business_line(value)
    if business_lines and business_lines != {
        SELF_OPERATED_BUSINESS_LINE_NAME
    }:
        return (
            "【回收业务层级硬边界】\n"
            "当前输入包含非自营回收业务层级。禁止套用自营回收十品类"
            "质检聚类口径；只能依据当前业务层级的会话证据保守聚类，"
            "并进入人工优先审核。"
        )
    product_categories = _terminology_product_categories(*values)
    return build_clustering_rules_prompt_block(
        product_categories or None,
        context_values=values,
    )


_TRANSFER_METADATA_LINE_RE = re.compile(
    r"^\s*"
    r"(?:\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?::\d{2})?\s*)?"
    r"(?:问题类型|问题描述|转人工原因)\s*[:：]"
)


def _primary_conversation_evidence(
    value: Any,
    limit: int = 9000,
) -> str:
    """Return actual dialogue without the untrusted transfer-trigger header."""
    raw_text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    dialogue_lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip() and not _TRANSFER_METADATA_LINE_RE.match(line)
    ]
    dialogue = "\n".join(dialogue_lines)
    if len(dialogue) <= limit:
        return dialogue
    marker = "\n……中间部分已截断……\n"
    available = max(0, limit - len(marker))
    head_length = int(available * 0.68)
    tail_length = available - head_length
    return f"{dialogue[:head_length]}{marker}{dialogue[-tail_length:]}"


def _cluster_review_evidence_payload(value: Any) -> Any:
    """Keep chat primary while retaining human-corrected clustering evidence."""
    if isinstance(value, list):
        return [_cluster_review_evidence_payload(item) for item in value]
    if not isinstance(value, dict):
        return value

    payload: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"source_conversation", "聊天内容", "conversation"}:
            payload["primary_conversation_evidence"] = (
                _primary_conversation_evidence(item)
            )
        elif key in {
            "source_core_problem",
            "原始核心问题",
            "upstream_core_problem",
        }:
            payload["human_corrected_core_problem"] = _text(item, 1200)
        elif key == "核心问题":
            payload["atomic_question"] = _text(item, 1200)
        elif key in {
            "source_judgment_conclusion",
            "判定结论",
            "原始判定结论",
            "upstream_judgment_conclusion",
        }:
            payload["human_corrected_judgment_conclusion"] = _text(
                item,
                1200,
            )
        elif key == "quality_question_description":
            continue
        else:
            payload[key] = _cluster_review_evidence_payload(item)
    return payload


def _standard_ref(item: StandardCatalogItem) -> str:
    return item.standard_id or item.standard_path or item.title


def _standard_payload(matches: list[tuple[StandardCatalogItem, float]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item, score in matches:
        payload.append(
            {
                "standard_ref": _standard_ref(item),
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
        )
    return payload


def _source_payload(source_row: dict[str, Any]) -> dict[str, str]:
    fields = [
        "工单ID", "回收单号", "回收业务层级", "产品类型", "一级分类", "二级分类",
        "核心问题", "聊天内容", "判定结论", "判定依据", "参考话术",
    ]
    return {field: _text(source_row.get(field)) for field in fields if _text(source_row.get(field))}


def _image_metadata(images: list[ImageEvidence]) -> list[dict[str, Any]]:
    return [image.metadata() for image in images]


def _remote_media_urls(value: Any, limit: int) -> list[str]:
    urls: list[str] = []
    for raw_url in split_image_urls(_text(value, 12000)):
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if parsed.username or parsed.password:
            continue
        urls.append(raw_url)
        if len(urls) >= limit:
            break
    return list(dict.fromkeys(urls))


def _cluster_media_parts(
    source_row: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    image_urls = _remote_media_urls(source_row.get("图片链接"), 4)
    video_urls = _remote_media_urls(source_row.get("视频链接"), 2)
    parts = [
        {"type": "image_url", "image_url": {"url": url}}
        for url in image_urls
    ]
    parts.extend(
        {"type": "video_url", "video_url": {"url": url}}
        for url in video_urls
    )
    return parts, {
        "mode": "mimo-direct-multimodal" if parts else "text-only",
        "images": [{"url": url, "status": "attached"} for url in image_urls],
        "videos": [{"url": url, "status": "attached"} for url in video_urls],
    }


def _should_attach_cluster_media(
    source_row: dict[str, Any],
    media_parts: list[dict[str, Any]],
    *,
    policy: str,
    min_text_chars: int,
) -> bool:
    if not media_parts:
        return False
    normalized_policy = policy.strip().lower()
    if normalized_policy == "always":
        return True
    if normalized_policy == "never":
        return False
    if any(part.get("type") == "video_url" for part in media_parts):
        return True
    conversation = _primary_conversation_evidence(
        source_row.get("聊天内容"),
        max(9000, min_text_chars),
    )
    explicit_visual_markers = (
        "请看图",
        "看下图",
        "图中",
        "上图",
        "下图",
        "截图",
        "照片中",
        "实拍图",
        "圈出",
        "框出",
        "标记位置",
        "这个位置",
        "视频中",
        "视频里",
    )
    if any(marker in conversation for marker in explicit_visual_markers):
        return True
    return len(conversation) < max(80, min_text_chars)


def _topic_signal_source_payload(source_row: dict[str, Any]) -> dict[str, Any]:
    """Separate primary conversation evidence from legacy classifier metadata."""
    return {
        "primary_evidence": {
            "work_order_id": _text(source_row.get("工单ID")),
            "business_line": _text(source_row.get("回收业务层级")),
            "product_type": _text(source_row.get("产品类型")),
            "conversation": _text(source_row.get("聊天内容"), 9000),
            "historical_actual_reply": _text(
                source_row.get("历史实际回复") or source_row.get("参考话术"),
                4000,
            ),
            "video_links": _text(source_row.get("视频链接"), 3000),
            "video_processing_status": _text(source_row.get("视频处理状态")),
        },
        "legacy_reference_only": {
            "quality_question_description": _text(source_row.get("核心问题")),
            "legacy_judgment": _text(source_row.get("判定结论")),
            "legacy_basis": _text(source_row.get("判定依据")),
            "legacy_category_l1": _text(source_row.get("一级分类")),
            "legacy_category_l2": _text(source_row.get("二级分类")),
        },
    }


def _build_topic_signal_prompt(
    source_row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    images: list[ImageEvidence],
    retry_reason: str = "",
    use_standard_references: bool = True,
) -> str:
    has_ready_images = any(image.status == "ready" and image.data_url for image in images)
    image_note = (
        "没有可用图片。"
        if not has_ready_images
        else (
            "已附上现场图片；图片是会话证据的一部分，不能脱离有效标准单独下结论。"
            if use_standard_references
            else "已附上现场图片；图片是会话证据的一部分，不能脱离完整会话和已知事实单独下结论。"
        )
    )
    video_note = (
        _text(source_row.get("视频处理状态"))
        or ("存在视频链接，但当前接口未上传或解析视频内容。" if _text(source_row.get("视频链接")) else "没有视频链接。")
    )
    evidence_instruction = (
        "必须以【primary_evidence】中的完整聊天内容和可用图片为主，结合本次检索到的有效标准理解用户真实意图。"
        if use_standard_references
        else "必须只以【primary_evidence】中的完整聊天内容、历史实际回复和可用图片为主；本模式不检索、不引用质检标准。"
    )
    standard_rule = (
        "standard_refs 只能从本次检索结果的 standard_ref 中选择；无可靠匹配则输出 []，并将 needs_human_review 设为 true。"
        if use_standard_references
        else "standard_refs 必须输出 []。不得补写、猜测或引用任何质检标准。"
    )
    standard_section = (
        "【本次检索到的当前品类生效标准与已有知识】\n"
        f"{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}"
        if use_standard_references
        else "【标准引用模式】\n本次关闭标准检索与标准引用，只处理第二部分案例证据。"
    )
    resolution_examples = (
        "“对照标准判定”“补拍图片核验”“信息查询与实物核对”“补充证据后再判定”"
        if use_standard_references
        else "“结合案例证据判断”“补拍图片核验”“信息查询与实物核对”“补充信息后再处理”"
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    terminology_prompt_block = _terminology_prompt_block(source_row)
    return f"""你是答疑中台的会话语义标注员。从一条已脱敏的多品类质检会话中提取用于主题聚类的规范化标签。当前生效品类为：{product_category_prompt()}。

本任务不是知识改写，也不是复述百晓生的质检问题。{evidence_instruction} legacy_reference_only 中的质检问题描述、历史判定和历史一二级分类来自旧系统，只能在与聊天证据一致时作弱参考；出现冲突时必须以完整会话为准。

{terminology_prompt_block}

红线规则：
1. intent 只能为："标准判定"、"检测核验"、"信息查询"、"流程操作"、"其他待确认" 之一。
2. subject 表示用户实际询问的对象或部位，只使用聊天中明确说出的描述，不过度细化。无法确认时填“待确认”。
   phenomenon 表示异常现象或查询目标。无法确认时填“待确认”，不得从旧分类猜测。
3. resolution_mode 表示最终应沉淀的处理方式，例如{resolution_examples}。
4. topic_tags 必须是 3 到 6 个可复用的短标签，使用“维度:值”格式，且至少覆盖 intent、subject、phenomenon 或 query_target、resolution_mode。不要复制旧分类名称凑标签。
5. {standard_rule}
6. category_l1 和 category_l2 是模型推断的工作分类，必须遵守：
   a. 不得直接照抄 legacy 分类。
   b. 以答疑人员的最终判定结论为准，不以回收师初始提问的字面词为准。
      例：问“是不是漏液”→答“选折痕”→分类跟“折痕”（外观问题）走，不跟“漏液”（显示问题）走。
   c. 理解检测项目的真实目的，不望文生义。
      例：“白光检测”的目的=检查屏幕是否被更换/拆修→分类为拆修问题，不是显示问题。
   d. 无充分证据时填“待确认”。
7. 需要通过外观、部位、颜色、裂纹、坏点、拆修痕迹等视觉差异才能判断时，requires_images=true；图片不可用或不足时在 image_evidence_summary 说明，并标记人工复核。
8. 当前接口没有上传视频内容。若存在视频链接，只能把它视为“尚待人工查看的视频证据”，不得猜测视频画面、动作或声音；结论依赖视频时应标记人工复核。
9. reasoning_summary 仅写给审核人的结论依据，不要输出思维过程，不超过 240 字。
10. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "intent": "标准判定 / 检测核验 / 信息查询 / 流程操作 / 其他待确认",
  "subject": "string",
  "phenomenon": "string",
  "resolution_mode": "string",
  "category_l1": "string",
  "category_l2": "string",
  "topic_tags": ["维度:值"],
  "standard_refs": ["standard_ref"],
  "requires_images": false,
  "image_evidence_summary": "string",
  "reasoning_summary": "string",
  "confidence": 0.0,
  "needs_human_review": true
}}

--- 示例 ---

示例输入：
回收师询问“帮我看一下这个是漏液吗”，答疑经过多轮确认（多颜色显示、息屏状态、触感）后说“选折痕”。
正确输出：
{{
  "intent": "标准判定",
  "subject": "折叠屏内屏",
  "phenomenon": "全颜色可见+息屏可见+有触感的异常区域",
  "resolution_mode": "多颜色+息屏+触感综合判断，排除漏液，归为折痕",
  "category_l1": "外观问题",
  "category_l2": "折痕/压伤",
  "topic_tags": ["intent:标准判定", "subject:折叠屏", "phenomenon:全色可见异常", "resolution_mode:多维度排除法", "category:外观问题"],
  "standard_refs": [],
  "requires_images": true,
  "image_evidence_summary": "需多颜色背景和息屏对比图确认",
  "reasoning_summary": "答疑通过多颜色+息屏+触感三维排除漏液，最终判定为折痕，属于外观问题",
  "confidence": 0.82,
  "needs_human_review": true
}}

【会话输入】
{json.dumps(_topic_signal_source_payload(source_row), ensure_ascii=False, indent=2)}

{standard_section}

【图片情况】
{image_note}
图片下载元数据：{json.dumps(_image_metadata(images), ensure_ascii=False)}

【视频情况】
{video_note}
{retry_instruction}
"""


def _build_prompt(
    source_row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    images: list[ImageEvidence],
    retry_reason: str = "",
) -> str:
    standard_payload = _standard_payload(matches)
    has_ready_images = any(image.status == "ready" and image.data_url for image in images)
    image_note = "本条没有可用图片。" if not has_ready_images else "已附上可用现场图片；图片只能作为证据，不能脱离标准自行下结论。"
    chat_note = (
        "本条未提供原始聊天内容；只能依据结构化字段、图片证据和检索标准，"
        "不得补写会话中未出现的事实，并将 needs_human_review 设为 true。"
        if not _text(source_row.get("聊天内容"))
        else ""
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    terminology_prompt_block = _terminology_prompt_block(source_row)
    return f"""你是答疑中台的多品类回收质检知识标注员。请把一条第二部分会话记录改写成一条可供人工复核的知识候选。当前生效品类为：{product_category_prompt()}；无法确认时填写“{UNKNOWN_PRODUCT_NAME}”并进入人工复核。

{terminology_prompt_block}

必须遵守：
1. 只能依据输入会话、图片证据和下方【本次检索到的当前品类标准与已有知识】；不可捏造标准。knowledge_type 为“已有知识”时优先复用其口径，只有现有知识未覆盖时才新增。
2. standard_refs 只能填写本次检索结果中的 standard_ref，且必须是 JSON 字符串数组；不能匹配时填 [] 并将 needs_human_review 设为 true。
3. 图片不能单独构成发布结论；图片模糊、不可判断或与标准不充分对应时，在 image_evidence_summary 中说明，并设 needs_human_review=true。
4. reasoning_summary 是给审核人的简短依据摘要，不要输出思维过程，不超过 240 字。
5. title 必须是可复用的知识标题，不得复述“回收师遇到/咨询/希望获得”等会话叙述。subtitles 必须尽可能填写 2～6 个、最多 8 个可检索的自然问句：第一个为覆盖整条知识范围的面问法，其余为覆盖阈值、正反条件、分支、测量或操作要点的点问法。副标题不是主标题的机械同义改写，不得只写品类、部位、范围、标准编号、审核状态或关键词；每条都应保留与主标题一致的对象、现象和适用范围粒度。content 必须采用 content_type 对应的简洁编号格式，总长度不超过 500 字；不得整段复制输入的核心问题、判定依据或参考话术。
6. content_type 只能为“定义型”“阈值型”“核验型”“区分型”，按以下优先级选择：多个易混淆现象且存在“区分、优先按、转按、分别计数”等关系时为区分型；必须依赖明确操作、测试或取证步骤才能判断时为核验型；单一现象存在尺寸、数量、时长、次数等决定性条件时为阈值型；其余仅解释单一现象含义或优先级时为定义型。定义型通常写 1～2 条；阈值型、核验型按已有证据写 1～3 条或 1～4 条；区分型通常写 2～5 条。只保留解决当前问题的最少要点，没有来源依据的组件不要凑写，也不要使用空占位。
7. 若会话没有“具体对象 + 明确现象/图片证据 + 可对应标准”中的关键证据，或包含“疑似、不确定、证据不足、未识别、未发现”等表达，不得给出确定性质检结论。此时 knowledge_form 必须为“流程方法”，内容应给出检查步骤、需补充证据和标准对照，并以“补充证据后再判定”收口；本次个案结论不可外推为通用规则。
8. applicable_scope 只能填写当前生效品类中的正式名称，例如“手机”“笔记本”“平板电脑”，不得包含任何“-通用”“-iOS”“-Windows”或其他后缀。来源明确限制品牌时填写 applicable_brands；来源明确限制具体机型时填写 applicable_models；没有明确来源时两个数组都填 []，不得推测。
9. recommended_reply 是人工答疑可直接发送的推荐回复，80～180 字，语气简洁，不写内部审核术语，不承诺证据不足时的确定结论。
10. 只输出一个 JSON 对象，不要 Markdown，不要额外解释。字段必须完整：
{{
  "title": "string",
  "subtitles": ["string"],
  "content": "string",
  "content_type": "定义型、阈值型、核验型或区分型",
  "category_l1": "string",
  "category_l2": "string",
  "layer": "L2",
  "knowledge_form": "具体判定 或 流程方法",
  "standard_refs": ["standard_ref"],
  "applicable_scope": "string",
  "applicable_brands": ["string"],
  "applicable_models": ["string"],
  "recommended_reply": "string",
  "confidence": 0.0,
  "reasoning_summary": "string",
  "needs_human_review": true,
  "image_evidence_summary": "string"
}}

【第二部分记录】
{json.dumps(_source_payload(source_row), ensure_ascii=False, indent=2)}

【本次检索到的当前品类标准与已有知识】
{json.dumps(standard_payload, ensure_ascii=False, indent=2)}

【图片情况】
{image_note}
图片下载元数据：{json.dumps(_image_metadata(images), ensure_ascii=False)}
{chat_note}
{retry_instruction}
"""


def _build_topic_prompt(
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    retry_reason: str = "",
    use_standard_references: bool = True,
) -> str:
    evidence_instruction = (
        "只能依据主题特征、来源事实证据包和本次检索标准"
        if use_standard_references
        else "只能依据主题特征、来源事实证据包、历史实际回复和对应案例图"
    )
    concrete_rule = (
        "只有同时满足“明确边界问题 +（可引用的本次标准或足够支持的主题来源事实）”时，knowledge_form 才可为“具体判定”；无标准引用时必须标记人工复核，不能把经验补充写成总部标准。"
        if use_standard_references
        else "只有主题内案例证据清楚、一致且足以支持可复用结论时，knowledge_form 才可为“具体判定”；否则必须为“流程方法”。"
    )
    standard_rule = (
        "standard_refs 只能填写本次检索结果的 standard_ref；knowledge_type 为“已有知识”时优先复用，未覆盖才新增；标准依据来源会在主题输入中说明，只有明确为总部标准时才可称“总部标准”，本地质检标准不得伪装成总部标准；无可信标准时填 []，仍可依据来源事实、历史实际回复和人工判定结论转写为经验补充候选，但必须 needs_human_review=true，且不得编造标准条款、阈值或平台规则。"
        if use_standard_references
        else "standard_refs 必须输出 []。不得根据记忆补写标准，也不得把质检标准问题改写成案例知识。"
    )
    standard_section = (
        "【本次检索到的当前品类生效标准与已有知识】\n"
        f"{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}"
        if use_standard_references
        else "【标准引用模式】\n本次关闭标准检索与标准引用，草稿必须完全来源于主题案例证据。"
    )
    image_flow_rule = (
        "确认部位、补充近景/全景/多角度证据、对照有效标准、证据不足时补充证据后再判定"
        if use_standard_references
        else "确认部位、补充近景/全景/多角度证据、结合完整会话与案例证据核验、信息不足时补充后再处理"
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    terminology_prompt_block = _terminology_prompt_block(topic)
    return f"""你是答疑中台的多品类质检知识标注员。输入是已经聚类的 1～N 个原子问题或案例特征，不是固定两两配对。当前生效品类为：{product_category_prompt()}。

请沉淀一条可复用、供人工审核的主题级知识草稿。{evidence_instruction}；不能把任何单条工单结论直接外推为通用事实。

{terminology_prompt_block}

必须遵守：
1. 主标题必须是自然、清楚、可直接使用的知识标题，优先写成用户会检索的问题或明确判定主题；不得堆砌关键词、使用斜杠串词，也不得直接使用“质检记录规则”“记录要求”“管理规定”等描述文档形式的公文式标题。若主题确实涉及记录要求，应改写成“工单中需要记录哪些信息”一类自然问法。subtitles 不得默认留空：应尽可能输出 2～6 个、最多 8 个可检索的自然问句。第一个必须是覆盖整条知识范围的面问法；其余是覆盖阈值、正反条件、分支、测量或操作要点的点问法。副标题不是主标题的机械同义改写，不得只写品类、部位、范围、标准编号、审核状态或关键词；每条都必须保留与主标题一致的对象、现象和适用范围粒度。
2. {concrete_rule} 其余情况必须为“流程方法”。
3. 外观、显示、拆修、胶状物、功能异常等需要现场图片判断的问题，默认沉淀核验过程：{image_flow_rule}。
4. 只有机型/型号身份或功能结论在来源中无法确认时，才输出查询与核对流程；来源已经明确机型及功能结论时，必须输出该机型的具体事实，不得改写成“查询官网/核对型号”的泛化流程。
5. {standard_rule}
6. reasoning_summary 只写简短审核依据，不输出思维过程；不要编造聊天细节、图片细节或标准条款。
7. 文字已经能完整表达规则时，requires_images=false，不能把图片当装饰；只有必须通过外观、部位、颜色、裂纹、坏点或拆修痕迹等视觉差异才能解释时，requires_images=true，并给出 image_usage_instruction。
8. content_type 只能为“定义型”“阈值型”“核验型”“区分型”，并按以下优先级选择：多个易混淆现象且存在“区分、优先按、转按、分别计数”等关系时为区分型；必须依赖明确操作、测试或取证步骤才能判断时为核验型；单一现象存在尺寸、数量、时长、次数等决定性条件时为阈值型；其余仅解释单一现象含义或优先级时为定义型。content 必须使用对应的简洁编号格式：定义型通常 1～2 条，阈值型和核验型按已有证据写 1～3 条或 1～4 条，区分型通常 2～5 条；只写来源支持的最少规则，不得写空占位或通用流程凑数。不得使用“问题背景、判断对象、来源核验依据、人工处理结论”等内部分析报告标签；应直接写适用情形、核验要点、处理结论和适用边界。applicable_scope 只能填写品类名称，不得带任何后缀；来源明确限制品牌或具体机型时分别填写 applicable_brands、applicable_models，否则都填 []。单机型事实时，标题、正文、推荐回复和 applicable_models 都必须保留该机型，且不得外推到整个品类或其他机型。不得根据常识推测品牌或机型。recommended_reply 为 80～180 字、可直接发送的人工答疑回复，且必须保留正文中的具体对象、结论或处理边界。recommended_reply 只能回答当前单一原子主题，不得把同一会话中其他原子问题、副主题或对应答案拼接进来；即使相关文字在历史实际回复中出现过，也属于当前主题外内容，必须忽略。不得重复问候，不得使用“关于另一问题”“针对其他问题”等标签切换主题，不得输出残句或“，；”“，。”等异常标点。
9. 无标准引用模式下，正文必须提炼来源聊天、历史实际回复或判定结论中的具体信息，至少覆盖“具体对象/代际/来源问题”“检查项或触发条件”“实际结论或处理方式”“不能适用时的边界”中的 2 项。禁止只输出“适用主题、明确对象、补充截图/视频/查询结果、结合案例、完善资料后再处理”之类的通用四步模板，也不得套用“适用对象、处理原则、记录要求、边界说明”这类制度条款模板把个案包装成通用规则。
10. 【来源事实证据包】中的 facts 是全部可审计来源，representative_facts 是本轮优先组织知识的代表性证据。人工核心问题和人工判定结论是明确的结构化事实，必须与聊天、历史实际回复共同使用；发生冲突时标记人工复核，不得静默覆盖。
11. 每个对象、条件、结论、例外和图片说明都必须能回到证据包中的事实ID。没有来源的内容不得补写；来源没有提供核验方式或适用边界时，应明确说明证据缺口并标记人工复核，不能使用模型常识扩写。
12. 案例图只能使用 case_images 或 representative_facts.image_urls 中与该事实ID对应的链接，不得引用其他主题或其他案例图片。
13. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "title": "string",
  "subtitles": ["string"],
  "content": "string",
  "content_type": "定义型、阈值型、核验型或区分型",
  "category_l1": "string",
  "category_l2": "string",
  "layer": "L2",
  "knowledge_form": "具体判定 或 流程方法",
  "standard_refs": ["standard_ref"],
  "applicable_scope": "string",
  "applicable_brands": ["string"],
  "applicable_models": ["string"],
  "recommended_reply": "string",
  "confidence": 0.0,
  "reasoning_summary": "string",
  "needs_human_review": true,
  "image_evidence_summary": "string",
  "requires_images": false,
  "image_usage_instruction": "string"
}}

【主题输入】
{json.dumps(topic, ensure_ascii=False, indent=2)}

{standard_section}
{retry_instruction}
"""


def _build_topic_review_prompt(
    topic: dict[str, Any],
    draft: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    transcription_matches: list[tuple[StandardCatalogItem, float]] | None = None,
    retry_reason: str = "",
    use_standard_references: bool = True,
) -> str:
    responsibility = (
        "不得修改标题、正文、分类或标准引用"
        if use_standard_references
        else "不得修改标题、正文或分类，也不得要求补写标准引用"
    )
    evidence_rule = (
        "只能依据来源事实证据包、转写阶段使用的标准和本次独立检索到的标准审核"
        if use_standard_references
        else "只能依据来源事实证据包、来源案例、历史实际回复和对应案例图审核"
    )
    concrete_rule = (
        "草稿为“具体判定”时，必须有本次检索标准引用，或有清楚、一致且足够的主题来源事实；后者只能作为经验补充候选，必须重点人工复核。"
        if use_standard_references
        else "草稿为“具体判定”时，必须有清楚、一致且可复用的案例证据；证据不足时结论为“需修改”或“证据不足待补充”。"
    )
    standard_rules = (
        "4. 无可信标准时，standard_consistency 填“无可信标准”，必须标记重点复核；若正文和推荐回复均有充分来源事实支撑，可以作为经验补充候选标“通过”，但不得伪装为总部标准或编造阈值、条款。\n"
        "8. 转写引用标准与独立复核标准不一致时，应标记“标准项映射错”或“标准召回不足”。"
        if use_standard_references
        else "4. 本模式不使用标准引用；standard_consistency 固定填写“无可信标准”，但不能仅因此判定不通过。\n"
        "8. 草稿、回复或图片说明中出现编造的标准编号、标准条款或标准问题时，应标记需修改。"
    )
    standard_sections = (
        "【本次检索到的当前品类生效标准与已有知识】\n"
        f"{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}\n\n"
        "【转写阶段使用的标准】\n"
        f"{json.dumps(_standard_payload(transcription_matches or []), ensure_ascii=False, indent=2)}"
        if use_standard_references
        else "【标准引用模式】\n本次关闭标准检索与标准引用，不得因缺少标准而否决草稿。"
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    terminology_prompt_block = _terminology_prompt_block(topic)
    return f"""你是答疑中台的多品类质检知识内容质量初审员。现在需要审核一条已经转写完成的主题级知识草稿。当前生效品类为：{product_category_prompt()}。

你的职责是“审核标注”，不是改写知识：{responsibility}；只判断草稿能否进入人工复标。

该主题的沉淀价值已经在转写前完成；所有通过聚类准入的主题都可能进入本环节。你不得重新判断是否值得沉淀，knowledge_value 仅作兼容字段，原样返回主题输入中的 knowledge_value；缺失时返回“待确认”。

{terminology_prompt_block}

审核规则：
1. {evidence_rule}，不得补充未提供的事实。
2. {concrete_rule}
3. 外观、显示、拆修、胶状物、功能等依赖图片的问题，草稿沉淀为核验流程是合理的；不能因为没有个案最终判定而驳回流程型知识。
{standard_rules}
5. 必须审核知识内容是否准确覆盖规则、处理步骤和限制条件，不能只检查格式和字段完整性。
6. 必须审核主标题是否自然清楚、副标题是否只是关键词堆砌；直接使用“质检记录规则”“记录要求”“管理规定”等公文式元描述的标题必须标记为需修改，但“工单中需要记录哪些信息”一类自然问法可以保留。主标题清楚时不应强行要求副标题。
7. 必须审核图片必要性：文字能说清时不应要求图片；依赖视觉差异时没有保留图片，应标记需修改或证据不足。
9. 本环节只审核标题、正文、分类、推荐回复、证据一致性和图片必要性；不得因为重新评价沉淀价值而驳回草稿。
10. knowledge_value 只是兼容字段，必须原样返回主题输入中的 knowledge_value；不得在本环节重判沉淀价值。
11. 逐项核对草稿中的对象、条件、结论、例外和图片说明是否能回到主题信息中的事实ID；找不到来源事实的内容必须判为“需修改”或“证据不足待补充”。无标准引用模式下，正文若套用“适用对象、处理原则、记录要求、边界说明”等制度条款模板，应检查是否把单个案例外推成了通用规则。
12. 人工核心问题和人工判定结论必须明确纳入审核；它们与聊天证据冲突时必须重点复核，不能选择性忽略任一侧。
13. 推荐回复必须只服务当前单一原子主题。若混入其他原子问题或答案、重复问候、使用“关于××问题”“针对××”切换主题、出现残句或“，；”“，。”等异常标点，必须标记“需修改”；不能因为这些内容出现在历史实际回复中就判断一致。
14. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "decision": "通过 / 需修改 / 驳回 / 证据不足待补充",
  "knowledge_value": "值得沉淀 / 不值得沉淀 / 待确认",
  "error_type": "string",
  "reason": "string",
  "standard_consistency": "一致 / 不一致 / 无可信标准",
  "evidence_sufficiency": "充分 / 部分充分 / 不足",
  "content_consistency": "一致 / 部分一致 / 不一致",
  "image_necessity": "需要保留 / 不需要 / 图片不足",
  "title_quality": "清晰 / 需修改",
  "confidence": 0.0,
  "priority_review": true
}}

【主题信息】
{json.dumps(topic, ensure_ascii=False, indent=2)}

【待审核的转写草稿】
{json.dumps(draft, ensure_ascii=False, indent=2)}

{standard_sections}
{retry_instruction}
"""


def _build_topic_stage_prompt(
    topic: dict[str, Any],
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    terminology_prompt_block = _terminology_prompt_block(topic)
    return f"""你是答疑中台的主题分类与知识沉淀价值标注员。输入是已经完成原子问题拆分和聚类的一个主题，可能包含 1～N 个来源案例。

请完成两个互相独立的判断：
1. 判断该主题主要属于哪个环节：质检标准、质检流程、案例解析、课外常识、不确定。
2. 判断该主题是否值得沉淀为可复用知识：值得沉淀、不值得沉淀。

{terminology_prompt_block}

证据优先级：
- conversation_evidence、historical_replies、evidence_summaries，以及从完整会话提取的 intents、subjects、phenomena、resolution_modes 是主要会话证据。
- 人工校正后的核心问题、产品类型和判定结论是重要结构化证据。产品类型是硬边界；核心问题和判定结论可补充本轮未读取图片时已由第二部分人工确认的对象、现象和结论。
- normalized_issues 应是会话证据与人工校正字段的综合提炼。若两类证据冲突，必须标记 needs_human_review=true，不得静默选择一方。
- 判定依据、旧分类和已有标准路径仍只作为弱参考，不得据此补写输入中没有的标准、阈值、边界或步骤。

环节定义：
- 质检标准：核心在“判什么、算不算、是否合格、应选哪个质检项、等级/阈值/边界是什么”；可复用答案应是判定口径、适用条件或例外边界。
- 质检流程：核心在“怎么查、怎么测、怎么操作、先后步骤、使用什么入口或工具”；可复用答案应是检查或操作步骤，而不是某台设备的最终结论。
- 案例解析：核心结论依赖当前案例的图片、视频、实物状态或上下文，只能分析这一个案例；离开该案例证据无法直接得出同样结论。
- 课外常识：核心是型号、版本、功能、配件、行业常识等信息，不是在询问质检判定标准、质检操作流程，也不是要求分析当前具体案例。
- 不确定：证据不足、主要诉求互相冲突，或需要人工结合业务背景才能在上述四类中选择；使用该值时 needs_human_review 必须为 true。

冲突处理顺序：
1. 不要因为文本出现“标准、流程、案例”等字样直接分类，要判断回答该主题真正需要输出什么。
2. 若问题由具体案例触发，但可以抽象成稳定的判定口径，标“质检标准”；只有结论主要依赖该案例独有证据时才标“案例解析”。
3. 同时包含“怎么检查”和“检查后判什么”时，以主要诉求为准；无法确认主要诉求时选择证据更充分的一类，并将 needs_human_review=true。
4. 机型/版本查询若只是回答产品事实，标“课外常识”；若是在说明质检项如何读取或核对，标“质检流程”。
5. 若证据摘要显示后台只是查看当前图片/视频后回复“正常、异常、可以、没事”等结论，且没有给出通用判断条件，必须标“案例解析”，不能仅因 normalized_issue 或 intent 中出现“标准判定”而标“质检标准”。

沉淀价值规则：
- 值得沉淀：能形成稳定、清楚、可复用的判定口径、操作流程、查询方法，或为少见案例、特殊案例、标准咨询提供明确边界和处理原则；适用于后续类似问题，而不是只记录一个工单答案。
- 基础常见问题若已有稳定知识覆盖，且当前主题没有新增边界、例外、反例或操作差异，应标“不值得沉淀”，避免重复收集。
- 不值得沉淀：只有单个案例结论、缺少可复用规则或步骤、证据冲突/严重不足、主题过于模糊，或内容只是“看图后正常/异常”且无法说明可复用依据。
- “案例解析”并不自动等于不值得沉淀。若该案例具有代表性，能够提炼出明确边界、核验要点或反例说明，仍可标“值得沉淀”。
- 不得为了提高沉淀率而补写输入中没有的标准、事实、阈值或处理步骤。
- 必须依据当前输入已经包含的信息判断，不能因为“该问题高频、以后补充标准后可能有价值”就标“值得沉淀”。
- 单成员主题不等于不值得沉淀。少见案例、特殊案例或标准咨询只要输入明确提供了可复用条件、检查步骤、定义、判定边界或例外，就可标“值得沉淀”；不得为了获得多成员簇而强行合并。
- 单成员主题中的个案回复不能自动外推为平台通用标准。若只有当前机器的“正常/异常/某分类”结论，仍应标“不值得沉淀”。
- 单成员主题若只是“某个具体现象 → 正常/异常/某分类”的直接映射，且没有可信标准来源、多个适用条件或可执行核验步骤，必须标“不值得沉淀”。“工具结果与人工复核冲突时以谁为准”这类通用冲突处理原则，或包含明确先后步骤的方法，可以作为例外。
- 若 standard_paths、thresholds_or_exceptions 等关键字段为“待确认/无明确阈值”，且当前输入没有其他明确规则或方法，应标“不值得沉淀”。
- reusable_knowledge 只能总结输入中已经出现的规则或步骤。若输入不足，直接说明缺失项，禁止推测可能原因、行业惯例或平台标准。
- 主题来源字段互相冲突、上游需要复核、或你的结论依赖把单案例外推为通用规则时，needs_human_review 必须为 true。

只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "topic_stage": "质检标准 / 质检流程 / 案例解析 / 课外常识 / 不确定",
  "knowledge_value": "值得沉淀 / 不值得沉淀",
  "stage_reason": "string",
  "value_reason": "string",
  "reusable_knowledge": "可沉淀的规则、步骤或知识摘要；不值得沉淀时说明缺失项",
  "confidence": 0.0,
  "needs_human_review": true
}}

【主题输入】
{json.dumps(topic, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _build_topic_display_questions_prompt(
    topics: list[dict[str, Any]],
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    terminology_prompt_block = _terminology_prompt_block(topics)
    return f"""你是答疑中台的主题问句改写员。请把每个规范化主题改写成组员一眼能懂的一句现场提问。

{terminology_prompt_block}

改写要求：
1. 每个主题只输出一个问句，必须保留 theme_id。
2. 问句必须以中文问号“？”结尾，建议 8～28 个汉字，最长不超过 40 个字符。
3. 使用现场自然说法，优先采用以下句式：
   - “防水标变红怎么判？”
   - “电池健康度读不出来怎么办？”
   - “自动检测异常但人工复检正常时怎么判？”
   - “型号ZP是什么版本？”
4. 保留必要的对象、现象和判断目标；删除机型年份、尺寸等不影响理解的冗余信息。
5. 不要出现“回收师、用户、咨询、希望、主题、标准判定、案例解析、知识沉淀”等后台术语。
6. 不要写答案、结论、原因或操作步骤，不要补充输入中没有的事实。
7. 多个来源问题属于同一主题时，概括它们共同的问题，不要逐个罗列案例。
8. 只输出一个 JSON 对象，不要 Markdown：
{{
  "questions": [
    {{"theme_id": "C001", "question": "防水标变红怎么判？"}}
  ]
}}

【待改写主题】
{json.dumps(topics, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _build_cluster_pair_review_prompt(
    left: dict[str, Any],
    right: dict[str, Any],
    similarity: float,
    threshold: float,
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    left_payload = _cluster_review_evidence_payload(left)
    right_payload = _cluster_review_evidence_payload(right)
    terminology_prompt_block = _terminology_prompt_block(left, right)
    clustering_rules_prompt_block = _clustering_rules_prompt_block(left, right)
    return f"""你是人工答疑知识库的主题聚类审核员。请判断候选原子知识 A 是否能够加入主题簇 B。

主题簇 B 可以包含 1 到 N 个知识点。不得只挑选其中最相似的一条进行判断；只有候选 A 能够与簇内所有成员共用同一条标准答疑知识时，才允许加入。

唯一标准：合并后是否可以直接共用同一条知识标题、适用范围、判定标准和处理结论。

{terminology_prompt_block}

{clustering_rules_prompt_block}

判断优先级：
1. 回收业务层级不同绝对禁止合并，例如自营回收与聚合回收不能合并。
2. 产品品类不同绝对禁止合并，这是不可放宽的硬门禁。
3. 同业务层级、同品类内优先比较已固化的标准族、现象值、核心对象和最终判定目标。
4. 二级分类或标准路径的文字不同，不是绝对拆分条件；只有语义、阈值或最终处理结论确实不同时才拆分。

只有同时满足以下条件才能判断为"同一主题"：
1. 适用品类、平台、品牌和机型范围一致，或者明确属于同一通用标准。
2. 核心对象或部位一致。
3. 判定目标一致。
4. 标准处理路径一致。
5. 阈值、例外条件不会产生不同答疑结论。

以下情况可以判断为"同一主题"（即使机型/品牌不同）：
- 询问的是同一判定原则，如"全新机判定标准"、"外观损伤定性（磕碰/碎裂/划痕的边界）"、"包装塑封是否完整"。不同机型/品牌只要共享同一个判定口径，就可以合并。
- 核心对象相同 + 判定目标相同 + 处理路径相同，只是案例来源不同。

以下情况应判断为"不同主题"：
- 回收业务层级不同；自营回收和聚合回收绝对不能聚类到一起。
- 产品品类不同；即使文字、工具或处理逻辑相似，也绝对不能跨品类合并。
- 苹果、安卓、鸿蒙或其他平台适用标准不同。
- 一级分类不同（如功能问题 vs 外观问题，显示问题 vs 拆修问题）。
- 一级分类相同但判定对象不同（如都是"外观问题"，但一个是屏幕胶条、一个是散热结构、一个是滤镜卡住：对象不同=标准不同=不同主题）。
- 经质检口径和聊天证据确认，判定阈值、例外条件或标准处理路径确实不同。
- 合并后需要写多个互不相关的处理结论。
- 只是出现"屏幕、摄像头、拆修、异常"等相同宽泛词语。

特别注意："同属外观问题"不等于"同一主题"。外观问题覆盖胶条、外壳、散热、滤镜、折痕、划痕等多种对象，每种对象的判定标准和操作流程完全不同。只有当品类相同 + 判定对象相同 + 判定标准相同时，才能判定为同一主题。

证据优先级：
1. primary_conversation_evidence 中的实际问答、追问、澄清和客服答复是第一主证据。
2. human_corrected_core_problem 和 human_corrected_judgment_conclusion 是第二部分人工校正证据，特别用于补充本轮未读取图片时人工已确认的对象、现象和结论。
3. 已经提取出的图片/视频事实是可靠补充证据。
4. 转人工首行的问题描述可能乱填，已从审核输入中移除；旧分类不得作为硬边界。
5. 人工校正字段、结构化原子问题与聊天证据明显冲突时，不得静默选择任一方，应判断为"不确定"或"不同主题"并进入人工复核。

不得猜测未解析的图片或视频内容，不得补充输入中没有的业务标准。

不要根据相似度直接下结论。相似度只用于候选召回，不是合并证据。

--- 示例 ---

输入：记录A normalized_issue="手机｜屏幕｜坏点｜判定是否合格"，记录B normalized_issue="手机｜屏幕｜亮线｜判定是否合格"，相似度=0.85
输出：
{{
  "decision": "不同主题",
  "topic_label": "屏幕坏点判定 vs 屏幕亮线判定",
  "reason": "虽然都是屏幕显示问题，但坏点和亮线的判定标准、阈值和处理路径完全不同，合并后需要写两个互不相关的处理结论。",
  "key_difference": "判定对象不同：坏点 vs 亮线，对应不同的质检标准和阈值",
  "confidence": 0.9
}}

只输出一个 JSON 对象，不要 Markdown：
{{
  "decision": "同一主题 / 不同主题 / 不确定",
  "topic_label": "两条记录可能共享的简短主题；不同主题时概括主要差异",
  "reason": "给人工审核人的简短判断依据，不超过 300 字",
  "key_difference": "决定是否拆分的关键差异；没有则为空字符串",
  "confidence": 0.0
}}

【语义聚类信息】
余弦相似度：{similarity:.4f}
当前阈值：{threshold:.4f}

【记录 A】
{json.dumps(left_payload, ensure_ascii=False, indent=2)}

【记录 B】
{json.dumps(right_payload, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _cluster_unit_input_payload(
    source_row: dict[str, Any],
    attached_image_count: int = 0,
    attached_video_count: int = 0,
    media_policy: str = "",
) -> dict[str, Any]:
    has_image_links = bool(_text(source_row.get("图片链接")))
    has_video_links = bool(_text(source_row.get("视频链接")))
    normalized_policy = media_policy.strip().lower()
    return {
        "work_order_id": _text(source_row.get("工单ID")),
        "product_type": _text(source_row.get("产品类型")),
        "human_corrected_core_problem": _text(
            source_row.get("核心问题") or source_row.get("原始核心问题"),
            1200,
        ),
        "human_corrected_judgment_conclusion": _text(
            source_row.get("判定结论") or source_row.get("原始判定结论"),
            1200,
        ),
        "human_corrected_judgment_basis": _text(
            source_row.get("判定依据"),
            1600,
        ),
        "human_corrected_reference_reply": _text(
            source_row.get("参考话术"),
            1200,
        ),
        "ai_result_conflict_fields": _text(
            source_row.get("AI结果冲突字段"),
            240,
        ),
        "device_model": _text(source_row.get("机型")),
        "primary_conversation_evidence": _primary_conversation_evidence(
            source_row.get("聊天内容"),
            9000,
        ),
        "has_image_links": has_image_links,
        "has_video_links": has_video_links,
        "attached_image_count": attached_image_count,
        "attached_video_count": attached_video_count,
        "media_attachment_status": (
            "已附媒体"
            if attached_image_count or attached_video_count
            else "聚类验证已关闭媒体输入"
            if normalized_policy == "never" and (has_image_links or has_video_links)
            else "聊天证据充分，本轮聚类暂不附媒体"
            if has_image_links or has_video_links
            else "无媒体"
        ),
    }


def _cluster_unit_output_schema(*, batch_mode: bool) -> str:
    topic_schema = {
        "normalized_issue": "可独立聚类的核心问题，不超过80字",
        "product_category": (
            f"一级产品品类，只能为{product_category_prompt()}或"
            f"{UNKNOWN_PRODUCT_NAME}"
        ),
        "scope_type": "通用 / 品类专用 / 平台专用 / 品牌专用 / 机型专用 / 待确认",
        "platform": "iOS / Android / HarmonyOS / Windows / macOS / 通用 / 待确认",
        "brand": "品牌名称；不限制品牌填通用；无法确认填待确认",
        "model_scope": "具体机型、机型系列、通用或待确认",
        "category_l1": "知识一级分类",
        "category_l2": "知识二级分类",
        "intent": "标准判定 / 检测核验 / 信息查询 / 流程操作 / 其他待确认",
        "subject": "实际对象或部位",
        "phenomenon": "异常现象或查询目标",
        "judgment_target": "希望得到的统一判定目标",
        "resolution_mode": "应沉淀的处理方式",
        "standard_path": "适用的标准处理路径；无法确认填待确认",
        "threshold_or_exception": "明确阈值或例外；没有填无明确阈值；无法确认填待确认",
        "evidence_summary": "支持该问题单元的聊天及本轮媒体事实摘要，不超过300字",
        "confidence": 0.0,
        "requires_review": True,
    }
    result_schema = {
        "conversation_type": "single_topic / multi_topic / uncertain",
        "reason": "简短说明为什么单主题、拆分或不确定，不超过240字",
        "media_analysis": {
            "image_summary": "图片事实；无图片填无图片；无法读取需说明",
            "video_summary": "视频事实；无视频填无视频；无法读取需说明",
            "media_relevance": "相关 / 不相关 / 无法读取 / 无媒体",
            "used_for_topic_split": False,
            "requires_review": False,
        },
        "topics": [topic_schema],
    }
    if batch_mode:
        result_schema = {
            "results": [
                {
                    "record_id": "必须原样返回输入中的 record_id",
                    **result_schema,
                }
            ]
        }
    return json.dumps(result_schema, ensure_ascii=False, indent=2)


def _build_cluster_unit_prompt(
    source_row: dict[str, Any],
    retry_reason: str = "",
    attached_image_count: int = 0,
    attached_video_count: int = 0,
    batch_records: list[tuple[str, dict[str, Any]]] | None = None,
    media_policy: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    batch_mode = bool(batch_records)
    if batch_mode:
        payload = {
            "records": [
                {
                    "record_id": record_id,
                    **_cluster_unit_input_payload(
                        record,
                        media_policy=media_policy,
                    ),
                }
                for record_id, record in batch_records or []
            ]
        }
    else:
        payload = _cluster_unit_input_payload(
            source_row,
            attached_image_count,
            attached_video_count,
            media_policy,
        )
    output_schema = _cluster_unit_output_schema(batch_mode=batch_mode)
    task_scope = (
        "一组同品类会话。必须逐条独立判断，每个输入 record_id 恰好返回一次，"
        "不得混合不同 record_id 的聊天证据"
        if batch_mode
        else "一条会话"
    )
    terminology_prompt_block = _terminology_prompt_block(source_row)
    clustering_rules_prompt_block = _clustering_rules_prompt_block(source_row)
    return f"""你是人工答疑知识库的原子知识提取器。判断{task_scope}包含一个还是多个可以独立沉淀的知识主题，并输出用于聚类的原子知识点。

=== 证据规则（8条红线）===

1. primary_conversation_evidence、人工校正后的核心问题和判定结论需要合并使用；判定依据和参考话术作为辅助审计证据一并参考。必须综合阅读整段问答、追问、澄清和客服答复，不能只抓取最后一句或某个关键词。
   聊天开头的“问题类型、问题描述、转人工原因”是系统转人工元数据，已从 primary_conversation_evidence 中排除。
   product_type 是第二部分人工校正后的产品品类，属于不可跨越的聚类硬边界。
   human_corrected_core_problem 和 human_corrected_judgment_conclusion 是第二部分人工复核后的结构化证据；在本轮不读取图片时，它们可补充人工基于图片确认的对象、现象和结论。判定依据和参考话术是辅助审计证据，不能单独覆盖聊天事实。
   如果 ai_result_conflict_fields 非空，说明接口结构化字段与已有字段冲突，必须设置 requires_review=true，并在 reason 中指出冲突字段。

2. 本轮实际附带图片或视频时，它们也是主证据。必须直接识别其中明确可见的事实：
   - 系统截图/弹窗中的文字（型号、序列号、电池健康、系统版本等）→ 直接提取，作为信息查询的证据。
   - 实物照片中的外观、部位、颜色、裂纹、坏点、拆修痕迹 → 只记录确实可见的，不推测不可见的。
   - 视频中的操作过程、动态异常、字幕和可听语音 → 只记录确实发生的。
   - 画面模糊、文字不清、关键部位被遮挡 → 不得猜测，在 media_analysis 中说明并标记 requires_review=true。

3. 媒体无法读取或不足以支持结论时，不得猜测。媒体中出现清晰的第二个独立质检问题时必须按多主题拆分；媒体只是同一问题的补充角度时不得重复拆题。
   如果 media_attachment_status 为“聊天证据充分，本轮聚类暂不附媒体”，表示系统为提升批量聚类效率而只使用完整聊天；不得猜测媒体内容，也不得仅因为本轮未附媒体就标记不确定或 requires_review=true。
   如果 media_attachment_status 为“聚类验证已关闭媒体输入”，表示本次只验证聊天文本聚类；不得引用、概括或猜测图片/视频内容，也不得仅因媒体未输入而标记不确定。

4. 转场词识别：聊天中如果出现"另外/还有/这个也/再看一下/另一个问题/顺便"等转场词，且后续内容是新的独立质检问题，必须考虑多主题拆分。不要因为前后问题在同一个会话里就默认合并。

5. 术语消歧：“一根线/靓机助手/验机精灵/爬虫/工具读出/用户判断”通常指验机工具或检测结果，不代表屏幕上出现物理线条。只有实际聊天或图片明确提到屏幕显示线条时才提取为屏幕线条问题。

6. normalized_issue 必须结合聊天原子诉求、human_corrected_core_problem 和 human_corrected_judgment_conclusion 提炼，不能只复述其中一个字段。
   若人工校正后的核心问题或判定结论与完整聊天明显冲突，不得静默覆盖；应保留聊天中能确认的主题，并设置 requires_review=true，在 reason/evidence_summary 中说明冲突。
   不得根据结构化字段虚构图片中未写明的细节、阈值或标准。

7. 不得根据旧分类习惯猜测 category_l1 或 category_l2。

8. 每个输出主题必须能在实际聊天或可靠媒体中找到独立证据；evidence_summary 应概括真实对话证据。

{terminology_prompt_block}

{clustering_rules_prompt_block}

=== 主题拆分规则（5条红线）===

1. 同一对象+同一异常的追问、澄清、补充图片或处理过程属于一个主题。
   single_topic 必须只输出 1 个原子问题，不得把同一主题重复拆写。
   只有明确命中多主题信号时才标记 multi_topic，并拆成 2～3 个原子问题。
   uncertain 最多保留 1 个暂定原子问题，不得为了凑数量拆分。
    2. 拆分判断：默认不拆。只有命中以下任一必须拆分信号时才拆，且命中任一信号就优先拆分，不要被“同属外观问题”“同一个会话”“同一类质检问题”等宽泛词带偏。
    以下信号可支持拆分：

   信号A — 对象隔离：两个问题的判定对象在物理上独立，且判定标准不同。
   例："振动功能"和"距离感应功能"→拆（独立硬件+独立标准）。
   例："屏幕划痕"和"充电口松动"→拆（不同组件+不同标准）。

   信号B — 分类隔离：两个问题的一级分类不同，且各自需要独立的知识正文。
   例："屏幕有划痕（外观问题）"+"电池健康读取不出（基本信息）"→拆。
   例："摄像头有黑斑（外观问题）"+"拍照闪退（功能问题）"→拆。

   信号C — 答疑分别处理：答疑在聊天中对两个问题分别给出了独立的判定结论或处理路径。
   例：答疑先说"面容判异常"，再说"摄像头另外要判异常"→虽然共享判定原则，但答疑明确分开处理了→视情况可拆。
   信号D — 拆修/非原厂多对象：同一会话里同时询问或答复屏幕、主板、电池、后壳、摄像头、排线等多个拆修对象，且分别对应不同质检项或处理结论时，必须拆分。
   例："主板有标签，屏幕有贴纸怎么判"→拆（主板标签和屏幕贴纸是不同拆修对象/非原厂特征）。
   例：答疑分别给出"屏幕-其它非原厂屏特征、电池-第三方标识、后壳-第三方标识、其它零部件维修特征"→拆（答疑已按部件分别处理）。
   但如果多个部位只是共同佐证同一个"整机是否拆修"结论，且答疑只给一个统一结论，可不拆。
   信号E — 信息查询多目标：同一会话里连续确认 BIOS/锁状态、型号/年款、硬盘/内存品牌、指纹/功能支持等多个独立查询目标，且答疑分别回答，必须拆分。
   例："有没有 BIOS 锁→型号是这个吗→硬盘内存品牌吗→支持指纹吗"→拆（锁状态、型号、硬件品牌、功能支持是不同查询目标）。
3. 即使最终客服只回答了其中一个问题，也不能丢弃会话中清晰存在的另一个独立问题。
   4. 最多提取 3 个主题。寒暄、催促、致谢和系统提示不作为主题。孤立的单个词或客服简短回复（如只出现一次“闪屏”+“正常”）不能自动成为独立主题。
   5. 能明确识别多个独立问题时标记 multi_topic；只有聊天或媒体证据不足时才标记 uncertain。

   不拆的情况：
   - 同一对象的多个描述角度（如"破损"+"凹陷"都是描述胶条）→不拆。
   - 同一问题的追问和澄清（如"屏幕亮线→具体在哪个位置→拍个照看看"）→不拆。
   - 两个问题共享同一个上位判定原则，且答疑没有分别给出独立结论→不拆。
   - 少见案例、特殊案例或标准咨询可以独立形成单成员主题；不得为了减少单例数量强行合并。

=== 分类规则（5条红线）===

1. category_l1 只能为：基本情况、成色与回收标准、外观问题、显示问题、功能问题、拆修问题、信息查询、流程操作、其他待确认。
2. 分类必须以答疑人员的最终判定结论为准，不以回收师初始提问的字面词为准。
   例：问“是不是漏液”→答“选折痕”→分类=外观问题-折痕，不是显示问题-漏液。
   漏液=显示问题，折痕=外观问题，二者是不同的质检项。
3b. 外观问题（category_l1="外观问题"）覆盖范围很广（胶条、外壳、散热结构、滤镜、折痕、划痕等），但不同对象的外观判定标准完全不同。标注时 subject 必须精确到具体部位，后续聚类时不同 subject 的外观问题不可合并。
3. 理解检测项目的真实目的，不望文生义。
   例：“白光检测”目的=检查屏幕是否被更换/拆修→分类=拆修问题，不是显示问题。
   不确定检测目的时填“待确认”并设 requires_review=true。
4. 包装盒防拆标签→“成色与回收标准”，不能因出现“防拆”归入拆修问题。
5. 不得因为上游分类或标准名称中出现某个词就覆盖实际聊天中的真实问题类型。

=== subject 和范围规则（5条红线）===

1. subject 只使用聊天中明确说出的具体部位描述，不过度细化。
   例：聊天只说“散热断了”→填“散热部位”，不填“散热器”或“散热孔”。
   无法确认具体部位时填“待确认”。
2. normalized_issue 格式：“适用范围｜对象/部位｜异常现象或查询目标｜判定目标/处理动作”。
3. 默认不同品类不能共用一条知识。scope_type 默认“品类专用”；只有输入明确说明各品类标准一致时才标“通用”。
4. 只有输入明确说明某品牌/机型有特殊规则时，才标“品牌专用”或“机型专用”。
5. standard_path/threshold_or_exception 只能提取输入中明确出现的内容。没有→“无明确阈值”；无法确认→“待确认”。任一字段为“待确认”时 requires_review=true。

每个原子知识点的 normalized_issue 应尽量遵循：
“适用范围｜对象/部位｜异常现象或查询目标｜判定目标/处理动作”。

只返回一个 JSON 对象，不要 Markdown。输出结构：
{output_schema}

【输入】
{json.dumps(payload, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _build_cluster_fusion_prompt(
    source_row: dict[str, Any],
    text_candidate: dict[str, Any],
    media_candidate: dict[str, Any],
    media_audit: dict[str, Any],
    retry_reason: str = "",
) -> str:
    retry_instruction = (
        f"\n【上次输出不合格原因】\n{retry_reason}"
        if retry_reason
        else ""
    )
    payload = {
        "source": {
            "work_order_id": _text(source_row.get("工单ID")),
            "product_type": _text(source_row.get("产品类型")),
            "human_corrected_core_problem": _text(
                source_row.get("核心问题") or source_row.get("原始核心问题"),
                1200,
            ),
            "human_corrected_judgment_conclusion": _text(
                source_row.get("判定结论") or source_row.get("原始判定结论"),
                1200,
            ),
            "device_model": _text(source_row.get("机型")),
            "primary_conversation_evidence": _primary_conversation_evidence(
                source_row.get("聊天内容"),
                9000,
            ),
        },
        "text_pro_candidate": text_candidate,
        "media_candidate": media_candidate,
        "media_audit": media_audit,
    }
    terminology_prompt_block = _terminology_prompt_block(source_row)
    return f"""你是人工答疑知识库的多模态主题融合裁决器。

输入包括：
1. MiMo Pro仅依据完整文字提取的主题；
2. 全模态MiMo提取的图片/视频事实和候选主题；
3. 原始聊天与业务字段。

融合原则（5条）：
1. 文本中明确存在的独立问题，即使图片/视频没展示，也必须保留。媒体"未看到"≠文字问题不存在。
2. 图片/视频清晰展示文本中没有的第二个独立质检问题时，可以新增主题。
3. 最终主题集合 = "明确文字主题" ∪ "可靠媒体新增主题"（去重并集）。
4. 文本已是 multi_topic 时不得降为 single_topic，除非两个主题实际是同一对象+同一异常的重复表达。
5. 最多保留 3 个主题。不发明标准、阈值或业务结论。

{terminology_prompt_block}

术语消歧："一根线/靓机助手/验机精灵/爬虫/工具读出"优先理解为验机工具，不是屏幕物理线条。
产品类型/机型/部位与媒体冲突时，保留文字主题并设 requires_review=true。视频或图片无法读取时保留文字提取结果并设 requires_review=true。

只返回一个JSON对象，不要Markdown：
{{
  "conversation_type": "single_topic / multi_topic / uncertain",
  "reason": "融合依据，不超过240字",
  "media_analysis": {{
    "image_summary": "图片事实摘要",
    "video_summary": "视频事实摘要",
    "media_relevance": "相关 / 不相关 / 无法读取 / 无媒体",
    "used_for_topic_split": false,
    "requires_review": false
  }},
  "topics": [
    {{
      "normalized_issue": "可独立聚类的核心问题，不超过80字",
      "product_category": "只能为{product_category_prompt()}或{UNKNOWN_PRODUCT_NAME}",
      "scope_type": "通用 / 品类专用 / 平台专用 / 品牌专用 / 机型专用 / 待确认",
      "platform": "iOS / Android / HarmonyOS / Windows / macOS / 通用 / 待确认",
      "brand": "品牌名称、通用或待确认",
      "model_scope": "机型范围、通用或待确认",
      "category_l1": "基本情况 / 成色与回收标准 / 外观问题 / 显示问题 / 功能问题 / 拆修问题 / 信息查询 / 流程操作 / 其他待确认",
      "category_l2": "知识二级分类",
      "intent": "标准判定 / 检测核验 / 信息查询 / 流程操作 / 其他待确认",
      "subject": "实际对象或部位",
      "phenomenon": "异常现象或查询目标",
      "judgment_target": "希望得到的统一判定目标",
      "resolution_mode": "应沉淀的处理方式",
      "standard_path": "输入明确的处理路径；无法确认填待确认",
      "threshold_or_exception": "明确阈值；没有填无明确阈值；无法确认填待确认",
      "evidence_summary": "聊天和媒体证据摘要，不超过300字",
      "confidence": 0.0,
      "requires_review": true
    }}
  ]
}}

【输入】
{json.dumps(payload, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _fusion_media_analysis(media_candidate: dict[str, Any]) -> dict[str, Any]:
    analysis = media_candidate.get("media_analysis")
    if isinstance(analysis, dict):
        return copy.deepcopy(analysis)
    return {
        "image_summary": "媒体候选未返回图片摘要",
        "video_summary": "媒体候选未返回视频摘要",
        "media_relevance": "无法读取",
        "used_for_topic_split": False,
        "requires_review": True,
    }


def _enforce_cluster_fusion_guardrails(
    fused_candidate: dict[str, Any],
    text_candidate: dict[str, Any],
    media_candidate: dict[str, Any],
    media_audit: dict[str, Any],
) -> dict[str, Any]:
    text_type = _text(text_candidate.get("conversation_type"), 32)
    media_type = _text(media_candidate.get("conversation_type"), 32)
    fused_type = _text(fused_candidate.get("conversation_type"), 32)
    text_topics = list(text_candidate.get("topics") or [])
    media_topics = list(media_candidate.get("topics") or [])
    media_analysis = _fusion_media_analysis(media_candidate)
    media_added_topic = bool(media_analysis.get("used_for_topic_split"))

    selected = copy.deepcopy(fused_candidate)
    guardrail_reason = ""
    if text_type == "multi_topic" and (
        fused_type != "multi_topic"
        or len(selected.get("topics") or []) < len(text_topics)
    ):
        selected = copy.deepcopy(text_candidate)
        guardrail_reason = "硬规则保留MiMo Pro从文字中识别出的多个独立主题。"
    elif media_type == "multi_topic" and media_added_topic and (
        fused_type != "multi_topic"
        or len(selected.get("topics") or []) < len(media_topics)
    ):
        selected = copy.deepcopy(media_candidate)
        guardrail_reason = "硬规则保留媒体中清晰新增的独立主题。"

    selected["media_analysis"] = media_analysis
    if guardrail_reason:
        selected["reason"] = _text(
            f"{guardrail_reason}{_text(selected.get('reason'), 180)}",
            240,
        )

    text_categories = {
        _text(topic.get("product_category"), 80)
        for topic in text_topics
        if _text(topic.get("product_category"), 80)
        not in {"", UNKNOWN_PRODUCT_NAME}
    }
    media_categories = {
        _text(topic.get("product_category"), 80)
        for topic in media_topics
        if _text(topic.get("product_category"), 80)
        not in {"", UNKNOWN_PRODUCT_NAME}
    }
    product_conflict = bool(
        text_categories
        and media_categories
        and text_categories.isdisjoint(media_categories)
    )
    unavailable_media = any(
        item.get("status") == "unavailable"
        for media_type_key in ("images", "videos")
        for item in media_audit.get(media_type_key, [])
    )
    if product_conflict or unavailable_media:
        selected["media_analysis"]["requires_review"] = True
        for topic in selected.get("topics") or []:
            topic["requires_review"] = True
        reason = (
            "文字与媒体产品/对象冲突，需人工复核。"
            if product_conflict
            else "存在无法读取的媒体，已保留文字主题并转人工复核。"
        )
        selected["reason"] = _text(
            f"{reason}{_text(selected.get('reason'), 190)}",
            240,
        )
    return selected


def _atomic_unit_payload(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "atomic_id": _text(unit.get("unit_id") or unit.get("atomic_id"), 120),
        "conversation_evidence_excerpt": _primary_conversation_evidence(
            unit.get("source_conversation"),
            1200,
        ),
        "evidence_summary": _text(unit.get("evidence_summary"), 500),
        "normalized_issue": _text(unit.get("normalized_issue"), 160),
        "source_core_problem": _text(unit.get("source_core_problem"), 1200),
        "source_judgment_conclusion": _text(
            unit.get("source_judgment_conclusion"),
            1200,
        ),
        "source_judgment_basis": _text(
            unit.get("source_judgment_basis"),
            1600,
        ),
        "source_reference_reply": _text(
            unit.get("source_reference_reply"),
            1200,
        ),
        "ai_result_conflict_fields": _text(
            unit.get("ai_result_conflict_fields"),
            240,
        ),
        "business_line": _text(
            unit.get("business_line") or unit.get("回收业务层级"),
            80,
        ),
        "product_category": _text(unit.get("product_category"), 80),
        "scope_type": _text(unit.get("scope_type"), 32),
        "platform": _text(unit.get("platform"), 80),
        "brand": _text(unit.get("brand"), 80),
        "model_scope": _text(unit.get("model_scope"), 120),
        "category_l1": _text(unit.get("category_l1"), 80),
        "category_l2": _text(unit.get("category_l2"), 80),
        "intent": _text(unit.get("intent"), 32),
        "subject": _text(unit.get("subject"), 120),
        "phenomenon": _text(unit.get("phenomenon"), 160),
        "judgment_target": _text(unit.get("judgment_target"), 160),
        "resolution_mode": _text(unit.get("resolution_mode"), 160),
        "standard_path": _text(unit.get("standard_path"), 240),
        "threshold_or_exception": _text(unit.get("threshold_or_exception"), 240),
        "clustering_rule_id": _text(unit.get("_聚类判定规则ID"), 80),
        "clustering_standard_family": _text(unit.get("_聚类标准族"), 120),
        "clustering_phenomenon_value": _text(unit.get("_聚类现象值"), 80),
        "clustering_merge_policy": _text(unit.get("_聚类合并策略"), 80),
        "precluster_rule_status": _text(
            unit.get("_预聚类规则状态"),
            40,
        ),
        "precluster_rule_id": _text(
            unit.get("_预聚类判定规则ID"),
            80,
        ),
        "precluster_standard_family": _text(
            unit.get("_预聚类标准族"),
            120,
        ),
        "precluster_phenomenon_value": _text(
            unit.get("_预聚类现象值"),
            80,
        ),
        "precluster_merge_policy": _text(
            unit.get("_预聚类合并策略"),
            80,
        ),
        "classification_catalog_status": _text(
            unit.get("_分类库状态"),
            40,
        ),
        "classification_catalog_paths": _text(
            unit.get("_分类库候选标准路径"),
            500,
        ),
        "classification_catalog_categories": _text(
            unit.get("_分类库候选问题分类"),
            300,
        ),
        "requires_review": _as_bool(unit.get("requires_review")),
    }


def _build_atomic_topic_cluster_prompt(
    atomic_units: list[dict[str, Any]],
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    payload = [_atomic_unit_payload(unit) for unit in atomic_units]
    terminology_prompt_block = _terminology_prompt_block(atomic_units)
    clustering_rules_prompt_block = _clustering_rules_prompt_block(atomic_units)
    return f"""你是人工答疑知识库的主题聚类专家。

任务：
将输入的原子知识点直接归并为若干主题簇。一个主题簇可以包含 1 到 N 个知识点。
这不是两两配对任务，不得限制每个聚类的成员数量，不得为了减少簇数而强行合并。

唯一聚类标准：
簇内所有知识点是否能够共用同一条标准答疑知识。

{terminology_prompt_block}

{clustering_rules_prompt_block}

判断优先级：
1. 回收业务层级不同绝对禁止合并，例如自营回收与聚合回收不能合并。
2. 产品品类不同绝对禁止合并，这是不可放宽的硬门禁。
3. 同业务层级、同品类内优先比较已固化的标准族、现象值、核心对象和最终判定目标。
4. 二级分类或标准路径的文字不同，不是绝对拆分条件；先判断它们是否只是同一口径的不同表述。
5. requires_review 或 review_requests 不等于必须单独成簇。主题本身清晰且能与同业务层级、同品类、同标准族成员共用一条知识时，应正常聚类；只有输入证据不足或字段冲突到无法判断时才进入 review_requests。

只有同时满足以下条件才能合并：
1. 适用范围一致；
2. 核心对象一致；
3. 判定目标一致；
4. 标准处理路径一致；
5. 阈值、例外条件不会导致不同答疑结论。

以下情况必须拆分：
1. 回收业务层级不同，例如自营回收与聚合回收；
2. 产品品类不同，例如手机与平板电脑、笔记本与游戏机；即使问题文字和处理逻辑相似，也绝对不能跨品类聚类；
3. 苹果、安卓、鸿蒙或通用标准不同；
4. 功能问题与外观问题、显示问题、拆修问题不同；
5. 判定阈值不同；
6. 经语义和质检口径确认，标准处理路径或最终处理结论确实不同；
7. 合并后需要在一条知识中写多个互不相关的处理结论。

执行规则：
1. 允许单知识点独立成簇。
2. conversation_evidence_excerpt、evidence_summary、source_core_problem 和 source_judgment_conclusion 共同用于核对真实问题。后两项来自第二部分人工校正，尤其用于补充本轮未读取图片时已由人工确认的对象、现象和结论。
3. 不得按关键词或字面相似直接合并，优先依据真实聊天中的对象、判定目标、标准处理路径和阈值例外。
4. 若人工校正字段、normalized_issue 与聊天证据明显冲突，不得按任一字段强行合并；应放入 review_requests。
5. 聊天原文中的“问题类型、问题描述、转人工原因”是系统转人工元数据，可能为快速转人工而乱填，不得作为聚类依据。
6. standard_path、resolution_mode 或 threshold_or_exception 的文字不同不代表一定不同；必须判断语义和最终答疑结论是否一致。
7. 不得发明输入中没有的阈值、例外、适用范围或业务规则。
8. 多成员簇的五个一致性字段必须全部为 true；任一项不一致时不得合并，应拆为单成员簇。
9. 如果一个原子知识点本身仍包含多个独立主题，放入 split_requests，不得直接聚类。
10. 每个 atomic_id 必须且只能出现在 clusters、split_requests、review_requests 三者之一。
11. 不得遗漏、重复或改写 atomic_id。
12. theme_name 应概括可直接沉淀的一条标准答疑知识，不要使用宽泛对象名。
13. “无法与其他知识点合并”不等于“不确定”。只要该原子知识点自身主题清楚，就必须建立单成员簇。
    少见、特殊或标准咨询类原子问题可以合理保持单成员簇，不得为了减少单例或提高簇大小而强制合并。
14. review_requests 仅用于输入字段自身矛盾、缺失或无法判断适用范围/路径/阈值的情况；不得用于存放清晰但独立的知识点。
15. precluster_* 是系统根据源聊天、人工核心问题、判定结论和本地聚类关键词规则得到的预识别结果。它只用于聚类边界，不是知识正文或标准引用。若 precluster_rule_status=rule_matched，应优先核对该标准族和现象值；模型不得无依据改成另一标准族。若模型结构化字段与预识别结果冲突，必须将该原子问题放入 review_requests，不得静默覆盖。
16. classification_catalog_* 是新分类库给出的候选分类和标准路径，只用于补充问题识别，不是最终聚类依据，也不是标准正文引用。classification_catalog_status=classification_matched 时，同一路径可作为“值得比较”的辅助信号，但仍必须核对对象、判定目标和处理结论。classification_catalog_status=classification_ambiguous 时，不得仅因候选路径不同把同品类案例强制拆开；应先按聊天、人工校正字段和聚类规则判断能否共用知识，并将最终簇标记 requires_review=true。分类库候选与当前聚类规则或聊天证据不一致时，以当前聚类规则和聊天证据为准，并进入人工复核。
17. 每个主题簇必须返回 confidence 和 requires_review。confidence 表示“簇内所有成员确实能共用同一条知识”的把握，不是单条原子问题自身清晰度。字段冲突、证据不足或合并边界不清时 requires_review=true。
18. 只输出一个 JSON 对象，不要 Markdown。

输出结构：
{{
  "clusters": [
    {{
      "cluster_id": "C001",
      "theme_name": "简短、唯一、可沉淀的知识主题",
      "member_atomic_ids": ["原子知识ID"],
      "scope_consistent": true,
      "object_consistent": true,
      "judgment_target_consistent": true,
      "standard_path_consistent": true,
      "threshold_exception_consistent": true,
      "shared_knowledge_definition": "该簇可共用的一条标准答疑知识定义",
      "merge_basis": "说明为什么所有成员能够共用同一条知识，不超过300字",
      "confidence": 0.0,
      "requires_review": false
    }}
  ],
  "split_requests": [
    {{
      "atomic_id": "原子知识ID",
      "reason": "为什么仍需拆成多个原子知识点",
      "suggested_splits": ["建议拆分方向"]
    }}
  ],
  "review_requests": [
    {{
      "atomic_id": "原子知识ID",
      "review_type": "适用范围 / 对象 / 判定目标 / 标准路径 / 阈值例外 / 其他",
      "reason": "为什么无法自动聚类"
    }}
  ]
}}

【待聚类原子知识点】
{json.dumps(payload, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end >= start else text


def _content_from_response(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MimoError("MiMo 响应中没有 choices[0].message.content") from exc
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def _post_request_with_deadline(
    request: Request,
    connection_timeout_seconds: float,
    timeout_seconds: float,
) -> str:
    result: list[str] = []
    errors: list[BaseException] = []
    completed = threading.Event()
    response_lock = threading.Lock()
    active_response: list[Any] = []

    def request_worker() -> None:
        try:
            with urlopen(
                request,
                timeout=connection_timeout_seconds,
            ) as response:
                with response_lock:
                    active_response.append(response)
                try:
                    result.append(
                        response.read().decode("utf-8", errors="replace")
                    )
                finally:
                    with response_lock:
                        if active_response and active_response[0] is response:
                            active_response.clear()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(
        target=request_worker,
        name="answer-hub-mimo-request",
        daemon=True,
    )
    worker.start()
    if not completed.wait(max(0.1, float(timeout_seconds))):
        with response_lock:
            response = active_response[0] if active_response else None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        raise MimoError(
            f"MiMo 响应总超时（超过 {timeout_seconds:g} 秒）"
        )
    if errors:
        raise errors[0]
    if not result:
        raise MimoError("MiMo 响应为空")
    return result[0]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "是"}
    return bool(value)


def _fallback_recommended_reply(title: str, content: str) -> str:
    lines = [
        re.sub(r"^\s*(?:[-•]|\d+[.、])\s*", "", line).strip()
        for line in str(content or "").splitlines()
    ]
    points = [line for line in lines if line and not line.endswith("：")][:3]
    body = "；".join(points)
    if not body:
        body = "请先确认具体对象和现象，补充必要的图片、截图或检测结果，再对照当前有效标准判定。"
    return _text(f"您好，关于“{title}”，建议{body}。若现有证据不能对应标准，请补充证据后再判定。", 240)


def _content_type_has_expected_numbered_points(
    content: str,
    content_type: str,
) -> bool:
    counts = {
        "定义型": (1, 2),
        "阈值型": (1, 3),
        "核验型": (1, 4),
        "区分型": (2, 5),
    }
    minimum, maximum = counts[content_type]
    points = re.findall(r"(?:^|\n)\s*\d+[.、]\s*\S+", content)
    return minimum <= len(points) <= maximum


_CONTENT_TYPE_ALIASES = {
    "流程型": "核验型",
    "检测型": "核验型",
    "查询型": "核验型",
    "操作型": "核验型",
    "标准判定型": "阈值型",
    "阈值判定型": "阈值型",
    "边界型": "区分型",
    "区别型": "区分型",
    "说明型": "定义型",
    "解释型": "定义型",
}


def _normalize_content_type(value: Any, content: str) -> str:
    supplied = _text(value, 16)
    if not supplied:
        return ""
    if supplied in CONTENT_TYPES:
        return supplied
    aliased = _CONTENT_TYPE_ALIASES.get(supplied)
    if aliased:
        return aliased

    if supplied not in {"判定型", "规则型"}:
        return ""
    if any(
        marker in content
        for marker in ("区分", "区别", "分别", "优先按", "转按")
    ):
        return "区分型"
    if (
        re.search(r"\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米|%|次|处|个|颗)", content)
        and any(
            marker in content
            for marker in ("大于", "小于", "不超过", "不少于", "达到", "低于", "高于")
        )
    ):
        return "阈值型"
    if any(
        marker in content
        for marker in (
            "打开",
            "进入",
            "查看",
            "查询",
            "检测",
            "核验",
            "测试",
            "确认",
            "读取",
            "拍摄",
            "补充",
        )
    ):
        return "核验型"
    return "定义型"


_SUBTITLE_QUESTION_MARKERS = (
    "如何",
    "怎么",
    "是否",
    "什么",
    "哪些",
    "能否",
    "多大",
    "多少",
    "几处",
    "有无",
    "为啥",
    "为什么",
)


def _validate_subtitles(
    subtitles: list[str],
    title: str,
    *,
    required: bool,
) -> list[str]:
    normalized_title = _text(title, 120)
    validated: list[str] = []
    for subtitle in dict.fromkeys(
        _text(item, 120)
        for item in subtitles
        if _text(item, 120)
    ):
        if subtitle == normalized_title:
            continue
        if required and candidate_title_structure_issue(subtitle):
            continue
        if required and not any(
            marker in subtitle
            for marker in _SUBTITLE_QUESTION_MARKERS
        ):
            continue
        validated.append(subtitle)
        if len(validated) >= 8:
            break
    return validated


def _validate_candidate(value: Any, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("MiMo 输出不是 JSON 对象")
    required_text = [
        "title", "content", "category_l1", "category_l2", "layer",
        "knowledge_form", "content_type", "applicable_scope",
        "reasoning_summary", "image_evidence_summary",
    ]
    for key in required_text:
        if not _text(value.get(key)):
            raise MimoError(f"MiMo 输出缺少或为空：{key}")
    subtitles = value.get("subtitles")
    refs = value.get("standard_refs")
    applicable_brands = value.get("applicable_brands", [])
    applicable_models = value.get("applicable_models", [])
    if not isinstance(subtitles, list) or not all(isinstance(item, str) for item in subtitles):
        raise MimoError("MiMo 输出的 subtitles 必须为字符串数组")
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        raise MimoError("MiMo 输出的 standard_refs 必须为字符串数组")
    if not isinstance(applicable_brands, list) or not all(
        isinstance(item, str)
        for item in applicable_brands
    ):
        raise MimoError("MiMo 输出的 applicable_brands 必须为字符串数组")
    if not isinstance(applicable_models, list) or not all(
        isinstance(item, str)
        for item in applicable_models
    ):
        raise MimoError("MiMo 输出的 applicable_models 必须为字符串数组")
    invalid_refs = set(refs) - allowed_refs
    if invalid_refs:
        raise MimoError(f"MiMo 引用了本次未检索到的标准：{', '.join(sorted(invalid_refs))}")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("MiMo 输出的 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("MiMo 输出的 confidence 必须在 0~1")
    knowledge_form = _text(value["knowledge_form"], 32)
    if knowledge_form not in {"具体判定", "流程方法"}:
        raise MimoError("MiMo 输出的 knowledge_form 必须为 具体判定 或 流程方法")
    title = _text(value["title"], 120)
    title_structure_issue = candidate_title_structure_issue(title)
    if title_structure_issue:
        raise MimoError(
            f"主标题存在{title_structure_issue}，"
            "必须改写为自然、完整的检索问题或明确判定主题"
        )
    title_style_issue = candidate_title_style_issue(title)
    if title_style_issue:
        raise MimoError(
            f"主标题不能使用“{title_style_issue}”这类公文式元描述，"
            "应改为自然的检索问题或明确判定主题"
        )
    supplied_content_type = _text(value.get("content_type"), 16)
    content = _text(value["content"], 1200)
    content_type = _normalize_content_type(supplied_content_type, content)
    if not content_type:
        raise MimoError(
            "MiMo 输出的 content_type 必须为 定义型、阈值型、核验型 或 区分型"
        )
    validated_subtitles = _validate_subtitles(
        subtitles,
        title,
        required=True,
    )
    if not _content_type_has_expected_numbered_points(
        content,
        content_type,
    ):
        raise MimoError(
            f"MiMo 输出的{content_type}正文必须使用对应数量的编号要点"
        )
    applicable_category = resolve_product_category(value["applicable_scope"])
    if applicable_category is None:
        raise MimoError(
            "MiMo 输出的 applicable_scope 必须是当前固定12项产品品类之一"
        )
    return {
        "title": title,
        "subtitles": validated_subtitles,
        "content": content,
        "content_type": content_type,
        "category_l1": _text(value["category_l1"], 80),
        "category_l2": _text(value["category_l2"], 80),
        "layer": _text(value["layer"], 32) or "L2",
        "knowledge_form": knowledge_form,
        "standard_refs": list(dict.fromkeys(refs)),
        "applicable_scope": applicable_category.name,
        "applicable_brands": list(
            dict.fromkeys(
                _text(item, 120)
                for item in applicable_brands
                if _text(item, 120)
            )
        ),
        "applicable_models": list(
            dict.fromkeys(
                _text(item, 160)
                for item in applicable_models
                if _text(item, 160)
            )
        ),
        "recommended_reply": _text(value.get("recommended_reply"), 240)
        or _fallback_recommended_reply(title, content),
        "confidence": round(confidence, 3),
        "reasoning_summary": _text(value["reasoning_summary"], 240),
        "needs_human_review": _as_bool(value.get("needs_human_review")),
        "image_evidence_summary": _text(value["image_evidence_summary"], 800),
        "requires_images": _as_bool(value.get("requires_images")),
        "image_usage_instruction": _text(value.get("image_usage_instruction"), 400),
    }


def _validate_topic_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("MiMo 初标输出不是 JSON 对象")
    decision = _text(value.get("decision"), 32)
    if decision not in {"通过", "需修改", "驳回", "证据不足待补充"}:
        raise MimoError("MiMo 初标 decision 不合法")
    knowledge_value = _text(value.get("knowledge_value"), 32)
    if knowledge_value not in {"值得沉淀", "不值得沉淀", "待确认"}:
        raise MimoError("MiMo 初标 knowledge_value 不合法")
    if knowledge_value == "不值得沉淀" and decision != "驳回":
        raise MimoError("MiMo 初标标记不值得沉淀时 decision 必须为驳回")
    if knowledge_value == "待确认" and decision == "通过":
        raise MimoError("MiMo 初标标记待确认时 decision 不能为通过")
    standard_consistency = _text(value.get("standard_consistency"), 32)
    if standard_consistency not in {"一致", "不一致", "无可信标准"}:
        raise MimoError("MiMo 初标 standard_consistency 不合法")
    evidence_sufficiency = _text(value.get("evidence_sufficiency"), 32)
    if evidence_sufficiency not in {"充分", "部分充分", "不足"}:
        raise MimoError("MiMo 初标 evidence_sufficiency 不合法")
    content_consistency = _text(value.get("content_consistency"), 32)
    if content_consistency not in {"一致", "部分一致", "不一致"}:
        raise MimoError("MiMo 初标 content_consistency 不合法")
    image_necessity = _text(value.get("image_necessity"), 32)
    if image_necessity not in {"需要保留", "不需要", "图片不足"}:
        raise MimoError("MiMo 初标 image_necessity 不合法")
    title_quality = _text(value.get("title_quality"), 32)
    if title_quality not in {"清晰", "需修改"}:
        raise MimoError("MiMo 初标 title_quality 不合法")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("MiMo 初标 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("MiMo 初标 confidence 必须在 0~1")
    return {
        "decision": decision,
        "knowledge_value": knowledge_value,
        "error_type": _text(value.get("error_type"), 120),
        "reason": _text(value.get("reason"), 500),
        "standard_consistency": standard_consistency,
        "evidence_sufficiency": evidence_sufficiency,
        "content_consistency": content_consistency,
        "image_necessity": image_necessity,
        "title_quality": title_quality,
        "confidence": round(confidence, 3),
        "priority_review": _as_bool(value.get("priority_review")),
    }


def _validate_topic_stage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("MiMo 主题环节输出不是 JSON 对象")
    topic_stage = _text(value.get("topic_stage"), 32)
    if topic_stage not in SUPPORTED_KNOWLEDGE_CATEGORIES:
        raise MimoError("MiMo 主题环节 topic_stage 不合法")
    knowledge_value = _text(value.get("knowledge_value"), 32)
    if knowledge_value not in {"值得沉淀", "不值得沉淀"}:
        raise MimoError("MiMo 主题环节 knowledge_value 不合法")
    stage_reason = _text(value.get("stage_reason"), 500)
    value_reason = _text(value.get("value_reason"), 500)
    reusable_knowledge = _text(value.get("reusable_knowledge"), 800)
    if not stage_reason or not value_reason or not reusable_knowledge:
        raise MimoError("MiMo 主题环节缺少判断依据或可复用知识摘要")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("MiMo 主题环节 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("MiMo 主题环节 confidence 必须在 0~1")
    return {
        "topic_stage": topic_stage,
        "knowledge_value": knowledge_value,
        "stage_reason": stage_reason,
        "value_reason": value_reason,
        "reusable_knowledge": reusable_knowledge,
        "confidence": round(confidence, 3),
        "needs_human_review": (
            True
            if topic_stage == UNCERTAIN_CATEGORY
            else _as_bool(value.get("needs_human_review"))
        ),
    }


def _validate_topic_display_questions(
    value: Any,
    allowed_theme_ids: set[str],
) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        raise MimoError("MiMo 主题问句输出不是 JSON 对象")
    questions = value.get("questions")
    if not isinstance(questions, list):
        raise MimoError("MiMo 主题问句 questions 必须是数组")
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    banned_words = {
        "回收师",
        "用户",
        "咨询",
        "希望",
        "主题",
        "标准判定",
        "案例解析",
        "知识沉淀",
    }
    for item in questions:
        if not isinstance(item, dict):
            raise MimoError("MiMo 主题问句数组项必须是 JSON 对象")
        theme_id = _text(item.get("theme_id"), 80)
        question = re.sub(r"\s+", "", _text(item.get("question"), 80))
        if theme_id not in allowed_theme_ids:
            raise MimoError(f"MiMo 主题问句包含未知 theme_id：{theme_id}")
        if theme_id in seen_ids:
            raise MimoError(f"MiMo 主题问句 theme_id 重复：{theme_id}")
        if question.endswith("?"):
            question = f"{question[:-1]}？"
        if not question.endswith("？"):
            raise MimoError(f"MiMo 主题问句必须以中文问号结尾：{theme_id}")
        if question.count("？") != 1 or "?" in question:
            raise MimoError(f"MiMo 每个主题只能输出一个问句：{theme_id}")
        if not 5 <= len(question) <= 40:
            raise MimoError(f"MiMo 主题问句长度必须为 5～40 个字符：{theme_id}")
        hit = next((word for word in banned_words if word in question), "")
        if hit:
            raise MimoError(f"MiMo 主题问句包含后台术语“{hit}”：{theme_id}")
        seen_ids.add(theme_id)
        result.append({"theme_id": theme_id, "question": question})
    missing_ids = allowed_theme_ids - seen_ids
    if missing_ids:
        raise MimoError(
            f"MiMo 主题问句缺少 theme_id：{', '.join(sorted(missing_ids))}"
        )
    return result


def _validate_cluster_pair_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("聚类标注输出不是 JSON 对象")
    decision = _text(value.get("decision"), 32)
    if decision not in {"同一主题", "不同主题", "不确定"}:
        raise MimoError("聚类标注 decision 不合法")
    topic_label = _text(value.get("topic_label"), 160)
    reason = _text(value.get("reason"), 300)
    if not topic_label or not reason:
        raise MimoError("聚类标注缺少 topic_label 或 reason")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("聚类标注 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("聚类标注 confidence 必须在 0~1")
    return {
        "decision": decision,
        "topic_label": topic_label,
        "reason": reason,
        "key_difference": _text(value.get("key_difference"), 240),
        "confidence": round(confidence, 3),
    }


def _validate_cluster_units(
    value: Any,
    *,
    require_media_analysis: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("聚类问题单元输出不是 JSON 对象")
    conversation_type = _text(value.get("conversation_type"), 32)
    if conversation_type not in {"single_topic", "multi_topic", "uncertain"}:
        raise MimoError("聚类问题单元 conversation_type 不合法")
    reason = _text(value.get("reason"), 240)
    if not reason:
        raise MimoError("聚类问题单元缺少 reason")
    raw_media_analysis = value.get("media_analysis")
    if require_media_analysis and not isinstance(raw_media_analysis, dict):
        raise MimoError("包含媒体的聚类问题单元必须返回 media_analysis")
    if isinstance(raw_media_analysis, dict):
        image_summary = _text(raw_media_analysis.get("image_summary"), 800)
        video_summary = _text(raw_media_analysis.get("video_summary"), 800)
        raw_media_relevance = _text(
            raw_media_analysis.get("media_relevance"),
            32,
        )
        if raw_media_relevance in {"相关", "不相关", "无法读取", "无媒体"}:
            media_relevance = raw_media_relevance
        elif any(
            marker in raw_media_relevance
            for marker in ("无法", "不可读", "未读取", "读取失败")
        ):
            media_relevance = "无法读取"
        elif "不相关" in raw_media_relevance or "无关" in raw_media_relevance:
            media_relevance = "不相关"
        elif "相关" in raw_media_relevance:
            media_relevance = "相关"
        elif "无媒体" in raw_media_relevance:
            media_relevance = "无媒体"
        else:
            media_relevance = ""
        if not image_summary or not video_summary:
            raise MimoError("media_analysis 缺少图片或视频摘要")
        if not media_relevance:
            raise MimoError("media_analysis.media_relevance 不合法")
        media_analysis = {
            "image_summary": image_summary,
            "video_summary": video_summary,
            "media_relevance": media_relevance,
            "used_for_topic_split": _as_bool(
                raw_media_analysis.get("used_for_topic_split")
            ),
            "requires_review": _as_bool(raw_media_analysis.get("requires_review")),
        }
    else:
        media_analysis = {
            "image_summary": "无图片",
            "video_summary": "无视频",
            "media_relevance": "无媒体",
            "used_for_topic_split": False,
            "requires_review": False,
        }
    topics = value.get("topics")
    if not isinstance(topics, list):
        raise MimoError("聚类问题单元 topics 必须为数组")
    if conversation_type == "single_topic" and len(topics) != 1:
        raise MimoError("single_topic 必须恰好包含 1 个问题单元")
    if conversation_type == "multi_topic" and not 2 <= len(topics) <= 3:
        raise MimoError("multi_topic 必须包含 2 到 3 个问题单元")
    if conversation_type == "uncertain" and len(topics) > 1:
        raise MimoError("uncertain 最多保留 1 个暂定问题单元")

    allowed_intents = {"标准判定", "检测核验", "信息查询", "流程操作", "其他待确认"}
    allowed_scope_types = {
        "通用",
        "品类专用",
        "平台专用",
        "品牌专用",
        "机型专用",
        "待确认",
    }
    allowed_product_categories = {*configured_product_names(), UNKNOWN_PRODUCT_NAME}
    allowed_category_l1 = {
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
    category_l1_aliases = {
        "基本信息": "基本情况",
        "基本信息问题": "基本情况",
        "成色问题": "成色与回收标准",
        "回收标准": "成色与回收标准",
        "外观": "外观问题",
        "显示": "显示问题",
        "功能": "功能问题",
        "拆修": "拆修问题",
        "其他问题": "其他待确认",
    }
    normalized_topics: list[dict[str, Any]] = []
    for topic in topics:
        if not isinstance(topic, dict):
            raise MimoError("聚类问题单元 topic 必须为 JSON 对象")
        normalized_issue = _text(topic.get("normalized_issue"), 80)
        raw_product_category = _text(topic.get("product_category"), 80)
        product_category = canonical_product_name(
            raw_product_category,
            unknown=UNKNOWN_PRODUCT_NAME if raw_product_category == UNKNOWN_PRODUCT_NAME else "",
        )
        scope_type = _text(topic.get("scope_type"), 32)
        platform = _text(topic.get("platform"), 80)
        brand = _text(topic.get("brand"), 80)
        model_scope = _text(topic.get("model_scope"), 120)
        raw_category_l1 = _text(topic.get("category_l1"), 80)
        category_l1 = category_l1_aliases.get(raw_category_l1, raw_category_l1)
        category_l2 = _text(topic.get("category_l2"), 80)
        intent = _text(topic.get("intent"), 32)
        subject = _text(topic.get("subject"), 120)
        phenomenon = _text(topic.get("phenomenon"), 160)
        judgment_target = _text(topic.get("judgment_target"), 160)
        resolution_mode = _text(topic.get("resolution_mode"), 160)
        standard_path = _text(topic.get("standard_path"), 200)
        threshold_or_exception = _text(topic.get("threshold_or_exception"), 200)
        evidence_summary = _text(topic.get("evidence_summary"), 300)
        required_text_fields = {
            "normalized_issue": normalized_issue,
            "product_category": product_category,
            "scope_type": scope_type,
            "platform": platform,
            "brand": brand,
            "model_scope": model_scope,
            "category_l1": category_l1,
            "category_l2": category_l2,
            "subject": subject,
            "phenomenon": phenomenon,
            "judgment_target": judgment_target,
            "resolution_mode": resolution_mode,
            "standard_path": standard_path,
            "threshold_or_exception": threshold_or_exception,
            "evidence_summary": evidence_summary,
        }
        missing_fields = [
            key for key, field_value in required_text_fields.items() if not field_value
        ]
        if missing_fields:
            raise MimoError(
                "聚类问题单元缺少必要文本字段：" + ", ".join(missing_fields)
            )
        if intent not in allowed_intents:
            raise MimoError("聚类问题单元 intent 不合法")
        if product_category not in allowed_product_categories:
            raise MimoError("聚类问题单元 product_category 不合法")
        if scope_type not in allowed_scope_types:
            raise MimoError("聚类问题单元 scope_type 不合法")
        if category_l1 not in allowed_category_l1:
            raise MimoError("聚类问题单元 category_l1 不合法")
        semantic_text = " ".join(
            (
                normalized_issue,
                phenomenon,
                judgment_target,
                resolution_mode,
                evidence_summary,
            )
        )
        display_markers = (
            "色斑",
            "颜色异常",
            "偏绿",
            "偏蓝",
            "闪屏",
            "闪烁",
            "亮线",
            "绿线",
            "坏点",
            "漏液",
            "显示异常",
        )
        repair_markers = ("拆修", "维修", "更换", "非原装", "第三方", "弹窗")
        if (
            any(marker in semantic_text for marker in display_markers)
            and not any(marker in semantic_text for marker in repair_markers)
            and category_l1 != "显示问题"
        ):
            raise MimoError("屏幕显示现象必须归入显示问题，不得误归外观或拆修问题")
        if (
            any(marker in semantic_text for marker in ("全新机", "二手", "成色定级"))
            and category_l1 == "基本情况"
        ):
            category_l1 = "成色与回收标准"
        scope_level = {
            "通用": 0,
            "品类专用": 1,
            "平台专用": 2,
            "品牌专用": 3,
            "机型专用": 4,
            "待确认": -1,
        }[scope_type]
        if scope_level < 2 and scope_level >= 0:
            platform = "通用"
        if scope_level < 3 and scope_level >= 0:
            brand = "通用"
        if scope_level < 4 and scope_level >= 0:
            model_scope = "通用"
        try:
            confidence = float(topic.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise MimoError("聚类问题单元 confidence 必须是 0~1 数字") from exc
        if not 0 <= confidence <= 1:
            raise MimoError("聚类问题单元 confidence 必须在 0~1")
        requires_review = (
            _as_bool(topic.get("requires_review"))
            or media_analysis["requires_review"]
            or confidence < 0.75
            or any(
            "待确认" in item
            for item in (
                product_category,
                scope_type,
                platform,
                brand,
                model_scope,
                category_l1,
                category_l2,
                judgment_target,
                standard_path,
                threshold_or_exception,
            )
            )
        )
        normalized_topics.append(
            {
                "normalized_issue": normalized_issue,
                "product_category": product_category,
                "scope_type": scope_type,
                "platform": platform,
                "brand": brand,
                "model_scope": model_scope,
                "category_l1": category_l1,
                "category_l2": category_l2,
                "intent": intent,
                "subject": subject,
                "phenomenon": phenomenon,
                "judgment_target": judgment_target,
                "resolution_mode": resolution_mode,
                "standard_path": standard_path,
                "threshold_or_exception": threshold_or_exception,
                "evidence_summary": evidence_summary,
                "confidence": round(confidence, 3),
                "requires_review": requires_review,
            }
        )
    return {
        "conversation_type": conversation_type,
        "reason": reason,
        "media_analysis": media_analysis,
        "topics": normalized_topics,
    }


_NO_THRESHOLD_MARKERS = (
    "无明确阈值",
    "无阈值",
    "待确认",
    "未提供",
    "不适用",
)
_NO_STANDARD_PATH_MARKERS = (
    "待确认",
    "无明确标准路径",
    "无明确路径",
    "未提供",
    "不适用",
)
_STANDARD_PATH_GENERIC_WORDS = (
    "标准",
    "路径",
    "流程",
    "判定",
    "核验",
    "处理",
    "方法",
    "要求",
)
_THRESHOLD_RULE_MARKERS = (
    "大于",
    "小于",
    "超过",
    "低于",
    "至少",
    "至多",
    "不少于",
    "不超过",
    ">",
    "<",
    "≥",
    "≤",
)


def _compact_source_claim(value: Any) -> str:
    return re.sub(r"[\W_]+", "", _text(value).casefold())


def _source_supports_standard_path(
    standard_path: str,
    source_compact: str,
) -> bool:
    core = _compact_source_claim(standard_path)
    for word in _STANDARD_PATH_GENERIC_WORDS:
        core = core.replace(word, "")
    if not core:
        return False
    if core in source_compact:
        return True
    if len(core) < 2:
        return False
    pairs = {core[index : index + 2] for index in range(len(core) - 1)}
    if not pairs:
        return False
    matched = sum(pair in source_compact for pair in pairs)
    return matched / len(pairs) >= 0.6


def _guard_cluster_unit_source_claims(
    candidate: dict[str, Any],
    source_row: dict[str, Any],
) -> dict[str, Any]:
    source_text = "\n".join(
        _text(source_row.get(field), 9000)
        for field in (
            "聊天内容",
            "核心问题",
            "判定结论",
            "历史实际回复",
            "参考话术",
            "判定依据",
        )
        if _text(source_row.get(field))
    )
    source_compact = _compact_source_claim(source_text)
    source_lower = source_text.casefold()
    guarded = copy.deepcopy(candidate)
    for topic in guarded.get("topics", []):
        reasons: list[str] = []
        standard_path = _text(topic.get("standard_path"), 200)
        if (
            standard_path
            and not any(
                marker in standard_path
                for marker in _NO_STANDARD_PATH_MARKERS
            )
            and not _source_supports_standard_path(
                standard_path,
                source_compact,
            )
        ):
            topic["standard_path"] = "待确认"
            reasons.append("模型标准路径未在来源事实中出现")

        threshold = _text(topic.get("threshold_or_exception"), 200)
        if threshold and not any(
            marker in threshold
            for marker in _NO_THRESHOLD_MARKERS
        ):
            numeric_tokens = [
                token.casefold().replace(" ", "")
                for token in re.findall(
                    r"\d+(?:\.\d+)?\s*(?:mm|cm|%|秒|次|个|台|元)?",
                    threshold,
                    flags=re.IGNORECASE,
                )
            ]
            unsupported_numbers = any(
                token and token not in source_lower.replace(" ", "")
                for token in numeric_tokens
            )
            unsupported_rule = (
                any(marker in threshold for marker in _THRESHOLD_RULE_MARKERS)
                and _compact_source_claim(threshold) not in source_compact
            )
            if unsupported_numbers or unsupported_rule:
                topic["threshold_or_exception"] = "无明确阈值"
                reasons.append("模型阈值或例外未在来源事实中出现")

        if reasons:
            topic["requires_review"] = True
            topic["evidence_summary"] = _text(
                "；".join(
                    [
                        _text(topic.get("evidence_summary"), 240),
                        *reasons,
                    ]
                ),
                300,
            )
    return guarded


def _validate_cluster_unit_batch(
    value: Any,
    allowed_record_ids: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise MimoError("批量聚类问题单元输出不是 JSON 对象")
    results = value.get("results")
    if not isinstance(results, list):
        raise MimoError("批量聚类问题单元 results 必须为数组")
    expected_ids = set(allowed_record_ids)
    validated_by_id: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict):
            raise MimoError("批量聚类问题单元 result 必须为 JSON 对象")
        record_id = _text(result.get("record_id"), 80)
        if record_id not in expected_ids:
            raise MimoError("批量聚类问题单元返回未知 record_id")
        if record_id in validated_by_id:
            raise MimoError("批量聚类问题单元 record_id 重复")
        candidate = dict(result)
        candidate.pop("record_id", None)
        validated_by_id[record_id] = _validate_cluster_units(candidate)
    missing_ids = [
        record_id
        for record_id in allowed_record_ids
        if record_id not in validated_by_id
    ]
    if missing_ids:
        raise MimoError(
            "批量聚类问题单元缺少 record_id："
            + "、".join(missing_ids[:10])
        )
    return [validated_by_id[record_id] for record_id in allowed_record_ids]


def _validate_atomic_topic_clusters(
    value: Any,
    allowed_atomic_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("原子知识主题聚类输出不是 JSON 对象")
    if not allowed_atomic_ids:
        raise MimoError("原子知识主题聚类输入不能为空")

    clusters = value.get("clusters")
    split_requests = value.get("split_requests")
    review_requests = value.get("review_requests")
    if not isinstance(clusters, list):
        raise MimoError("原子知识主题聚类 clusters 必须为数组")
    if not isinstance(split_requests, list):
        raise MimoError("原子知识主题聚类 split_requests 必须为数组")
    if not isinstance(review_requests, list):
        raise MimoError("原子知识主题聚类 review_requests 必须为数组")

    seen_atomic_ids: set[str] = set()
    seen_cluster_ids: set[str] = set()
    consistency_fields = (
        "scope_consistent",
        "object_consistent",
        "judgment_target_consistent",
        "standard_path_consistent",
        "threshold_exception_consistent",
    )
    normalized_clusters: list[dict[str, Any]] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            raise MimoError("原子知识主题簇必须为 JSON 对象")
        cluster_id = _text(cluster.get("cluster_id"), 80)
        theme_name = _text(cluster.get("theme_name"), 160)
        shared_definition = _text(cluster.get("shared_knowledge_definition"), 500)
        merge_basis = _text(cluster.get("merge_basis"), 300)
        if not all((cluster_id, theme_name, shared_definition, merge_basis)):
            raise MimoError("原子知识主题簇缺少名称、知识定义或合并依据")
        if cluster_id in seen_cluster_ids:
            raise MimoError(f"原子知识主题簇 ID 重复：{cluster_id}")
        seen_cluster_ids.add(cluster_id)

        member_ids = cluster.get("member_atomic_ids")
        if (
            not isinstance(member_ids, list)
            or not member_ids
            or not all(isinstance(item, str) and _text(item, 120) for item in member_ids)
        ):
            raise MimoError("member_atomic_ids 必须为非空字符串数组")
        normalized_member_ids = [_text(item, 120) for item in member_ids]
        if len(normalized_member_ids) != len(set(normalized_member_ids)):
            raise MimoError(f"主题簇 {cluster_id} 内存在重复 atomic_id")

        flags: dict[str, bool] = {}
        for field in consistency_fields:
            field_value = cluster.get(field)
            if not isinstance(field_value, bool):
                raise MimoError(f"主题簇 {cluster_id} 的 {field} 必须为布尔值")
            flags[field] = field_value
        if len(normalized_member_ids) > 1 and not all(flags.values()):
            raise MimoError(f"多成员主题簇 {cluster_id} 未通过全部五项一致性检查")
        try:
            confidence = float(cluster.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise MimoError(
                f"主题簇 {cluster_id} 的 confidence 必须是 0~1 数字"
            ) from exc
        if not 0 <= confidence <= 1:
            raise MimoError(
                f"主题簇 {cluster_id} 的 confidence 必须在 0~1"
            )
        requires_review = cluster.get("requires_review")
        if not isinstance(requires_review, bool):
            raise MimoError(
                f"主题簇 {cluster_id} 的 requires_review 必须为布尔值"
            )

        for atomic_id in normalized_member_ids:
            if atomic_id not in allowed_atomic_ids:
                raise MimoError(f"输出包含输入中不存在的 atomic_id：{atomic_id}")
            if atomic_id in seen_atomic_ids:
                raise MimoError(f"atomic_id 重复分配：{atomic_id}")
            seen_atomic_ids.add(atomic_id)
        normalized_clusters.append(
            {
                "cluster_id": cluster_id,
                "theme_name": theme_name,
                "member_atomic_ids": normalized_member_ids,
                **flags,
                "shared_knowledge_definition": shared_definition,
                "merge_basis": merge_basis,
                "confidence": confidence,
                "requires_review": requires_review,
            }
        )

    normalized_splits: list[dict[str, Any]] = []
    for request in split_requests:
        if not isinstance(request, dict):
            raise MimoError("split_requests 成员必须为 JSON 对象")
        atomic_id = _text(request.get("atomic_id"), 120)
        reason = _text(request.get("reason"), 300)
        suggested_splits = request.get("suggested_splits")
        if (
            not atomic_id
            or not reason
            or not isinstance(suggested_splits, list)
            or not all(isinstance(item, str) for item in suggested_splits)
        ):
            raise MimoError("split_requests 缺少 atomic_id、reason 或 suggested_splits")
        if atomic_id not in allowed_atomic_ids:
            raise MimoError(f"split_requests 包含输入中不存在的 atomic_id：{atomic_id}")
        if atomic_id in seen_atomic_ids:
            raise MimoError(f"atomic_id 重复分配：{atomic_id}")
        seen_atomic_ids.add(atomic_id)
        normalized_splits.append(
            {
                "atomic_id": atomic_id,
                "reason": reason,
                "suggested_splits": [
                    _text(item, 160)
                    for item in suggested_splits[:5]
                    if _text(item, 160)
                ],
            }
        )

    normalized_reviews: list[dict[str, Any]] = []
    for request in review_requests:
        if not isinstance(request, dict):
            raise MimoError("review_requests 成员必须为 JSON 对象")
        atomic_id = _text(request.get("atomic_id"), 120)
        review_type = _text(request.get("review_type"), 80)
        reason = _text(request.get("reason"), 300)
        if not all((atomic_id, review_type, reason)):
            raise MimoError("review_requests 缺少 atomic_id、review_type 或 reason")
        if atomic_id not in allowed_atomic_ids:
            raise MimoError(f"review_requests 包含输入中不存在的 atomic_id：{atomic_id}")
        if atomic_id in seen_atomic_ids:
            raise MimoError(f"atomic_id 重复分配：{atomic_id}")
        seen_atomic_ids.add(atomic_id)
        normalized_reviews.append(
            {
                "atomic_id": atomic_id,
                "review_type": review_type,
                "reason": reason,
            }
        )

    missing_ids = allowed_atomic_ids - seen_atomic_ids
    if missing_ids:
        raise MimoError(f"原子知识主题聚类遗漏 atomic_id：{', '.join(sorted(missing_ids))}")
    return {
        "clusters": normalized_clusters,
        "split_requests": normalized_splits,
        "review_requests": normalized_reviews,
    }


def _validate_topic_signal(value: Any, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("会话语义标注输出不是 JSON 对象")
    intent = _text(value.get("intent"), 32)
    allowed_intents = {"标准判定", "检测核验", "信息查询", "流程操作", "其他待确认"}
    if intent not in allowed_intents:
        raise MimoError("会话语义标注 intent 不合法")
    required_text = [
        "subject",
        "phenomenon",
        "resolution_mode",
        "category_l1",
        "category_l2",
        "image_evidence_summary",
        "reasoning_summary",
    ]
    for key in required_text:
        if not _text(value.get(key)):
            raise MimoError(f"会话语义标注缺少或为空：{key}")
    tags = value.get("topic_tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise MimoError("会话语义标注 topic_tags 必须为字符串数组")
    normalized_tags = list(dict.fromkeys(_text(tag, 80) for tag in tags if _text(tag, 80)))
    if not 3 <= len(normalized_tags) <= 6 or any(":" not in tag for tag in normalized_tags):
        raise MimoError("会话语义标注 topic_tags 必须为 3 到 6 个“维度:值”标签")
    refs = value.get("standard_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise MimoError("会话语义标注 standard_refs 必须为字符串数组")
    normalized_refs = list(dict.fromkeys(_text(ref, 160) for ref in refs if _text(ref, 160)))
    invalid_refs = set(normalized_refs) - allowed_refs
    if invalid_refs:
        raise MimoError(f"会话语义标注引用了本次未检索到的标准：{', '.join(sorted(invalid_refs))}")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("会话语义标注 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("会话语义标注 confidence 必须在 0~1")
    return {
        "intent": intent,
        "subject": _text(value.get("subject"), 120),
        "phenomenon": _text(value.get("phenomenon"), 160),
        "resolution_mode": _text(value.get("resolution_mode"), 160),
        "category_l1": _text(value.get("category_l1"), 80),
        "category_l2": _text(value.get("category_l2"), 80),
        "topic_tags": normalized_tags,
        "standard_refs": normalized_refs,
        "requires_images": _as_bool(value.get("requires_images")),
        "image_evidence_summary": _text(value.get("image_evidence_summary"), 800),
        "reasoning_summary": _text(value.get("reasoning_summary"), 240),
        "confidence": round(confidence, 3),
        "needs_human_review": _as_bool(value.get("needs_human_review")),
    }


class MimoClient:
    def __init__(self, config: MimoConfig) -> None:
        self.terminology = ensure_terminology_loaded()
        self.config = config
        self._circuit_open_reason = ""
        self._circuit_lock = threading.Lock()
        self._response_timeout_count = 0
        self._api_key_lock = threading.Lock()
        self._active_api_key_index = 0
        self._disabled_api_key_indexes: set[int] = set()
        self._metrics_lock = threading.Lock()
        self._rate_limit_lock = threading.Lock()
        self._last_request_at = 0.0
        self._metrics: dict[str, float | int] = {
            "model_calls": 0,
            "model_failed_calls": 0,
            "model_retries": 0,
            "model_key_switches": 0,
            "model_input_tokens": 0,
            "model_output_tokens": 0,
            "model_total_tokens": 0,
            "model_latency_ms": 0.0,
            "model_estimated_cost": 0.0,
            "cluster_media_attached_rows": 0,
            "cluster_media_deferred_rows": 0,
            "cluster_text_only_rows": 0,
        }

    @classmethod
    def from_env(cls) -> "MimoClient | None":
        config = MimoConfig.from_env()
        return cls(config) if config else None

    def check_availability(self) -> dict[str, Any]:
        """Make one small request to verify the configured MiMo key and endpoint."""
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是用于连通性预检的助手，只返回 JSON。",
                },
                {
                    "role": "user",
                    "content": "请只返回 {\"ok\": true}，不要添加解释。",
                },
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        response = self._post(payload)
        content = _content_from_response(response)
        try:
            parsed = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise MimoError("MiMo 预检响应不是合法 JSON") from exc
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise MimoError("MiMo 预检响应缺少 ok=true")
        return {
            "passed": True,
            "model": self.config.model,
            "key_pool_size": len(self.config.all_api_keys()),
            "terminology": self.terminology,
        }

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metrics_lock:
            snapshot = dict(self._metrics)
        calls = int(snapshot["model_calls"])
        snapshot["model_average_latency_ms"] = (
            round(float(snapshot["model_latency_ms"]) / calls, 3)
            if calls
            else 0.0
        )
        snapshot["model_latency_ms"] = round(float(snapshot["model_latency_ms"]), 3)
        snapshot["model_estimated_cost"] = round(
            float(snapshot["model_estimated_cost"]),
            6,
        )
        return snapshot

    def _throttle(self) -> None:
        interval = 1.0 / max(0.1, self.config.max_requests_per_second)
        with self._rate_limit_lock:
            now = time.monotonic()
            wait_seconds = interval - (now - self._last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()

    def _record_call_metrics(
        self,
        response: dict[str, Any] | None,
        *,
        latency_ms: float,
        failed: bool = False,
        retried: bool = False,
        key_switches: int = 0,
    ) -> None:
        usage = response.get("usage") if isinstance(response, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        output_tokens = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        total_tokens = int(
            usage.get("total_tokens")
            or input_tokens + output_tokens
        )
        cost = (
            input_tokens * self.config.input_cost_per_million_tokens
            + output_tokens * self.config.output_cost_per_million_tokens
        ) / 1_000_000
        with self._metrics_lock:
            self._metrics["model_calls"] = int(self._metrics["model_calls"]) + 1
            self._metrics["model_failed_calls"] = int(
                self._metrics["model_failed_calls"]
            ) + int(failed)
            self._metrics["model_retries"] = int(
                self._metrics["model_retries"]
            ) + int(retried)
            self._metrics["model_key_switches"] = int(
                self._metrics["model_key_switches"]
            ) + max(0, int(key_switches))
            self._metrics["model_input_tokens"] = int(
                self._metrics["model_input_tokens"]
            ) + input_tokens
            self._metrics["model_output_tokens"] = int(
                self._metrics["model_output_tokens"]
            ) + output_tokens
            self._metrics["model_total_tokens"] = int(
                self._metrics["model_total_tokens"]
            ) + total_tokens
            self._metrics["model_latency_ms"] = float(
                self._metrics["model_latency_ms"]
            ) + latency_ms
            self._metrics["model_estimated_cost"] = float(
                self._metrics["model_estimated_cost"]
            ) + cost

    def _current_api_key(self) -> tuple[int, str]:
        api_keys = self.config.all_api_keys()
        with self._api_key_lock:
            for offset in range(len(api_keys)):
                index = (
                    self._active_api_key_index + offset
                ) % len(api_keys)
                if index in self._disabled_api_key_indexes:
                    continue
                self._active_api_key_index = index
                return index, api_keys[index]
        raise MimoError("所有配置的 MiMo API Key 均不可用")

    def _disable_api_key(self, index: int) -> bool:
        api_keys = self.config.all_api_keys()
        with self._api_key_lock:
            self._disabled_api_key_indexes.add(index)
            for offset in range(1, len(api_keys) + 1):
                candidate = (index + offset) % len(api_keys)
                if candidate in self._disabled_api_key_indexes:
                    continue
                self._active_api_key_index = candidate
                return True
        return False

    def label(
        self,
        source_row: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        images: list[ImageEvidence],
    ) -> MimoLabelResult:
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_prompt(source_row, matches, images, validation_error)
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images[:4]:
                if image.status == "ready" and image.data_url:
                    content.append({"type": "image_url", "image_url": {"url": image.data_url}})
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类回收质检知识标注助手。"},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": PROMPT_VERSION,
                "source": _source_payload(source_row),
                "retrieved_standards": _standard_payload(matches),
                "images": _image_metadata(images),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                candidate = _validate_candidate(json.loads(_strip_json_fence(raw_content)), allowed_refs)
                return MimoLabelResult(candidate=candidate, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 调用失败：{exc}") from exc
        raise MimoError("MiMo 调用未产生有效结果")

    def analyze_topic_signal(
        self,
        source_row: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        images: list[ImageEvidence],
        use_standard_references: bool = True,
    ) -> MimoLabelResult:
        """Normalize one conversation into auditable clustering signals."""
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_signal_prompt(
                source_row,
                matches,
                images,
                validation_error,
                use_standard_references=use_standard_references,
            )
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images[:4]:
                if image.status == "ready" and image.data_url:
                    content.append({"type": "image_url", "image_url": {"url": image.data_url}})
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类质检会话语义标注助手。"},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": TOPIC_SIGNAL_PROMPT_VERSION,
                "source": _topic_signal_source_payload(source_row),
                "retrieved_standards": _standard_payload(matches),
                "images": _image_metadata(images),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                signal = _validate_topic_signal(json.loads(_strip_json_fence(raw_content)), allowed_refs)
                return MimoLabelResult(candidate=signal, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 会话语义标注 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 会话语义标注调用失败：{exc}") from exc
        raise MimoError(f"MiMo 会话语义标注未产生有效结果：{last_response}")

    def can_batch_cluster_units(self, source_row: dict[str, Any]) -> bool:
        """Return whether atomic extraction can safely use a text-only batch."""
        media_parts, _media_audit = _cluster_media_parts(source_row)
        return not _should_attach_cluster_media(
            source_row,
            media_parts,
            policy=self.config.cluster_media_policy,
            min_text_chars=self.config.cluster_media_min_text_chars,
        )

    def analyze_cluster_units_batch(
        self,
        source_rows: list[dict[str, Any]],
    ) -> list[MimoLabelResult]:
        """Split multiple same-product text conversations in one request."""
        if not source_rows:
            return []
        products = {
            canonical_product_name(
                row.get("产品类型"),
                unknown=_text(row.get("产品类型"), 80),
            )
            for row in source_rows
        }
        if len(products) != 1:
            raise MimoError("批量原子问题提取只允许同一产品品类")
        if any(not self.can_batch_cluster_units(row) for row in source_rows):
            raise MimoError("需要附加图片或视频的会话不得进入文本批量提取")

        record_ids = [
            f"R{index:04d}"
            for index in range(1, len(source_rows) + 1)
        ]
        batch_records = list(zip(record_ids, source_rows))
        with self._metrics_lock:
            for source_row in source_rows:
                media_parts, _media_audit = _cluster_media_parts(source_row)
                metric_name = (
                    "cluster_media_deferred_rows"
                    if media_parts
                    else "cluster_text_only_rows"
                )
                self._metrics[metric_name] = int(
                    self._metrics[metric_name]
                ) + 1

        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_cluster_unit_prompt(
                source_rows[0],
                retry_reason=validation_error,
                batch_records=batch_records,
                media_policy=self.config.cluster_media_policy,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是严谨的知识主题聚类问题单元批量提取器。",
                    },
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    },
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
                "batch_size": len(source_rows),
                "sources": [
                    {
                        "record_id": record_id,
                        **_cluster_unit_input_payload(
                            source_row,
                            media_policy=self.config.cluster_media_policy,
                        ),
                    }
                    for record_id, source_row in batch_records
                ],
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                candidates = _validate_cluster_unit_batch(
                    json.loads(_strip_json_fence(raw_content)),
                    record_ids,
                )
                candidates = [
                    _guard_cluster_unit_source_claims(candidate, source_row)
                    for candidate, source_row in zip(candidates, source_rows)
                ]
                return [
                    MimoLabelResult(
                        candidate=candidate,
                        request_audit={
                            **request_audit,
                            "record_id": record_id,
                        },
                        response_audit=raw_response,
                    )
                    for record_id, candidate in zip(record_ids, candidates)
                ]
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        "MiMo批量原子问题提取JSON校验失败（已重试一次）："
                        f"{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(
                    f"MiMo批量原子问题提取调用失败：{exc}"
                ) from exc
        raise MimoError(
            f"MiMo批量原子问题提取未产生有效结果：{last_response}"
        )

    def analyze_cluster_units(
        self,
        source_row: dict[str, Any],
    ) -> MimoLabelResult:
        """Split one conversation into one to three auditable clustering units."""
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        media_parts, media_audit = _cluster_media_parts(source_row)
        attach_media = _should_attach_cluster_media(
            source_row,
            media_parts,
            policy=self.config.cluster_media_policy,
            min_text_chars=self.config.cluster_media_min_text_chars,
        )
        active_media_parts = list(media_parts) if attach_media else []
        media_audit["policy"] = self.config.cluster_media_policy
        media_audit["min_text_chars"] = self.config.cluster_media_min_text_chars
        media_audit["conversation_chars"] = len(
            _primary_conversation_evidence(source_row.get("聊天内容"))
        )
        if media_parts and not attach_media:
            media_disabled = (
                self.config.cluster_media_policy.strip().lower() == "never"
            )
            media_audit["mode"] = (
                "mimo-direct-text-only"
                if media_disabled
                else "mimo-direct-on-demand-text-only"
            )
            for media_type in ("images", "videos"):
                for item in media_audit[media_type]:
                    item["status"] = "skipped" if media_disabled else "deferred"
                    item["reason"] = (
                        "聚类验证已关闭图片和视频输入"
                        if media_disabled
                        else "完整聊天达到聚类文本充分阈值"
                    )
        had_media = bool(media_parts)
        attached_media = bool(active_media_parts)
        with self._metrics_lock:
            if attached_media:
                self._metrics["cluster_media_attached_rows"] = int(
                    self._metrics["cluster_media_attached_rows"]
                ) + 1
            elif media_parts:
                self._metrics["cluster_media_deferred_rows"] = int(
                    self._metrics["cluster_media_deferred_rows"]
                ) + 1
            else:
                self._metrics["cluster_text_only_rows"] = int(
                    self._metrics["cluster_text_only_rows"]
                ) + 1
        request_model = (
            (self.config.media_model or self.config.model)
            if attached_media
            else self.config.model
        )
        for attempt in range(3):
            attached_image_count = sum(
                part.get("type") == "image_url" for part in active_media_parts
            )
            attached_video_count = sum(
                part.get("type") == "video_url" for part in active_media_parts
            )
            prompt = _build_cluster_unit_prompt(
                source_row,
                retry_reason=validation_error,
                attached_image_count=attached_image_count,
                attached_video_count=attached_video_count,
                media_policy=self.config.cluster_media_policy,
            )
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            content.extend(active_media_parts)
            payload = {
                "model": request_model,
                "messages": [
                    {"role": "system", "content": "你是严谨的知识主题聚类问题单元提取器。"},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": request_model,
                "configured_text_model": self.config.model,
                "configured_media_model": self.config.media_model,
                "prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
                "source": {
                    "工单ID": _text(source_row.get("工单ID")),
                    "产品类型": _text(source_row.get("产品类型")),
                    "聊天内容": _text(source_row.get("聊天内容"), 9000),
                    "核心问题": _text(source_row.get("核心问题"), 1200),
                    "判定结论": _text(source_row.get("判定结论"), 1200),
                    "上游媒体分析摘要": _text(source_row.get("上游媒体分析摘要"), 2400),
                    "有图片链接": bool(_text(source_row.get("图片链接"))),
                    "有视频链接": bool(_text(source_row.get("视频链接"))),
                },
                "media": media_audit,
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                cluster_units = _validate_cluster_units(
                    json.loads(_strip_json_fence(raw_content)),
                    require_media_analysis=attached_media,
                )
                cluster_units = _guard_cluster_unit_source_claims(
                    cluster_units,
                    source_row,
                )
                unavailable_media = [
                    item
                    for media_type in ("images", "videos")
                    for item in media_audit[media_type]
                    if item.get("status") == "unavailable"
                ]
                if unavailable_media:
                    cluster_units["media_analysis"]["requires_review"] = True
                    unavailable_videos = [
                        item
                        for item in media_audit["videos"]
                        if item.get("status") == "unavailable"
                    ]
                    if (
                        unavailable_videos
                        and cluster_units["media_analysis"]["video_summary"]
                        in {"无视频", "无视频。"}
                    ):
                        cluster_units["media_analysis"]["video_summary"] = (
                            f"{len(unavailable_videos)}个视频无法读取，"
                            "已降级使用图片和聊天内容分析。"
                        )
                    for topic in cluster_units["topics"]:
                        topic["requires_review"] = True
                return MimoLabelResult(
                    candidate=cluster_units,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                error_text = str(exc)
                corrupted_media = (
                    "Multimodal data is corrupted" in error_text
                    or "cannot be processed" in error_text
                )
                active_video_parts = [
                    part
                    for part in active_media_parts
                    if part.get("type") == "video_url"
                ]
                active_image_parts = [
                    part
                    for part in active_media_parts
                    if part.get("type") == "image_url"
                ]
                if corrupted_media and active_video_parts:
                    active_media_parts = active_image_parts
                    for video in media_audit["videos"]:
                        video["status"] = "unavailable"
                        video["error"] = "MiMo 无法解析该视频，已降级为图片和文本分析"
                    media_audit["mode"] = "mimo-direct-multimodal-video-fallback"
                    validation_error = (
                        "视频附件无法读取，已移除视频并保留图片和文本。"
                        "video_summary 必须明确写“视频无法读取”，并设置 requires_review=true。"
                    )
                    continue
                if corrupted_media and active_image_parts:
                    active_media_parts = []
                    for image in media_audit["images"]:
                        image["status"] = "unavailable"
                        image["error"] = "MiMo 无法解析该图片，已降级为文本分析"
                    media_audit["mode"] = "mimo-direct-multimodal-text-fallback"
                    validation_error = (
                        "媒体附件无法读取，已降级为文本分析。"
                        "media_analysis 必须说明媒体无法读取，并设置 requires_review=true。"
                    )
                    continue
                validation_error = error_text
                if attempt == 2:
                    raise MimoError(
                        f"MiMo 聚类问题单元 JSON 校验失败（已重试两次）：{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo 聚类问题单元调用失败：{exc}") from exc
        raise MimoError(f"MiMo 聚类问题单元未产生有效结果：{last_response}")

    def fuse_cluster_units(
        self,
        source_row: dict[str, Any],
        text_candidate: dict[str, Any],
        media_candidate: dict[str, Any],
        media_audit: dict[str, Any],
    ) -> MimoLabelResult:
        """Fuse text-Pro topics with media facts without deleting explicit text topics."""
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_cluster_fusion_prompt(
                source_row,
                text_candidate,
                media_candidate,
                media_audit,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是严格执行文字主题保留与媒体增量规则的融合裁决器。",
                    },
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    },
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": CLUSTER_FUSION_PROMPT_VERSION,
                "text_conversation_type": _text(
                    text_candidate.get("conversation_type"),
                    32,
                ),
                "media_conversation_type": _text(
                    media_candidate.get("conversation_type"),
                    32,
                ),
                "media": media_audit,
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                fused = _validate_cluster_units(
                    json.loads(_strip_json_fence(raw_content)),
                    require_media_analysis=True,
                )
                fused = _enforce_cluster_fusion_guardrails(
                    fused,
                    text_candidate,
                    media_candidate,
                    media_audit,
                )
                fused = _guard_cluster_unit_source_claims(
                    fused,
                    source_row,
                )
                return MimoLabelResult(
                    candidate=fused,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        "MiMo媒体融合JSON校验失败（已重试一次）："
                        f"{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo媒体融合调用失败：{exc}") from exc
        raise MimoError(f"MiMo媒体融合未产生有效结果：{last_response}")

    def cluster_atomic_units(
        self,
        atomic_units: list[dict[str, Any]],
    ) -> MimoLabelResult:
        """Directly partition one compatible bucket into one-to-N knowledge topics."""
        allowed_atomic_ids = {
            _text(unit.get("unit_id") or unit.get("atomic_id"), 120)
            for unit in atomic_units
            if _text(unit.get("unit_id") or unit.get("atomic_id"), 120)
        }
        if len(allowed_atomic_ids) != len(atomic_units):
            raise MimoError("原子知识点 ID 为空或重复")

        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_atomic_topic_cluster_prompt(
                atomic_units,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严格执行硬业务规则的知识主题聚类专家。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
                "atomic_units": [_atomic_unit_payload(unit) for unit in atomic_units],
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                cluster_result = _validate_atomic_topic_clusters(
                    json.loads(_strip_json_fence(raw_content)),
                    allowed_atomic_ids,
                )
                return MimoLabelResult(
                    candidate=cluster_result,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        f"MiMo 原子知识主题聚类 JSON 校验失败（已重试一次）：{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo 原子知识主题聚类调用失败：{exc}") from exc
        raise MimoError(f"MiMo 原子知识主题聚类未产生有效结果：{last_response}")

    def label_topic(
        self,
        topic: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        use_standard_references: bool = True,
        retry_reason: str = "",
    ) -> MimoLabelResult:
        """Generate a reusable draft after clustering, never from a single case."""
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = retry_reason
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_prompt(
                topic,
                matches,
                validation_error,
                use_standard_references=use_standard_references,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类主题知识标注助手。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": PROMPT_VERSION,
                "topic": topic,
                "retrieved_standards": _standard_payload(matches),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                candidate = _validate_candidate(json.loads(_strip_json_fence(raw_content)), allowed_refs)
                return MimoLabelResult(candidate=candidate, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 主题 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 主题调用失败：{exc}") from exc
        raise MimoError(f"MiMo 主题调用未产生有效结果：{last_response}")

    def review_topic(
        self,
        topic: dict[str, Any],
        draft: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        transcription_matches: list[tuple[StandardCatalogItem, float]] | None = None,
        use_standard_references: bool = True,
    ) -> MimoLabelResult:
        """Audit a transcribed topic draft without changing its 13-column content."""
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_review_prompt(
                topic,
                draft,
                matches,
                transcription_matches=transcription_matches,
                retry_reason=validation_error,
                use_standard_references=use_standard_references,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类主题知识初审员。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": TOPIC_REVIEW_PROMPT_VERSION,
                "topic": topic,
                "draft": draft,
                "retrieved_standards": _standard_payload(matches),
                "transcription_retrieved_standards": _standard_payload(transcription_matches or []),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                review = _validate_topic_review(json.loads(_strip_json_fence(raw_content)))
                expected_knowledge_value = _text(topic.get("knowledge_value"), 32)
                if (
                    expected_knowledge_value == "值得沉淀"
                    and review.get("knowledge_value") != expected_knowledge_value
                ):
                    raise MimoError(
                        "内容质量初标不得重新修改主题沉淀价值，knowledge_value 必须为值得沉淀"
                    )
                return MimoLabelResult(candidate=review, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 主题初标 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 主题初标调用失败：{exc}") from exc
        raise MimoError(f"MiMo 主题初标未产生有效结果：{last_response}")

    def classify_topic_stage(
        self,
        topic: dict[str, Any],
    ) -> MimoLabelResult:
        """Classify a clustered topic by lifecycle stage and reuse value."""
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_stage_prompt(
                topic,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的主题环节与知识沉淀价值标注员。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": TOPIC_STAGE_PROMPT_VERSION,
                "topic": topic,
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                classification = _validate_topic_stage(
                    json.loads(_strip_json_fence(raw_content))
                )
                return MimoLabelResult(
                    candidate=classification,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        f"MiMo 主题环节 JSON 校验失败（已重试一次）：{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo 主题环节调用失败：{exc}") from exc
        raise MimoError(f"MiMo 主题环节调用未产生有效结果：{last_response}")

    def rewrite_topic_display_questions(
        self,
        topics: list[dict[str, Any]],
    ) -> MimoLabelResult:
        """Rewrite clustered topics into short frontline questions."""
        allowed_theme_ids = {
            _text(topic.get("theme_id"), 80)
            for topic in topics
            if _text(topic.get("theme_id"), 80)
        }
        if not topics or len(allowed_theme_ids) != len(topics):
            raise MimoError("主题问句改写要求 theme_id 非空且不重复")
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_display_questions_prompt(
                topics,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是简洁准确的中文主题问句改写员。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
                "topics": topics,
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                questions = _validate_topic_display_questions(
                    json.loads(_strip_json_fence(raw_content)),
                    allowed_theme_ids,
                )
                return MimoLabelResult(
                    candidate={"questions": questions},
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        f"MiMo 主题问句 JSON 校验失败（已重试一次）：{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo 主题问句调用失败：{exc}") from exc
        raise MimoError(f"MiMo 主题问句调用未产生有效结果：{last_response}")

    def review_cluster_pair(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        similarity: float,
        threshold: float,
    ) -> MimoLabelResult:
        """Judge whether two conversation records belong to the same knowledge topic."""
        return self._review_cluster_candidate(left, right, similarity, threshold)

    def review_cluster_membership(
        self,
        candidate: dict[str, Any],
        cluster_members: list[dict[str, Any]],
        similarity: float,
        threshold: float,
    ) -> MimoLabelResult:
        """Judge whether one atomic knowledge point can join every member of a topic cluster."""
        cluster_payload = {
            "cluster_member_count": len(cluster_members),
            "cluster_members": cluster_members,
        }
        return self._review_cluster_candidate(
            candidate,
            cluster_payload,
            similarity,
            threshold,
        )

    def _review_cluster_candidate(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        similarity: float,
        threshold: float,
    ) -> MimoLabelResult:
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_cluster_pair_review_prompt(
                left,
                right,
                similarity,
                threshold,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的知识主题聚类标注员。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": CLUSTER_PAIR_REVIEW_PROMPT_VERSION,
                "left": left,
                "right": right,
                "similarity": round(similarity, 4),
                "threshold": round(threshold, 4),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                review = _validate_cluster_pair_review(json.loads(_strip_json_fence(raw_content)))
                return MimoLabelResult(
                    candidate=review,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 聚类标注 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 聚类标注调用失败：{exc}") from exc
        raise MimoError(f"MiMo 聚类标注未产生有效结果：{last_response}")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._circuit_lock:
            circuit_open_reason = self._circuit_open_reason
        if circuit_open_reason:
            raise MimoError(
                "MiMo 本次运行已熔断："
                f"{circuit_open_reason}"
            )
        request_payload = dict(payload)
        request_payload.setdefault(
            "thinking",
            {"type": self.config.thinking_type},
        )
        request_payload.setdefault(
            "max_completion_tokens",
            self.config.max_completion_tokens,
        )
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        request_attempt = 0
        transient_retry = 0
        key_switches = 0
        api_keys = self.config.all_api_keys()
        while True:
            try:
                api_key_index, api_key = self._current_api_key()
            except MimoError as exc:
                self._record_call_metrics(
                    None,
                    latency_ms=0.0,
                    failed=True,
                    retried=request_attempt > 0,
                    key_switches=key_switches,
                )
                raise exc
            self._throttle()
            request_attempt += 1
            request = Request(
                self.config.chat_completions_url(),
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            started = time.monotonic()
            try:
                raw = _post_request_with_deadline(
                    request,
                    self.config.timeout_seconds,
                    self.config.response_read_timeout_seconds,
                )
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise MimoError("MiMo 返回的根节点不是对象")
                latency_ms = (time.monotonic() - started) * 1000
                self._record_call_metrics(
                    parsed,
                    latency_ms=latency_ms,
                    retried=request_attempt > 1,
                    key_switches=key_switches,
                )
                with self._circuit_lock:
                    self._response_timeout_count = 0
                parsed.setdefault(
                    "_answer_hub_metrics",
                    {
                        "latency_ms": round(latency_ms, 3),
                        "attempt": request_attempt,
                        "key_switches": key_switches,
                        "key_pool_size": len(api_keys),
                    },
                )
                return parsed
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:600]
                for configured_key in api_keys:
                    detail = detail.replace(configured_key, "[REDACTED]")
                last_error = MimoError(f"MiMo HTTP {exc.code}: {detail}")
                normalized_detail = detail.lower()
                rotate_key = exc.code in {401, 402, 403} or any(
                    marker in normalized_detail
                    for marker in _API_KEY_ROTATION_MARKERS
                )
                retryable = (
                    not rotate_key
                    and (exc.code == 429 or 500 <= exc.code < 600)
                )
            except URLError as exc:
                last_error = MimoError(f"MiMo 网络错误：{exc.reason}")
                rotate_key = False
                retryable = True
            except TimeoutError as exc:
                last_error = MimoError(f"MiMo 网络读取超时：{exc}")
                rotate_key = False
                retryable = True
            except json.JSONDecodeError as exc:
                last_error = MimoError(f"MiMo 返回非 JSON 响应：{raw[:300]}")
                rotate_key = False
                retryable = False
            except MimoError as exc:
                last_error = exc
                rotate_key = False
                retryable = False
                if "响应总超时" in str(exc):
                    with self._circuit_lock:
                        self._response_timeout_count += 1
                        if (
                            self._response_timeout_count
                            >= self.config.response_timeout_circuit_breaker_failures
                        ):
                            self._circuit_open_reason = (
                                f"{exc}（连续响应超时 "
                                f"{self._response_timeout_count} 次）"
                            )
            latency_ms = (time.monotonic() - started) * 1000
            if rotate_key:
                if self._disable_api_key(api_key_index):
                    key_switches += 1
                    continue
                self._record_call_metrics(
                    None,
                    latency_ms=latency_ms,
                    failed=True,
                    retried=request_attempt > 1,
                    key_switches=key_switches,
                )
                raise MimoError(
                    f"{last_error}；所有配置的 MiMo API Key 均不可用"
                )
            if not retryable or transient_retry >= self.config.max_retries:
                self._record_call_metrics(
                    None,
                    latency_ms=latency_ms,
                    failed=True,
                    retried=request_attempt > 1,
                    key_switches=key_switches,
                )
                raise last_error
            time.sleep(
                self.config.retry_backoff_seconds
                * (2**transient_retry)
            )
            transient_retry += 1
