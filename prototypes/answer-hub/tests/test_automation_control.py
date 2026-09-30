import locale
from subprocess import CompletedProcess

import pytest

from answer_hub import automation_control
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


# 中文 Windows 上 schtasks.exe 的真实输出（代码页 936，字段名与取值都是中文）。
# 「已禁用」说明任务处于 Disabled，必须解析成 enabled=False。
GBK_DISABLED_OUTPUT = (
    "文件夹: \\\r\n"
    "主机名:                             WIN-LOCALHOST\r\n"
    "任务名:                             \\AnswerHubAutomationQueue\r\n"
    "模式:                               已禁用\r\n"
    "登录状态:                           只使用交互方式\r\n"
    "上次运行时间:                       1999/11/30 0:00:00\r\n"
    "计划任务状态:                       已禁用\r\n"
)

# 控制台输出代码页是 65001 时，schtasks.exe 改用英文并输出 UTF-8。
UTF8_DISABLED_OUTPUT = (
    "\r\n"
    "Folder: \\\r\n"
    "TaskName:                             \\AnswerHubAutomationQueue\r\n"
    "Status:                               Disabled\r\n"
    "Scheduled Task State:                 Disabled\r\n"
    "Task To Run:                          wscript.exe \"E:\\项目目录\\run_hidden.vbs\"\r\n"
)


class _FakeSchTasksProcess:
    """最小复刻 subprocess.run 的 text/encoding 行为，把真实原始字节交给被测代码。"""

    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self._returncode = returncode

    def __call__(
        self,
        command,
        *,
        text: bool = False,
        encoding: str | None = None,
        errors: str | None = None,
        **_kwargs,
    ) -> CompletedProcess:
        if text or encoding:
            decoding = encoding or locale.getpreferredencoding(False)
            return CompletedProcess(
                command,
                self._returncode,
                self._stdout.decode(decoding, errors or "strict"),
                "",
            )
        return CompletedProcess(command, self._returncode, self._stdout, b"")


def _fake_schtasks(monkeypatch: pytest.MonkeyPatch, payload: bytes, console_encoding: str) -> None:
    """让被测代码在真实原始字节上运行；raising=False 便于在修复前的旧版本上跑出断言失败。"""

    monkeypatch.setattr(automation_control.subprocess, "run", _FakeSchTasksProcess(payload))
    monkeypatch.setattr(
        automation_control,
        "_console_output_encoding",
        lambda: console_encoding,
        raising=False,
    )


def test_gbk_schtasks_output_reports_disabled_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """GBK（cp936）中文输出里的「计划任务状态: 已禁用」必须读成 enabled=False。"""

    _fake_schtasks(monkeypatch, GBK_DISABLED_OUTPUT.encode("gbk"), "cp936")

    status = automation_control.AutomationTaskController().status()

    assert status["installed"] is True
    assert status["enabled"] is False
    assert status["running"] is False
    assert status["message"] == "计划任务已暂停，不会自动扫描新任务。"


def test_utf8_english_schtasks_output_reports_disabled_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UTF-8 英文输出（控制台代码页 65001）同样必须读成 enabled=False。"""

    _fake_schtasks(monkeypatch, UTF8_DISABLED_OUTPUT.encode("utf-8"), "cp65001")

    status = automation_control.AutomationTaskController().status()

    assert status["installed"] is True
    assert status["enabled"] is False
    assert status["running"] is False
    assert status["message"] == "计划任务已暂停，不会自动扫描新任务。"
