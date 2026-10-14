"""退池运维脚本可独立执行的回归测试（不需要数据库、不访问网络）。

背景：``backend/scripts/retire_session_work_orders.py`` 是给运维在无法改镜像时用的
审计入口（``docker cp`` 进容器再 ``python`` 执行）。应用进程由 ``app.main`` 预先导入
了全部模型，脚本却只导入 ``app.models.blind_labeling``，于是单独执行时 SQLAlchemy
配置 mapper 会失败：

    sqlalchemy.exc.InvalidRequestError: When initializing mapper
    Mapper[BlindLabelBatch(blind_label_batches)], expression 'User' failed to
    locate a name ("name 'User' is not defined") ...

2026-10-14 上线时正是靠临时包一层 ``import app.main`` 才跑通 dry-run。这里用一个干净
的子解释器复现该场景：导入脚本模块后立刻 ``configure_mappers()``，只要脚本漏掉
relationship 目标模型就会失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_DIR / "scripts" / "retire_session_work_orders.py"


def test_script_can_configure_mappers_in_a_clean_interpreter():
    """脚本自身必须注册完 relationship 目标模型，才能被单独执行。"""

    assert SCRIPT_PATH.is_file(), f"退池脚本不存在：{SCRIPT_PATH}"
    code = (
        "import runpy\n"
        "import sqlalchemy.orm as orm\n"
        f"runpy.run_path({str(SCRIPT_PATH)!r}, run_name='retire_script_probe')\n"
        "orm.configure_mappers()\n"
        "print('mapper ok')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "单独执行退池脚本时 ORM mapper 配置失败：\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "mapper ok" in result.stdout


def test_script_exposes_dry_run_by_default():
    """``--help`` 必须能在没有数据库的环境里跑通，且默认不带 ``--apply``。"""

    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--apply" in result.stdout
    assert "dry-run" in result.stdout
