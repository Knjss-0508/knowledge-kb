"""按保留期清理“检索事件”，人工反馈记录永久保留。

策略（与用户确认）：
- 默认只保留最近 90 天；
- 只删除纯检索事件（feedback_type 为空或 'none'）；
- 所有人工反馈记录（helpful / unhelpful）以及其它带反馈类型的记录一律不删。

用法：
  python prune_retrieval_events.py --dry-run     # 只统计，不删除
  python prune_retrieval_events.py               # 实际执行
  RETENTION_DAYS=120 ... 或 --days 120           # 自定义保留天数
"""
import argparse
import os
import sys
import time

from sqlalchemy import create_engine, text

BATCH = 5000


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    parser.add_argument("--days", type=int, default=int(os.environ.get("RETENTION_DAYS", "90")))
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        log("错误：DATABASE_URL 未设置，已中止")
        return 2

    engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    with engine.connect() as conn:
        host = conn.execute(text("SELECT current_database()")).scalar()
        now = conn.execute(text("SELECT now()")).scalar()
        cutoff = conn.execute(
            text("SELECT now() - (:d || ' days')::interval"), {"d": args.days}
        ).scalar()
        total_before = conn.execute(text("SELECT count(*) FROM retrieval_quality_events")).scalar()
        oldest = conn.execute(text("SELECT min(created_at) FROM retrieval_quality_events")).scalar()
        log(f"库={host} 当前时间={now} 保留天数={args.days} 截止时间={cutoff}")
        log(f"清理前：总行数={total_before} 最早事件={oldest}")

        pending = conn.execute(
            text(
                "SELECT count(*) FROM retrieval_quality_events "
                "WHERE created_at < :cutoff AND (feedback_type IS NULL OR feedback_type = 'none')"
            ),
            {"cutoff": cutoff},
        ).scalar()
        log(f"超过保留期的纯检索事件：{pending} 行（人工反馈记录不在清理范围内）")

        if args.dry_run:
            log("dry-run 模式：未做任何删除")
            return 0
        if not pending:
            log("无需清理")
            return 0

        deleted = 0
        while True:
            result = conn.execute(
                text(
                    "DELETE FROM retrieval_quality_events WHERE id IN ("
                    "  SELECT id FROM retrieval_quality_events"
                    "  WHERE created_at < :cutoff"
                    "    AND (feedback_type IS NULL OR feedback_type = 'none')"
                    "  ORDER BY created_at LIMIT :batch)"
                ),
                {"cutoff": cutoff, "batch": BATCH},
            )
            n = result.rowcount or 0
            deleted += n
            if n:
                log(f"  已删除 {n} 行（累计 {deleted}）")
            if n < BATCH:
                break

        total_after = conn.execute(text("SELECT count(*) FROM retrieval_quality_events")).scalar()
        oldest_after = conn.execute(text("SELECT min(created_at) FROM retrieval_quality_events")).scalar()
        kept_feedback = conn.execute(
            text("SELECT count(*) FROM retrieval_quality_events WHERE feedback_type IS NOT NULL AND feedback_type <> 'none'")
        ).scalar()
        log(f"清理完成：共删除 {deleted} 行；剩余 {total_after} 行，最早事件={oldest_after}")
        log(f"人工反馈记录保留：{kept_feedback} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
