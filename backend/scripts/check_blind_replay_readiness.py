"""Preflight a handoff against current reviews and an optional sanitized manifest."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.services.blind_label_learning import LearningReviewError, load_learning_review
from app.services.blind_label_replay import ReplayManifest
from scripts._blind_replay_io import controlled_artifact_path, local_database, read_manifest, readonly_engine


class HandoffCase(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    knowledge_id: str = Field(min_length=1, max_length=64)
    lane: Literal["recall_candidate_review", "ranking_candidate_review"]


class ReviewHandoff(BaseModel):
    schema_version: Literal[1]
    scope: Literal["replay_review_handoff_not_manifest"]
    runnable: Literal[False]
    cases: list[HandoffCase] = Field(max_length=150)


_ISSUES = {"recall_candidate_review": "retrieval_missed", "ranking_candidate_review": "ranking"}


def check_readiness(db: Session, packet: ReviewHandoff, manifest: ReplayManifest | None) -> dict:
    cases = {case.work_order_id: case for case in manifest.cases} if manifest else {}
    targets = defaultdict(set)
    for hint in packet.cases:
        targets[hint.work_order_id].add((_ISSUES[hint.lane], hint.knowledge_id))
    counts = Counter()
    for order_id, allowed in targets.items():
        try:
            detail = load_learning_review(db, order_id)
        except LearningReviewError as exc:
            if exc.status_code != 409:
                raise
            counts["order_unavailable"] += 1
            continue
        review = detail["review"] or {}
        case = cases.get(order_id)
        if not detail["replay_eligible"]:
            counts["no_current_eligible_review"] += 1
        elif (review["issue_type"], review["knowledge_id"]) not in allowed:
            counts["review_not_matched_to_handoff"] += 1
        elif case is None:
            counts["sanitized_request_missing"] += 1
        elif case.review_id != review["id"]:
            counts["manifest_review_version_mismatch"] += 1
        else:
            counts["ready"] += 1
    outside = len(cases.keys() - targets.keys())
    manifest_ready = bool(cases) and not outside and counts["ready"] == len(cases)
    return {
        "scope": "blind_replay_readiness_not_baseline",
        "handoff_orders": len(targets),
        "manifest_orders": len(cases),
        "manifest_orders_outside_handoff": outside,
        "ready": counts["ready"],
        "manifest_ready": manifest_ready,
        "statuses": dict(sorted(counts.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handoff", type=Path)
    parser.add_argument("--manifest", type=Path, help="已人工复核并脱敏的回放清单")
    parser.add_argument("--confirm-isolated-replica", action="store_true")
    parser.add_argument("--confirm-local-sanitized", action="store_true")
    args = parser.parse_args()
    if not args.confirm_isolated_replica:
        parser.error("必须确认断网隔离副本，不允许生产隧道。")
    if args.manifest and not args.confirm_local_sanitized:
        parser.error("仅接受已人工复核并脱敏的回放清单。")
    try:
        packet_path = controlled_artifact_path(args.handoff, ".json")
        manifest_path = controlled_artifact_path(args.manifest, ".json") if args.manifest else None
        _, packet = read_manifest(parser, packet_path, ReviewHandoff)
        manifest = read_manifest(parser, manifest_path, ReplayManifest)[1] if manifest_path else None
        engine = readonly_engine(local_database())
        try:
            with engine.begin() as connection:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                with Session(bind=connection, autoflush=False) as db:
                    result = check_readiness(db, packet, manifest)
        finally:
            engine.dispose()
    except (ArgumentError, SQLAlchemyError, OSError, ValueError):
        parser.error("无法完成预检；未输出请求、证据或凭据。")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["manifest_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
