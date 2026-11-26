"""Persistent offline editor inbox. Never connects to the knowledge database."""

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def open_inbox(path: Path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        yield db


def sync_inbox(db: sqlite3.Connection, report: dict) -> dict:
    """Add eligible work, and mark superseded versions seen on this page."""
    discovery = {row["work_order_id"]: row for row in report["discovery"]["cases"]}
    drafts = {row["work_order_id"]: row for row in report["knowledge_drafts"]}
    if len(drafts) != len(report["knowledge_drafts"]):
        raise ValueError("duplicate draft")
    for order, draft in drafts.items():
        source = discovery[order]
        if (source["review_status"] != "confirmed" or source["evidence_version"] != draft["evidence_version"]
                or draft["issue"] not in {"knowledge_issue", "knowledge_gap"}):
            raise ValueError("draft does not have current review evidence")
    # Caller locks the inbox before opening the PostgreSQL snapshot so an
    # older process cannot supersede a newer revision out of order.
    if not db.in_transaction:
        raise ValueError("inbox transaction required")
    db.execute("""
        CREATE TABLE IF NOT EXISTS offline_knowledge_inbox (
            draft_id TEXT PRIMARY KEY,
            work_order_id TEXT NOT NULL,
            review_id INTEGER NOT NULL,
            evidence_version TEXT NOT NULL,
            issue TEXT NOT NULL CHECK (issue IN ('knowledge_issue', 'knowledge_gap')),
            status TEXT NOT NULL CHECK (status IN ('needs_editor', 'superseded')),
            UNIQUE (work_order_id, review_id, evidence_version)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS offline_knowledge_inbox_order
        ON offline_knowledge_inbox (work_order_id, status)
    """)
    inserted = superseded = 0
    for order in discovery:
        draft = drafts.get(order)
        if draft is None:
            # Even if the evidence digest stayed the same, the latest review
            # might have changed to 'exclude' or to a non-editor issue.
            superseded += db.execute("""
                UPDATE offline_knowledge_inbox SET status='superseded'
                WHERE work_order_id=? AND status='needs_editor'
            """, (order,)).rowcount
            continue
        superseded += db.execute("""
            UPDATE offline_knowledge_inbox SET status='superseded'
            WHERE work_order_id=? AND draft_id<>? AND status='needs_editor'
        """, (order, draft["draft_id"])).rowcount
        inserted += db.execute("""
            INSERT INTO offline_knowledge_inbox
                (draft_id, work_order_id, review_id, evidence_version, issue, status)
            VALUES(?, ?, ?, ?, ?, 'needs_editor')
            ON CONFLICT(draft_id) DO NOTHING
        """, (draft["draft_id"], order, draft["review_id"], draft["evidence_version"], draft["issue"])).rowcount
    return {"inserted": inserted, "superseded": superseded}
