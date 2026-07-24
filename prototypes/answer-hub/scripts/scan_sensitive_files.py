from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


SKIP_DIRS = {
    ".git",
    ".venv",
    ".cz_test_venv",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "outputs",
    "handoff",
    ".codex_stage",
    ".codex_tmp_sheet",
}
FORBIDDEN_NAMES = {".env", "cookies.json", "secrets.toml"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log"}
SECRET_PATTERNS = {
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Bearer token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
TEXT_SUFFIXES = {
    ".py",
    ".ps1",
    ".cmd",
    ".json",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".html",
    ".js",
    ".css",
    ".txt",
}


def _is_local_runtime_path(relative_parts: tuple[str, ...]) -> bool:
    if not relative_parts:
        return False
    if relative_parts[0] in {"data", "backups"}:
        return True
    if (
        len(relative_parts) == 1
        and re.fullmatch(
            r"streamlit-.+-(?:stdout|stderr)\.log",
            relative_parts[0],
            flags=re.IGNORECASE,
        )
    ):
        return True
    return (
        len(relative_parts) >= 5
        and relative_parts[0:2] == ("tools", "dify")
        and relative_parts[3:5] == ("docker", "volumes")
    )


def scan(
    root: Path,
    *,
    ignore_local_env: bool = False,
    ignore_local_runtime: bool = False,
) -> list[str]:
    issues: list[str] = []
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in relative_parts):
            continue
        if ignore_local_runtime and _is_local_runtime_path(relative_parts):
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if (
            path.name in FORBIDDEN_NAMES
            and not (ignore_local_env and path.name == ".env")
        ) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append(f"forbidden artifact: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                issues.append(f"{label}: {relative}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--ignore-local-env", action="store_true")
    parser.add_argument("--ignore-local-runtime", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    issues = scan(
        root,
        ignore_local_env=args.ignore_local_env,
        ignore_local_runtime=args.ignore_local_runtime,
    )
    if issues:
        print("\n".join(issues))
        return 1
    print("Sensitive artifact scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
