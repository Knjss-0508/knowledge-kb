import json
from pathlib import Path

from answer_hub.automation_api import create_automation_api_app


class _Controller:
    def __init__(self):
        self.enabled_calls = []

    def status(self):
        return {"installed": True, "enabled": False, "running": False, "task_name": "AnswerHubAutomationQueue"}

    def set_enabled(self, value):
        self.enabled_calls.append(value)
        return {"enabled": value, "message": "ok"}

    def run_now(self):
        return {"message": "ok"}

    def retry_failed(self, _project_root: Path):
        return {"message": "ok"}


def test_schedule_switch_controls_scheduler_without_disabling_manual_runs(tmp_path):
    controller = _Controller()
    app = create_automation_api_app(
        api_key="test-key",
        task_controller=controller,
        project_root=tmp_path,
        automation_plan_path=tmp_path / "automation-plan.json",
    )
    client = app.test_client()
    response = client.patch(
        "/api/v1/automation/control",
        headers={"X-Answer-Hub-Key": "test-key"},
        json={
            "enabled": True,
            "schedule_enabled": False,
            "schedule_time": "02:00",
            "schedule_frequency": "daily",
            "window_days": 1,
            "cursor_date": "2026-09-17",
            "catchup_enabled": True,
        },
    )
    assert response.status_code == 200
    assert controller.enabled_calls == [False]
    payload = response.get_json()
    assert payload["control"]["enabled"] is True
    assert payload["control"]["schedule_enabled"] is False
    assert payload["control"]["plan"]["cursor_date"] == "2026-09-17"
    assert payload["control"]["plan"]["window_days"] == 1


def test_cursor_only_update_keeps_derived_date_range_populated(tmp_path):
    controller = _Controller()
    plan_path = tmp_path / "automation-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "enabled": False,
                "knowledge_settle_from_date": "2026-09-01",
                "knowledge_settle_to_date": "2026-09-07",
                "second_part_query_from_date": "2026-09-01",
                "second_part_query_to_date": "2026-09-07",
                "cursor_date": "2026-09-01",
            }
        ),
        encoding="utf-8",
    )
    app = create_automation_api_app(
        api_key="test-key",
        task_controller=controller,
        project_root=tmp_path,
        automation_plan_path=plan_path,
    )
    client = app.test_client()

    response = client.patch(
        "/api/v1/automation/control",
        headers={"X-Answer-Hub-Key": "test-key"},
        json={
            "enabled": True,
            "schedule_enabled": False,
            "schedule_time": "02:00",
            "schedule_frequency": "daily",
            "window_days": 1,
            "cursor_date": "2026-09-08",
            "catchup_enabled": True,
        },
    )

    assert response.status_code == 200
    plan = response.get_json()["control"]["plan"]
    assert plan["cursor_date"] == "2026-09-08"
    assert plan["knowledge_settle_from_date"] == "2026-09-08"
    assert plan["knowledge_settle_to_date"] == "2026-09-08"


def _client_with_plan(tmp_path, plan: dict) -> tuple:
    controller = _Controller()
    plan_path = tmp_path / "automation-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    app = create_automation_api_app(
        api_key="test-key",
        task_controller=controller,
        project_root=tmp_path,
        automation_plan_path=plan_path,
    )
    return app.test_client(), plan_path


def test_control_snapshot_exposes_the_deferral_note_separately(tmp_path):
    """「本次跳过」必须与「最近失败」分开返回，否则页面上分不清是跳过还是故障。"""
    client, _ = _client_with_plan(
        tmp_path,
        {
            "enabled": True,
            "cursor_date": "2026-10-08",
            "window_days": 1,
            "last_deferral_date": "2026-10-11",
            "last_deferral_reason": "队列存在未解决的 failed 文件。",
            "last_failure_reason": "",
            "consecutive_failure_count": 0,
        },
    )
    plan = client.get(
        "/api/v1/automation/control", headers={"X-Answer-Hub-Key": "test-key"}
    ).get_json()["plan"]
    assert plan["last_deferral_reason"] == "队列存在未解决的 failed 文件。"
    assert plan["last_deferral_date"] == "2026-10-11"
    assert plan["last_failure_reason"] == ""
    assert plan["auto_advance_paused"] is False


def test_resume_endpoint_clears_the_pause_without_moving_the_cursor(tmp_path):
    """恢复入口必须同时清掉失败记录与暂停标记，并且不动游标。"""
    client, plan_path = _client_with_plan(
        tmp_path,
        {
            "enabled": True,
            "cursor_date": "2026-10-08",
            "window_days": 1,
            "last_successful_to_date": "2026-10-04",
            "last_failure_date": "2026-10-11",
            "last_failure_reason": "队列存在未解决的 failed 文件。",
            "consecutive_failure_count": 3,
            "auto_advance_paused": True,
        },
    )
    response = client.post(
        "/api/v1/automation/resume", headers={"X-Answer-Hub-Key": "test-key"}
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "resumed"
    plan = response.get_json()["control"]["plan"]
    assert plan["auto_advance_paused"] is False
    assert plan["consecutive_failure_count"] == 0
    assert plan["last_failure_reason"] == ""
    assert plan["cursor_date"] == "2026-10-08"
    assert plan["last_successful_to_date"] == "2026-10-04"
    # 写盘也要生效：下一次 prepare 不能再因为暂停而抛错。
    from answer_hub.automation_schedule import prepare_window

    assert prepare_window(plan_path)["from_date"] == "2026-10-08"


def test_resume_endpoint_requires_the_api_key(tmp_path):
    client, _ = _client_with_plan(tmp_path, {"enabled": True, "cursor_date": "2026-10-08"})
    assert client.post("/api/v1/automation/resume").status_code == 401


class _PausedScheduleController:
    """整体启用、但定时任务已暂停：手动执行应当仍然放行。"""

    def __init__(self) -> None:
        self.run_now_calls = 0

    def status(self):
        return {
            "installed": True,
            "enabled": True,
            "running": False,
            "task_name": "AnswerHubAutomationQueue",
            "message": "计划任务已暂停，不会自动扫描新任务。",
        }

    def set_enabled(self, value):
        return {"enabled": value, "message": "ok"}

    def run_now(self):
        self.run_now_calls += 1
        return {"message": "已临时启用并请求立即执行；定时任务仍保持暂停。"}

    def retry_failed(self, _project_root: Path):
        return {"message": "ok"}


def test_manual_run_allowed_while_daily_schedule_is_off(tmp_path):
    controller = _PausedScheduleController()
    plan_path = tmp_path / "automation-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "schedule_enabled": False,
                "cursor_date": "2026-10-08",
                "window_days": 1,
            }
        ),
        encoding="utf-8",
    )
    app = create_automation_api_app(
        api_key="test-key",
        task_controller=controller,
        project_root=tmp_path,
        automation_plan_path=plan_path,
    )
    client = app.test_client()

    response = client.post(
        "/api/v1/automation/runs", headers={"X-Answer-Hub-Key": "test-key"}
    )

    assert response.status_code == 202
    assert controller.run_now_calls == 1
