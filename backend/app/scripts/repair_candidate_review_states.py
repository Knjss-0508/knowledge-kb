"""Recalculate selected candidate states; preview and roll back unless --apply."""

from __future__ import annotations

import argparse
import json

from app.core.database import SessionLocal
from app.models.integration import IntegrationIngestion
from app.routes.integration import _candidate_is_locked, _persist_candidate_state


def repair_candidate_states(db, ingestion_ids: list[str], *, apply: bool = False) -> list[dict]:
    ids = list(dict.fromkeys(ingestion_ids))
    if not ids or len(ids) > 100:
        raise ValueError("必须指定 1 至 100 个候选 ID。")
    results = []
    try:
        query = db.query(IntegrationIngestion).filter(
            IntegrationIngestion.id.in_(ids),
            IntegrationIngestion.review_status.isnot(None),
            IntegrationIngestion.source_system != "excel",
        )
        if apply:
            query = query.with_for_update()
        items = {item.id: item for item in query.all()}
        for ingestion_id in ids:
            item = items.get(ingestion_id)
            if item is None or _candidate_is_locked(item):
                results.append({
                    "ingestion_id": ingestion_id,
                    "result": "skipped",
                    "reason": "候选不存在或已送审锁定",
                })
                continue
            before = item.review_status
            state = _persist_candidate_state(
                item, dict(item.candidate_payload or {}), dict(item.review_metadata or {}),
            )
            results.append({
                "ingestion_id": ingestion_id,
                "result": "applied" if apply else "preview",
                "before": before,
                "after": state["review_status"],
                "blocking_reasons": state["blocking_reasons"],
            })
        if apply:
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="按指定 ID 修复候选状态，默认仅预览并回滚。")
    parser.add_argument("--ingestion-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true", help="核对预览后显式写入状态")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        results = repair_candidate_states(db, args.ingestion_id, apply=args.apply)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
