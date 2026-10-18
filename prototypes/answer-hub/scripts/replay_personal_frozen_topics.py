"""Replay frozen memberships through real topic transcription and review."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from answer_hub.answer_evidence import verify_human_answer
from answer_hub.audit import AuditStore
from answer_hub.catalog import load_standard_catalog
from answer_hub.mimo import MimoClient, MimoConfig
from answer_hub import workflow
from run_isolated_real_audit import model_profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--source-rows", required=True, nargs="+", type=int)
    parser.add_argument("--historical-standards-snapshot", type=Path)
    args = parser.parse_args()
    key, base, model = model_profile("personal")
    root = args.output_root.resolve()
    snapshot_bytes = None
    snapshot_metadata = None
    if args.historical_standards_snapshot:
        snapshot_path = args.historical_standards_snapshot.resolve(strict=True)
        snapshot_bytes = snapshot_path.read_bytes()
        payload = json.loads(snapshot_bytes.decode("utf-8-sig"))
        if (
            not isinstance(payload, dict)
            or not payload.get("compiled_at")
            or not payload.get("version")
            or not isinstance(payload.get("items"), list)
            or not payload["items"]
        ):
            raise ValueError("Historical snapshot requires explicit compilation metadata and items")
        for item in payload["items"]:
            if not isinstance(item, dict) or not all(
                item.get(field)
                for field in ("standard_id", "status", "version", "source_file", "source_sheet", "source_rows")
            ) or item["status"] != "published":
                raise ValueError("Historical snapshot items require explicit published status and source provenance")
        snapshot_metadata = {
            "source_path": str(snapshot_path),
            "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "compiled_at": payload["compiled_at"],
            "version": payload["version"],
            "item_count": len(payload["items"]),
            "scope": "historical_local_standard_snapshot_not_current_headquarters_validation",
            "current_effectiveness_verified": False,
        }
    root.mkdir(parents=True, exist_ok=False)
    catalog = []
    if snapshot_bytes is not None:
        frozen_snapshot = root / "historical_standards_snapshot.json"
        frozen_snapshot.write_bytes(snapshot_bytes)
        catalog = load_standard_catalog(frozen_snapshot)
    all_groups = json.loads(args.groups.read_text(encoding="utf-8-sig"))
    requested = set(args.source_rows)
    selected = [
        group for group in all_groups
        if requested.intersection(group["source_excel_rows"])
    ]
    if not selected:
        raise ValueError("No matching frozen groups")
    found = {number for group in selected for number in group["source_excel_rows"]}
    if not requested.issubset(found):
        raise ValueError("Some requested source rows are missing")
    rows = [row for group in selected for row in group["unit_rows"]]
    source_by_id = {}
    for group in selected:
        for row in group["unit_rows"]:
            source_by_id[row["数据ID"]] = group["source_excel_rows"]
    provenance = {
        "scope": "frozen_memberships_real_topic_stage_transcription_and_review_no_reclustering",
        "requested_source_excel_rows": sorted(requested),
        "included_source_excel_rows": sorted(found),
        "frozen_group_count": len(selected),
        "frozen_atom_count": len(rows),
        "groups_sha256": hashlib.sha256(args.groups.read_bytes()).hexdigest(),
        "model": model, "base_url": base,
        "credential_source": "DEEPSEEK_PERSONAL_API_KEY",
        "standard_retrieval": bool(catalog),
        "historical_standard_snapshot": snapshot_metadata,
        "online_headquarters_retrieval": False, "website_sync": False,
        "enforce_cluster_admission": True,
        "transcribe_all_admitted_topics": True,
        "started_at_epoch": time.time(),
        "source_fingerprints": {
            name: hashlib.sha256(Path(workflow.__file__).parent.joinpath(name).read_bytes()).hexdigest()
            for name in (
                "workflow.py", "mimo.py", "answer_evidence.py", "knowledge_value.py",
                "topic_transcription/service.py",
            )
        },
    }
    (root / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (root / "frozen_inputs.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    for name in tuple(os.environ):
        if name.startswith(("KB_", "CZ_", "EMBEDDING_", "AUTO_REVIEW_", "MIMO_")):
            os.environ[name] = ""
    os.environ.update({
        "AUTO_REVIEW_ENABLED": "false", "AUTO_REVIEW_KILL_SWITCH": "true",
        "ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW": "false",
        "ANSWER_HUB_DB_PATH": str(root / "isolated-audit.db"),
    })
    client = MimoClient(MimoConfig(
        api_key=key, base_url=base, model=model, media_model=model,
        max_completion_tokens=8192, max_retries=1,
        timeout_seconds=60, response_read_timeout_seconds=90,
        cluster_media_policy="never",
    ))
    groups = [(tuple(g["group_key"]), g["unit_rows"]) for g in selected]
    meta = {}

    def progress(message, metrics):
        snapshot = {"message": message, "metrics": metrics}
        (root / "progress.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)

    with ExitStack() as stack:
        for module in ("mimo", "audit", "auto_review", "cz_integration"):
            stack.enter_context(patch(f"answer_hub.{module}.load_dotenv"))
        stack.enter_context(patch.object(
            workflow, "_direct_mimo_topic_groups",
            return_value=(groups, {
                "requested_mode": "direct_mimo", "effective_mode": "direct_mimo",
                "provider": "frozen-memberships", "model": model,
                "cluster_count": len(groups),
            }),
        ))
        topics, mappings, gaps, pending = workflow.build_topic_review_rows(
            rows, use_mimo=True, mimo_client=client,
            audit_store=AuditStore(root / "isolated-audit.db"),
            run_id=root.name, clustering_mode="direct_mimo",
            standard_catalog=catalog,
            use_standard_references=bool(catalog), enforce_cluster_admission=True,
            transcribe_all_admitted_topics=True, topic_model_call_limit=200,
            topic_progress_callback=progress, clustering_meta=meta,
        )
    report = {
        "topics": topics, "mapping": mappings, "evidence_gaps": gaps,
        "pending": pending, "clustering_meta": meta,
        "frozen_group_rows_by_source_id": source_by_id,
        "answer_gate": [
            {"atomic_id": row.get("_原子知识ID"),
             "source_id": row.get("数据ID"), **asdict(verify_human_answer(row))}
            for row in rows
        ],
        "finished_at_epoch": time.time(),
    }
    (root / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    sheets = {}
    for name, values in (
        ("topic_review_queue", topics), ("topic_source_mapping", mappings),
        ("evidence_gap_rows", gaps), ("pending_cluster_rows", pending),
    ):
        columns = list(dict.fromkeys(key for row in values for key in row))
        sheets[name] = (columns or ["状态"], values)
    workflow.write_rows_to_workbook(sheets, root / "topic_review_queue.xlsx")
    print(json.dumps({
        "status": "completed_for_audit", "topics": len(topics),
        "mappings": len(mappings), "gaps": len(gaps), "pending": len(pending),
        "nonempty_bodies": sum(bool(t.get("知识内容")) for t in topics),
        "output_root": str(root),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
