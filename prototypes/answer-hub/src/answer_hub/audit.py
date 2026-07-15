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
