from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    output_dir = project_root / "outputs" / "cluster-v2-test"
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "embedding_map.stdout.log"
    stderr_path = output_dir / "embedding_map.stderr.log"
    pid_path = output_dir / "embedding_map.pid"

    command = [
        str(project_root / ".venv" / "Scripts" / "python.exe"),
        str(project_root / "scripts" / "map_atomic_standards.py"),
        "--atomic-json",
        str(output_dir / "atomic_topic_clusters.json"),
        "--standards-json",
        str(output_dir / "qc_standard_catalog_4_categories.json"),
        "--review-xlsx",
        str(output_dir / "workbooks" / "新版主题聚类_74原子知识点_审核.xlsx"),
        "--output-json",
        str(output_dir / "atomic_standard_candidates_embedding.json"),
        "--top-k",
        "5",
    ]
    creation_flags = (
        subprocess.CREATE_NO_WINDOW
        | subprocess.CREATE_NEW_PROCESS_GROUP
    )
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w",
        encoding="utf-8",
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
            close_fds=False,
        )
    pid_path.write_text(str(process.pid), encoding="ascii")
    print(process.pid)


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("This launcher is only intended for Windows.")
    main()
