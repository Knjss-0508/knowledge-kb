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
                """
            )

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
