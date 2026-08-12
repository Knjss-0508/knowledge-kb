from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .automation_api import AutomationJobStore
from .automation_queue import (
    AutomationQueue,
    AutomationQueueLocked,
    process_automation_queue,
    queue_job_metadata_path,
)
from .mimo import load_dotenv


class FullFlowTestError(RuntimeError):
    """A safe, user-facing failure while preparing a full-flow test."""


def _configured_path(
    value: str | None,
    *,
    project_root: Path,
    default: str,
) -> Path:
    path = Path(str(value or default).strip())
    if not path.is_absolute():
        path = project_root / path
    return path


def _missing_configuration(environ: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    for name in (
        "MIMO_API_KEY",
        "MIMO_BASE_URL",
        "MIMO_MODEL",
        "KB_BASE_URL",
        "KB_INTEGRATION_KEY",
    ):
        if not str(environ.get(name) or "").strip():
            missing.append(name)
    return missing


def check_cz_ready(base_url: str) -> None:
    ready_url = f"{base_url.rstrip('/')}/ready"
    request = Request(ready_url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise FullFlowTestError(
            "CZ 未就绪，请先启动本地 CZ 并确认 /ready 返回 ready。"
        ) from exc
    if response.status != 200 or payload.get("status") != "ready":
        raise FullFlowTestError(
            "CZ 未就绪，请先启动本地 CZ 并确认 /ready 返回 ready。"
        )


def _ensure_local_cz_target(base_url: str) -> None:
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
    ):
        raise FullFlowTestError(
            "本地全流程测试仅允许连接本机 CZ（127.0.0.1、localhost 或 ::1），"
            "禁止把测试候选推送到远程环境。"
        )


def _safe_sync_summary(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: int(source.get(key) or 0)
        for key in ("queued", "ready", "rejected", "reused", "failed")
    }


def _queue_source_count(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(
        path.is_file()
        and not path.name.startswith("~$")
        and path.suffix.lower() in {".xlsx", ".xlsm"}
        for path in directory.iterdir()
    )


def _remove_pending_job(
    queue: AutomationQueue,
    metadata: Mapping[str, Any] | None,
) -> None:
    queued_value = str((metadata or {}).get("queue_path") or "").strip()
    if not queued_value:
        return
    queued_path = Path(queued_value).resolve()
    pending_root = queue.pending.resolve()
    if not queued_path.is_relative_to(pending_root):
        return
    queued_path.unlink(missing_ok=True)
    metadata_path = queue_job_metadata_path(queued_path).resolve()
    if metadata_path.is_relative_to(pending_root):
        metadata_path.unlink(missing_ok=True)


def run_full_flow_test(
    source_path: str | Path,
    *,
    project_root: str | Path,
    product_type: str = "",
    environ: Mapping[str, str] | None = None,
    ready_checker: Callable[[str], None] = check_cz_ready,
    processor: Callable[..., dict[str, Any]] = process_automation_queue,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FullFlowTestError(f"没有找到 Excel 文件：{source}")
    if source.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise FullFlowTestError("请选择 .xlsx 或 .xlsm 格式的第二部分数据文件。")

    active_environment = environ if environ is not None else os.environ
    missing = _missing_configuration(active_environment)
    if missing:
        raise FullFlowTestError(
            "缺少必要配置：" + "、".join(missing) + "。"
        )

    base_url = str(active_environment["KB_BASE_URL"]).strip()
    _ensure_local_cz_target(base_url)
    ready_checker(base_url)

    queue_root = _configured_path(
        active_environment.get("ANSWER_HUB_AUTOMATION_QUEUE"),
        project_root=root,
        default="data/automation-queue",
    )
    output_root = _configured_path(
        active_environment.get("ANSWER_HUB_AUTOMATION_OUTPUT"),
        project_root=root,
        default="outputs/automation-runs",
    )
    queue = AutomationQueue(queue_root)
    queue.ensure()
    metadata: dict[str, Any] | None = None
    try:
        with queue.lock():
            processing_count = _queue_source_count(queue.processing)
            if processing_count:
                raise FullFlowTestError(
                    f"自动化队列正在处理 {processing_count} 个任务。"
                    "请先在 Streamlit“运行记录”中核查，避免混跑。"
                )
            pending_jobs = queue.candidates()
            if pending_jobs:
                raise FullFlowTestError(
                    f"自动化队列已有 {len(pending_jobs)} 个待处理任务。"
                    "请先在 Streamlit“运行记录”中处理或核查这些任务，避免混跑。"
                )

            store = AutomationJobStore(queue_root, output_root)
            try:
                metadata = store.create(
                    source.name,
                    source.read_bytes(),
                    {
                        "product_type": product_type.strip(),
                        "use_mimo": True,
                        "clustering_mode": "direct_mimo",
                        "semantic_threshold": 0.84,
                        "cluster_review_floor": 0.75,
                        "cluster_auto_merge_threshold": 0.92,
                        "cluster_review_limit": 100,
                        "continue_on_mimo_unavailable": False,
                        "sync_to_cz_review": True,
                        "submit_to_cz": True,
                    },
                )
            except ValueError as exc:
                raise FullFlowTestError(str(exc)) from exc
            except OSError as exc:
                raise FullFlowTestError(
                    "无法把测试文件写入自动化队列，请检查项目目录权限。"
                ) from exc

            try:
                batch = processor(
                    queue_root,
                    None,
                    output_root,
                    product_type=product_type.strip(),
                    use_mimo=True,
                    clustering_mode="direct_mimo",
                    max_files=1,
                    submit_to_cz=True,
                    continue_on_mimo_unavailable=False,
                    acquire_lock=False,
                )
            except Exception as exc:
                _remove_pending_job(queue, metadata)
                raise FullFlowTestError(
                    "自动化队列执行失败。请打开 Streamlit“运行记录”查看阶段和错误。"
                ) from exc
    except AutomationQueueLocked as exc:
        raise FullFlowTestError(
            "自动化队列正在运行。请等待当前任务结束后再启动新的全流程测试。"
        ) from exc

    if metadata is None:
        raise FullFlowTestError(
            "测试任务未能创建，请检查自动化队列目录权限。"
        )
    job_id = str(metadata.get("job_id") or "")
    result = next(
        (
            item
            for item in batch.get("results") or []
            if str(item.get("job_id") or "") == job_id
        ),
        None,
    )
    if not isinstance(result, dict):
        _remove_pending_job(queue, metadata)
        raise FullFlowTestError(
            "任务已入队，但本轮没有返回对应运行结果。请在 Streamlit“运行记录”中核查。"
        )
    _remove_pending_job(queue, metadata)
    return {
        "job_id": job_id,
        "run_id": str(result.get("run_id") or ""),
        "status": str(result.get("status") or ""),
        "cz_candidate_sync": _safe_sync_summary(
            result.get("cz_candidate_sync")
        ),
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行一次 Excel 到 CZ 候选价值复核的本地全流程测试。"
    )
    parser.add_argument("--source", required=True, help="第二部分 Excel 文件路径")
    parser.add_argument(
        "--product-type",
        default="",
        help="可选；留空时处理全部品类，仍按品类隔离聚类。",
    )
    args = parser.parse_args()

    root = _project_root()
    load_dotenv(root / ".env")
    try:
        result = run_full_flow_test(
            args.source,
            project_root=root,
            product_type=args.product_type,
        )
    except FullFlowTestError as exc:
        print(f"全流程测试未完成：{exc}")
        return 1

    sync = result["cz_candidate_sync"]
    print("全流程测试运行结束。")
    print(f"任务 ID：{result['job_id']}")
    print(f"运行 ID：{result['run_id'] or '-'}")
    print(f"运行状态：{result['status'] or '-'}")
    print(
        "CZ 候选同步："
        f"排队 {sync['queued']}，"
        f"直接就绪 {sync['ready']}，"
        f"正常拦截 {sync['rejected']}，"
        f"幂等复用 {sync['reused']}，"
        f"失败 {sync['failed']}。"
    )
    if result["status"] != "completed" or sync["failed"]:
        print("本轮未完全成功，请打开 Streamlit“自动化看板 → 运行记录”核查。")
        return 1
    print("请打开 CZ“候选价值复核”进行人工审核；系统没有自动送审或发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
