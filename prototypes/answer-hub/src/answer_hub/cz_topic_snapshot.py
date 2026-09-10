from __future__ import annotations

"""Small, read-only-at-runtime snapshot of CZ published topic boundaries.

The snapshot intentionally stores matching metadata only.  It is not a copy of
knowledge正文、会话或媒体, so its size grows with topic count rather than with
all historical evidence volume.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
import json
import sqlite3


SNAPSHOT_FIELDS = (
    "cz_topic_id", "status", "title", "business_line", "product_category",
    "source_topic_key",
    "standard_family", "merge_policy", "object_key", "phenomenon_value",
    "query_target", "detection_target", "platform", "brand", "model_scope",
    "threshold_values",
)
BOUNDARY_FIELDS = (
    "business_line",
    "product_category",
    "standard_family",
    "merge_policy",
    "object_key",
    "phenomenon_value",
    "query_target",
    "detection_target",
    "platform",
    "brand",
    "model_scope",
    "threshold_values",
)
SNAPSHOT_ALLOWED_STATUSES = {"published", "review"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def boundary_hash(topic: dict[str, Any]) -> str:
    payload = {field: topic.get(field, "") for field in BOUNDARY_FIELDS}
    return sha256(_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SnapshotSyncResult:
    snapshot_version: str
    upserted: int
    retired: int
    skipped: int


class CZTopicSnapshot:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = 86400,
        max_items: int = 100_000,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_items = max(1, int(max_items))
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cz_topic_snapshot (
                    cz_topic_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    business_line TEXT NOT NULL,
                    product_category TEXT NOT NULL,
                    source_topic_key TEXT NOT NULL,
                    standard_family TEXT NOT NULL,
                    merge_policy TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    phenomenon_value TEXT NOT NULL,
                    query_target TEXT NOT NULL,
                    detection_target TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    model_scope TEXT NOT NULL,
                    threshold_values TEXT NOT NULL,
                    boundary_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_cz_topic_snapshot_boundary
                ON cz_topic_snapshot (boundary_hash);
                CREATE INDEX IF NOT EXISTS ix_cz_topic_snapshot_scope
                ON cz_topic_snapshot (business_line, product_category, status);
                CREATE TABLE IF NOT EXISTS cz_topic_snapshot_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    snapshot_version TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(cz_topic_snapshot)"
                ).fetchall()
            }
            if "source_topic_key" not in columns:
                connection.execute(
                    """ALTER TABLE cz_topic_snapshot
                    ADD COLUMN source_topic_key TEXT NOT NULL DEFAULT ''"""
                )

    def sync(
        self,
        topics: Iterable[dict[str, Any]],
        *,
        snapshot_version: str,
        synced_at: datetime | None = None,
        updated_since: str = "",
    ) -> SnapshotSyncResult:
        synced = synced_at or datetime.now(timezone.utc)
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=timezone.utc)
        expires_at = synced + timedelta(seconds=self.ttl_seconds)
        normalized: dict[str, dict[str, Any]] = {}
        skipped = 0
        for raw in topics:
            if len(normalized) >= self.max_items:
                skipped += 1
                continue
            topic = {
                field: _text(raw.get(field))
                for field in SNAPSHOT_FIELDS
            }
            raw_updated_at = _text(raw.get("updated_at"))
            topic["status"] = _text(raw.get("status")) or "published"
            topic_id = topic["cz_topic_id"]
            if (
                not topic_id
                or topic["status"] not in SNAPSHOT_ALLOWED_STATUSES
            ):
                skipped += 1
                continue
            if updated_since and raw_updated_at and raw_updated_at <= updated_since:
                skipped += 1
                continue
            topic["boundary_hash"] = boundary_hash(topic)
            topic["updated_at"] = raw_updated_at or synced.isoformat()
            topic["snapshot_version"] = _text(snapshot_version)
            topic["expires_at"] = expires_at.isoformat()
            normalized[topic_id] = topic
        with self._connect() as connection:
            upserted = 0
            for topic in normalized.values():
                columns = [*SNAPSHOT_FIELDS, "boundary_hash", "updated_at", "snapshot_version", "expires_at"]
                values = [topic.get(column, "") for column in columns]
                connection.execute(
                    f"""INSERT INTO cz_topic_snapshot ({','.join(columns)})
                    VALUES ({','.join('?' for _ in columns)})
                    ON CONFLICT(cz_topic_id) DO UPDATE SET
                    {','.join(f'{column}=excluded.{column}' for column in columns[1:])}""",
                    values,
                )
                upserted += 1
            retired = 0
            if not updated_since:
                cursor = connection.execute(
                    """DELETE FROM cz_topic_snapshot
                    WHERE snapshot_version != ?""",
                    (snapshot_version,),
                )
                retired = max(0, int(cursor.rowcount or 0))
            connection.execute(
                """INSERT INTO cz_topic_snapshot_meta (id, snapshot_version, synced_at, expires_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET snapshot_version=excluded.snapshot_version,
                synced_at=excluded.synced_at, expires_at=excluded.expires_at""",
                (snapshot_version, synced.isoformat(), expires_at.isoformat()),
            )
        return SnapshotSyncResult(snapshot_version, upserted, retired, skipped)

    def sync_from_file(
        self,
        path: str | Path,
        *,
        updated_since: str = "",
        synced_at: datetime | None = None,
    ) -> SnapshotSyncResult:
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"无法读取本地 CZ 主题快照文件：{source}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("本地 CZ 主题快照文件不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("本地 CZ 主题快照文件必须是 JSON 对象")
        snapshot_version = _text(payload.get("snapshot_version"))
        items = payload.get("items")
        if not snapshot_version:
            raise ValueError("本地 CZ 主题快照文件缺少 snapshot_version")
        if not isinstance(items, list):
            raise ValueError("本地 CZ 主题快照文件缺少 items 数组")
        if any(not isinstance(item, dict) for item in items):
            raise ValueError("本地 CZ 主题快照 items 必须全部为对象")
        return self.sync(
            (dict(item) for item in items),
            snapshot_version=snapshot_version,
            synced_at=synced_at,
            updated_since=updated_since,
        )

    def is_fresh(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM cz_topic_snapshot_meta WHERE id = 1"
            ).fetchone()
        if not row:
            return False
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return current < expires

    def list_published(
        self,
        *,
        business_line: str = "",
        product_category: str = "",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_topics(
            business_line=business_line,
            product_category=product_category,
            statuses={"published"},
            now=now,
        )

    def list_topics(
        self,
        *,
        business_line: str = "",
        product_category: str = "",
        statuses: set[str] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not self.is_fresh(now):
            return []
        effective_statuses = statuses or {"published"}
        allowed_statuses = sorted(
            status
            for status in effective_statuses
            if status in SNAPSHOT_ALLOWED_STATUSES
        )
        if not allowed_statuses:
            return []
        clauses = [
            "status IN (" + ",".join("?" for _ in allowed_statuses) + ")"
        ]
        params: list[str] = list(allowed_statuses)
        if business_line:
            clauses.append("business_line = ?")
            params.append(business_line)
        if product_category:
            clauses.append("product_category = ?")
            params.append(product_category)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM cz_topic_snapshot WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, cz_topic_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
