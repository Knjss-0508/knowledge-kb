from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .automation import (
    list_automation_runs,
    resume_automation_pipeline,
    run_automation_pipeline,
)
from .automation_queue import process_automation_queue
from .operations import apply_retention_cleanup, write_operations_report
from .transfer_analysis import (
    TransferAnalysisStore,
    build_weekly_report,
    collect_with_endpoint_profile,
    discover_network_requests,
    import_source_file,
    run_weekly_analysis,
)
from .workflow import (
    evaluate_review_workbook,
    finalize_topic_review_workbook,
    initial_label_from_workbook,
    publish_rows,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="answer-hub")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Create review_queue.xlsx from source workbook")
    ingest.add_argument("--source", required=True, help="Source workbook path")
    ingest.add_argument("--standards", default="", help="Standard catalog path (.xlsx or .json)")
    ingest.add_argument("--output-dir", required=True, help="Output directory")
    ingest.add_argument("--min-confidence", type=float, default=0.75, help="Minimum confidence for auto pass")
    ingest.add_argument("--product-type", default="", help="Only process one configured product type, such as 手机")
    ingest.add_argument("--rule-only", action="store_true", help="Do not call MiMo; generate rule-based candidates only")
    ingest.add_argument("--audit-db", default="", help="SQLite audit database path (default: ANSWER_HUB_DB_PATH)")

    finalize = subparsers.add_parser("finalize", help="Publish approved rows from cz review workbook")
    finalize.add_argument("--review-file", required=True, help="Annotated review workbook path")
    finalize.add_argument("--output-dir", required=True, help="Output directory")
    finalize.add_argument("--audit-db", default="", help="SQLite audit database path (default: ANSWER_HUB_DB_PATH)")

    finalize_topic = subparsers.add_parser(
        "finalize-topic",
        help="Export locally reviewed topic candidates for submission and training feedback",
    )
    finalize_topic.add_argument("--review-file", required=True, help="Annotated topic_review_queue.xlsx path")
    finalize_topic.add_argument("--output-dir", required=True, help="Output directory")

    evaluate = subparsers.add_parser("evaluate", help="Create a quality report from a cz-reviewed workbook")
    evaluate.add_argument("--review-file", required=True, help="Annotated review workbook path")
    evaluate.add_argument("--output-dir", required=True, help="Output directory")

    automate = subparsers.add_parser(
        "automate",
        help="Run the traceable conversation-to-review automation pipeline",
    )
    automate.add_argument("--source", required=True, help="Source workbook path")
    automate.add_argument(
        "--standards",
        default="",
        help="Optional standard catalog path; omit for case-only knowledge generation",
    )
    automate.add_argument(
        "--output-dir",
        default="outputs/automation-runs",
        help="Automation run root directory",
    )
    automate.add_argument(
        "--product-type",
        default="",
        help="Only process one configured product type; empty means all active product types",
    )
    automate.add_argument("--rule-only", action="store_true", help="Do not call MiMo")
    automate.add_argument(
        "--clustering-mode",
        choices=["direct_mimo", "semantic_mimo", "semantic", "rule"],
        default="direct_mimo",
    )
    automate.add_argument("--semantic-threshold", type=float, default=0.84)
    automate.add_argument("--cluster-review-floor", type=float, default=0.75)
    automate.add_argument("--cluster-auto-merge-threshold", type=float, default=0.92)
    automate.add_argument("--cluster-review-limit", type=int, default=100)

    automation_queue = subparsers.add_parser(
        "automation-queue",
        help="Process unattended source workbooks from a durable inbox queue",
    )
    automation_queue.add_argument(
        "--queue-dir",
        default="data/automation-queue",
        help="Queue root containing pending, processing, completed and failed folders",
    )
    automation_queue.add_argument(
        "--standards",
        default="",
        help="Optional standard catalog path; omit for case-only knowledge generation",
    )
    automation_queue.add_argument(
        "--output-dir",
        default="outputs/automation-runs",
        help="Automation run root directory",
    )
    automation_queue.add_argument("--product-type", default="")
    automation_queue.add_argument("--rule-only", action="store_true")
    automation_queue.add_argument(
        "--clustering-mode",
        choices=["direct_mimo", "semantic_mimo", "semantic", "rule"],
        default="direct_mimo",
    )
    automation_queue.add_argument("--semantic-threshold", type=float, default=0.84)
    automation_queue.add_argument("--cluster-review-floor", type=float, default=0.75)
    automation_queue.add_argument(
        "--cluster-auto-merge-threshold",
        type=float,
        default=0.92,
    )
    automation_queue.add_argument("--cluster-review-limit", type=int, default=100)
    automation_queue.add_argument(
        "--max-files",
        type=int,
        default=10,
        help="Maximum workbooks handled in one scheduled batch",
    )
    automation_queue.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also retry workbooks currently in the failed folder",
    )
    automation_queue.add_argument(
        "--sync-to-cz-review",
        "--submit-to-cz",
        dest="submit_to_cz",
        action="store_true",
        help=(
            "Model-review all candidates and sync them to the CZ candidate "
            "value-review queue; --submit-to-cz is kept as a compatibility alias"
        ),
    )
    automation_queue.add_argument(
        "--stale-after-seconds",
        type=int,
        default=7200,
        help="Recover stale processing files and stale runner locks after this age",
    )

    retry_run = subparsers.add_parser(
        "retry-run",
        help="Resume a failed automation run from its latest workflow checkpoint",
    )
    retry_run.add_argument("--run-id", required=True)
    retry_run.add_argument(
        "--output-dir",
        default="outputs/automation-runs",
        help="Automation run root directory",
    )

    operations_report = subparsers.add_parser(
        "operations-report",
        help="Summarize automation success, latency, fallback, model usage and SLA alerts",
    )
    operations_report.add_argument(
        "--output-dir",
        default="outputs/automation-runs",
        help="Automation run root directory",
    )
    operations_report.add_argument(
        "--output",
        default="outputs/operations/automation_metrics.json",
    )
    operations_report.add_argument("--limit", type=int, default=1000)

    retention = subparsers.add_parser(
        "retention-cleanup",
        help="Preview or execute cleanup of expired automation run directories",
    )
    retention.add_argument(
        "--output-dir",
        default="outputs/automation-runs",
        help="Automation run root directory",
    )
    retention.add_argument("--days", type=int, default=0)
    retention.add_argument(
        "--execute",
        action="store_true",
        help="Delete candidates; without this flag the command is a dry run",
    )

    transfer_discover = subparsers.add_parser(
        "transfer-discover",
        help="Open a logged-in browser and record sanitized JSON/XHR endpoint shapes",
    )
    transfer_discover.add_argument(
        "--system",
        required=True,
        choices=["manhattan", "baixiaosheng"],
    )
    transfer_discover.add_argument("--login-url", required=True)
    transfer_discover.add_argument("--output", required=True)
    transfer_discover.add_argument(
        "--profile-dir",
        default="",
        help="Persistent browser profile directory",
    )
    transfer_discover.add_argument("--timeout-seconds", type=int, default=900)

    transfer_collect = subparsers.add_parser(
        "transfer-collect",
        help="Import transfer data or collect it through a configured endpoint profile",
    )
    transfer_collect.add_argument(
        "--system",
        required=True,
        choices=["manhattan", "baixiaosheng"],
    )
    source_group = transfer_collect.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-file", help="Excel, CSV or JSON source file")
    source_group.add_argument(
        "--endpoint-profile",
        help="Configured Playwright endpoint profile JSON",
    )
    transfer_collect.add_argument("--start", default="")
    transfer_collect.add_argument("--end", default="")
    transfer_collect.add_argument(
        "--work-order-id",
        action="append",
        default=[],
        help="Work order to collect from Baixiaosheng; repeat as needed",
    )
    transfer_collect.add_argument(
        "--transfer-id",
        action="append",
        default=[],
        help="Manhattan transfer detail ID; repeat as needed",
    )
    transfer_collect.add_argument(
        "--show-browser",
        action="store_true",
        help="Show Chrome during API collection",
    )
    transfer_collect.add_argument(
        "--db",
        default="data/transfer_analysis.db",
    )

    transfer_analyze = subparsers.add_parser(
        "transfer-analyze",
        help="Sample, link, label and export one week of transfer-to-human sessions",
    )
    transfer_analyze.add_argument("--week-start", required=True, help="Monday in YYYY-MM-DD")
    transfer_analyze.add_argument("--standards", required=True)
    transfer_analyze.add_argument(
        "--output-dir",
        default="outputs/transfer-analysis",
    )
    transfer_analyze.add_argument("--sample-size", type=int, default=350)
    transfer_analyze.add_argument("--rule-only", action="store_true")
    transfer_analyze.add_argument("--manhattan-profile", default="")
    transfer_analyze.add_argument("--baixiaosheng-profile", default="")
    transfer_analyze.add_argument(
        "--db",
        default="data/transfer_analysis.db",
    )

    transfer_report = subparsers.add_parser(
        "transfer-report",
        help="Regenerate a weekly report from saved annotations and reviews",
    )
    transfer_report.add_argument("--week-start", required=True)
    transfer_report.add_argument("--output", required=True)
    transfer_report.add_argument(
        "--db",
        default="data/transfer_analysis.db",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        summary = initial_label_from_workbook(
            source_path=Path(args.source),
            standards_path=Path(args.standards) if args.standards else None,
            output_dir=Path(args.output_dir),
            min_confidence=args.min_confidence,
            product_type=args.product_type or None,
            use_mimo=not args.rule_only,
            audit_db_path=Path(args.audit_db) if args.audit_db else None,
        )
        print(summary)
        return 0

    if args.command == "finalize":
        summary = publish_rows(
            review_path=Path(args.review_file),
            output_dir=Path(args.output_dir),
            audit_db_path=Path(args.audit_db) if args.audit_db else None,
        )
        print(summary)
        return 0

    if args.command == "finalize-topic":
        summary = finalize_topic_review_workbook(
            review_path=Path(args.review_file),
            output_dir=Path(args.output_dir),
        )
        print(summary)
        return 0

    if args.command == "evaluate":
        summary = evaluate_review_workbook(
            review_path=Path(args.review_file),
            output_dir=Path(args.output_dir),
        )
        print(summary)
        return 0

    if args.command == "automate":
        manifest = run_automation_pipeline(
            source_path=Path(args.source),
            standards_path=Path(args.standards) if args.standards else None,
            output_root=Path(args.output_dir),
            product_type=args.product_type,
            use_mimo=not args.rule_only,
            clustering_mode=args.clustering_mode,
            semantic_threshold=args.semantic_threshold,
            cluster_review_floor=args.cluster_review_floor,
            cluster_auto_merge_threshold=args.cluster_auto_merge_threshold,
            cluster_review_limit=args.cluster_review_limit,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1 if manifest["status"] == "failed" else 0

    if args.command == "automation-queue":
        summary = process_automation_queue(
            queue_root=Path(args.queue_dir),
            standards_path=Path(args.standards) if args.standards else None,
            output_root=Path(args.output_dir),
            product_type=args.product_type,
            use_mimo=not args.rule_only,
            clustering_mode=args.clustering_mode,
            semantic_threshold=args.semantic_threshold,
            cluster_review_floor=args.cluster_review_floor,
            cluster_auto_merge_threshold=args.cluster_auto_merge_threshold,
            cluster_review_limit=args.cluster_review_limit,
            max_files=args.max_files,
            retry_failed=args.retry_failed,
            stale_after_seconds=args.stale_after_seconds,
            submit_to_cz=args.submit_to_cz,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["failed"] else 0

    if args.command == "retry-run":
        manifest = resume_automation_pipeline(
            Path(args.output_dir),
            args.run_id,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1 if manifest["status"] == "failed" else 0

    if args.command == "operations-report":
        manifests = list_automation_runs(
            Path(args.output_dir),
            limit=max(1, args.limit),
        )
        summary = write_operations_report(manifests, Path(args.output))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "retention-cleanup":
        summary = apply_retention_cleanup(
            Path(args.output_dir),
            retention_days=args.days or None,
            execute=args.execute,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "transfer-discover":
        profile_dir = (
            Path(args.profile_dir)
            if args.profile_dir
            else Path("data") / "browser_profiles" / args.system
        )
        summary = discover_network_requests(
            args.system,
            args.login_url,
            Path(args.output),
            profile_dir,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "transfer-collect":
        store = TransferAnalysisStore(Path(args.db))
        if args.source_file:
            summary = import_source_file(
                Path(args.source_file),
                args.system,
                store,
            )
        else:
            summary = collect_with_endpoint_profile(
                Path(args.endpoint_profile),
                store,
                start_date=args.start,
                end_date=args.end,
                work_order_ids=args.work_order_id,
                transfer_ids=args.transfer_id,
                headless=not args.show_browser,
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "transfer-analyze":
        store = TransferAnalysisStore(Path(args.db))
        summary = run_weekly_analysis(
            store,
            args.week_start,
            Path(args.standards),
            Path(args.output_dir),
            sample_size=args.sample_size,
            use_mimo=not args.rule_only,
            manhattan_profile=(
                Path(args.manhattan_profile) if args.manhattan_profile else None
            ),
            baixiaosheng_profile=(
                Path(args.baixiaosheng_profile)
                if args.baixiaosheng_profile
                else None
            ),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "transfer-report":
        store = TransferAnalysisStore(Path(args.db))
        summary = build_weekly_report(
            store,
            args.week_start,
            Path(args.output),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
