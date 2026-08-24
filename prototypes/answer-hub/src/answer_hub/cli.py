from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .automation import (
    automation_run_succeeded,
    list_automation_runs,
    resume_automation_pipeline,
    run_automation_pipeline,
)
from .automation_queue import process_automation_queue, retry_cz_candidate_sync
from .operations import apply_retention_cleanup, write_operations_report
from .second_part_pull import (
    SecondPartPullError,
    pull_second_part_to_queue,
)
from .topic_label_model import (
    predict_topic_labels_from_json,
    train_topic_label_model,
)
from .workflow import (
    evaluate_review_workbook,
    finalize_topic_review_workbook,
    initial_label_from_workbook,
    publish_rows,
)


def _cli_progress_printer():
    seen_messages: set[str] = set()

    def callback(manifest: dict) -> None:
        stages = list(manifest.get("stages") or [])
        stage = next(
            (item for item in stages if item.get("status") == "running"),
            None,
        )
        if stage is None:
            stage = next(
                (
                    item
                    for item in reversed(stages)
                    if item.get("status") not in {"pending", ""}
                ),
                {},
            )
        message = (
            f"[{manifest.get('updated_at', '')}] "
            f"{stage.get('label', manifest.get('status_label', '运行状态'))} "
            f"{stage.get('status', manifest.get('status', ''))} - "
            f"{stage.get('detail', manifest.get('error', ''))}"
        )
        if message in seen_messages:
            return
        seen_messages.add(message)
        print(message, file=sys.stderr, flush=True)

    return callback


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
        "--cluster-only",
        action="store_true",
        help="Only validate clustering; skip topic value, transcription, and content review",
    )
    automate.add_argument(
        "--max-source-rows",
        type=int,
        default=None,
        help="Only with --cluster-only: process at most this many source rows for a throughput trial",
    )
    automate.add_argument(
        "--clustering-mode",
        choices=["direct_mimo", "semantic_mimo", "semantic", "rule"],
        default="direct_mimo",
    )
    automate.add_argument("--semantic-threshold", type=float, default=0.84)
    automate.add_argument("--cluster-review-floor", type=float, default=0.75)
    automate.add_argument("--cluster-auto-merge-threshold", type=float, default=0.92)
    automate.add_argument("--cluster-review-limit", type=int, default=100)
    automate.add_argument(
        "--continue-on-mimo-unavailable",
        action="store_true",
        help="When MiMo preflight fails, continue with explicit rule fallback generation",
    )
    automate.add_argument(
        "--direct-mimo-progress",
        default="",
        help="Reuse an existing direct_mimo_progress.json to avoid repeating atomic extraction",
    )
    automate.add_argument(
        "--cluster-media-policy",
        choices=["always", "on_demand", "never"],
        default=None,
        help=(
            "Control image/video input during direct clustering. "
            "cluster-only defaults to never; full runs use MIMO_CLUSTER_MEDIA_POLICY."
        ),
    )

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
        "--continue-on-mimo-unavailable",
        action="store_true",
        help="Retry queued workbooks with explicit rule fallback when MiMo is unavailable",
    )
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

    second_part_pull = subparsers.add_parser(
        "second-part-pull",
        help="Pull new records from a configured second-part JSON API into the automation queue",
    )
    second_part_pull.add_argument(
        "--profile",
        required=True,
        help="Second-part pull profile JSON path",
    )
    second_part_pull.add_argument(
        "--queue-dir",
        default="data/automation-queue",
    )
    second_part_pull.add_argument(
        "--output-dir",
        default="outputs/automation-runs",
    )
    second_part_pull.add_argument(
        "--state-file",
        default="data/second-part-pull/state.json",
    )
    second_part_pull.add_argument(
        "--max-pages",
        type=int,
        default=10,
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
    retry_run.add_argument(
        "--allow-interrupted-running",
        action="store_true",
        help="Resume a run left in running status after Ctrl+C or terminal interruption",
    )

    retry_cz_sync = subparsers.add_parser(
        "retry-cz-sync",
        help="Retry only CZ candidate value-review sync for a completed local run",
    )
    retry_cz_sync.add_argument("--run-id", required=True)
    retry_cz_sync.add_argument(
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

    train_topic_label = subparsers.add_parser(
        "train-topic-label-model",
        help=(
            "Train an offline topic category and knowledge-value classifier "
            "from human review labels or an explicitly allowed pseudo-label JSON"
        ),
    )
    train_topic_label.add_argument(
        "--source",
        required=True,
        help=(
            "Human-reviewed .xlsx or topic_stage_predictions.json"
        ),
    )
    train_topic_label.add_argument(
        "--output-dir",
        required=True,
        help="Directory for model.npz, metadata.json and training_report.json",
    )
    train_topic_label.add_argument(
        "--allow-pseudo-labels",
        action="store_true",
        help=(
            "Explicitly allow MiMo predictions as experimental training labels; "
            "the resulting model always requires human review"
        ),
    )
    train_topic_label.add_argument("--hash-size", type=int, default=8192)
    train_topic_label.add_argument("--ngram-min", type=int, default=1)
    train_topic_label.add_argument("--ngram-max", type=int, default=3)
    train_topic_label.add_argument("--alpha", type=float, default=1.0)
    train_topic_label.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.72,
    )
    train_topic_label.add_argument(
        "--pseudo-min-confidence",
        type=float,
        default=0.72,
        help=(
            "Discard MiMo pseudo labels below this teacher confidence; "
            "non-uncertain pseudo labels marked for human review are also discarded"
        ),
    )
    train_topic_label.add_argument(
        "--allow-upstream-risk-pseudo-labels",
        action="store_true",
        help=(
            "Explicitly include pseudo labels whose source topic carries "
            "an upstream review-risk flag; resulting predictions still "
            "require human review"
        ),
    )
    train_topic_label.add_argument("--seed", type=int, default=42)

    predict_topic_label = subparsers.add_parser(
        "predict-topic-label-model",
        help=(
            "Predict topic category and knowledge value from a themes JSON "
            "without copying source text into the output"
        ),
    )
    predict_topic_label.add_argument("--model-dir", required=True)
    predict_topic_label.add_argument("--source", required=True)
    predict_topic_label.add_argument("--output", required=True)

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
        if args.max_source_rows is not None:
            if args.max_source_rows < 1:
                parser.error("--max-source-rows 必须是正整数")
            if not args.cluster_only:
                parser.error("--max-source-rows 只能与 --cluster-only 一起使用")
        progress_callback = _cli_progress_printer()
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
            continue_on_mimo_unavailable=args.continue_on_mimo_unavailable,
            cluster_only=args.cluster_only,
            source_row_limit=args.max_source_rows,
            direct_mimo_progress_path=(
                Path(args.direct_mimo_progress)
                if args.direct_mimo_progress
                else None
            ),
            cluster_media_policy=args.cluster_media_policy,
            progress_callback=progress_callback,
        )
        if (
            manifest.get("status") == "needs_confirmation"
            and not args.continue_on_mimo_unavailable
            and sys.stdin.isatty()
        ):
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            answer = input(
                "MiMo API 不可用。是否继续用规则兜底生成待人工审核结果？输入 y 继续，其他键停止："
            )
            if answer.strip().lower() in {"y", "yes"}:
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
                    continue_on_mimo_unavailable=True,
                    cluster_only=args.cluster_only,
                    source_row_limit=args.max_source_rows,
                    direct_mimo_progress_path=(
                        Path(args.direct_mimo_progress)
                        if args.direct_mimo_progress
                        else None
                    ),
                    cluster_media_policy=args.cluster_media_policy,
                    progress_callback=progress_callback,
                )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0 if automation_run_succeeded(manifest) else 1

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
            continue_on_mimo_unavailable=args.continue_on_mimo_unavailable,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["failed"] else 0

    if args.command == "second-part-pull":
        try:
            summary = pull_second_part_to_queue(
                args.profile,
                queue_root=Path(args.queue_dir),
                output_root=Path(args.output_dir),
                state_path=Path(args.state_file),
                max_pages=max(1, args.max_pages),
            )
        except SecondPartPullError as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "retry-run":
        manifest = resume_automation_pipeline(
            Path(args.output_dir),
            args.run_id,
            allow_interrupted_running=args.allow_interrupted_running,
            progress_callback=_cli_progress_printer(),
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1 if manifest["status"] == "failed" else 0

    if args.command == "retry-cz-sync":
        manifest = retry_cz_candidate_sync(
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

    if args.command == "train-topic-label-model":
        try:
            summary = train_topic_label_model(
                Path(args.source),
                Path(args.output_dir),
                allow_pseudo_labels=args.allow_pseudo_labels,
                hash_size=args.hash_size,
                ngram_min=args.ngram_min,
                ngram_max=args.ngram_max,
                alpha=args.alpha,
                confidence_threshold=args.confidence_threshold,
                pseudo_min_confidence=args.pseudo_min_confidence,
                allow_upstream_risk_pseudo_labels=(
                    args.allow_upstream_risk_pseudo_labels
                ),
                seed=args.seed,
            )
        except (OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "predict-topic-label-model":
        try:
            summary = predict_topic_labels_from_json(
                Path(args.model_dir),
                Path(args.source),
                Path(args.output),
            )
        except (OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["failed_count"] else 0

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
