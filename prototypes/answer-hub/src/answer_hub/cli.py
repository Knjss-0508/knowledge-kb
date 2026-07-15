from __future__ import annotations

import argparse
from pathlib import Path
import sys

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
    ingest.add_argument("--product-type", default="", help="Only process one product type, such as 手机")
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

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
