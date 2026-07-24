from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "outputs"
PACKAGE_NAME = "聚类标注工具_回家版_20260723"
PACKAGE_ROOT = OUTPUT_ROOT / PACKAGE_NAME
ARCHIVE_PATH = OUTPUT_ROOT / f"{PACKAGE_NAME}.zip"
SOURCE_DATA = (
    PROJECT_ROOT
    / "outputs"
    / "cluster-full-current-379"
    / "cluster_titles.json"
)
SOURCE_DATABASE = (
    PROJECT_ROOT
    / "outputs"
    / "cluster-full-current-379"
    / "cluster_annotations.db"
)
PYTHON_HOME = Path(sys.base_prefix).resolve()
VENV_SITE_PACKAGES = (
    Path(sys.prefix).resolve() / "Lib" / "site-packages"
)


ANNOTATION_APP = """from __future__ import annotations

from pathlib import Path
import sys

import streamlit as st


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from answer_hub.cluster_annotation_ui import render_cluster_annotation


st.set_page_config(
    page_title="完整聚类标注工具",
    page_icon=":material/fact_check:",
    layout="wide",
)
render_cluster_annotation()
"""


BACKUP_SCRIPT = """from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from answer_hub.cluster_annotation import (
    ClusterAnnotationStore,
    annotation_csv_bytes,
    annotation_export_rows,
    load_cluster_payload,
)


data_path = ROOT / "outputs" / "cluster-full-current-379" / "cluster_titles.json"
database_path = ROOT / "outputs" / "cluster-full-current-379" / "cluster_annotations.db"
backup_root = ROOT / "标注结果备份"
backup_root.mkdir(parents=True, exist_ok=True)

store = ClusterAnnotationStore(database_path)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_database = backup_root / f"cluster_annotations_{timestamp}.db"

with sqlite3.connect(database_path) as source:
    with sqlite3.connect(backup_database) as target:
        source.backup(target)

payload = load_cluster_payload(data_path)
annotations = store.list_all()
rows = annotation_export_rows(payload["clusters"], annotations)
(backup_root / f"完整聚类人工标注_{timestamp}.csv").write_bytes(
    annotation_csv_bytes(rows)
)
(backup_root / f"完整聚类人工标注_{timestamp}.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"备份完成：{backup_root}")
print(f"已保存标注：{len(annotations)} 个主题簇")
"""


START_COMMAND = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 完整聚类标注工具

if not exist "runtime\python.exe" (
  echo 找不到便携运行环境：runtime\python.exe
  echo 请完整解压压缩包后再运行，不要只复制启动脚本。
  pause
  exit /b 1
)

echo 正在启动完整聚类标注工具……
echo 浏览器未自动打开时，请访问：http://127.0.0.1:8501
echo 标注期间请不要关闭本窗口。
echo.

set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
"runtime\python.exe" -m streamlit run "annotation_app.py" ^
  --server.address 127.0.0.1 ^
  --server.port 8501 ^
  --server.headless false ^
  --server.fileWatcherType none ^
  --browser.gatherUsageStats false

if errorlevel 1 (
  echo.
  echo 启动失败，请保留本窗口并截图错误信息。
  pause
)
"""


BACKUP_COMMAND = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 备份聚类标注结果

if not exist "runtime\python.exe" (
  echo 找不到便携运行环境。
  pause
  exit /b 1
)

set PYTHONUTF8=1
"runtime\python.exe" "backup_annotations.py"
echo.
echo 请将“标注结果备份”文件夹或整个工具文件夹带回公司。
pause
"""


README = """完整聚类标注工具（回家便携版）
================================

一、怎么启动
------------
1. 必须先完整解压 ZIP，不能直接在压缩包内运行。
2. 双击“启动聚类标注工具.cmd”。
3. 浏览器通常会自动打开。
4. 如果没有自动打开，访问：http://127.0.0.1:8501
5. 标注期间不要关闭黑色命令窗口；关闭窗口即停止本地服务。

二、数据保存在哪里
------------------
标注会自动保存到：

outputs\\cluster-full-current-379\\cluster_annotations.db

浏览器刷新或电脑重启不会丢失已经保存的标注。

三、回公司前怎么备份
--------------------
1. 关闭或暂停标注。
2. 双击“备份标注结果.cmd”。
3. 将新生成的“标注结果备份”文件夹带回公司。
4. 最稳妥的方式是将整个“聚类标注工具_回家版_20260723”文件夹带回。

四、当前标注规则
----------------
1. 同一簇成员能由同一篇知识准确回答：归簇判断选“正确”。
2. 成员主题不同：归簇判断选“错误”，处理动作选“拆分”。
3. 两个成员互不相同：保留一个，移出一个。
4. 三个成员互不相同：保留一个，移出两个。
5. 标题错误时，“人工主题标题”只写保留下来主题的一个标题。
6. 被移出成员的新标题暂时写在备注中，格式建议：
   原子ID -> 正确主题标题
7. “排除”表示不进入知识库，不会删除原始聊天或 JSON。

五、图片和视频
--------------
图片、视频使用原始网络链接，家里需要联网才能展示。
MiMo 的媒体分析结果已经写入数据，本工具不会重新调用 MiMo。

六、注意事项
------------
本工具包含完整聊天内容及内部媒体链接，请按公司数据安全要求保存：
- 不要上传个人网盘或公开聊天软件。
- 不要转发给无关人员。
- 使用经过授权的电脑和存储设备。
"""


STREAMLIT_CONFIG = """[browser]
gatherUsageStats = false

[server]
address = "127.0.0.1"
fileWatcherType = "none"
headless = false
"""


def _ensure_safe_target(path: Path) -> None:
    output_root = OUTPUT_ROOT.resolve()
    resolved = path.resolve()
    if output_root != resolved and output_root not in resolved.parents:
        raise RuntimeError(f"目标目录不在 outputs 内：{resolved}")


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyo",
            ".pytest_cache",
        ),
    )


def _copy_runtime(target: Path) -> None:
    runtime_root = target / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    for filename in (
        "python.exe",
        "pythonw.exe",
        "python3.dll",
        "python314.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "LICENSE.txt",
    ):
        shutil.copy2(PYTHON_HOME / filename, runtime_root / filename)
    _copy_tree(PYTHON_HOME / "DLLs", runtime_root / "DLLs")
    _copy_tree(PYTHON_HOME / "Lib", runtime_root / "Lib")
    _copy_tree(
        VENV_SITE_PACKAGES,
        runtime_root / "Lib" / "site-packages",
    )


def _copy_database(target: Path) -> None:
    destination = (
        target
        / "outputs"
        / "cluster-full-current-379"
        / "cluster_annotations.db"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not SOURCE_DATABASE.is_file():
        destination.touch()
        return
    with sqlite3.connect(SOURCE_DATABASE) as source:
        with sqlite3.connect(destination) as output:
            source.backup(output)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_archive(source_root: Path, archive_path: Path) -> None:
    if archive_path.exists():
        archive_path.unlink()
    with ZipFile(
        archive_path,
        "w",
        compression=ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(source_root.rglob("*")):
            if path.is_file():
                archive.write(
                    path,
                    path.relative_to(source_root.parent),
                )


def build() -> dict[str, object]:
    if not SOURCE_DATA.is_file():
        raise FileNotFoundError(f"找不到聚类数据：{SOURCE_DATA}")
    if not PYTHON_HOME.is_dir():
        raise FileNotFoundError(f"找不到 Python 运行环境：{PYTHON_HOME}")
    if not VENV_SITE_PACKAGES.is_dir():
        raise FileNotFoundError(
            f"找不到项目依赖目录：{VENV_SITE_PACKAGES}"
        )

    _ensure_safe_target(PACKAGE_ROOT)
    _ensure_safe_target(ARCHIVE_PATH)
    if PACKAGE_ROOT.exists():
        shutil.rmtree(PACKAGE_ROOT)
    PACKAGE_ROOT.mkdir(parents=True)

    _copy_runtime(PACKAGE_ROOT)
    _copy_tree(
        PROJECT_ROOT / "src" / "answer_hub",
        PACKAGE_ROOT / "src" / "answer_hub",
    )
    data_target = (
        PACKAGE_ROOT
        / "outputs"
        / "cluster-full-current-379"
        / "cluster_titles.json"
    )
    data_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_DATA, data_target)
    _copy_database(PACKAGE_ROOT)

    (PACKAGE_ROOT / "annotation_app.py").write_text(
        ANNOTATION_APP,
        encoding="utf-8",
    )
    (PACKAGE_ROOT / "backup_annotations.py").write_text(
        BACKUP_SCRIPT,
        encoding="utf-8",
    )
    (PACKAGE_ROOT / "启动聚类标注工具.cmd").write_text(
        START_COMMAND,
        encoding="utf-8-sig",
    )
    (PACKAGE_ROOT / "备份标注结果.cmd").write_text(
        BACKUP_COMMAND,
        encoding="utf-8-sig",
    )
    (PACKAGE_ROOT / "使用说明.txt").write_text(
        README,
        encoding="utf-8-sig",
    )
    config_root = PACKAGE_ROOT / ".streamlit"
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "config.toml").write_text(
        STREAMLIT_CONFIG,
        encoding="utf-8",
    )

    payload = json.loads(SOURCE_DATA.read_text(encoding="utf-8"))
    version_info = {
        "package_name": PACKAGE_NAME,
        "built_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "python_version": sys.version,
        "streamlit_version": "1.59.2",
        "cluster_count": len(payload.get("clusters") or []),
        "atomic_unit_count": len(payload.get("atomic_units") or []),
        "source_data": str(SOURCE_DATA.relative_to(PROJECT_ROOT)),
        "source_data_sha256": _sha256(SOURCE_DATA),
        "annotation_database_included": SOURCE_DATABASE.is_file(),
    }
    (PACKAGE_ROOT / "版本信息.json").write_text(
        json.dumps(version_info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _write_archive(PACKAGE_ROOT, ARCHIVE_PATH)
    archive_sha256 = _sha256(ARCHIVE_PATH)
    checksum_path = OUTPUT_ROOT / f"{PACKAGE_NAME}_SHA256.txt"
    checksum_path.write_text(
        f"{archive_sha256}  {ARCHIVE_PATH.name}\n",
        encoding="utf-8",
    )

    return {
        **version_info,
        "package_directory": str(PACKAGE_ROOT),
        "package_size_mb": round(
            sum(
                path.stat().st_size
                for path in PACKAGE_ROOT.rglob("*")
                if path.is_file()
            )
            / 1024
            / 1024,
            2,
        ),
        "archive_path": str(ARCHIVE_PATH),
        "archive_size_mb": round(
            ARCHIVE_PATH.stat().st_size / 1024 / 1024,
            2,
        ),
        "archive_sha256": archive_sha256,
        "checksum_path": str(checksum_path),
    }


def main() -> int:
    result = build()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
