"""Safe local controls for the Answer Hub Windows automation task."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess
import subprocess
from typing import Any


AUTOMATION_TASK_NAME = "AnswerHubAutomationQueue"


class AutomationTaskControlError(RuntimeError):
    """Raised when Windows Task Scheduler cannot complete a requested action."""


CommandRunner = Callable[..., CompletedProcess[str]]
RetryLauncher = Callable[..., Any]


def _run_command(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        **kwargs,
    )


def _start_retry(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=creation_flags,
        **kwargs,
    )


def _command_error(result: CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "任务计划程序未返回详细错误。").strip()


def _status_value(output: str, *keys: str) -> str:
    normalized_keys = {key.casefold() for key in keys}
    for raw_line in output.splitlines():
        if ":" not in raw_line:
            continue
        raw_key, value = raw_line.split(":", 1)
        if raw_key.strip().casefold() in normalized_keys:
            return value.strip()
    return ""


class AutomationTaskController:
    """Restricts scheduler operations to the known Answer Hub task name."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _run_command,
        retry_launcher: RetryLauncher = _start_retry,
        task_name: str = AUTOMATION_TASK_NAME,
    ) -> None:
        if task_name != AUTOMATION_TASK_NAME:
            raise ValueError("只允许控制 Answer Hub 自动化计划任务。")
        self._runner = runner
        self._retry_launcher = retry_launcher
        self.task_name = task_name

    def status(self) -> dict[str, Any]:
        command = ["schtasks.exe", "/Query", "/TN", self.task_name, "/FO", "LIST", "/V"]
        try:
            result = self._runner(command)
        except OSError as exc:
            raise AutomationTaskControlError(f"无法读取计划任务状态：{exc}") from exc
        if result.returncode != 0:
            return {
                "task_name": self.task_name,
                "installed": False,
                "enabled": False,
                "running": False,
                "message": "自动化计划任务尚未安装。",
            }

        output = result.stdout or ""
        task_state = _status_value(output, "Scheduled Task State", "计划任务状态")
        runtime_state = _status_value(output, "Status", "状态")
        state = task_state.casefold()
        runtime = runtime_state.casefold()
        enabled = not any(value in state for value in ("disabled", "已禁用", "已停用"))
        running = any(value in runtime for value in ("running", "正在运行"))
        if not enabled:
            message = "计划任务已暂停，不会自动扫描新任务。"
        elif running:
            message = "计划任务已启用，当前正在扫描或处理队列。"
        else:
            message = "计划任务已启用，等待下一次计划扫描。"
        return {
            "task_name": self.task_name,
            "installed": True,
            "enabled": enabled,
            "running": running,
            "message": message,
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if not enabled:
            # Disabling a scheduled task only prevents future triggers; it does
            # not stop a scanner that is already running. End the current task
            # first so the next explicit run starts a fresh process.
            try:
                current = self.status()
            except AutomationTaskControlError:
                current = {"installed": False, "running": False}
            if current.get("installed") and current.get("running"):
                self._run_scheduler(["schtasks.exe", "/End", "/TN", self.task_name])
        action = "/Enable" if enabled else "/Disable"
        result = self._run_scheduler(["schtasks.exe", "/Change", "/TN", self.task_name, action])
        return {
            "enabled": enabled,
            "message": "已启用自动化计划任务，将创建新的扫描进程。" if enabled else "已停止当前自动化进程并暂停计划任务。",
        }

    def run_now(self) -> dict[str, str]:
        self._run_scheduler(["schtasks.exe", "/Run", "/TN", self.task_name])
        return {"message": "已请求立即执行自动化队列扫描。"}

    def retry_failed(self, project_root: Path) -> dict[str, str]:
        script = project_root / "scripts" / "run_automation_queue.ps1"
        if not script.is_file():
            raise AutomationTaskControlError(f"未找到失败重试脚本：{script}")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ProjectRoot",
            str(project_root),
            "-RetryFailed",
        ]
        try:
            self._retry_launcher(command, cwd=str(project_root))
        except OSError as exc:
            raise AutomationTaskControlError(f"无法启动失败任务重试：{exc}") from exc
        return {"message": "已启动失败任务重试扫描，请在运行记录中查看结果。"}

    def _run_scheduler(self, command: list[str]) -> CompletedProcess[str]:
        try:
            result = self._runner(command)
        except OSError as exc:
            raise AutomationTaskControlError(f"无法调用 Windows 计划任务：{exc}") from exc
        if result.returncode != 0:
            raise AutomationTaskControlError(_command_error(result))
        return result


def read_automation_log_tail(project_root: Path, *, lines: int = 80) -> dict[str, str]:
    """Return a small, recent log tail without exposing configuration files."""

    log_dir = project_root / "outputs" / "automation-logs"
    log_paths = sorted(log_dir.glob("queue-*.log")) if log_dir.is_dir() else []
    if not log_paths:
        return {"name": "", "content": "暂时没有自动化队列日志。"}
    log_path = log_paths[-1]
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"name": log_path.name, "content": f"无法读取运行日志：{exc}"}
    return {
        "name": log_path.name,
        "content": "\n".join(content.splitlines()[-max(1, lines) :]) or "日志文件暂时为空。",
    }
