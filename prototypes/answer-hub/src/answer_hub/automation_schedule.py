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
        "consecutive_failure_count": 0,
        "auto_advance_paused": False,
    })
    return write_plan(path, plan)


def record_failure(path: str | Path, *, run_date: str, reason: str) -> dict[str, Any]:
    plan = read_plan(path)
    count = int(plan.get("consecutive_failure_count") or 0) + 1
    plan.update({
        "last_failure_date": run_date,
        "last_failure_reason": str(reason)[:512],
        "consecutive_failure_count": count,
        "auto_advance_paused": count >= 3,
    })
    return write_plan(path, plan)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "commit", "failure"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--today", default="")
    parser.add_argument("--from-date", default="")
    parser.add_argument("--to-date", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    if args.action == "prepare":
        today = _parse_date(args.today) if args.today else None
        result = prepare_window(args.plan, today=today)
    elif args.action == "commit":
        result = commit_success(args.plan, from_date=args.from_date, to_date=args.to_date)
    else:
        result = record_failure(args.plan, run_date=args.today or _date_text(date.today()), reason=args.reason)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
