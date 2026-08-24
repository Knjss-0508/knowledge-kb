from __future__ import annotations

"""Authenticated CZ knowledge-base integration with bounded retries."""

from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import hashlib
import json
import os
import re
import time

from .auto_review import (
    AutoReviewPolicy,
    UNUSABLE_VALUES,
    UNWORTHY_VALUES,
    USABLE_VALUES,
    WORTHY_VALUES,
    select_candidates_for_submission,
)
from .business_taxonomy import (
    AGGREGATE_BUSINESS_LINE_CODE,
    SELF_OPERATED_BUSINESS_LINE_CODE,
    business_line_from_record,
    cz_applicable_category_path,
)
from .catalog import StandardCatalogItem
from .knowledge_categories import category_lookup_names
from .mimo import load_dotenv
from .product_taxonomy import (
    infer_product_category,
    is_concrete_unconfigured_product,
    resolve_product_category,
)


TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
PASS_DECISIONS = {"通过", "修改后通过"}
PROCESSING_PLUGIN_NAME = "answer-hub-topic-transcription"
PROCESSING_PLUGIN_VERSION = "2026-08-06-evidence-facts-v1"
CZ_HEADQUARTERS_STANDARD_ORIGIN = "headquarters_standard"
CZ_BUSINESS_ACCUMULATION_ORIGIN = "business_accumulation"
CZ_BUSINESS_TYPE_BY_ANSWER_HUB_CODE = {
    SELF_OPERATED_BUSINESS_LINE_CODE: "self_operated",
    AGGREGATE_BUSINESS_LINE_CODE: "aggregated",
}
UNFINISHED_TRANSCRIPTION_MARKERS = (
    "未生成知识草稿",
    "未进入知识转写",
    "未完成知识转写",
    "完成转写后才能提交",
    "需要补充完整知识内容后再送审",
    "需要补充完整、可追溯的知识内容后再送审",
    "已阻止通用模板作为知识草稿",
    "转写正文只有通用模板",
    "主题未进入知识转写",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _split_values(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []
    for separator in ("\n", "；", ";", "、", "|"):
        text = text.replace(separator, "\n")
    return list(dict.fromkeys(item.strip() for item in text.splitlines() if item.strip()))


def _candidate_raw_case_image_urls(
    candidate: dict[str, Any],
) -> list[str]:
    return _candidate_https_urls(
        candidate.get("图例") or candidate.get("主题图片链接"),
        limit=4,
    )


def _candidate_https_urls(value: Any, *, limit: int) -> list[str]:
    value = _text(value)
    urls: list[str] = []
    for line in value.splitlines():
        url = line.strip()
        if not _candidate_is_safe_https_url(url):
            continue
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _candidate_is_safe_https_url(url: str) -> bool:
    if len(url) > 2048:
        return False
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username
        or parsed.password
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        return False
    try:
        address = ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _candidate_unsafe_media_urls(value: Any) -> list[str]:
    return [
        url
        for line in _text(value).splitlines()
        if (url := line.strip()) and not _candidate_is_safe_https_url(url)
    ]


def _candidate_raw_case_video_urls(
    candidate: dict[str, Any],
) -> list[str]:
    return _candidate_https_urls(
        candidate.get("主题视频链接") or candidate.get("视频链接"),
        limit=2,
    )


def _candidate_traced_case_image_urls(
    candidate: dict[str, Any],
) -> set[str]:
    valid_pairs = _candidate_fact_source_pairs(candidate)
    valid_image_traces = _candidate_package_image_traces(
        candidate
    )
    pattern = re.compile(
        r"\[(?P<fact_id>F\d+)\]\s*"
        r"来源记录=(?P<source_id>[^|\n]+)\s*\|\s*"
        r"图片=(?P<url>https://\S+)"
    )
    return {
        match.group("url").strip()
        for match in pattern.finditer(
            _text(candidate.get("主题图例来源"))
        )
        if (
            match.group("fact_id").strip(),
            match.group("source_id").strip(),
        )
        in valid_pairs
        and (
            match.group("fact_id").strip(),
            match.group("source_id").strip(),
            match.group("url").strip(),
        )
        in valid_image_traces
    }


def _candidate_traced_case_video_urls(
    candidate: dict[str, Any],
) -> set[str]:
    valid_pairs = _candidate_fact_source_pairs(candidate)
    valid_video_traces = _candidate_package_video_traces(candidate)
    pattern = re.compile(
        r"\[(?P<fact_id>F\d+)\]\s*"
        r"来源记录=(?P<source_id>[^|\n]+)\s*\|\s*"
        r"视频=(?P<url>https://\S+)"
    )
    return {
        match.group("url").strip()
        for match in pattern.finditer(
            _text(candidate.get("主题视频来源"))
        )
        if (
            match.group("fact_id").strip(),
            match.group("source_id").strip(),
        )
        in valid_pairs
        and (
            match.group("fact_id").strip(),
            match.group("source_id").strip(),
            match.group("url").strip(),
        )
        in valid_video_traces
    }


def _candidate_fact_source_pairs(
    candidate: dict[str, Any],
) -> set[tuple[str, str]]:
    topic_source_ids = set(
        _split_values(candidate.get("主题来源记录ID"))
    )
    reference_pairs: set[tuple[str, str]] = set()
    reference_pattern = re.compile(
        r"\[(?P<fact_id>F\d+)\]\s*"
        r"(?:代表|来源)记录=(?P<source_id>[^|\n]+)"
    )
    for match in reference_pattern.finditer(
        _text(candidate.get("主题事实引用"))
    ):
        reference_pairs.add(
            (
                match.group("fact_id").strip(),
                match.group("source_id").strip(),
            )
        )

    package_pairs: set[tuple[str, str]] = set()
    raw_package = _text(candidate.get("主题事实证据包"))
    if raw_package:
        try:
            package = json.loads(raw_package)
        except (TypeError, ValueError, json.JSONDecodeError):
            package = {}
        for fact in package.get("representative_facts") or []:
            fact_id = _text(fact.get("fact_id"))
            source_id = _text(fact.get("source_record_id"))
            if fact_id and source_id:
                package_pairs.add((fact_id, source_id))
        for source_fact_ref in package.get("source_fact_refs") or []:
            match = reference_pattern.search(_text(source_fact_ref))
            if match:
                package_pairs.add(
                    (
                        match.group("fact_id").strip(),
                        match.group("source_id").strip(),
                    )
                )
    return {
        pair
        for pair in reference_pairs & package_pairs
        if pair[1] in topic_source_ids
    }


def _candidate_package_image_traces(
    candidate: dict[str, Any],
) -> set[tuple[str, str, str]]:
    raw_package = _text(candidate.get("主题事实证据包"))
    if not raw_package:
        return set()
    try:
        package = json.loads(raw_package)
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    return {
        (fact_id, source_id, _text(image_url))
        for fact in package.get("representative_facts") or []
        if (fact_id := _text(fact.get("fact_id")))
        and (source_id := _text(fact.get("source_record_id")))
        for image_url in fact.get("image_urls") or []
        if _text(image_url)
    }


def _candidate_package_video_traces(
    candidate: dict[str, Any],
) -> set[tuple[str, str, str]]:
    raw_package = _text(candidate.get("主题事实证据包"))
    if not raw_package:
        return set()
    try:
        package = json.loads(raw_package)
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    return {
        (fact_id, source_id, _text(video_url))
        for fact in package.get("representative_facts") or []
        if (fact_id := _text(fact.get("fact_id")))
        and (source_id := _text(fact.get("source_record_id")))
        for video_url in fact.get("video_urls") or []
        if _text(video_url)
    }


def _candidate_case_image_urls(candidate: dict[str, Any]) -> list[str]:
    traced_urls = _candidate_traced_case_image_urls(candidate)
    return [
        url
        for url in _candidate_raw_case_image_urls(candidate)
        if url in traced_urls
    ]


def _candidate_untraced_case_image_urls(
    candidate: dict[str, Any],
) -> list[str]:
    traced_urls = _candidate_traced_case_image_urls(candidate)
    return [
        url
        for url in _candidate_raw_case_image_urls(candidate)
        if url not in traced_urls
    ]


def _candidate_case_video_urls(candidate: dict[str, Any]) -> list[str]:
    traced_urls = _candidate_traced_case_video_urls(candidate)
    return [
        url
        for url in _candidate_raw_case_video_urls(candidate)
        if url in traced_urls
    ]


def _candidate_untraced_case_video_urls(
    candidate: dict[str, Any],
) -> list[str]:
    traced_urls = _candidate_traced_case_video_urls(candidate)
    return [
        url
        for url in _candidate_raw_case_video_urls(candidate)
        if url not in traced_urls
    ]


def _candidate_content_blocks(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    title = _text(candidate.get("主标题")) or "知识候选"
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "value": _text(candidate.get("知识内容")),
        }
    ]
    for index, url in enumerate(
        _candidate_case_image_urls(candidate),
        start=1,
    ):
        blocks.append(
            {
                "type": "image",
                "external_url": url,
                "alt": f"{title}案例图{index}",
                "caption": "来源案例图",
            }
        )
    for index, url in enumerate(
        _candidate_case_video_urls(candidate),
        start=1,
    ):
        blocks.append(
            {
                "type": "video",
                "external_url": url,
                "alt": f"{title}案例视频{index}",
                "caption": "来源案例视频（仅供人工播放）",
            }
        )
    return blocks


def _candidate_evidence_excerpt(candidate: dict[str, Any]) -> str | None:
    sections = []
    for title, field in (
        ("主题证据摘要", "主题证据摘要"),
        ("主题事实引用", "主题事实引用"),
    ):
        value = _text(candidate.get(field))
        if value:
            sections.append(f"【{title}】\n{value}")
    for title, value in (
        (
            "主题图例来源",
            _candidate_validated_media_trace(candidate, media_type="image"),
        ),
        (
            "主题视频来源",
            _candidate_validated_media_trace(candidate, media_type="video"),
        ),
    ):
        if value:
            sections.append(f"【{title}】\n{value}")
    excerpt = "\n\n".join(sections)[:4000]
    return excerpt or None


def _candidate_validated_media_trace(
    candidate: dict[str, Any],
    *,
    media_type: str,
) -> str:
    if media_type == "image":
        field = "主题图例来源"
        label = "图片"
        valid_triples = _candidate_package_image_traces(candidate)
    else:
        field = "主题视频来源"
        label = "视频"
        valid_triples = _candidate_package_video_traces(candidate)
    valid_pairs = _candidate_fact_source_pairs(candidate)
    pattern = re.compile(
        rf"\[(?P<fact_id>F\d+)\]\s*"
        rf"来源记录=(?P<source_id>[^|\n]+)\s*\|\s*"
        rf"{label}=(?P<url>https://\S+)"
    )
    lines: list[str] = []
    for match in pattern.finditer(_text(candidate.get(field))):
        fact_id = match.group("fact_id").strip()
        source_id = match.group("source_id").strip()
        url = match.group("url").strip()
        if (
            (fact_id, source_id) in valid_pairs
            and (fact_id, source_id, url) in valid_triples
            and _candidate_is_safe_https_url(url)
        ):
            lines.append(
                f"[{fact_id}] 来源记录={source_id} | {label}={url}"
            )
    return "\n".join(dict.fromkeys(lines))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _has_unfinished_transcription_placeholder(candidate: dict[str, Any]) -> bool:
    combined = "\n".join(
        _text(candidate.get(field))
        for field in ("知识内容", "校验备注", "模型初标原因", "自动审核原因")
    )
    return any(marker in combined for marker in UNFINISHED_TRANSCRIPTION_MARKERS)


def _product_type(candidate: dict[str, Any]) -> str:
    direct = candidate.get("产品类型编码") or candidate.get("产品类型")
    category = resolve_product_category(direct)
    if category:
        return category.name
    business_line = business_line_from_record(candidate)
    aggregate_product = candidate.get("适用范围") or direct
    if (
        business_line
        and business_line.code == AGGREGATE_BUSINESS_LINE_CODE
        and is_concrete_unconfigured_product(aggregate_product)
    ):
        return _text(aggregate_product)
    inferred = infer_product_category(
        (
            candidate.get("适用范围"),
            candidate.get("主题聚类键"),
            candidate.get("知识分类"),
        )
    )
    return inferred.name if inferred else ""


def _candidate_category_id(
    category_mapping: dict[str, str],
    product_type: str,
    knowledge_category: str,
) -> str:
    del product_type
    for category_name in category_lookup_names(knowledge_category):
        category_id = category_mapping.get(category_name)
        if category_id:
            return category_id
    return ""


def _candidate_applicable_categories(
    candidate: dict[str, Any],
    product_type: str,
) -> list[str]:
    explicit_ids = _split_values(candidate.get("CZ适用类目ID"))
    return explicit_ids or ([product_type] if product_type else [])


def _candidate_specific_applicability(value: Any) -> list[str]:
    ignored = {"", "通用", "不限", "全部", "所有", "待确认", "未知"}
    return [
        item
        for item in _split_values(value)
        if item not in ignored
    ]


def _stable_hash(*values: Any) -> str:
    payload_parts = []
    for value in values:
        if isinstance(value, (dict, list, tuple)):
            payload_parts.append(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
        else:
            payload_parts.append(_text(value))
    payload = "\n".join(payload_parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_submittable_candidates(
    candidates: list[dict[str, Any]],
    policy: AutoReviewPolicy | None = None,
) -> list[dict[str, Any]]:
    return select_candidates_for_submission(
        candidates,
        policy or AutoReviewPolicy.from_env(),
    )


@dataclass(frozen=True)
class CzIntegrationConfig:
    base_url: str
    integration_key: str
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    retrieval_key: str = ""

    @classmethod
    def from_env(cls) -> "CzIntegrationConfig | None":
        load_dotenv()
        base_url = os.getenv("KB_BASE_URL", "").strip()
        integration_key = os.getenv("KB_INTEGRATION_KEY", "").strip()
        retrieval_key = os.getenv("KB_RETRIEVAL_API_KEY", "").strip()
        if not (base_url and (integration_key or retrieval_key)):
            return None
        try:
            timeout = max(3.0, min(float(os.getenv("KB_TIMEOUT_SECONDS", "30")), 180.0))
        except ValueError:
            timeout = 30.0
        try:
            retries = max(0, min(int(os.getenv("KB_MAX_RETRIES", "3")), 8))
        except ValueError:
            retries = 3
        try:
            backoff = max(
                0.05,
                min(float(os.getenv("KB_RETRY_BACKOFF_SECONDS", "0.5")), 10.0),
            )
        except ValueError:
            backoff = 0.5
        return cls(
            base_url=base_url.rstrip("/"),
            integration_key=integration_key,
            retrieval_key=retrieval_key,
            timeout_seconds=timeout,
            max_retries=retries,
            retry_backoff_seconds=backoff,
        )

    @property
    def status(self) -> str:
        return "已配置"

    def endpoint(self, path: str) -> str:
        base_url = self.base_url.rstrip("/")
        normalized_path = "/" + path.lstrip("/")
        if base_url.endswith("/api/v1") and normalized_path.startswith("/api/v1/"):
            return f"{base_url[:-7]}{normalized_path}"
        return f"{base_url}{normalized_path}"


class CzIntegrationAdapter:
    taxonomy_path = "/api/v1/integration/taxonomy"
    qc_standards_path = "/api/v1/integration/qc-standards"
    qc_standards_search_path = "/api/v1/integration/qc-standards:search"
    standard_search_path = "/api/v1/integration/standard-search"
    dedup_path = "/api/v1/integration/knowledge-dedup:check"
    candidates_path = "/api/v1/integration/knowledge-candidates:batch"
    review_candidates_path = "/api/v1/integration/knowledge-review-candidates:batch"
    second_part_path = "/api/v1/integration/second-part/records:batch"
    ingestion_path = "/api/v1/integration/ingestions"

    def __init__(self, config: CzIntegrationConfig | None = None) -> None:
        self.config = config or CzIntegrationConfig.from_env()

    def readiness(self) -> dict[str, str | bool]:
        return {
            "configured": bool(self.config),
            "status": self.config.status if self.config else "API 未配置",
            "taxonomy_endpoint": self.taxonomy_path,
            "qc_standards_endpoint": self.qc_standards_path,
            "standard_search_endpoint": self.standard_search_path,
            "standard_retrieval_configured": bool(
                self.config and self.config.retrieval_key
            ),
            "dedup_endpoint": self.dedup_path,
            "candidate_endpoint": self.candidates_path,
            "review_candidate_endpoint": self.review_candidates_path,
            "second_part_endpoint": self.second_part_path,
        }

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.config:
            raise RuntimeError("未配置 KB_BASE_URL 或 CZ API Key。")
        request_key = api_key if api_key is not None else self.config.integration_key
        if not request_key:
            raise RuntimeError("未配置所需的 CZ API Key。")
        url = self.config.endpoint(path)
        if query:
            url = f"{url}?{urlencode(query)}"
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "X-Integration-Key": request_key,
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        if body is not None:
            headers["Content-Type"] = "application/json"

        last_error = ""
        for attempt in range(self.config.max_retries + 1):
            request = Request(url, data=body, headers=headers, method=method.upper())
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read()
                if not raw:
                    return {}
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise RuntimeError("CZ接口返回值不是JSON对象。")
                return decoded
            except HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
                except Exception:
                    detail = ""
                last_error = f"HTTP {exc.code}" + (f"：{detail}" if detail else "")
                if exc.code not in TRANSIENT_HTTP_CODES or attempt >= self.config.max_retries:
                    raise RuntimeError(f"CZ接口调用失败：{last_error}") from exc
            except (URLError, TimeoutError) as exc:
                last_error = _text(getattr(exc, "reason", exc))
                if attempt >= self.config.max_retries:
                    raise RuntimeError(f"CZ接口调用失败：{last_error}") from exc
            except json.JSONDecodeError as exc:
                raise RuntimeError("CZ接口返回了无法解析的JSON。") from exc
            time.sleep(self.config.retry_backoff_seconds * (2**attempt))
        raise RuntimeError(f"CZ接口调用失败：{last_error or '未知错误'}")

    def can_search_headquarters_standards(self) -> bool:
        return bool(self.config and self.config.retrieval_key)

    def search_headquarters_standards(
        self,
        *,
        conversation_id: str,
        normalized_question: str,
        business_type: str,
        product_type: str,
        model: str = "",
        limit: int = 5,
    ) -> tuple[list[tuple[StandardCatalogItem, float]], dict[str, Any]]:
        if not self.config or not self.config.retrieval_key:
            raise RuntimeError("未配置 KB_RETRIEVAL_API_KEY，无法读取 CZ 已生效标准。")
        source_id = _text(conversation_id)
        if not re.fullmatch(r"[0-9]{1,64}", source_id):
            raise ValueError("CZ 标准检索需要来源工单ID为 1 至 64 位数字。")
        question = _text(normalized_question)
        if not question:
            raise ValueError("CZ 标准检索缺少主题问题。")
        request_id = "answer-hub-standard:" + _stable_hash(
            source_id,
            question,
            business_type,
            product_type,
            model,
        )[:32]
        payload = {
            "conversationId": source_id,
            "requestId": request_id,
            "normalizedQuestion": question[:8000],
            "knowledgeOrigin": CZ_HEADQUARTERS_STANDARD_ORIGIN,
            "businessType": _text(business_type),
            "productType": _text(product_type),
            "model": _text(model),
            "orderInfo": {
                "category": _text(product_type),
                "model": _text(model),
            },
            "limit": max(1, min(int(limit), 5)),
        }
        response = self._request_json(
            "POST",
            self.standard_search_path,
            payload,
            api_key=self.config.retrieval_key,
            extra_headers={
                "X-Conversation-Id": source_id,
                "X-Request-Id": request_id,
            },
        )
        candidates = response.get("candidates")
        if not isinstance(candidates, list):
            raise RuntimeError("CZ 标准检索接口返回格式不正确。")
        knowledge_version = _text(response.get("knowledgeVersion"))
        matches: list[tuple[StandardCatalogItem, float]] = []
        standard_ids: list[str] = []
        ignored_business_accumulation_count = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise RuntimeError("CZ 标准检索候选格式不正确。")
            knowledge_origin = _text(candidate.get("knowledgeOrigin"))
            if knowledge_origin == CZ_BUSINESS_ACCUMULATION_ORIGIN:
                ignored_business_accumulation_count += 1
                continue
            if knowledge_origin != CZ_HEADQUARTERS_STANDARD_ORIGIN:
                raise RuntimeError("CZ 标准检索返回了非总部标准知识，已拒绝使用。")
            if _text(candidate.get("status")) != "published":
                raise RuntimeError("CZ 标准检索返回了未生效知识，已拒绝使用。")
            standard_id = _text(candidate.get("id"))
            if not standard_id:
                raise RuntimeError("CZ 标准检索候选缺少知识ID。")
            try:
                score = max(0.0, min(float(candidate.get("finalScore", candidate.get("score", 0))), 1.0))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("CZ 标准检索候选分数不正确。") from exc
            keywords = candidate.get("keywords")
            item = StandardCatalogItem(
                standard_id=standard_id,
                title=_text(candidate.get("title")),
                category_l1=_text(candidate.get("level1Label")),
                category_l2="",
                knowledge_type="总部标准",
                standard_path=f"CZ总部标准：{standard_id}",
                keywords=[
                    _text(value)
                    for value in keywords
                    if _text(value)
                ] if isinstance(keywords, list) else [],
                scope=_text(candidate.get("productType")),
                response_snippet=_text(candidate.get("text")),
                status="published",
                version=knowledge_version or "unknown",
            )
            matches.append((item, score))
            standard_ids.append(standard_id)
        return matches, {
            "source": CZ_HEADQUARTERS_STANDARD_ORIGIN,
            "status": _text(response.get("status")),
            "retrieval_mode": _text(response.get("retrievalMode")),
            "knowledge_version": knowledge_version,
            "score_threshold": response.get("scoreThreshold"),
            "standard_ids": standard_ids,
            "ignored_business_accumulation_count": (
                ignored_business_accumulation_count
            ),
            "ignored_nonstandard_candidate_count": (
                ignored_business_accumulation_count
            ),
        }

    def fetch_taxonomy(self) -> dict[str, Any]:
        payload = self._request_json("GET", self.taxonomy_path)
        if not isinstance(payload.get("categories"), list):
            raise RuntimeError("CZ分类字典返回格式不正确。")
        return payload

    def category_mapping(self) -> dict[str, str]:
        taxonomy = self.fetch_taxonomy()
        return {
            _text(item.get("name")): _text(item.get("id"))
            for item in taxonomy.get("categories", [])
            if _text(item.get("name")) and _text(item.get("id"))
        }

    def fetch_qc_standard_snapshot(
        self,
        category_id: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            self.qc_standards_path,
            query={
                "category_id": category_id,
                "limit": max(1, min(limit, 500)),
                "offset": max(0, offset),
            },
        )
        if not isinstance(payload.get("items"), list):
            raise RuntimeError("质检标准接口返回格式不正确。")
        return payload

    def fetch_all_qc_standards(self, category_id: str = "") -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        offset = 0
        snapshot_version = ""
        generated_at = ""
        while True:
            page = self.fetch_qc_standard_snapshot(
                category_id=category_id,
                limit=500,
                offset=offset,
            )
            page_version = _text(page.get("snapshot_version"))
            if snapshot_version and page_version != snapshot_version:
                raise RuntimeError("分页读取期间CZ质检标准快照发生变化，请重试。")
            snapshot_version = page_version
            generated_at = _text(page.get("generated_at"))
            page_items = page.get("items") or []
            items.extend(dict(item) for item in page_items)
            next_offset = page.get("next_offset")
            if next_offset is None:
                break
            offset = int(next_offset)
        return {
            "version": snapshot_version,
            "snapshot_version": snapshot_version,
            "generated_at": generated_at,
            "total_items": len(items),
            "items": items,
        }

    def save_qc_standard_snapshot(
        self,
        path: str | Path,
        category_id: str = "",
    ) -> dict[str, Any]:
        payload = self.fetch_all_qc_standards(category_id=category_id)
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(f"{output.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(output)
        return payload

    def validate_candidate(
        self,
        candidate: dict[str, Any],
        category_id: str,
        idempotency_key: str,
        *,
        require_eligible: bool = True,
    ) -> list[str]:
        errors: list[str] = []
        if not category_id:
            errors.append("未映射 category_id，禁止送审。")
        if not _text(candidate.get("主标题")):
            errors.append("缺少主标题。")
        if not _text(candidate.get("知识内容")):
            errors.append("缺少知识内容。")
        if _has_unfinished_transcription_placeholder(candidate):
            errors.append("未完成知识转写，禁止送审。请先补充可上线知识正文并完成内容质量初标。")
        if (
            require_eligible
            and _candidate_untraced_case_image_urls(candidate)
        ):
            errors.append(
                "案例图缺少来源事实引用，禁止送审。"
                "请补充事实ID、来源记录ID和图片对应关系。"
            )
        if (
            require_eligible
            and _candidate_untraced_case_video_urls(candidate)
        ):
            errors.append(
                "案例视频缺少来源事实引用，禁止送审。"
                "请补充事实ID、来源记录ID和视频对应关系。"
            )
        unsafe_media_urls = [
            *_candidate_unsafe_media_urls(
                candidate.get("图例") or candidate.get("主题图片链接")
            ),
            *_candidate_unsafe_media_urls(
                candidate.get("主题视频链接") or candidate.get("视频链接")
            ),
        ]
        if require_eligible and unsafe_media_urls:
            errors.append("案例媒体包含不安全或不受支持的链接，禁止送审。")
        if require_eligible and _text(candidate.get("关联标准项")):
            errors.append("已有标准关联，必须留在标准关联搁置流程，禁止直接送审。")
        if require_eligible and (
            _text(candidate.get("自动审核状态")) != "auto_approved"
            and _text(candidate.get("是否值得沉淀")).lower() not in WORTHY_VALUES
        ):
            errors.append("知识点尚未标注为值得沉淀，禁止送审。")
        if not idempotency_key:
            errors.append("缺少幂等键。")
        return errors

    def build_batch_payload(
        self,
        candidates: list[dict[str, Any]],
        category_mapping: dict[str, str],
        *,
        require_eligible: bool = True,
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for candidate in candidates:
            product_type = _product_type(candidate)
            knowledge_category = _text(candidate.get("知识分类"))
            category_id = _candidate_category_id(
                category_mapping,
                product_type,
                knowledge_category,
            )
            topic_id = _text(candidate.get("主题ID")) or _text(candidate.get("主标题"))
            idempotency_key = (
                "sha256:"
                f"{_stable_hash('knowledge-candidate', topic_id, candidate.get('来源版本'))}"
            )
            errors = self.validate_candidate(
                candidate,
                category_id,
                idempotency_key,
                require_eligible=require_eligible,
            )
            if errors:
                raise ValueError("；".join(errors))
            subtitles = _split_values(candidate.get("副标题"))
            source_record_ids = _split_values(candidate.get("主题来源记录ID"))
            scene_tags = list(
                dict.fromkeys(
                    value
                    for value in (
                        _text(candidate.get("主题问题意图")),
                        _text(candidate.get("主题对象/部位")),
                        _text(candidate.get("主题异常现象")),
                    )
                    if value
                )
            )
            if not scene_tags:
                scene_tags = _split_values(candidate.get("检索关键词"))
            confidence = _safe_float(
                candidate.get("主题置信度") or candidate.get("模型初标置信度"),
                0.5,
            )
            decision = _text(candidate.get("审核结论"))
            usable = _text(candidate.get("是否可用"))
            knowledge_value = _text(candidate.get("是否值得沉淀"))
            standard_reference = _text(candidate.get("关联标准项"))
            auto_review_status = _text(candidate.get("自动审核状态"))
            business_line = business_line_from_record(candidate)
            business_type = CZ_BUSINESS_TYPE_BY_ANSWER_HUB_CODE.get(
                business_line.code if business_line else ""
            )
            if not business_type:
                raise ValueError("回收业务层级无法映射到 CZ business_type。")
            applicable_category_path = cz_applicable_category_path(
                business_line.name if business_line else "",
                product_type,
            )
            eligible = (
                (
                    knowledge_value.lower() in WORTHY_VALUES
                    and (
                        decision in PASS_DECISIONS
                        or usable in {"是", "可用", "通过"}
                    )
                )
                or auto_review_status == "auto_approved"
            ) and not standard_reference
            payload.append(
                {
                    "event_id": topic_id,
                    "idempotency_key": idempotency_key,
                    "source": {
                        "system": "answer-hub-third-part",
                        "conversation_id": topic_id,
                        "conversation_url": None,
                        "message_ids": source_record_ids,
                        "redaction_status": "redacted",
                    },
                    "processing": {
                        "summary_version": _text(candidate.get("主题标准快照版本"))
                        or _text(candidate.get("来源版本"))
                        or "unknown",
                        "label_model": _text(candidate.get("语义标注模型"))
                        or _text(candidate.get("主题模型名称"))
                        or "answer-hub",
                        "plugin_name": PROCESSING_PLUGIN_NAME,
                        "plugin_version": PROCESSING_PLUGIN_VERSION,
                        "prompt_version": _text(candidate.get("主题Prompt版本")) or None,
                        "model_name": _text(candidate.get("主题模型名称")) or None,
                    },
                    "selection": {
                        "eligible": eligible,
                        "confidence": confidence,
                        "duplicate_fingerprint": f"sha256:{_stable_hash(candidate.get('主标题'), candidate.get('知识内容'))}",
                        "reasons": [
                            value
                            for value in (
                                f"人工审核：{decision}" if decision else "",
                                (
                                    f"沉淀价值：{knowledge_value}"
                                    if knowledge_value
                                    else ""
                                ),
                                f"组员标注：{usable}" if usable else "",
                                (
                                    f"如何修改：{_text(candidate.get('如何修改'))}"
                                    if _text(candidate.get("如何修改"))
                                    else ""
                                ),
                                (
                                    f"问题反馈：{_text(candidate.get('问题反馈'))}"
                                    if _text(candidate.get("问题反馈"))
                                    else ""
                                ),
                                (
                                    f"自动审核：{auto_review_status}"
                                    if auto_review_status
                                    else ""
                                ),
                                (
                                    f"CZ适用类目路径：{applicable_category_path}"
                                    if applicable_category_path
                                    else ""
                                ),
                                _text(candidate.get("自动审核原因")),
                                (
                                    f"模型沉淀价值：{_text(candidate.get('模型初标是否值得沉淀'))}"
                                    if _text(candidate.get("模型初标是否值得沉淀"))
                                    else ""
                                ),
                                _text(candidate.get("模型初标原因")),
                                (
                                    "已有标准关联（仅审计，未自动映射）："
                                    f"{standard_reference}"
                                    if standard_reference
                                    else ""
                                ),
                                (
                                    "案例图缺少来源事实引用，已从CZ富文本图片块中移除，"
                                    "等待人工补充对应关系。"
                                if _candidate_untraced_case_image_urls(
                                    candidate
                                )
                                else ""
                            ),
                            (
                                "案例视频缺少来源事实引用，已从CZ富文本视频块中移除，"
                                "等待人工补充对应关系。"
                                if _candidate_untraced_case_video_urls(
                                    candidate
                                )
                                else ""
                            ),
                            )
                            if value
                        ],
                    },
                    "model_review": {
                        "status": _text(candidate.get("模型初标状态")) or None,
                        "decision": _text(candidate.get("模型初标结论")) or None,
                        "knowledge_value": (
                            "worthy"
                            if _text(candidate.get("模型初标是否值得沉淀")).lower()
                            in WORTHY_VALUES
                            else (
                                "unworthy"
                                if _text(candidate.get("模型初标是否值得沉淀")).lower()
                                in UNWORTHY_VALUES
                                else None
                            )
                        ),
                        "reason": _text(candidate.get("模型初标原因")) or None,
                        "error_type": _text(candidate.get("模型初标错误类型")) or None,
                        "standard_consistency": (
                            _text(candidate.get("模型初标标准一致性")) or None
                        ),
                        "evidence_sufficiency": (
                            _text(candidate.get("模型初标证据充分性")) or None
                        ),
                        "content_consistency": (
                            _text(candidate.get("模型初标内容一致性")) or None
                        ),
                        "image_necessity": (
                            _text(candidate.get("模型初标图片必要性")) or None
                        ),
                        "title_quality": (
                            _text(candidate.get("模型初标标题质量")) or None
                        ),
                        "confidence": (
                            _safe_float(candidate.get("模型初标置信度"))
                            if _text(candidate.get("模型初标置信度"))
                            else None
                        ),
                        "priority_review": (
                            _text(candidate.get("模型初标重点复核")) == "是"
                        ),
                        "provider": _text(candidate.get("模型初标提供方")) or None,
                        "model_name": (
                            _text(candidate.get("模型初标模型名称")) or None
                        ),
                        "prompt_version": (
                            _text(candidate.get("模型初标Prompt版本")) or None
                        ),
                        "run_id": _text(candidate.get("模型初标运行ID")) or None,
                    },
                    "human_review": {
                        "knowledge_value": (
                            "worthy"
                            if knowledge_value.lower() in WORTHY_VALUES
                            else (
                                "unworthy"
                                if knowledge_value.lower() in UNWORTHY_VALUES
                                else "pending"
                            )
                        ),
                        "usability": (
                            "usable"
                            if usable.lower() in USABLE_VALUES
                            else (
                                "unusable"
                                if usable.lower() in UNUSABLE_VALUES
                                else "pending"
                            )
                        ),
                        "modification_notes": (
                            _text(candidate.get("如何修改")) or None
                        ),
                        "feedback": _text(candidate.get("问题反馈")) or None,
                        "decision": {
                            "通过": "approved",
                            "修改后通过": "approved_with_changes",
                            "驳回": "rejected",
                            "标记Bad Case": "bad_case",
                        }.get(decision),
                        "error_type": _text(candidate.get("错误类型")) or None,
                        "training_eligible": (
                            _text(candidate.get("是否进入训练集")) or None
                        ),
                        "notes": _text(candidate.get("审核备注")) or None,
                        "reviewer": _text(candidate.get("审核人")) or None,
                        "reviewed_at": _text(candidate.get("审核时间")) or None,
                    },
                    "knowledge": {
                        "title": _text(candidate.get("主标题")),
                        "subtitles": subtitles,
                        "content": {
                            "blocks": _candidate_content_blocks(candidate)
                        },
                        "knowledge_origin": CZ_BUSINESS_ACCUMULATION_ORIGIN,
                        "business_type": business_type,
                        "recommended_reply": _text(candidate.get("推荐回复")) or None,
                        "category_id": category_id,
                        "scene_tags": scene_tags,
                        "applicable_categories": _candidate_applicable_categories(
                            candidate,
                            product_type,
                        ),
                        "applicable_brands": _candidate_specific_applicability(
                            candidate.get("适用品牌")
                        ),
                        "applicable_models": _candidate_specific_applicability(
                            candidate.get("适用机型")
                        ),
                        "evidence_excerpt": _candidate_evidence_excerpt(candidate),
                    },
                }
            )
        if len(payload) > 100:
            raise ValueError("单次候选送审最多 100 条。")
        return payload

    def submit_candidates(
        self,
        candidates: list[dict[str, Any]],
        category_mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        mapping = (
            category_mapping
            if category_mapping is not None
            else self.category_mapping()
        )
        totals = {
            "accepted": 0,
            "rejected": 0,
            "reused": 0,
            "intercepted": 0,
            "blocked": 0,
            "results": [],
        }
        for start in range(0, len(candidates), 100):
            batch = self.build_batch_payload(candidates[start : start + 100], mapping)
            response = self._request_json(
                "POST",
                self.candidates_path,
                {"items": batch},
            )
            for key in ("accepted", "rejected", "reused", "intercepted", "blocked"):
                totals[key] += int(response.get(key) or 0)
            totals["results"].extend(response.get("results") or [])
        return totals

    def sync_review_candidates(
        self,
        candidates: list[dict[str, Any]],
        category_mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        mapping = (
            category_mapping
            if category_mapping is not None
            else self.category_mapping()
        )
        totals = {
            "queued": 0,
            "ready": 0,
            "rejected": 0,
            "reused": 0,
            "failed": 0,
            "results": [],
        }
        valid_items: list[dict[str, Any]] = []
        for candidate in candidates:
            if _has_unfinished_transcription_placeholder(candidate):
                totals["rejected"] += 1
                totals["results"].append(
                    {
                        "event_id": (
                            _text(candidate.get("主题ID"))
                            or _text(candidate.get("主标题"))
                        ),
                        "status": "rejected",
                        "error_code": "TRANSCRIPTION_NOT_READY",
                        "error_message": "未完成知识转写，不进入 CZ 候选价值复核。",
                    }
                )
                continue
            try:
                valid_items.extend(
                    self.build_batch_payload(
                        [candidate],
                        mapping,
                        require_eligible=False,
                    )
                )
            except ValueError as exc:
                totals["failed"] += 1
                totals["results"].append(
                    {
                        "event_id": (
                            _text(candidate.get("主题ID"))
                            or _text(candidate.get("主标题"))
                        ),
                        "status": "failed",
                        "error_code": "LOCAL_VALIDATION_ERROR",
                        "error_message": str(exc),
                    }
                )

        def sync_batch(batch: list[dict[str, Any]]) -> None:
            try:
                response = self._request_json(
                    "POST",
                    self.review_candidates_path,
                    {"items": batch},
                )
            except RuntimeError as exc:
                message = str(exc)
                is_item_validation_error = (
                    "HTTP 400" in message or "HTTP 422" in message
                )
                if is_item_validation_error and len(batch) > 1:
                    midpoint = max(1, len(batch) // 2)
                    sync_batch(batch[:midpoint])
                    sync_batch(batch[midpoint:])
                    return
                if is_item_validation_error and len(batch) == 1:
                    totals["failed"] += 1
                    totals["results"].append(
                        {
                            "event_id": _text(batch[0].get("event_id")),
                            "status": "failed",
                            "error_code": "REMOTE_VALIDATION_ERROR",
                            "error_message": message,
                        }
                    )
                    return
                raise
            for key in ("queued", "ready", "rejected", "reused", "failed"):
                totals[key] += int(response.get(key) or 0)
            totals["results"].extend(response.get("results") or [])

        for start in range(0, len(valid_items), 100):
            sync_batch(valid_items[start : start + 100])
        return totals

    def build_second_part_payload(
        self,
        records: list[dict[str, Any]],
        *,
        source_system: str = "second-part",
        start_index: int = 0,
    ) -> list[dict[str, Any]]:
        if len(records) > 100:
            raise ValueError("单次第二部分数据提交最多 100 条。")
        items: list[dict[str, Any]] = []
        for index, record in enumerate(records, start=start_index + 1):
            event_id = (
                _text(record.get("事件ID"))
                or _text(record.get("数据ID"))
                or _text(record.get("工单ID"))
                or f"record-{index:06d}"
            )
            items.append(
                {
                    "event_id": event_id,
                    "idempotency_key": (
                        f"sha256:{_stable_hash(source_system, event_id, record)}"
                    ),
                    "source_system": source_system,
                    "redaction_status": "redacted",
                    "record": record,
                }
            )
        return items

    def submit_second_part_records(
        self,
        records: list[dict[str, Any]],
        *,
        source_system: str = "second-part",
    ) -> dict[str, Any]:
        totals = {
            "accepted": 0,
            "reused": 0,
            "rejected": 0,
            "protected": 0,
            "source_total_rows": 0,
            "topic_rows": 0,
            "topic_imported": 0,
            "topic_refreshed": 0,
            "topic_skipped": 0,
            "knowledge_mode": "case_only",
            "standard_references_enabled": False,
            "results": [],
        }
        for start in range(0, len(records), 100):
            items = self.build_second_part_payload(
                records[start : start + 100],
                source_system=source_system,
                start_index=start,
            )
            response = self._request_json(
                "POST",
                self.second_part_path,
                {"items": items},
            )
            for key in (
                "accepted",
                "reused",
                "rejected",
                "protected",
                "source_total_rows",
                "topic_rows",
                "topic_imported",
                "topic_refreshed",
                "topic_skipped",
            ):
                totals[key] += int(response.get(key) or 0)
            totals["results"].extend(response.get("results") or [])
            totals["knowledge_mode"] = _text(response.get("knowledge_mode")) or "case_only"
            totals["standard_references_enabled"] = bool(
                response.get("standard_references_enabled", False)
            )
        return totals

    def ingestion_status(self, ingestion_id: str) -> dict[str, Any]:
        if not _text(ingestion_id):
            raise ValueError("ingestion_id 不能为空。")
        return self._request_json(
            "GET",
            f"{self.ingestion_path}/{_text(ingestion_id)}",
        )
