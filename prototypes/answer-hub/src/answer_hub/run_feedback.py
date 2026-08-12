from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import sqlite3

from .run_history import sanitize_run_text


RUN_FEEDBACK_STATUS_LABELS = {
    "unhandled": "未处理",
    "acknowledged": "已确认",
    "in_progress": "处理中",
    "resolved": "已解决",
    "ignored": "已忽略",
}
RUN_FEEDBACK_CAUSE_LABELS = {
    "": "未分类",
    "stalled": "运行卡住",
    "input": "输入或脱敏",
    "model": "模型调用",
    "clustering": "原子问题或主题聚类",
    "admission": "聚类准入或历史归并",
    "transcription": "知识转写或内容初审",
    "cz_sync": "CZ候选同步",
    "deployment": "部署或任务调度",
    "other": "其他",
}


class RunFeedbackStoreError(RuntimeError):
    """Raised when the persistent feedback store cannot be read or updated."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_text(value: Any, *, limit: int) -> str:
    return sanitize_run_text(value).strip()[:limit]


def default_run_feedback(record_id: str) -> dict[str, Any]:
    return {
        "record_id": str(record_id or "").strip(),
        "status": "unhandled",
        "status_label": RUN_FEEDBACK_STATUS_LABELS["unhandled"],
        "owner": "",
        "cause_type": "",
        "note": "",
        "actor": "",
        "updated_at": "",
        "history": [],
    }


class RunFeedbackStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_feedback (
                    record_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    cause_type TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_feedback_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    cause_type TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_run_feedback_history_record
                ON run_feedback_history(record_id, id)
                """
            )
            connection.commit()
            return connection
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.close()
            raise RunFeedbackStoreError(
                "无法读取监管反馈存储，请检查文件是否损坏。"
            ) from exc

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        status = str(row["status"] or "")
        return {
            "status": status,
            "status_label": RUN_FEEDBACK_STATUS_LABELS.get(status, status),
            "owner": str(row["owner"] or ""),
            "cause_type": str(row["cause_type"] or ""),
            "note": str(row["note"] or ""),
            "actor": str(row["actor"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def get_many(
        self,
        record_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        keys = list(
            dict.fromkeys(
                str(record_id or "").strip()
                for record_id in record_ids
                if str(record_id or "").strip()
            )
        )
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        connection = self._connect()
        try:
            current_rows = connection.execute(
                f"""
                SELECT record_id, status, owner, cause_type, note, actor,
                       updated_at
                FROM run_feedback
                WHERE record_id IN ({placeholders})
                """,
                keys,
            ).fetchall()
            history_rows = connection.execute(
                f"""
                SELECT record_id, status, owner, cause_type, note, actor,
                       updated_at
                FROM run_feedback_history
                WHERE record_id IN ({placeholders})
                ORDER BY id
                """,
                keys,
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RunFeedbackStoreError(
                "无法读取监管反馈存储，请稍后重试。"
            ) from exc
        finally:
            connection.close()
        history_by_record: dict[str, list[dict[str, Any]]] = {}
        for row in history_rows:
            history_by_record.setdefault(str(row["record_id"]), []).append(
                self._event_from_row(row)
            )
        return {
            str(row["record_id"]): {
                "record_id": str(row["record_id"]),
                **self._event_from_row(row),
                "history": history_by_record.get(
                    str(row["record_id"]),
                    [],
                ),
            }
            for row in current_rows
        }

    def get(self, record_id: str) -> dict[str, Any] | None:
        key = str(record_id or "").strip()
        if not key:
            return None
        return self.get_many([key]).get(key)

    def update(
        self,
        record_id: str,
        *,
        status: str,
        owner: str = "",
        cause_type: str = "",
        note: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        key = str(record_id or "").strip()
        if not key:
            raise ValueError("缺少运行记录ID。")
        normalized_status = str(status or "").strip()
        if normalized_status not in RUN_FEEDBACK_STATUS_LABELS:
            raise ValueError("不支持的监管处理状态。")
        normalized_cause = str(cause_type or "").strip()
        if normalized_cause not in RUN_FEEDBACK_CAUSE_LABELS:
            raise ValueError("不支持的监管原因分类。")
        event = {
            "status": normalized_status,
            "owner": _clean_text(owner, limit=100),
            "cause_type": normalized_cause,
            "note": _clean_text(note, limit=2000),
            "actor": _clean_text(actor, limit=100),
            "updated_at": _now(),
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO run_feedback (
                    record_id, status, owner, cause_type, note, actor,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    status = excluded.status,
                    owner = excluded.owner,
                    cause_type = excluded.cause_type,
                    note = excluded.note,
                    actor = excluded.actor,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    event["status"],
                    event["owner"],
                    event["cause_type"],
                    event["note"],
                    event["actor"],
                    event["updated_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO run_feedback_history (
                    record_id, status, owner, cause_type, note, actor,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    event["status"],
                    event["owner"],
                    event["cause_type"],
                    event["note"],
                    event["actor"],
                    event["updated_at"],
                ),
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise RunFeedbackStoreError(
                "无法保存监管反馈，请稍后重试。"
            ) from exc
        finally:
            connection.close()
        saved = self.get(key)
        if saved is None:
            raise RunFeedbackStoreError("监管反馈保存后无法重新读取。")
        return saved
