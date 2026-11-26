"""Create a bounded, non-runnable handoff packet for missing replay evidence."""

import argparse
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scripts._blind_replay_io import controlled_artifact_path, read_manifest


_REQUEST_FIELDS = (
    "normalizedQuestion", "businessType", "productType", "categoryId", "brand", "brandId",
    "model", "modelId", "orderInfo", "limit", "candidatePoolLimit",
)


class AuditHint(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    knowledge_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    finding: Literal[
        "stale_hint", "already_in_frozen_candidates", "present_in_original_event",
        "present_in_same_request_event", "original_snapshot_unusable", "not_seen_in_checked_events",
    ]
    original_event_rank: int | None = Field(ge=1, le=999999, strict=True)
    events_checked: int = Field(ge=0, strict=True)


class RecallAudit(BaseModel):
    schema_version: Literal[1, 2]
    scope: Literal["retained_event_candidate_presence_not_review"]
    hints: list[AuditHint] = Field(max_length=150)


def build_packet(audit: RecallAudit) -> dict:
    cases = []
    for row in audit.hints:
        finding = row.finding
        if finding not in {"not_seen_in_checked_events", "present_in_original_event"}:
            continue
        cases.append({
            "work_order_id": row.work_order_id,
            "knowledge_id": row.knowledge_id,
            "lane": "recall_candidate_review" if finding == "not_seen_in_checked_events" else "ranking_candidate_review",
            "finding": finding,
            "original_event_rank": row.original_event_rank,
            "events_checked": row.events_checked,
            "required_evidence": (
                ["independent_applicability_review", "reviewed_sanitized_request", "published_knowledge_version"]
                if finding == "not_seen_in_checked_events"
                else ["original_candidate_window_review", "reviewed_sanitized_request", "published_knowledge_version"]
            ),
            "request_template": {field: None for field in _REQUEST_FIELDS},
            "review_id": None,
            "replay_ready": False,
            "blockers": ["review_id_missing", "request_not_supplied_or_reviewed"],
        })
    cases.sort(key=lambda case: (
        case["lane"] != "recall_candidate_review", case["work_order_id"], case["knowledge_id"],
    ))
    return {
        "schema_version": 1,
        "scope": "replay_review_handoff_not_manifest",
        "runnable": False,
        "summary": {
            "cases": len(cases),
            "recall_candidate_review": sum(row["lane"] == "recall_candidate_review" for row in cases),
            "ranking_candidate_review": sum(row["lane"] == "ranking_candidate_review" for row in cases),
            "replay_ready": 0,
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, help="受控目录中的审计报告")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        audit_path = controlled_artifact_path(args.audit, ".json")
        output = controlled_artifact_path(args.out, ".json", new=True)
        _, audit = read_manifest(parser, audit_path, RecallAudit)
        packet = build_packet(audit)
        with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as handle:
            json.dump(packet, handle, ensure_ascii=True, indent=2)
    except (OSError, ValueError):
        parser.error("无法读取审计报告或写入受控复核包；未输出原始数据。")
    print(json.dumps(packet["summary"], ensure_ascii=True))


if __name__ == "__main__":
    main()
