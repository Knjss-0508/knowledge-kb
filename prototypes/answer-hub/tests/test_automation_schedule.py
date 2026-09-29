from datetime import date
import json

import pytest

from answer_hub.automation_schedule import (
    commit_success,
    prepare_window,
    read_plan,
    record_failure,
    write_plan,
)


def test_success_moves_cursor_by_window_days(tmp_path):
    path = tmp_path / "automation-plan.json"
    write_plan(path, {"cursor_date": "2026-09-17", "window_days": 3})
    window = prepare_window(path, today=date(2026, 9, 24))
    assert window["from_date"] == "2026-09-17"
    assert window["to_date"] == "2026-09-19"
    commit_success(path, from_date=window["from_date"], to_date=window["to_date"])
    assert read_plan(path)["cursor_date"] == "2026-09-20"


def test_failure_does_not_move_cursor_and_pauses_after_three_failures(tmp_path):
    path = tmp_path / "automation-plan.json"
    write_plan(path, {"cursor_date": "2026-09-17", "window_days": 1})
    for _ in range(3):
        record_failure(path, run_date="2026-09-24", reason="接口超时")
    plan = read_plan(path)
    assert plan["cursor_date"] == "2026-09-17"
    assert plan["auto_advance_paused"] is True
    with pytest.raises(RuntimeError, match="自动顺延已暂停"):
        prepare_window(path, today=date(2026, 9, 24))


def test_weekly_plan_only_runs_on_configured_weekday(tmp_path):
    path = tmp_path / "automation-plan.json"
    write_plan(path, {"cursor_date": "2026-09-21", "schedule_frequency": "weekly", "schedule_weekday": 0})
    with pytest.raises(RuntimeError, match="每周运行日"):
        prepare_window(path, today=date(2026, 9, 24))
    assert prepare_window(path, today=date(2026, 9, 28))["from_date"] == "2026-09-21"
