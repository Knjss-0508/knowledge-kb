from datetime import date
import json

import pytest

from answer_hub.automation_schedule import (
    commit_success,
    prepare_window,
    read_plan,
    record_deferral,
    record_failure,
    resume_plan,
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


def test_deferral_is_not_a_failure_and_never_pauses_the_cursor(tmp_path):
    """队列没清干净只是「本轮跳过」，不能累积失败次数、不能暂停自动顺延。

    线上故障：队列里一直留着没清理的文件，定时脚本每个晚上都调用 failure，
    连续三晚之后 auto_advance_paused 变成 True，prepare 直接抛错，
    脚本再也走不到队列检查，游标永远停在原地，页面那条
    「最近失败：队列存在未解决的 failed 文件。」也永远清不掉。
    """
    path = tmp_path / "automation-plan.json"
    write_plan(path, {"cursor_date": "2026-10-08", "window_days": 1})
    for _ in range(3):
        record_deferral(path, run_date="2026-10-11", reason="队列存在未解决的 failed 文件。")
    plan = read_plan(path)
    assert plan["last_deferral_reason"] == "队列存在未解决的 failed 文件。"
    assert plan["consecutive_failure_count"] == 0
    assert plan["auto_advance_paused"] is False
    assert plan["cursor_date"] == "2026-10-08"
    # 跳过之后仍可正常 prepare，脚本下一次照样能跑到队列检查。
    assert prepare_window(path, today=date(2026, 10, 12))["from_date"] == "2026-10-08"


def test_resume_clears_the_pause_and_keeps_the_cursor(tmp_path):
    """恢复顺延只清记录类字段，游标与进度字段一个都不能动。"""
    path = tmp_path / "automation-plan.json"
    write_plan(path, {"cursor_date": "2026-10-08", "window_days": 1})
    record_failure(path, run_date="2026-10-11", reason="队列存在未解决的 failed 文件。")
    assert read_plan(path)["auto_advance_paused"] is False
    for _ in range(2):
        record_failure(path, run_date="2026-10-11", reason="队列存在未解决的 failed 文件。")
    paused = read_plan(path)
    assert paused["auto_advance_paused"] is True
    with pytest.raises(RuntimeError, match="自动顺延已暂停"):
        prepare_window(path, today=date(2026, 10, 12))

    resume_plan(path)

    resumed = read_plan(path)
    assert resumed["auto_advance_paused"] is False
    assert resumed["consecutive_failure_count"] == 0
    assert resumed["last_failure_reason"] == ""
    assert resumed["last_failure_date"] == ""
    assert resumed["last_deferral_reason"] == ""
    # 游标必须停在原位：恢复的是顺延权限，不是重新拉过的日期。
    assert resumed["cursor_date"] == "2026-10-08"
    assert resumed["last_successful_to_date"] == paused["last_successful_to_date"]
    assert prepare_window(path, today=date(2026, 10, 12))["from_date"] == "2026-10-08"


def test_success_clears_a_previous_deferral_note(tmp_path):
    """跑成功一次之后，「本次未执行」的旧提示不能再挂着。"""
    path = tmp_path / "automation-plan.json"
    write_plan(path, {"cursor_date": "2026-10-08", "window_days": 1})
    record_deferral(path, run_date="2026-10-11", reason="队列存在未解决的 pending 文件。")
    assert read_plan(path)["last_deferral_reason"]
    commit_success(path, from_date="2026-10-08", to_date="2026-10-08")
    plan = read_plan(path)
    assert plan["last_deferral_reason"] == ""
    assert plan["last_deferral_date"] == ""
