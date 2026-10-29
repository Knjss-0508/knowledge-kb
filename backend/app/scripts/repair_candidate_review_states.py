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


STUCK_STATUSES = ("revision_required", "hold_for_evidence", "pending_model_annotation")


def find_stuck_worthy_candidates(db, *, limit: int = 500) -> list[dict]:
    """找出「人工已确认值得沉淀、草稿完整，但状态卡在非送审」的候选。

    这类记录是 #236 修复前的存量：人工点了「值得沉淀」，模型初标却把状态
    改回退回转写/补证据/待重新标注。修复只对之后保存的候选生效，存量需要
    重新计算一次状态。
    """

    from app.services.candidate_review import (
        draft_blocking_reasons,
        normalize_human_review,
    )
    from app.routes.integration import _candidate_payload_with_taxonomy_defaults

    rows = (
        db.query(IntegrationIngestion)
        .filter(
            IntegrationIngestion.review_status.in_(STUCK_STATUSES),
            IntegrationIngestion.source_system != "excel",
        )
        .limit(max(1, int(limit)))
        .all()
    )
    found: list[dict] = []
    for item in rows:
        payload, knowledge = _candidate_payload_with_taxonomy_defaults(
            item.candidate_payload or {}
        )
        review_metadata = dict(item.review_metadata or {})
        human = normalize_human_review(
            payload.get("human_review") or review_metadata.get("human_review") or {}
        )
        if human.get("knowledge_value") != "worthy":
            continue
        if human.get("usability") != "usable" and human.get("decision") not in {
            "approved",
            "approved_with_changes",
        }:
            continue
        if draft_blocking_reasons(knowledge):
            continue
        found.append(
            {
                "ingestion_id": item.id,
                "current_status": item.review_status,
                "title": str((knowledge or {}).get("title") or "")[:60],
            }
        )
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="按指定 ID 修复候选状态，默认仅预览并回滚。")
    parser.add_argument("--ingestion-id", action="append", default=[])
    parser.add_argument(
        "--find-stuck-worthy",
        action="store_true",
        help="自动查找「人工已标值得沉淀但状态卡住」的候选（只列出，不写入）",
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="自动查找时最多扫描的候选数"
    )
    parser.add_argument("--apply", action="store_true", help="核对预览后显式写入状态")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.find_stuck_worthy:
            found = find_stuck_worthy_candidates(db, limit=args.limit)
            ids = [row["ingestion_id"] for row in found]
            print(
                json.dumps(
                    {"found": len(found), "candidates": found},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if not ids:
                return
            if not args.apply:
                print("（未加 --apply，仅列出。确认后可加 --apply 批量重算。）")
                return
            if len(ids) > 100:
                print(f"共 {len(ids)} 条，超过单次 100 条上限，请分批执行。")
                return
            results = repair_candidate_states(db, ids, apply=True)
        else:
            if not args.ingestion_id:
                parser.error("需要 --ingestion-id 或 --find-stuck-worthy")
            results = repair_candidate_states(db, args.ingestion_id, apply=args.apply)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
