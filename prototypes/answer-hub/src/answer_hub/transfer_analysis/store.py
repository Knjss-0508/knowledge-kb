from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import json
import sqlite3


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _decode(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


class TransferAnalysisStore:
    def __init__(self, path: str | Path = "data/transfer_analysis.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS transfer_collection_runs (
                    run_id TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfer_records (
                    transfer_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    engineer TEXT NOT NULL,
                    transfer_reason TEXT NOT NULL,
                    category TEXT NOT NULL,
                    model TEXT NOT NULL,
                    order_status TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transfer_records_event_time
                    ON transfer_records(event_time);
                CREATE INDEX IF NOT EXISTS idx_transfer_records_work_order
                    ON transfer_records(work_order_id);
                CREATE TABLE IF NOT EXISTS transfer_conversations (
                    system TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL,
                    engineer TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    first_question TEXT NOT NULL,
                    last_answer TEXT NOT NULL,
                    intent_result TEXT NOT NULL,
                    conversation_text TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    retrievals_json TEXT NOT NULL,
                    tools_json TEXT NOT NULL,
                    attachments_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (system, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_transfer_conversations_work_order
                    ON transfer_conversations(system, work_order_id);
                CREATE TABLE IF NOT EXISTS transfer_links (
                    transfer_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfer_weekly_samples (
                    week_start TEXT NOT NULL,
                    transfer_id TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    selected_at TEXT NOT NULL,
                    PRIMARY KEY (week_start, transfer_id)
                );
                CREATE TABLE IF NOT EXISTS transfer_annotations (
                    week_start TEXT NOT NULL,
                    transfer_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    confidence_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    needs_review INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (week_start, transfer_id)
                );
                CREATE TABLE IF NOT EXISTS transfer_reviews (
                    week_start TEXT NOT NULL,
                    transfer_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    review_json TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    PRIMARY KEY (week_start, transfer_id)
                );
                """
            )

    def save_collection_run(
        self,
        run_id: str,
        system: str,
        start_date: str,
        end_date: str,
        status: str,
        metrics: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transfer_collection_runs
                (run_id, system, start_date, end_date, status, metrics_json, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    metrics_json=excluded.metrics_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    system,
                    start_date,
                    end_date,
                    status,
                    _json(metrics or {}),
                    error,
                    now,
                    now,
                ),
            )

    def list_collection_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transfer_collection_runs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                **dict(row),
                "metrics": _decode(row["metrics_json"], {}),
            }
            for row in rows
        ]

    def upsert_transfer(self, record: dict[str, Any]) -> None:
        now = _now()
        transfer_id = str(record.get("transfer_id") or record.get("conversation_id") or "").strip()
        if not transfer_id:
            raise ValueError("转人工记录缺少 transfer_id")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transfer_records
                (transfer_id, work_order_id, conversation_id, event_time, engineer,
                 transfer_reason, category, model, order_status, source_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transfer_id) DO UPDATE SET
                    work_order_id=excluded.work_order_id,
                    conversation_id=excluded.conversation_id,
                    event_time=excluded.event_time,
                    engineer=excluded.engineer,
                    transfer_reason=excluded.transfer_reason,
                    category=excluded.category,
                    model=excluded.model,
                    order_status=excluded.order_status,
                    source_json=excluded.source_json,
                    updated_at=excluded.updated_at
                """,
                (
                    transfer_id,
                    str(record.get("work_order_id") or "").strip(),
                    str(record.get("conversation_id") or "").strip(),
                    str(record.get("event_time") or "").strip(),
                    str(record.get("engineer") or "").strip(),
                    str(record.get("transfer_reason") or "").strip(),
                    str(record.get("category") or "").strip(),
                    str(record.get("model") or "").strip(),
                    str(record.get("order_status") or "").strip(),
                    _json(record.get("source") or record),
                    now,
                ),
            )

    def upsert_transfers(self, records: Iterable[dict[str, Any]]) -> int:
        count = 0
        for record in records:
            self.upsert_transfer(record)
            count += 1
        return count

    def list_transfers(self, start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[Any] = []
        if start_date:
            conditions.append("event_time >= ?")
            values.append(start_date)
        if end_date:
            conditions.append("event_time < ?")
            values.append(end_date)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM transfer_records
                {where}
                ORDER BY event_time, transfer_id
                """,
                values,
            ).fetchall()
        return [
            {
                **dict(row),
                "source": _decode(row["source_json"], {}),
            }
            for row in rows
        ]

    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transfer_records WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "source": _decode(row["source_json"], {})}

    def upsert_conversation(self, system: str, detail: dict[str, Any]) -> None:
        source_id = str(detail.get("source_id") or detail.get("conversation_id") or "").strip()
        if not source_id:
            raise ValueError(f"{system} 会话缺少 source_id")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transfer_conversations
                (system, source_id, work_order_id, engineer, started_at, ended_at,
                 first_question, last_answer, intent_result, conversation_text,
                 messages_json, retrievals_json, tools_json, attachments_json,
                 source_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(system, source_id) DO UPDATE SET
                    work_order_id=excluded.work_order_id,
                    engineer=excluded.engineer,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    first_question=excluded.first_question,
                    last_answer=excluded.last_answer,
                    intent_result=excluded.intent_result,
                    conversation_text=excluded.conversation_text,
                    messages_json=excluded.messages_json,
                    retrievals_json=excluded.retrievals_json,
                    tools_json=excluded.tools_json,
                    attachments_json=excluded.attachments_json,
                    source_json=excluded.source_json,
                    updated_at=excluded.updated_at
                """,
                (
                    system,
                    source_id,
                    str(detail.get("work_order_id") or "").strip(),
                    str(detail.get("engineer") or "").strip(),
                    str(detail.get("started_at") or "").strip(),
                    str(detail.get("ended_at") or "").strip(),
                    str(detail.get("first_question") or "").strip(),
                    str(detail.get("last_answer") or "").strip(),
                    str(detail.get("intent_result") or "").strip(),
                    str(detail.get("conversation_text") or "").strip(),
                    _json(detail.get("messages") or []),
                    _json(detail.get("retrievals") or []),
                    _json(detail.get("tools") or []),
                    _json(detail.get("attachments") or []),
                    _json(detail.get("source") or detail),
                    _now(),
                ),
            )

    def get_conversation(self, system: str, source_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM transfer_conversations
                WHERE system = ? AND source_id = ?
                """,
                (system, source_id),
            ).fetchone()
        return self._conversation_row(row) if row else None

    def conversations_for_work_order(self, system: str, work_order_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transfer_conversations
                WHERE system = ? AND work_order_id = ?
                ORDER BY ended_at, source_id
                """,
                (system, work_order_id),
            ).fetchall()
        return [self._conversation_row(row) for row in rows]

    @staticmethod
    def _conversation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "messages": _decode(row["messages_json"], []),
            "retrievals": _decode(row["retrievals_json"], []),
            "tools": _decode(row["tools_json"], []),
            "attachments": _decode(row["attachments_json"], []),
            "source": _decode(row["source_json"], {}),
        }

    def save_link(
        self,
        transfer_id: str,
        source_id: str,
        confidence: str,
        reason: str,
        candidate_count: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transfer_links
                (transfer_id, source_id, confidence, reason, candidate_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(transfer_id) DO UPDATE SET
                    source_id=excluded.source_id,
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    candidate_count=excluded.candidate_count,
                    updated_at=excluded.updated_at
                """,
                (transfer_id, source_id, confidence, reason, int(candidate_count), _now()),
            )

    def replace_samples(
        self,
        week_start: str,
        samples: Iterable[tuple[str, str]],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM transfer_weekly_samples WHERE week_start = ?",
                (week_start,),
            )
            now = _now()
            connection.executemany(
                """
                INSERT INTO transfer_weekly_samples
                (week_start, transfer_id, bucket, selected_at)
                VALUES (?, ?, ?, ?)
                """,
                [(week_start, transfer_id, bucket, now) for transfer_id, bucket in samples],
            )

    def list_samples(self, week_start: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.week_start, s.bucket, r.*
                FROM transfer_weekly_samples s
                JOIN transfer_records r ON r.transfer_id = s.transfer_id
                WHERE s.week_start = ?
                ORDER BY r.event_time, r.transfer_id
                """,
                (week_start,),
            ).fetchall()
        return [
            {
                **dict(row),
                "source": _decode(row["source_json"], {}),
            }
            for row in rows
        ]

    def save_annotation(
        self,
        week_start: str,
        transfer_id: str,
        row: dict[str, Any],
        confidence: dict[str, Any],
        evidence: list[Any],
        *,
        status: str,
        model_name: str,
        prompt_version: str,
        needs_review: bool,
        error: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transfer_annotations
                (week_start, transfer_id, status, row_json, confidence_json,
                 evidence_json, model_name, prompt_version, needs_review, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(week_start, transfer_id) DO UPDATE SET
                    status=excluded.status,
                    row_json=excluded.row_json,
                    confidence_json=excluded.confidence_json,
                    evidence_json=excluded.evidence_json,
                    model_name=excluded.model_name,
                    prompt_version=excluded.prompt_version,
                    needs_review=excluded.needs_review,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    week_start,
                    transfer_id,
                    status,
                    _json(row),
                    _json(confidence),
                    _json(evidence),
                    model_name,
                    prompt_version,
                    1 if needs_review else 0,
                    error,
                    _now(),
                ),
            )

    def save_review(
        self,
        week_start: str,
        transfer_id: str,
        reviewer: str,
        review: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transfer_reviews
                (week_start, transfer_id, reviewer, review_json, reviewed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(week_start, transfer_id) DO UPDATE SET
                    reviewer=excluded.reviewer,
                    review_json=excluded.review_json,
                    reviewed_at=excluded.reviewed_at
                """,
                (week_start, transfer_id, reviewer.strip(), _json(review), _now()),
            )

    def list_annotation_rows(
        self,
        week_start: str,
        *,
        only_needs_review: bool = False,
    ) -> list[dict[str, Any]]:
        review_condition = "AND a.needs_review = 1" if only_needs_review else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, v.reviewer, v.review_json, v.reviewed_at
                FROM transfer_annotations a
                LEFT JOIN transfer_reviews v
                    ON v.week_start = a.week_start AND v.transfer_id = a.transfer_id
                WHERE a.week_start = ? {review_condition}
                ORDER BY a.updated_at, a.transfer_id
                """,
                (week_start,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for item in rows:
            row = _decode(item["row_json"], {})
            review = _decode(item["review_json"], {})
            if review:
                row.update(review)
                row["审核状态"] = "已审核"
                row["审核人"] = item["reviewer"] or ""
                row["审核时间"] = item["reviewed_at"] or ""
            else:
                row.setdefault("审核状态", "待审核" if item["needs_review"] else "无需复核")
                row.setdefault("审核人", "")
                row.setdefault("审核时间", "")
            row["_week_start"] = item["week_start"]
            row["_transfer_id"] = item["transfer_id"]
            row["_needs_review"] = bool(item["needs_review"])
            row["_model_status"] = item["status"]
            row["_model_error"] = item["error"]
            row["_confidence"] = _decode(item["confidence_json"], {})
            row["_evidence"] = _decode(item["evidence_json"], [])
            results.append(row)
        return results

    def annotation_count(self, week_start: str) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(needs_review) AS needs_review,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM transfer_annotations
                WHERE week_start = ?
                """,
                (week_start,),
            ).fetchone()
            reviewed = connection.execute(
                "SELECT COUNT(*) FROM transfer_reviews WHERE week_start = ?",
                (week_start,),
            ).fetchone()[0]
        return {
            "total": int(row["total"] or 0),
            "needs_review": int(row["needs_review"] or 0),
            "completed": int(row["completed"] or 0),
            "failed": int(row["failed"] or 0),
            "reviewed": int(reviewed or 0),
        }
