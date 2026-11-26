"""汇总隔离环境中生成并经人工评审的助手最终答复，不调用生成模型。"""

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.services.blind_label_answer_replay import (
    AnswerReplayManifest,
    build_review_references,
    run_answer_replay,
)
from scripts._blind_replay_io import local_database, read_manifest, readonly_engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="本地脱敏答复与人工判定清单 JSON")
    parser.add_argument("--confirm-local-sanitized", action="store_true",
                        help="确认使用非生产隧道的隔离副本，且清单已脱敏")
    parser.add_argument("--review-references", action="store_true",
                        help="输出含脱敏预期要点的私有评审参考，而非统计报告；仅保存到受控本地文件")
    args = parser.parse_args()
    if not args.confirm_local_sanitized:
        parser.error("请先确认隔离环境和清单脱敏，再传 --confirm-local-sanitized")
    try:
        database = local_database()
    except (ArgumentError, ValueError):
        parser.error("请配置有效的 BLIND_REPLAY_DATABASE_URL（仅接受本机 IP）。")
    payload, manifest = read_manifest(parser, args.manifest, AnswerReplayManifest)

    engine = readonly_engine(database)
    try:
        with Session(engine, autoflush=False) as db:
            evaluate = build_review_references if args.review_references else run_answer_replay
            report = evaluate(db, manifest)
    except SQLAlchemyError:
        parser.error("隔离数据库连接或查询失败，未输出 SQL 或输入内容。")
    finally:
        engine.dispose()

    report["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
