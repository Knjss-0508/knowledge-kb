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
        },
    )
    assert response.status_code == 200
    assert controller.enabled_calls == [False]
    payload = response.get_json()
    assert payload["control"]["enabled"] is True
    assert payload["control"]["schedule_enabled"] is False
