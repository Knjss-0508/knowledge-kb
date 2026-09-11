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


def test_linux_status_uses_systemd_timer() -> None:
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["systemctl", "show", "answer-hub-queue.timer"]:
            return CompletedProcess(command, 0, stdout="loaded\n", stderr="")
        if command[:3] == ["systemctl", "is-enabled", "answer-hub-queue.timer"]:
            return CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if command[:3] == ["systemctl", "is-active", "answer-hub-queue.service"]:
            return CompletedProcess(command, 3, stdout="inactive\n", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    controller = AutomationTaskController(runner=runner, platform_name="posix")

    result = controller.status()

    assert result == {
        "task_name": "AnswerHubAutomationQueue",
        "installed": True,
        "enabled": True,
        "running": False,
        "message": "计划任务已启用，等待下一次计划扫描。",
    }
    assert all(command[0] != "schtasks.exe" for command in calls)


def test_linux_enable_and_run_use_systemd_units() -> None:
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["systemctl", "show", "answer-hub-queue.timer"]:
            return CompletedProcess(command, 0, stdout="loaded\n", stderr="")
        if command[:3] == ["systemctl", "is-enabled", "answer-hub-queue.timer"]:
            return CompletedProcess(command, 1, stdout="disabled\n", stderr="")
        if command[:3] == ["systemctl", "is-active", "answer-hub-queue.service"]:
            return CompletedProcess(command, 3, stdout="inactive\n", stderr="")
        return CompletedProcess(command, 0, stdout="", stderr="")

    controller = AutomationTaskController(runner=runner, platform_name="posix")

    result = controller.set_enabled(True)
    run_result = controller.run_now()

    assert result["enabled"] is True
    assert run_result["message"] == "已请求立即执行自动化队列扫描。"
    assert [
        ["systemctl", "show", "answer-hub-queue.timer", "--property=LoadState", "--value"],
        ["systemctl", "is-enabled", "answer-hub-queue.timer"],
        ["systemctl", "is-active", "answer-hub-queue.service"],
        ["systemctl", "enable", "--now", "answer-hub-queue.timer"],
        ["systemctl", "start", "--no-block", "answer-hub-queue.service"],
    ] == calls
