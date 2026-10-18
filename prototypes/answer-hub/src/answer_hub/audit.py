from __future__ import annotations

"""Local SQLite audit trail for the phone MVP.

The database deliberately stores metadata and sanitized model inputs, not API
keys or base64 image bodies. The source image URL plus download result remains
available for traceability without making the database unnecessarily large.
"""

from datetime import datetime
from pathlib import Path
from typing import Any
from contextlib import contextmanager
import json
import os
import sqlite3

from .mimo import load_dotenv


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def media_review_decision(
    decision: str,
    *,
    parse_status: str,
    media_type: str,
    existing_risk: str = "",
) -> dict[str, str]:
    mapping = {
        "关联当前主题": ("relevant", "human_confirmed"),
        "不相关": ("irrelevant", "human_confirmed"),
        "无法判断": ("unconfirmed", "human_pending"),
    }
    if decision not in mapping:
        raise ValueError("媒体复核结论必须是关联当前主题、不相关或无法判断")
    relevance, review_status = mapping[decision]
    if relevance == "relevant":
        audit_risk = (
            ""
            if str(parse_status or "").strip() == "已解析"
            else str(existing_risk or "").strip()
            or ("未解析视频" if media_type == "video" else "无法读取")
        )
    elif relevance == "irrelevant":
        audit_risk = "不相关"
    else:
        audit_risk = str(existing_risk or "").strip() or "无法判断"
    return {
        "relevance_status": relevance,
        "review_status": review_status,
        "audit_risk": audit_risk,
    }


class AuditStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    @classmethod
    def from_env(cls, path: str | Path | None = None) -> "AuditStore":
        load_dotenv()
        return cls(path or os.getenv("ANSWER_HUB_DB_PATH", "data/phone_mvp.db"))

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingestion_records (
                    run_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    preprocessed_json TEXT NOT NULL,
                    image_results_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS model_runs (
                    model_run_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retrieved_standards_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    error TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    model_run_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    final_candidate_json TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS feedback_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_run_id TEXT,
                    record_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    feedback_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS excluded_records (
                    run_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS topic_registry (
                    topic_id TEXT PRIMARY KEY,
                    business_line TEXT NOT NULL,
                    product_category TEXT NOT NULL,
                    topic_key_json TEXT NOT NULL,
                    signature_json TEXT NOT NULL,
                    representative_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    evidence_version INTEGER NOT NULL DEFAULT 0,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_topic_registry_scope
                ON topic_registry (business_line, product_category, status);
                CREATE TABLE IF NOT EXISTS topic_members (
                    membership_key TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    original_work_order_id TEXT NOT NULL,
                    atomic_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_topic_members_topic_id
                ON topic_members (topic_id);
                CREATE TABLE IF NOT EXISTS topic_merge_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    proposed_topic_id TEXT NOT NULL,
                    resolved_topic_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    added_member_count INTEGER NOT NULL,
                    duplicate_member_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topic_pending_overlays (
                    overlay_id TEXT PRIMARY KEY,
                    base_topic_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    proposed_topic_id TEXT NOT NULL,
                    business_line TEXT NOT NULL,
                    product_category TEXT NOT NULL,
                    topic_key_json TEXT NOT NULL,
                    signature_json TEXT NOT NULL,
                    members_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    base_evidence_version INTEGER NOT NULL DEFAULT 0,
                    added_member_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_member_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS ix_topic_pending_overlays_topic
                ON topic_pending_overlays (base_topic_id, status, created_at);
                CREATE TABLE IF NOT EXISTS media_evidence (
                    media_evidence_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    media_url TEXT NOT NULL,
                    source_record_id TEXT NOT NULL DEFAULT '',
                    atomic_id TEXT NOT NULL DEFAULT '',
                    topic_id TEXT NOT NULL DEFAULT '',
                    fact_id TEXT NOT NULL DEFAULT '',
                    work_order_id TEXT NOT NULL DEFAULT '',
                    relevance_status TEXT NOT NULL DEFAULT 'unconfirmed',
                    visible_fact TEXT NOT NULL DEFAULT '',
                    selection_reason TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    parse_status TEXT NOT NULL DEFAULT 'unparsed',
                    audit_risk TEXT NOT NULL DEFAULT '',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (run_id, media_url, topic_id)
                );
                CREATE INDEX IF NOT EXISTS ix_media_evidence_topic
                ON media_evidence (topic_id, audit_risk, review_status);
                CREATE INDEX IF NOT EXISTS ix_media_evidence_source
                ON media_evidence (source_record_id, atomic_id);
                """
            )

    def save_media_evidence(
        self,
        run_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        if not items:
            return
        with self._connect() as connection:
            for item in items:
                connection.execute(
                    """INSERT INTO media_evidence
                    (media_evidence_id, run_id, media_type, media_url,
                     source_record_id, atomic_id, topic_id, fact_id,
                     work_order_id, relevance_status, visible_fact,
                     selection_reason, review_status, parse_status, audit_risk,
                     reviewed_by, reviewed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO UPDATE SET
                        relevance_status=CASE
                            WHEN media_evidence.review_status LIKE 'human_%'
                            THEN media_evidence.relevance_status
                            ELSE excluded.relevance_status
                        END,
                        visible_fact=excluded.visible_fact,
                        selection_reason=excluded.selection_reason,
                        review_status=CASE
                            WHEN media_evidence.review_status LIKE 'human_%'
                            THEN media_evidence.review_status
                            ELSE excluded.review_status
                        END,
                        parse_status=excluded.parse_status,
                        audit_risk=CASE
                            WHEN media_evidence.review_status LIKE 'human_%'
                            THEN media_evidence.audit_risk
                            ELSE excluded.audit_risk
                        END""",
                    (
                        item.get("媒体证据ID", ""),
                        run_id,
                        item.get("媒体类型", ""),
                        item.get("媒体链接", ""),
                        item.get("来源记录ID", ""),
                        item.get("原子问题ID", ""),
                        item.get("主题ID", ""),
                        item.get("事实ID", ""),
                        item.get("工单ID", ""),
                        item.get("相关性状态", "unconfirmed"),
                        item.get("可见事实", ""),
                        item.get("选择原因", ""),
                        item.get("审核状态", "pending"),
                        item.get("解析状态", "未解析"),
                        item.get("审计风险", ""),
                        item.get("审核人", ""),
                        item.get("审核时间", ""),
                    ),
                )

    def list_media_evidence(
        self,
        *,
        topic_id: str | None = None,
        risk_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if topic_id:
            clauses.append("topic_id = ?")
            params.append(topic_id)
        if risk_only:
            clauses.append("audit_risk <> ''")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM media_evidence {where}
                ORDER BY topic_id, source_record_id, media_evidence_id""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def review_media_evidence(
        self,
        media_evidence_id: str,
        decision: str,
        reviewed_by: str = "",
    ) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            original = connection.execute(
                "SELECT * FROM media_evidence WHERE media_evidence_id = ?",
                (media_evidence_id,),
            ).fetchone()
            if original is None:
                raise ValueError("找不到媒体证据")
            outcome = media_review_decision(
                decision,
                parse_status=str(original["parse_status"] or ""),
                media_type=str(original["media_type"] or ""),
                existing_risk=str(original["audit_risk"] or ""),
            )
            cursor = connection.execute(
                """UPDATE media_evidence
                SET relevance_status = ?, review_status = ?, reviewed_by = ?,
                    reviewed_at = ?, audit_risk = ?
                WHERE media_evidence_id = ?""",
                (
                    outcome["relevance_status"],
                    outcome["review_status"],
                    reviewed_by,
                    now,
                    outcome["audit_risk"],
                    media_evidence_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("找不到媒体证据")
            row = connection.execute(
                "SELECT * FROM media_evidence WHERE media_evidence_id = ?",
                (media_evidence_id,),
            ).fetchone()
            return dict(row)

    def record_excluded(
        self,
        run_id: str,
        record_id: str,
        source_row: dict[str, Any],
        reason: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO excluded_records
                (run_id, record_id, created_at, reason, source_json)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    record_id,
                    datetime.now().isoformat(timespec="seconds"),
                    reason,
                    _json(source_row),
                ),
            )

    def record_ingestion(
        self,
        run_id: str,
        record_id: str,
        source_row: dict[str, Any],
        preprocessed_row: dict[str, Any],
        image_results: list[dict[str, Any]],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO ingestion_records
                (run_id, record_id, created_at, source_json, preprocessed_json, image_results_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, record_id, datetime.now().isoformat(timespec="seconds"), _json(source_row), _json(preprocessed_row), _json(image_results)),
            )

    def record_model_run(
        self,
        model_run_id: str,
        run_id: str,
        record_id: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        status: str,
        retrieved_standards: list[dict[str, Any]],
        request_audit: dict[str, Any],
        response_audit: dict[str, Any],
        error: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO model_runs
                (model_run_id, run_id, record_id, created_at, provider, model_name, prompt_version, status,
                retrieved_standards_json, request_json, response_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model_run_id, run_id, record_id, datetime.now().isoformat(timespec="seconds"), provider,
                    model_name, prompt_version, status, _json(retrieved_standards), _json(request_audit),
                    _json(response_audit), error,
                ),
            )

    def save_candidate(
        self,
        model_run_id: str,
        run_id: str,
        record_id: str,
        candidate: dict[str, Any],
        review_status: str = "review_pending",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO candidates
                (model_run_id, run_id, record_id, updated_at, review_status, candidate_json, final_candidate_json)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT final_candidate_json FROM candidates WHERE model_run_id = ?), ''))""",
                (
                    model_run_id, run_id, record_id, datetime.now().isoformat(timespec="seconds"), review_status,
                    _json(candidate), model_run_id,
                ),
            )

    def save_review_outcome(
        self,
        model_run_id: str,
        record_id: str,
        decision: str,
        final_candidate: dict[str, Any],
        feedback: dict[str, Any],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        review_status = "published" if decision in {"通过", "修改后通过"} else "review_rejected"
        with self._connect() as connection:
            connection.execute(
                """UPDATE candidates SET updated_at = ?, review_status = ?, final_candidate_json = ?
                WHERE model_run_id = ?""",
                (now, review_status, _json(final_candidate), model_run_id),
            )
            connection.execute(
                """INSERT INTO feedback_events
                (model_run_id, record_id, created_at, decision, feedback_json) VALUES (?, ?, ?, ?, ?)""",
                (model_run_id, record_id, now, decision, _json(feedback)),
            )

    def list_registered_topics(
        self,
        business_line: str,
        product_category: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            topic_rows = connection.execute(
                """SELECT topic_id, business_line, product_category, topic_key_json,
                signature_json, representative_json, status, evidence_version,
                member_count, created_at, updated_at
                FROM topic_registry
                WHERE business_line = ? AND product_category = ? AND status = 'active'
                ORDER BY updated_at DESC, topic_id""",
                (business_line, product_category),
            ).fetchall()
            topics: list[dict[str, Any]] = []
            for topic_row in topic_rows:
                topics.append(
                    {
                        "topic_id": topic_row["topic_id"],
                        "business_line": topic_row["business_line"],
                        "product_category": topic_row["product_category"],
                        "topic_key": json.loads(topic_row["topic_key_json"]),
                        "signature": json.loads(topic_row["signature_json"]),
                        "representative": json.loads(
                            topic_row["representative_json"]
                        ),
                        "status": topic_row["status"],
                        "evidence_version": int(topic_row["evidence_version"]),
                        "member_count": int(topic_row["member_count"]),
                        "created_at": topic_row["created_at"],
                        "updated_at": topic_row["updated_at"],
                    }
                )
            return topics

    def list_unregistered_topic_candidates(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            registered_ids = {
                row["topic_id"]
                for row in connection.execute(
                    "SELECT topic_id FROM topic_registry"
                ).fetchall()
            }
            candidate_rows = connection.execute(
                """SELECT run_id, record_id, updated_at, review_status,
                candidate_json, final_candidate_json
                FROM candidates
                WHERE review_status != 'review_rejected'
                AND record_id NOT IN (
                    SELECT topic_id FROM topic_registry
                )
                ORDER BY updated_at DESC, model_run_id DESC"""
            ).fetchall()
            latest_by_topic: dict[str, dict[str, Any]] = {}
            for row in candidate_rows:
                raw = row["final_candidate_json"] or row["candidate_json"]
                try:
                    candidate = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(candidate, dict):
                    continue
                topic_id = str(
                    candidate.get("主题ID") or row["record_id"] or ""
                ).strip()
                if (
                    not topic_id
                    or topic_id in registered_ids
                    or topic_id in latest_by_topic
                ):
                    continue
                latest_by_topic[topic_id] = {
                    "topic_id": topic_id,
                    "run_id": row["run_id"],
                    "updated_at": row["updated_at"],
                    "review_status": row["review_status"],
                    "candidate": candidate,
                }
            return list(latest_by_topic.values())

    def list_registered_topic_member_identities(
        self,
        topic_id: str,
    ) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT source_record_id, original_work_order_id, atomic_id
                FROM topic_members
                WHERE topic_id = ?
                ORDER BY created_at, membership_key""",
                (topic_id,),
            ).fetchall()
            return [
                {
                    "source_record_id": row["source_record_id"],
                    "original_work_order_id": row[
                        "original_work_order_id"
                    ],
                    "atomic_id": row["atomic_id"],
                }
                for row in rows
            ]

    def list_registered_topic_members(
        self,
        topic_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT membership_key, source_record_id,
                original_work_order_id, atomic_id, evidence_json
                FROM topic_members
                WHERE topic_id = ?
                ORDER BY created_at, membership_key""",
                (topic_id,),
            ).fetchall()
            return [
                {
                    "membership_key": row["membership_key"],
                    "source_record_id": row["source_record_id"],
                    "original_work_order_id": row["original_work_order_id"],
                    "atomic_id": row["atomic_id"],
                    "evidence": json.loads(row["evidence_json"]),
                }
                for row in rows
            ]

    def stage_topic_overlay(
        self,
        *,
        overlay_id: str,
        base_topic_id: str,
        run_id: str,
        proposed_topic_id: str,
        business_line: str,
        product_category: str,
        topic_key: tuple[str, ...],
        signature: dict[str, Any],
        members: list[dict[str, Any]],
        base_evidence_version: int,
        added_member_count: int,
        duplicate_member_count: int,
    ) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO topic_pending_overlays
                (overlay_id, base_topic_id, run_id, proposed_topic_id,
                business_line, product_category, topic_key_json, signature_json,
                members_json, status, base_evidence_version,
                added_member_count, duplicate_member_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (
                    overlay_id,
                    base_topic_id,
                    run_id,
                    proposed_topic_id,
                    business_line,
                    product_category,
                    _json(topic_key),
                    _json(signature),
                    _json(members),
                    int(base_evidence_version),
                    int(added_member_count),
                    int(duplicate_member_count),
                    now,
                ),
            )
        return {
            "overlay_id": overlay_id,
            "base_topic_id": base_topic_id,
            "status": "pending",
            "base_evidence_version": int(base_evidence_version),
            "added_member_count": int(added_member_count),
            "duplicate_member_count": int(duplicate_member_count),
        }

    def list_pending_topic_overlays(
        self,
        base_topic_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if base_topic_id:
                rows = connection.execute(
                    """SELECT * FROM topic_pending_overlays
                    WHERE base_topic_id = ? AND status = 'pending'
                    ORDER BY created_at, overlay_id""",
                    (base_topic_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM topic_pending_overlays
                    WHERE status = 'pending'
                    ORDER BY created_at, overlay_id"""
                ).fetchall()
            return [
                {
                    "overlay_id": row["overlay_id"],
                    "base_topic_id": row["base_topic_id"],
                    "run_id": row["run_id"],
                    "proposed_topic_id": row["proposed_topic_id"],
                    "business_line": row["business_line"],
                    "product_category": row["product_category"],
                    "topic_key": json.loads(row["topic_key_json"]),
                    "signature": json.loads(row["signature_json"]),
                    "members": json.loads(row["members_json"]),
                    "status": row["status"],
                    "base_evidence_version": int(row["base_evidence_version"]),
                    "added_member_count": int(row["added_member_count"]),
                    "duplicate_member_count": int(row["duplicate_member_count"]),
                    "created_at": row["created_at"],
                    "reviewed_at": row["reviewed_at"],
                    "review_note": row["review_note"],
                }
                for row in rows
            ]

    def review_topic_overlay(
        self,
        overlay_id: str,
        decision: str,
        review_note: str = "",
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("增量主题叠加层审核结论必须是 approved 或 rejected")
        overlays = self.list_pending_topic_overlays()
        overlay = next(
            (item for item in overlays if item["overlay_id"] == overlay_id),
            None,
        )
        if overlay is None:
            raise ValueError("找不到待审核的增量主题叠加层")
        if decision == "approved":
            from .topic_registry import _merge_signatures, _membership

            members = [
                item if "membership_key" in item else _membership(item)
                for item in overlay["members"]
            ]
            with self._connect() as connection:
                topic_row = connection.execute(
                    "SELECT signature_json FROM topic_registry WHERE topic_id = ?",
                    (overlay["base_topic_id"],),
                ).fetchone()
            current_signature = (
                json.loads(topic_row["signature_json"])
                if topic_row
                else {}
            )
            self.integrate_registered_topic(
                topic_id=overlay["base_topic_id"],
                proposed_topic_id=overlay["proposed_topic_id"],
                business_line=overlay["business_line"],
                product_category=overlay["product_category"],
                topic_key=tuple(overlay["topic_key"]),
                signature=_merge_signatures(
                    current_signature,
                    overlay["signature"],
                ),
                representative=(members[0].get("evidence") or {}),
                members=members,
                run_id=overlay["run_id"],
                decision="incremental_supplement_approved",
                confidence=1.0,
                reason=review_note or "人工审核通过增量主题补充",
            )
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """UPDATE topic_pending_overlays
                SET status = ?, reviewed_at = ?, review_note = ?
                WHERE overlay_id = ? AND status = 'pending'""",
                (decision, now, review_note, overlay_id),
            )
        return {**overlay, "status": decision, "reviewed_at": now, "review_note": review_note}

    def integrate_registered_topic(
        self,
        *,
        topic_id: str,
        proposed_topic_id: str,
        business_line: str,
        product_category: str,
        topic_key: tuple[str, ...],
        signature: dict[str, Any],
        representative: dict[str, Any],
        members: list[dict[str, Any]],
        run_id: str,
        decision: str,
        confidence: float,
        reason: str,
    ) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            foreign_topic_ids: set[str] = set()
            for member in members:
                membership_key = str(
                    member.get("membership_key") or ""
                ).strip()
                if not membership_key:
                    continue
                owner = connection.execute(
                    """SELECT topic_id FROM topic_members
                    WHERE membership_key = ?""",
                    (membership_key,),
                ).fetchone()
                if owner and owner["topic_id"] != topic_id:
                    foreign_topic_ids.add(str(owner["topic_id"]))
            if foreign_topic_ids:
                raise ValueError(
                    "原子证据已属于其他历史主题，必须人工确认主题归属："
                    + "、".join(sorted(foreign_topic_ids))
                )

            existing = connection.execute(
                """SELECT business_line, product_category, evidence_version
                FROM topic_registry WHERE topic_id = ?""",
                (topic_id,),
            ).fetchone()
            if existing and (
                existing["business_line"] != business_line
                or existing["product_category"] != product_category
            ):
                raise ValueError(
                    "稳定主题ID已属于其他回收业务层级或产品品类"
                )
            if not existing:
                connection.execute(
                    """INSERT INTO topic_registry
                    (topic_id, business_line, product_category, topic_key_json,
                    signature_json, representative_json, status, evidence_version,
                    member_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', 0, 0, ?, ?)""",
                    (
                        topic_id,
                        business_line,
                        product_category,
                        _json(topic_key),
                        _json(signature),
                        _json(representative),
                        now,
                        now,
                    ),
                )

            added_member_count = 0
            for member in members:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO topic_members
                    (membership_key, topic_id, run_id, source_record_id,
                    original_work_order_id, atomic_id, evidence_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        member["membership_key"],
                        topic_id,
                        run_id,
                        member["source_record_id"],
                        member["original_work_order_id"],
                        member["atomic_id"],
                        _json(member["evidence"]),
                        now,
                    ),
                )
                added_member_count += max(0, int(cursor.rowcount or 0))

            current = connection.execute(
                """SELECT evidence_version FROM topic_registry
                WHERE topic_id = ?""",
                (topic_id,),
            ).fetchone()
            evidence_version = int(current["evidence_version"])
            if added_member_count:
                evidence_version += 1
            member_count = int(
                connection.execute(
                    """SELECT COUNT(*) FROM topic_members WHERE topic_id = ?""",
                    (topic_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """UPDATE topic_registry
                SET signature_json = ?, member_count = ?, evidence_version = ?,
                updated_at = ?
                WHERE topic_id = ?""",
                (
                    _json(signature),
                    member_count,
                    evidence_version,
                    now,
                    topic_id,
                ),
            )
            duplicate_member_count = max(
                0,
                len(members) - added_member_count,
            )
            connection.execute(
                """INSERT INTO topic_merge_events
                (run_id, proposed_topic_id, resolved_topic_id, created_at,
                decision, confidence, reason, added_member_count,
                duplicate_member_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    proposed_topic_id,
                    topic_id,
                    now,
                    decision,
                    float(confidence),
                    reason,
                    added_member_count,
                    duplicate_member_count,
                ),
            )
            stored_rows = connection.execute(
                """SELECT evidence_json FROM topic_members
                WHERE topic_id = ? ORDER BY created_at, membership_key""",
                (topic_id,),
            ).fetchall()
            return {
                "topic_id": topic_id,
                "evidence_version": evidence_version,
                "member_count": member_count,
                "added_member_count": added_member_count,
                "duplicate_member_count": duplicate_member_count,
                "rows": [
                    json.loads(row["evidence_json"])
                    for row in stored_rows
                ],
            }

    def record_topic_merge_event(
        self,
        *,
        run_id: str,
        proposed_topic_id: str,
        resolved_topic_id: str,
        decision: str,
        confidence: float,
        reason: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO topic_merge_events
                (run_id, proposed_topic_id, resolved_topic_id, created_at,
                decision, confidence, reason, added_member_count,
                duplicate_member_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                (
                    run_id,
                    proposed_topic_id,
                    resolved_topic_id,
                    datetime.now().isoformat(timespec="seconds"),
                    decision,
                    float(confidence),
                    reason,
                ),
            )
