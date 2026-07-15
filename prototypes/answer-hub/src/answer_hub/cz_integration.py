from __future__ import annotations

"""Configuration and safe read-only helpers for the cz integration API."""

from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import os

from .mimo import load_dotenv


@dataclass(frozen=True)
class CzIntegrationConfig:
    base_url: str
    integration_key: str

    @classmethod
    def from_env(cls) -> "CzIntegrationConfig | None":
        load_dotenv()
        base_url = os.getenv("KB_BASE_URL", "").strip()
        integration_key = os.getenv("KB_INTEGRATION_KEY", "").strip()
        if not (base_url and integration_key):
            return None
        return cls(base_url=base_url.rstrip("/"), integration_key=integration_key)

    @property
    def status(self) -> str:
        return "待联调（本期不发送 API 请求）"


class CzIntegrationAdapter:
    taxonomy_path = "/api/v1/integration/taxonomy"
    qc_standards_path = "/api/v1/integration/qc-standards"
    qc_standards_search_path = "/api/v1/integration/qc-standards:search"
    dedup_path = "/api/v1/integration/knowledge-dedup:check"
    candidates_path = "/api/v1/integration/knowledge-candidates:batch"

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
        }

    def fetch_qc_standard_snapshot(
        self,
        category_id: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch one versioned page of published quality standards.

        The caller should persist ``snapshot_version`` with every topic so the
        transcription and initial review remain auditable against one standard
        snapshot. This method is read-only and never submits candidates.
        """
        if not self.config:
            raise RuntimeError("未配置 KB_BASE_URL 或 KB_INTEGRATION_KEY，无法拉取质检标准。")
        query = urlencode(
            {
                "category_id": category_id,
                "limit": max(1, min(limit, 500)),
                "offset": max(0, offset),
            }
        )
        request = Request(
            f"{self.config.base_url}{self.qc_standards_path}?{query}",
            headers={"X-Integration-Key": self.config.integration_key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"拉取质检标准失败：HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"拉取质检标准失败：{exc.reason}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RuntimeError("质检标准接口返回格式不正确。")
        return payload

    def validate_candidate(
        self,
        candidate: dict[str, Any],
        category_id: str,
        idempotency_key: str,
    ) -> list[str]:
        errors: list[str] = []
        if not category_id:
            errors.append("未映射 category_id，禁止送审。")
        if not str(candidate.get("主标题") or "").strip():
            errors.append("缺少主标题。")
        if not str(candidate.get("知识内容") or "").strip():
            errors.append("缺少知识内容。")
        if not idempotency_key:
            errors.append("缺少幂等键。")
        return errors

    def build_batch_payload(
        self,
        candidates: list[dict[str, Any]],
        category_mapping: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Build local-only payloads. No request method exists in the phone pilot."""
        payload: list[dict[str, Any]] = []
        for candidate in candidates:
            category = str(candidate.get("知识分类") or "").strip()
            category_id = category_mapping.get(category, "")
            topic_id = str(candidate.get("主题ID") or candidate.get("主标题") or "").strip()
            errors = self.validate_candidate(candidate, category_id, topic_id)
            if errors:
                raise ValueError("；".join(errors))
            payload.append(
                {
                    "category_id": category_id,
                    "layer": "L2",
                    "title": candidate["主标题"],
                    "content": candidate["知识内容"],
                    "idempotency_key": topic_id,
                    "source": "answer-hub-third-part",
                    "processing_metadata": {"topic_id": topic_id},
                    "selection_metadata": {"standard_refs": candidate.get("关联标准项", "")},
                }
            )
        if len(payload) > 100:
            raise ValueError("单次候选送审最多 100 条。")
        return payload
