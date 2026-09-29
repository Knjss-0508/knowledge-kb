"""Local confidence-training loop for the group-computer deployment."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable
from urllib.request import Request, urlopen

from .local_model_config import LocalModelConfig


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LocalConfidenceTrainingStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.getenv("ANSWER_HUB_CONFIDENCE_DB", "data/confidence-training.db")
        self.path = Path(configured)
        if not self.path.is_absolute():
            self.path = Path.cwd() / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS confidence_candidates (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    human_label TEXT NOT NULL DEFAULT '', model_result TEXT NOT NULL DEFAULT '{}',
                    shadow_result TEXT NOT NULL DEFAULT '{}', regression_decision TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS confidence_jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                    candidate_ids TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def upsert(self, candidates: list[dict[str, Any]]) -> int:
        now = _now()
        with self._connect() as db:
            for candidate in candidates:
                payload = dict(candidate)
                identifier = str(payload.get("id") or payload.get("event_id") or hashlib.sha256(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:24])
                db.execute(
                    """INSERT INTO confidence_candidates(id,payload,created_at,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                    (identifier, json.dumps(payload, ensure_ascii=False), now, now),
                )
        return len(candidates)

    def items(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM confidence_candidates ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload"])
            item.update({"id": row["id"], "human_label": row["human_label"], "model_result": json.loads(row["model_result"]), "shadow_result": json.loads(row["shadow_result"]), "regression_decision": row["regression_decision"]})
            result.append(item)
        return result

    def label(self, identifier: str, label: str) -> dict[str, Any]:
        if label not in {"worthy", "unworthy", "pending"}:
            raise ValueError("human_label 必须是 worthy、unworthy 或 pending")
        with self._connect() as db:
            db.execute("UPDATE confidence_candidates SET human_label=?,updated_at=? WHERE id=?", (label, _now(), identifier))
        return next((item for item in self.items() if item["id"] == identifier), {})

    def save_model(self, identifier: str, result: dict[str, Any], shadow: bool = False) -> None:
        column = "shadow_result" if shadow else "model_result"
        with self._connect() as db:
            db.execute(f"UPDATE confidence_candidates SET {column}=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), _now(), identifier))

    def create_job(self, ids: list[str]) -> str:
        job_id = "confidence-" + hashlib.sha256(("\x00".join(ids) + _now()).encode()).hexdigest()[:20]
        with self._connect() as db:
            db.execute("INSERT INTO confidence_jobs VALUES(?,?,?,?,?,?,?)", (job_id, "queued", "queued", json.dumps(ids), "{}", _now(), _now()))
        return job_id

    def update_job(self, job_id: str, status: str, stage: str, result: dict[str, Any] | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE confidence_jobs SET status=?,stage=?,result=?,updated_at=? WHERE id=?", (status, stage, json.dumps(result or {}, ensure_ascii=False), _now(), job_id))

    def jobs(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM confidence_jobs ORDER BY created_at DESC").fetchall()
        return [{"id": row["id"], "status": row["status"], "stage": row["stage"], "result": json.loads(row["result"]), "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]

    def job_ids(self, job_id: str) -> list[str]:
        with self._connect() as db:
            row = db.execute("SELECT candidate_ids FROM confidence_jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row["candidate_ids"]) if row else []

    def regression_items(self, job_id: str) -> list[dict[str, Any]]:
        ids = set(self.job_ids(job_id))
        result = []
        for item in self.items():
            if item["id"] not in ids or not item.get("shadow_result"):
                continue
            old = item.get("model_result") or {}
            new = item.get("shadow_result") or {}
            if old.get("knowledge_value") != new.get("knowledge_value") or float(new.get("confidence") or 0) < float(old.get("confidence") or 0) - 0.1:
                result.append(item)
        return result

    def review_regression(self, identifier: str, decision: str) -> dict[str, Any]:
        if decision not in {"accept", "reject", "needs_review"}:
            raise ValueError("decision 必须是 accept、reject 或 needs_review")
        with self._connect() as db:
            db.execute("UPDATE confidence_candidates SET regression_decision=?,updated_at=? WHERE id=?", (decision, _now(), identifier))
        return next((item for item in self.items() if item["id"] == identifier), {})


def annotate_with_deepseek(candidate: dict[str, Any], *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    config = LocalModelConfig.from_file()
    if config is None:
        raise RuntimeError("组内 DeepSeek 未配置；不会回退到个人 MiMo。")
    prompt = "请只返回 JSON：knowledge_value(worthy|unworthy|pending)、confidence(0到1)、reason、error_type。\n候选：" + json.dumps(candidate, ensure_ascii=False)
    body = json.dumps({"model": config.model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "temperature": 0}).encode("utf-8")
    req = Request(config.base_url.rstrip("/") + "/chat/completions", data=body, headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, method="POST")
    with opener(req, timeout=config.timeout_seconds or 120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content
    if not isinstance(result, dict):
        raise RuntimeError("DeepSeek 返回不是 JSON 对象")
    result["provider"] = config.provider
    result["model"] = config.model
    return result


def revise_prompt_with_deepseek(regressions: list[dict[str, Any]], *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    config = LocalModelConfig.from_file()
    if config is None:
        raise RuntimeError("组内 DeepSeek 未配置；不会回退到个人 MiMo。")
    prompt = "请根据以下退化样本返回 JSON，字段为 candidate_prompt、reason、risk_notes。只能给候选 Prompt，不得声称已修改生产 Prompt。\n" + json.dumps(regressions, ensure_ascii=False)
    body = json.dumps({"model": config.model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "temperature": 0}).encode("utf-8")
    req = Request(config.base_url.rstrip("/") + "/chat/completions", data=body, headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, method="POST")
    with opener(req, timeout=config.timeout_seconds or 120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content
    if not isinstance(result, dict):
        raise RuntimeError("DeepSeek 返回不是 JSON 对象")
    result.update({"provider": config.provider, "model": config.model, "production_changed": False})
    return result
