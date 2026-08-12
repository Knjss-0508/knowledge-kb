from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _prepare_launcher(
    tmp_path: Path,
) -> tuple[Path, dict[str, str], Path]:
    workspace = tmp_path / "workspace"
    scripts_dir = workspace / "scripts"
    cz_root = workspace / "cz-knowledge-kb" / "knowledge-kb-master"
    fake_bin = tmp_path / "fake-bin"
    scripts_dir.mkdir(parents=True)
    cz_root.mkdir(parents=True)
    fake_bin.mkdir()

    shutil.copy2(
        PROJECT_ROOT / "scripts" / "start_local_cz.ps1",
        scripts_dir / "start_local_cz.ps1",
    )
    (cz_root / ".env").write_text(
        "INTEGRATION_API_KEY=test-only-placeholder\n",
        encoding="utf-8",
    )

    docker_calls = tmp_path / "docker-calls.txt"
    (fake_bin / "docker.cmd").write_text(
        (
            "@echo off\r\n"
            '>>"%FAKE_DOCKER_CALLS%" echo %BACKEND_PORT%^|%*\r\n'
            "exit /b 0\r\n"
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_DOCKER_CALLS"] = str(docker_calls)
    env.pop("BACKEND_PORT", None)
    return scripts_dir / "start_local_cz.ps1", env, docker_calls


def _run_launcher(
    tmp_path: Path,
    *arguments: str,
) -> list[str]:
    script_path, env, docker_calls = _prepare_launcher(tmp_path)

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            *arguments,
        ],
        cwd=script_path.parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return docker_calls.read_text(encoding="utf-8").splitlines()


def test_local_cz_launcher_includes_database_and_cpu_embedding_overrides(
    tmp_path: Path,
) -> None:
    calls = _run_launcher(tmp_path)

    assert calls == [
        (
            "8801|compose -f docker-compose.yml "
            "-f docker-compose.local.yml "
            "-f docker-compose.embedding-cpu.yml up -d --build"
        ),
        (
            "8801|compose -f docker-compose.yml "
            "-f docker-compose.local.yml "
            "-f docker-compose.embedding-cpu.yml ps"
        ),
    ]


def test_local_cz_launcher_preserves_gpu_and_no_build_options(
    tmp_path: Path,
) -> None:
    calls = _run_launcher(tmp_path, "-Embedding", "gpu", "-NoBuild")

    assert calls == [
        (
            "8801|compose -f docker-compose.yml "
            "-f docker-compose.local.yml "
            "-f docker-compose.embedding-gpu.yml up -d"
        ),
        (
            "8801|compose -f docker-compose.yml "
            "-f docker-compose.local.yml "
            "-f docker-compose.embedding-gpu.yml ps"
        ),
    ]


def test_local_cz_launcher_restores_existing_backend_port(
    tmp_path: Path,
) -> None:
    script_path, env, _ = _prepare_launcher(tmp_path)
    env["BACKEND_PORT"] = "9123"
    escaped_script_path = str(script_path).replace("'", "''")

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                f"& '{escaped_script_path}'; "
                "Write-Output ('AFTER_BACKEND_PORT=' + $env:BACKEND_PORT)"
            ),
        ],
        cwd=script_path.parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "AFTER_BACKEND_PORT=9123" in result.stdout
