"""Store unverified recall hints separately from reviewed replay and editing tasks."""

import hashlib
import json
import sqlite3


_HINT_FIELDS = (
    "knowledge_id", "source_work_order_id", "referable_votes", "total_votes", "knowledge_updated_at",
)


def _record(row: dict) -> tuple[str, str, str] | None:
    hints = row.get("outside_candidate_hints")
    if not hints:
        return None
    if row["status"] != "awaiting_evidence":
        raise ValueError("only unverified recall checks may enter the queue")
    work_order_id = row["work_order_id"]
    version = row["evidence_version"]
    payload = json.dumps([{field: hint[field] for field in _HINT_FIELDS} for hint in hints],
                         sort_keys=True, separators=(",", ":"))
    return work_order_id, version, payload


def sync_recall_review_inbox(db: sqlite3.Connection, observed: set[str], recall: list[dict], selected: list[dict]) -> dict:
    """Refresh observed tasks; only selected items may create new work."""
    if not db.in_transaction:
        raise ValueError("inbox transaction required")
    current = {entry[0]: entry for row in recall if (entry := _record(row)) is not None}
    if not current.keys() <= observed:
        raise ValueError("hint missing from the current scan")
    db.execute("""
        CREATE TABLE IF NOT EXISTS offline_recall_review_queue (
            task_id TEXT PRIMARY KEY,
            work_order_id TEXT NOT NULL,
            evidence_version TEXT NOT NULL,
            hints_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('awaiting_evidence', 'superseded'))
        )
    """)
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS offline_recall_review_active
        ON offline_recall_review_queue (work_order_id) WHERE status='awaiting_evidence'
    """)
    superseded = 0
    for task_id, *record in db.execute("""
        SELECT task_id, work_order_id, evidence_version, hints_json
        FROM offline_recall_review_queue WHERE status='awaiting_evidence'
    """).fetchall():
        if record[0] in observed and current.get(record[0]) != tuple(record):
            superseded += db.execute("""
                UPDATE offline_recall_review_queue SET status='superseded' WHERE task_id=?
            """, (task_id,)).rowcount
    inserted = reactivated = 0
    for row in selected:
        record = _record(row)
        if record is None:
            continue
        if current.get(record[0]) != record:
            raise ValueError("selected hint missing from current evidence")
        task_id = hashlib.sha256(json.dumps(record, separators=(",", ":")).encode("utf-8")).hexdigest()
        inserted_here = db.execute("""
            INSERT INTO offline_recall_review_queue
                (task_id, work_order_id, evidence_version, hints_json, status)
            VALUES (?, ?, ?, ?, 'awaiting_evidence') ON CONFLICT(task_id) DO NOTHING
        """, (task_id, *record)).rowcount
        inserted += inserted_here
        if not inserted_here:
            reactivated += db.execute("""
                UPDATE offline_recall_review_queue SET status='awaiting_evidence'
                WHERE task_id=? AND status='superseded'
            """, (task_id,)).rowcount
    return {"inserted": inserted, "superseded": superseded, "reactivated": reactivated}
