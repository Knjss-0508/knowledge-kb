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
