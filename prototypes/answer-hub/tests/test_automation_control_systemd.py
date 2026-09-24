from subprocess import CompletedProcess

from answer_hub.automation_control import AutomationTaskController


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
    assert controller.status()["enabled"] is True
    assert all(command[0] != "schtasks.exe" for command in calls)


def test_linux_enable_and_run_use_whitelisted_systemctl() -> None:
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
    controller.set_enabled(True)
    controller.run_now()
    assert ["sudo", "-n", "/usr/bin/systemctl", "enable", "--now", "answer-hub-queue.timer"] in calls
    assert ["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "answer-hub-queue.service"] in calls
