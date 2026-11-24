"""Use real blind votes to calibrate frozen candidate suitability, offline only."""

import argparse
import hashlib
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from scripts._blind_knowledge_inbox import open_inbox
from scripts._blind_replay_io import local_database, readonly_engine


# Never retrieve question text, candidate content, user identifiers or free notes.
# The knowledge snapshot comparison takes place inside PostgreSQL.
EVIDENCE = text("""
WITH completed AS (
    SELECT work_order_id, count(*) AS n
    FROM blind_label_assignments
    WHERE status = 'completed' GROUP BY work_order_id
), orders AS (
    SELECT w.id, w.assignment_limit, w.candidate_snapshot,
           md5(lower(regexp_replace(trim(w.query_text), '[[:space:]]+', ' ', 'g'))) AS question_group
    FROM blind_label_work_orders w JOIN completed t ON t.work_order_id = w.id
    WHERE w.pool_state = 'active' AND w.assignment_limit IN (3,5)
      AND t.n = w.assignment_limit AND length(trim(w.query_text)) > 0
      AND NOT EXISTS (SELECT 1 FROM blind_label_arbitrations a WHERE a.work_order_id = w.id)
    ORDER BY w.id LIMIT 10000
), votes AS (
    SELECT a.work_order_id, a.candidate_ref,
           count(*) FILTER (WHERE a.verdict='referable') AS yes,
           count(*) FILTER (WHERE a.verdict='not_referable') AS no,
           count(*) FILTER (WHERE a.reason_code='needs_conditions' AND a.verdict='referable') AS needs_conditions,
           count(*) FILTER (WHERE a.reason_code='needs_merge' AND a.verdict='referable') AS needs_merge,
           count(*) FILTER (WHERE a.reason_code='content_incorrect' AND a.verdict='not_referable') AS content_incorrect,
           count(*) FILTER (WHERE a.reason_code='content_outdated' AND a.verdict='not_referable') AS content_outdated
    FROM blind_label_annotations a
    JOIN orders w ON w.id = a.work_order_id
    JOIN blind_label_assignments t ON t.id = a.assignment_id AND t.work_order_id = w.id
      AND t.status = 'completed' AND t.user_id = a.user_id
    GROUP BY a.work_order_id,a.candidate_ref
)
SELECT w.id AS work_order_id,w.question_group,w.assignment_limit,
       c->>'candidate_ref' AS candidate_ref,c->>'rank' AS rank,
       c->>'final_score' AS score,k.id AS knowledge_id,
       k.updated_at AS knowledge_updated_at,
       (k.id IS NOT NULL AND k.title=c->>'title' AND k.content::jsonb=c->'content') AS snapshot_matches,
       v.yes,v.no,v.needs_conditions,v.needs_merge,v.content_incorrect,v.content_outdated
FROM orders w CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(w.candidate_snapshot::jsonb)='array' THEN w.candidate_snapshot::jsonb ELSE '[]'::jsonb END
) c
JOIN votes v ON v.work_order_id=w.id AND v.candidate_ref=c->>'candidate_ref'
LEFT JOIN knowledge_items k ON k.id=c->>'knowledge_id' AND k.status='PUBLISHED'
ORDER BY w.id,c->>'candidate_ref'
""")

REASONS = ("needs_conditions", "needs_merge", "content_incorrect", "content_outdated")


def _samples(rows):
    by_question = defaultdict(dict)
    drafts = {}
    for row in rows:
        if not row["snapshot_matches"]:
            continue
        for reason in REASONS:
            if row[reason] * 2 > row["assignment_limit"]:
                updated = row["knowledge_updated_at"]
                if updated is None:
                    continue
                version = updated.isoformat()
                key = row["knowledge_id"], reason, version
                entry = drafts.setdefault(key, {
                    "knowledge_id": row["knowledge_id"], "reason": reason, "knowledge_updated_at": version,
                    "status": "needs_editor", "source": "explicit_blind_vote_majority",
                    "evidence": [], "published": False,
                })
                entry["evidence"].append({
                    "work_order_id": row["work_order_id"], "candidate_ref": row["candidate_ref"],
                    "support": row[reason], "total": row["assignment_limit"],
                })
        if row["yes"] * 2 > row["assignment_limit"]:
            label = 1
        elif row["no"] * 2 > row["assignment_limit"]:
            label = 0
        else:
            continue
        try:
            rank, score = int(row["rank"]), float(row["score"])
        except (ValueError, TypeError):
            continue
        if not (1 <= rank <= 3 and math.isfinite(score) and 0 <= score <= 1):
            continue
        order = by_question[row["question_group"]].setdefault(row["work_order_id"], {})
        candidate = (rank, score, label, row["knowledge_id"])
        key = row["candidate_ref"]
        if key not in order:
            order[key] = candidate
        else:
            order[key] = None  # ambiguous duplicate reference; never count it twice
    # Give one historical work order per normalized question; never split its candidates.
    selected = [(key, [r for r in orders[min(orders)].values() if r is not None])
                for key, orders in sorted(by_question.items())]
    return [(key, rows) for key, rows in selected if rows], list(drafts.values())


def _split(key):
    bucket = int(hashlib.sha256(f"blind-frozen-v1:{key}".encode()).hexdigest()[:8], 16) % 10
    return "train" if bucket < 6 else "validation" if bucket < 8 else "test"


def _fit(rows, width, smoothing):
    totals = Counter(rank for rank, *_ in rows)
    positives = Counter(rank for rank, _, label, _ in rows if label)
    prior = (sum(positives.values()) + 1) / (len(rows) + 2)
    by_rank = {r: (positives[r] + 10 * prior) / (totals[r] + 10) for r in range(1, 4)}
    n, y = Counter(), Counter()
    if width:
        for rank, score, label, _ in rows:
            key = rank, min(width - 1, int(score * width))
            n[key] += 1
            y[key] += label
    return {
        "rank_prior": by_rank, "score_bins": width, "smoothing": smoothing,
        "bins": {f"{r}:{b}": (y[r, b] + smoothing * by_rank[r]) / (n[r, b] + smoothing)
                 for r in by_rank for b in range(width)},
    }


def _brier(rows, model):
    width = model["score_bins"]
    def prediction(rank, score):
        return model["bins"].get(f"{rank}:{min(width - 1, int(score * width))}", model["rank_prior"][rank])
    return sum((prediction(rank, score) - label) ** 2 for rank, score, label, _ in rows) / len(rows)


def train(groups):
    partitions = {key: [] for key in ("train", "validation", "test")}
    counts = Counter()
    for key, rows in groups:
        name = _split(key)
        counts[name] += 1
        partitions[name].extend(rows)
    test, validation, training = (partitions[key] for key in ("test", "validation", "train"))
    # No learned parameter references an article ID. Additionally report a
    # stricter slice with knowledge not used in train or validation.
    prior_ids = {row[3] for row in training + validation}
    unseen = [row for row in test if row[3] not in prior_ids]
    result = {"orders": dict(counts), "labeled_candidates": {k: len(v) for k, v in partitions.items()},
              "unseen_knowledge_test_candidates": len(unseen)}
    if any(len(v) < 50 or min(sum(r[2] for r in v), sum(not r[2] for r in v)) < 10 for v in partitions.values()):
        return {**result, "status": "insufficient_two_class_evidence"}, None
    baseline = _fit(training, 0, 10)
    candidate = min(
        (_fit(training, width, smooth) for width in (10, 20, 40) for smooth in (10, 30, 100)),
        key=lambda model: _brier(validation, model),
    )
    val_baseline, val_candidate = _brier(validation, baseline), _brier(validation, candidate)
    test_baseline, test_candidate = _brier(test, baseline), _brier(test, candidate)
    unseen_baseline = _brier(unseen, baseline) if unseen else None
    unseen_candidate = _brier(unseen, candidate) if unseen else None
    result.update(validation_rank_only_brier=val_baseline, validation_candidate_brier=val_candidate,
                  test_rank_only_brier=test_baseline, test_candidate_brier=test_candidate,
                  unseen_rank_only_brier=unseen_baseline, unseen_candidate_brier=unseen_candidate)
    passed = (val_candidate < val_baseline and test_candidate < test_baseline and len(unseen) >= 30
              and unseen_candidate <= unseen_baseline)
    return {**result, "status": "offline_candidate" if passed else "quality_gate_failed"}, (
        {**candidate, "type": "frozen_candidate_vote_calibration", "production_approved": False} if passed else None
    )


def _write_editor_queue(db, tasks):
    db.execute("""
        CREATE TABLE IF NOT EXISTS offline_vote_editor_queue (
            task_id TEXT PRIMARY KEY,
            knowledge_id TEXT NOT NULL,
            knowledge_updated_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'needs_editor',
            UNIQUE(knowledge_id, knowledge_updated_at, reason)
        )
    """)
    inserted = 0
    for task in tasks:
        key = [task["knowledge_id"], task["knowledge_updated_at"], task["reason"]]
        task_id = hashlib.sha256(json.dumps(key, ensure_ascii=True).encode()).hexdigest()
        inserted += db.execute("""
            INSERT INTO offline_vote_editor_queue
                (task_id, knowledge_id, knowledge_updated_at, reason, evidence_json)
            VALUES(?,?,?,?,?)
            ON CONFLICT (task_id) DO NOTHING
        """, (task_id, *key, json.dumps(task["evidence"], ensure_ascii=True))).rowcount
    return inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated-replica", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--editor-inbox", type=Path)
    args = parser.parse_args()
    if not args.confirm_isolated_replica:
        parser.error("需要确认断网隔离副本，不允许生产隧道。")
    target = args.out.resolve()
    if target.exists() or target.suffix != ".json" or any((p / ".git").exists() for p in target.parents):
        parser.error("需要仓库外且尚不存在的受控 JSON 文件。")
    inbox = args.editor_inbox.resolve() if args.editor_inbox else None
    if inbox is not None and (inbox.suffix != ".sqlite3" or any((p / ".git").exists() for p in inbox.parents)):
        parser.error("编辑队列需要仓库外受控目录下的 .sqlite3 文件。")
    try:
        engine = readonly_engine(local_database())
        try:
            with open_inbox(inbox) if inbox else nullcontext() as queue:
                with engine.begin() as conn:
                    conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                    rows = conn.execute(EVIDENCE).mappings().all()
                groups, tasks = _samples(rows)
                training, candidate = train(groups)
                inserted = _write_editor_queue(queue, tasks) if queue is not None else None
        finally:
            engine.dispose()
    except (ArgumentError, ValueError, SQLAlchemyError):
        parser.error("隔离数据读取失败；未输出原始记录。")
    except (OSError, sqlite3.Error):
        parser.error("隔离编辑队列不可用；未输出原始记录。")
    summary = {
        "schema_version": 1, "scope": "real_frozen_candidates_not_full_retrieval_or_final_answers",
        "candidate_votes_scanned": len(rows), "training": training, "editor_tasks": len(tasks),
        "editor_tasks_inserted": inserted, "published": False,
    }
    try:
        with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as output:
            json.dump({**summary, "candidate": candidate, "editor_queue": tasks}, output, allow_nan=False, indent=2)
    except OSError:
        parser.error("无法写入受控产物。")
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    main()
