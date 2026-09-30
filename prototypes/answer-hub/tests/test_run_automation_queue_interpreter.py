"""``run_automation_queue.ps1`` 的 Python 解释器探测回归测试。

背景：脚本原来只判断 ``<ProjectRoot>\\.venv\\Scripts\\python.exe`` 是否存在，不存在就
直接拿 PATH 上的 ``python.exe``。Windows 上的 ``python.exe`` 往往只是 Microsoft Store
的「应用执行别名」存根：执行后只打印一句提示并以 9009 退出（在 Windows PowerShell 5.1
下还会因为 ``$ErrorActionPreference = "Stop"`` 变成终止错误，连日志都来不及写）。
于是脚本静默什么都不做就结束、日志 0 字节，而
``POST /api/v1/automation/retry-failed`` 仍然返回 HTTP 202，看起来像成功。

这些用例只用 ``-CheckInterpreter`` 跑「解释器探测」这一段，**不会执行任何队列处理**，
也不会碰 ``data/automation-queue``。测试优先用计划任务和 Answer Hub API 真正使用的
Windows PowerShell 5.1（``powershell.exe``），没有时才退回 ``pwsh.exe``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_automation_queue.ps1"
ANSWER_HUB_ROOT = SCRIPT.parents[1]
STORE_STUB = Path(
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe")
)

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="run_automation_queue.ps1 只由 Windows 计划任务和 Windows 上的 Answer Hub 调用。",
)


def _powershell() -> str:
    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("没有可用的 PowerShell。")


def _run_check_interpreter(
    project_root: Path, **env_overrides: str | None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for key in (
        "ANSWER_HUB_PYTHON",
        "ANSWER_HUB_PYTHON_FALLBACKS",
        "ANSWER_HUB_RUNTIME_DIR",
    ):
        environment.pop(key, None)
    for key, value in env_overrides.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ProjectRoot",
            str(project_root),
            "-CheckInterpreter",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=300,
    )


def _log_text(project_root: Path) -> str:
    log_dir = project_root / "outputs" / "automation-logs"
    logs = sorted(log_dir.glob("queue-*.log")) if log_dir.is_dir() else []
    return logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""


def test_script_is_saved_as_utf8_with_bom() -> None:
    """脚本必须带 UTF-8 BOM。

    Windows PowerShell 5.1（计划任务和 Answer Hub API 用的就是它）会把「无 BOM 的
    UTF-8」当 ANSI 读：中文提示变乱码，个别汉字的字节还会吃掉后面的引号，直接导致
    语法错误、脚本一行都不执行。去掉 BOM 等于重新引入「静默空跑」。
    """
    assert SCRIPT.read_bytes()[:3] == b"\xef\xbb\xbf"


def test_usable_interpreter_passes_the_probe() -> None:
    """显式指定的可用解释器必须被接受，并以退出码 0 结束探测。"""
    completed = _run_check_interpreter(
        ANSWER_HUB_ROOT, ANSWER_HUB_PYTHON=sys.executable
    )
    assert completed.returncode == 0, completed.stderr
    assert sys.executable in completed.stdout
    assert "未执行任何队列处理" in completed.stdout


def test_unusable_explicit_interpreter_fails_loudly(tmp_path: Path) -> None:
    """存在但不是可用解释器的文件必须被识破，并显式失败。"""
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"not a real executable")
    completed = _run_check_interpreter(tmp_path, ANSWER_HUB_PYTHON=str(fake_python))

    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert "找不到可用的 Python 解释器" in completed.stderr
    assert "ANSWER_HUB_PYTHON" in completed.stderr
    assert "校验失败" in completed.stderr
    # 失败原因必须写进页面「查看运行日志」读的那个文件，不能只留在 stderr（调用方丢弃 stderr）。
    assert "退出码：3" in _log_text(tmp_path)


def test_every_candidate_unusable_lists_what_was_tried(tmp_path: Path) -> None:
    """全部候选都不可用时，错误信息要说清找过哪些路径、怎么办，并以 3 退出。"""
    fake_path = tmp_path / "fake-path"
    fake_path.mkdir()
    (fake_path / "python.exe").write_bytes(b"not a real executable")
    completed = _run_check_interpreter(
        tmp_path,
        ANSWER_HUB_PYTHON=None,
        ANSWER_HUB_PYTHON_FALLBACKS=str(tmp_path / "nope.exe"),
        ANSWER_HUB_RUNTIME_DIR=str(tmp_path / "no-runtime"),
        PATH=str(fake_path),
    )

    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert "已按顺序探测全部候选" in completed.stderr
    assert "文件不存在" in completed.stderr
    assert "ANSWER_HUB_PYTHON_FALLBACKS" in completed.stderr
    assert "PATH 上的 python.exe" in completed.stderr
    assert "setx ANSWER_HUB_PYTHON" in completed.stderr


@pytest.mark.skipif(
    not STORE_STUB.is_file(), reason="本机没有 Microsoft Store 的 python.exe 存根。"
)
def test_microsoft_store_stub_is_skipped(tmp_path: Path) -> None:
    """PATH 上的 Store 存根必须被跳过，而不是被当成可用解释器执行。"""
    completed = _run_check_interpreter(
        tmp_path,
        ANSWER_HUB_PYTHON=None,
        ANSWER_HUB_PYTHON_FALLBACKS=None,
        ANSWER_HUB_RUNTIME_DIR=str(tmp_path / "no-runtime"),
        PATH=str(STORE_STUB.parent),
    )

    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert "应用执行别名存根" in completed.stderr
