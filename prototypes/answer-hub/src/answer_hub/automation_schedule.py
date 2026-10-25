"""Persistent date-window planning for scheduled Answer Hub runs."""

from __future__ import annotations

from datetime import date, timedelta
import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_WINDOW_DAYS = 1
DEFAULT_MAX_CATCHUP_DAYS = 7
DEFAULT_FREQUENCY = "daily"
FREQUENCIES = {"daily", "weekly"}


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _date_text(value: date | None) -> str:
    return value.isoformat() if value else ""


def normalize_plan(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    frequency = str(raw.get("schedule_frequency") or DEFAULT_FREQUENCY).strip().lower()
    if frequency not in FREQUENCIES:
        frequency = DEFAULT_FREQUENCY
    window_days = max(1, min(int(raw.get("window_days") or DEFAULT_WINDOW_DAYS), 31))
    max_catchup_days = max(1, min(int(raw.get("max_catchup_days") or DEFAULT_MAX_CATCHUP_DAYS), 31))
    cursor = _parse_date(raw.get("cursor_date"))
    initial_from = _parse_date(raw.get("initial_from_date"))
    if cursor is None:
        cursor = initial_from or _parse_date(raw.get("knowledge_settle_from_date"))
    if cursor is None:
        cursor = _parse_date(raw.get("second_part_query_from_date"))
    if cursor is None:
        cursor = date.today()
    weekday = int(raw.get("schedule_weekday") if str(raw.get("schedule_weekday") or "").isdigit() else 0)
    weekday = max(0, min(6, weekday))
    plan = {
        **raw,
        "schedule_frequency": frequency,
        "schedule_weekday": weekday,
        "window_days": window_days,
        "catchup_enabled": bool(raw.get("catchup_enabled", True)),
        "max_catchup_days": max_catchup_days,
        "cursor_date": _date_text(cursor),
        "initial_from_date": _date_text(initial_from) or _date_text(cursor),
        "last_successful_from_date": str(raw.get("last_successful_from_date") or ""),
        "last_successful_to_date": str(raw.get("last_successful_to_date") or ""),
        "last_failure_date": str(raw.get("last_failure_date") or ""),
        "last_failure_reason": str(raw.get("last_failure_reason") or ""),
        # 区分「真失败」与「本次主动跳过」。
        #
        # 队列里还留着未处理的 pending/processing/failed 文件时，定时脚本按设计跳过这一
        # 次并记下原因。但那不是失败：没有任何环节出错，只是队列还没被清理，本次不拉取。
        # 旧实现把这条也交给 record_failure()，于是「连续三个晚上队列没清干净」被当成
        # 「连续三次失败」，auto_advance_paused 被置为 true；之后 prepare_window() 每次
        # 直接抛错退出，脚本再也走不到队列检查，游标永远停在原地 —— 页面那条「最近失败：
        # 队列存在未解决的 failed 文件。」也就永远清不掉。
        # 两种记录分开保存，只有真失败才累积次数并暂停顺延。
        "last_deferral_date": str(raw.get("last_deferral_date") or ""),
        "last_deferral_reason": str(raw.get("last_deferral_reason") or ""),
        "consecutive_failure_count": max(0, int(raw.get("consecutive_failure_count") or 0)),
        "auto_advance_paused": bool(raw.get("auto_advance_paused", False)),
    }
    current_from = _parse_date(plan["cursor_date"]) or date.today()
    current_to = current_from + timedelta(days=window_days - 1)
    plan["next_run_from_date"] = _date_text(current_from)
    plan["next_run_to_date"] = _date_text(current_to)
    plan["knowledge_settle_from_date"] = plan["next_run_from_date"]
    plan["knowledge_settle_to_date"] = plan["next_run_to_date"]
    plan["second_part_query_from_date"] = plan["next_run_from_date"]
    plan["second_part_query_to_date"] = plan["next_run_to_date"]
    return plan


def read_plan(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path)
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return normalize_plan(payload if isinstance(payload, dict) else {})


def write_plan(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    plan_path = Path(path)
    plan = normalize_plan(payload)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(plan_path)
    return plan


def prepare_window(path: str | Path, *, today: date | None = None) -> dict[str, Any]:
    plan = read_plan(path)
    current = today or date.today()
    if plan.get("auto_advance_paused"):
        raise RuntimeError("自动顺延已暂停，请先处理失败批次并恢复计划。")
    if plan.get("schedule_frequency") == "weekly" and current.weekday() != int(plan["schedule_weekday"]):
        raise RuntimeError("本次不是计划的每周运行日。")
    from_date = _parse_date(plan["cursor_date"]) or current
    to_date = from_date + timedelta(days=int(plan["window_days"]) - 1)
    return {
        "from_date": _date_text(from_date),
        "to_date": _date_text(to_date),
        "window_days": int(plan["window_days"]),
        "schedule_frequency": plan["schedule_frequency"],
        "schedule_weekday": int(plan["schedule_weekday"]),
        "consecutive_failure_count": int(plan["consecutive_failure_count"]),
    }


def commit_success(path: str | Path, *, from_date: str, to_date: str) -> dict[str, Any]:
    plan = read_plan(path)
    end = _parse_date(to_date)
    if end is None:
        raise ValueError("to_date 必须是 YYYY-MM-DD。")
    plan.update({
        "cursor_date": _date_text(end + timedelta(days=1)),
        "last_successful_from_date": from_date,
        "last_successful_to_date": to_date,
        "last_failure_date": "",
        "last_failure_reason": "",
        "last_deferral_date": "",
        "last_deferral_reason": "",
        "consecutive_failure_count": 0,
        "auto_advance_paused": False,
    })
    return write_plan(path, plan)


def record_failure(path: str | Path, *, run_date: str, reason: str) -> dict[str, Any]:
    """记录一次真实失败：累积失败次数，连续三次后暂停自动顺延。"""
    plan = read_plan(path)
    count = int(plan.get("consecutive_failure_count") or 0) + 1
    plan.update({
        "last_failure_date": run_date,
        "last_failure_reason": str(reason)[:512],
        "consecutive_failure_count": count,
        "auto_advance_paused": count >= 3,
    })
    return write_plan(path, plan)


def record_deferral(path: str | Path, *, run_date: str, reason: str) -> dict[str, Any]:
    """记录一次「本轮主动跳过」：队列还没清干净，本次不拉取。

    跳过不是失败，所以不动 consecutive_failure_count，也不会暂停自动顺延；
    只把「本次为什么没跑」写进独立字段，页面照常展示，用户清完队列即自动恢复。
    """
    plan = read_plan(path)
    plan.update({
        "last_deferral_date": run_date,
        "last_deferral_reason": str(reason)[:512],
    })
    return write_plan(path, plan)


def resume_plan(path: str | Path) -> dict[str, Any]:
    """恢复被暂停的自动顺延：清掉失败/跳过记录与暂停标记，游标保持不动。

    用户在页面上处理完失败批次后调用。只清记录类字段，cursor_date 等进度字段一律不碰，
    否则会重复拉取或跳过日期。
    """
    plan = read_plan(path)
    plan.update({
        "last_failure_date": "",
        "last_failure_reason": "",
        "last_deferral_date": "",
        "last_deferral_reason": "",
        "consecutive_failure_count": 0,
        "auto_advance_paused": False,
    })
    return write_plan(path, plan)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("prepare", "commit", "failure", "defer", "resume", "log-skipped-window"),
    )
    # log-skipped-window 只写运行记录，不需要计划文件。
    parser.add_argument("--plan", default="")
    parser.add_argument("--today", default="")
    parser.add_argument("--from-date", default="")
    parser.add_argument("--to-date", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--fetched-records", type=int, default=0)
    args = parser.parse_args()
    if args.action == "log-skipped-window":
        if not args.output_dir:
            raise SystemExit("log-skipped-window 需要 --output-dir")
        from .automation import AutomationRunStore

        manifest = AutomationRunStore(args.output_dir).record_skipped_window(
            from_date=args.from_date,
            to_date=args.to_date,
            reason=args.reason
            or f"第二部分在 {args.from_date} 至 {args.to_date} 没有返回任何数据，已跳过。",
            fetched_records=args.fetched_records,
        )
        print(json.dumps({"run_id": manifest["run_id"], "status": manifest["status"], "recorded": True}, ensure_ascii=False))
        return 0
    if not args.plan:
        raise SystemExit(f"{args.action} 需要 --plan")
    if args.action == "prepare":
        today = _parse_date(args.today) if args.today else None
        result = prepare_window(args.plan, today=today)
    elif args.action == "commit":
        result = commit_success(args.plan, from_date=args.from_date, to_date=args.to_date)
    elif args.action == "failure":
        result = record_failure(args.plan, run_date=args.today or _date_text(date.today()), reason=args.reason)
    elif args.action == "defer":
        result = record_deferral(args.plan, run_date=args.today or _date_text(date.today()), reason=args.reason)
    else:
        result = resume_plan(args.plan)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
