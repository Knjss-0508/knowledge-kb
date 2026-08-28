from subprocess import CompletedProcess

from answer_hub.automation_control import AutomationTaskController


def _runner_with_status(calls, *, running: bool):
    def runner(command, **_kwargs):
        calls.append(command)
        if command[1] == "/Query":
            status = "Running" if running else "Ready"
            output = f"Scheduled Task State: Enabled\nStatus: {status}\n"
            return CompletedProcess(command, 0, stdout=output, stderr="")
        return CompletedProcess(command, 0, stdout="", stderr="")

    return runner


def test_disabling_running_task_ends_it_before_disabling() -> None:
    calls = []
    controller = AutomationTaskController(
        runner=_runner_with_status(calls, running=True),
    )

    result = controller.set_enabled(False)

    assert result["enabled"] is False
    assert calls == [
        ["schtasks.exe", "/Query", "/TN", "AnswerHubAutomationQueue", "/FO", "LIST", "/V"],
        ["schtasks.exe", "/End", "/TN", "AnswerHubAutomationQueue"],
        ["schtasks.exe", "/Change", "/TN", "AnswerHubAutomationQueue", "/Disable"],
    ]


def test_disabling_idle_task_does_not_issue_end() -> None:
    calls = []
    controller = AutomationTaskController(
        runner=_runner_with_status(calls, running=False),
    )

    controller.set_enabled(False)

    assert calls[-1] == [
        "schtasks.exe", "/Change", "/TN", "AnswerHubAutomationQueue", "/Disable"
    ]
    assert all("/End" not in call for call in calls)
