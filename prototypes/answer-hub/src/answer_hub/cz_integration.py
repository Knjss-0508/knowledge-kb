from __future__ import annotations

"""Authenticated CZ knowledge-base integration with bounded retries."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import hashlib
import json
import os
import time

from .auto_review import (
    AutoReviewPolicy,
    UNUSABLE_VALUES,
    UNWORTHY_VALUES,
    USABLE_VALUES,
    WORTHY_VALUES,
    select_candidates_for_submission,
)
from .mimo import load_dotenv
from .product_taxonomy import infer_product_category, resolve_product_category


TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
PASS_DECISIONS = {"通过", "修改后通过"}
PROCESSING_PLUGIN_NAME = "answer-hub-topic-transcription"
PROCESSING_PLUGIN_VERSION = "2026-07-22"


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _product_type(candidate: dict[str, Any]) -> str:
    direct = candidate.get("产品类型编码") or candidate.get("产品类型")
    category = resolve_product_category(direct)
    if category:
        return category.name
    inferred = infer_product_category(
        (
            candidate.get("适用范围"),
            candidate.get("主题聚类键"),
            candidate.get("知识分类"),
        )
    )
    return inferred.name if inferred else ""


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

    @classmethod
    def from_env(cls) -> "CzIntegrationConfig | None":
        load_dotenv()
        base_url = os.getenv("KB_BASE_URL", "").strip()
        integration_key = os.getenv("KB_INTEGRATION_KEY", "").strip()
        if not (base_url and integration_key):
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
    ) -> dict[str, Any]:
        if not self.config:
            raise RuntimeError("未配置 KB_BASE_URL 或 KB_INTEGRATION_KEY。")
        url = self.config.endpoint(path)
        if query:
            url = f"{url}?{urlencode(query)}"
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "X-Integration-Key": self.config.integration_key,
            "Accept": "application/json",
        }
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
            category_id = (
                category_mapping.get(product_type)
                or category_mapping.get(knowledge_category)
                or ""
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
            scope = _text(candidate.get("适用范围"))
            scope_parts = [part.strip() for part in scope.split("-") if part.strip()]
            confidence = _safe_float(
                candidate.get("主题置信度") or candidate.get("模型初标置信度"),
                0.5,
            )
            decision = _text(candidate.get("审核结论"))
            usable = _text(candidate.get("是否可用"))
            knowledge_value = _text(candidate.get("是否值得沉淀"))
            standard_reference = _text(candidate.get("关联标准项"))
            auto_review_status = _text(candidate.get("自动审核状态"))
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
                            "blocks": [
                                {
                                    "type": "text",
                                    "value": _text(candidate.get("知识内容")),
                                }
                            ]
                        },
                        "recommended_reply": _text(candidate.get("推荐回复")) or None,
                        "category_id": category_id,
                        "scene_tags": scene_tags,
                        "applicable_categories": [product_type] if product_type else [],
                        "applicable_brands": (
                            [scope_parts[1]]
                            if len(scope_parts) > 1 and scope_parts[1] != "通用"
                            else []
                        ),
                        "applicable_models": [],
                        "evidence_excerpt": _text(candidate.get("主题证据摘要"))[:4000]
                        or None,
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
