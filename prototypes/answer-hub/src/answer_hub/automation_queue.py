from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
import json
import os
import shutil
import time
import uuid

from .auto_review import (
    AutoReviewPolicy,
    apply_auto_review_annotation,
    partition_auto_review_candidates,
    select_candidates_for_submission,
)
from .automation import AutomationRunStore, run_automation_pipeline
from .cz_integration import CzIntegrationAdapter
from .embedding import EmbeddingClient
from .excel_io import read_workbook_rows, write_rows_to_workbook
from .workflow import (
    DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    DEFAULT_CLUSTER_REVIEW_FLOOR,
    DEFAULT_CLUSTER_REVIEW_LIMIT,
)


SUPPORTED_SOURCE_SUFFIXES = {".xlsx", ".xlsm"}
QUEUE_DIRECTORY_NAMES = ("pending", "processing", "completed", "failed", "logs")
JOB_METADATA_SUFFIX = ".job.json"
DELIVERY_STAGES = [
    ("model_review", "模型审核知识点"),
    ("cz_upload", "同步答疑中台候选价值复核"),
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _unique_destination(directory: Path, filename: str) -> Path:
    destination = directory / Path(filename).name
    if not destination.exists():
        return destination
    stem = destination.stem
    suffix = destination.suffix
    for index in range(1, 10_000):
        candidate = directory / f"{stem}-{index:03d}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为队列文件生成唯一名称：{filename}")


def queue_job_metadata_path(source_path: str | Path) -> Path:
    source = Path(source_path)
    return source.with_name(f"{source.name}{JOB_METADATA_SUFFIX}")


def read_queue_job_metadata(source_path: str | Path) -> dict[str, Any]:
    metadata_path = queue_job_metadata_path(source_path)
    if not metadata_path.is_file():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_queue_job_metadata(
    source_path: str | Path,
    payload: dict[str, Any],
) -> Path:
    metadata_path = queue_job_metadata_path(source_path)
    _write_json_atomic(metadata_path, payload)
    return metadata_path


def _move_queue_item(source: Path, destination: Path) -> None:
    metadata_source = queue_job_metadata_path(source)
    metadata_destination = queue_job_metadata_path(destination)
    shutil.move(str(source), str(destination))
    if metadata_source.is_file():
        shutil.move(str(metadata_source), str(metadata_destination))


def _option_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _option_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _option_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AutomationQueueLocked(RuntimeError):
    pass


class AutomationQueue:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.processing = self.root / "processing"
        self.completed = self.root / "completed"
        self.failed = self.root / "failed"
        self.logs = self.root / "logs"
        self.lock_path = self.root / ".runner.lock"

    def ensure(self) -> None:
        for name in QUEUE_DIRECTORY_NAMES:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, stale_after_seconds: int = 7_200) -> Iterator[None]:
        self.ensure()
        stale_after_seconds = max(60, int(stale_after_seconds))
        for attempt in range(2):
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    lock_age = time.time() - self.lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if attempt == 0 and lock_age >= stale_after_seconds:
                    self.lock_path.unlink(missing_ok=True)
                    continue
                raise AutomationQueueLocked(
                    f"自动化队列正在运行，锁文件为：{self.lock_path}"
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "pid": os.getpid(),
                                "created_at": _now(),
                            },
                            ensure_ascii=False,
                        )
                    )
                break
        else:
            raise AutomationQueueLocked(
                f"无法获取自动化队列锁：{self.lock_path}"
            )

        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def recover_stale_processing(self, stale_after_seconds: int) -> list[str]:
        recovered: list[str] = []
        cutoff = time.time() - max(60, int(stale_after_seconds))
        for source in self._source_files(self.processing):
            if source.stat().st_mtime > cutoff:
                continue
            destination = _unique_destination(self.pending, source.name)
            _move_queue_item(source, destination)
            recovered.append(str(destination))
        return recovered

    def candidates(self, retry_failed: bool = False) -> list[Path]:
        candidates = self._source_files(self.pending)
        if retry_failed:
            candidates.extend(self._source_files(self.failed))
        return sorted(candidates, key=lambda path: (path.stat().st_mtime, path.name))

    def claim(self, source: Path) -> Path:
        destination = _unique_destination(self.processing, source.name)
        _move_queue_item(source, destination)
        return destination

    def finish(self, source: Path, succeeded: bool) -> Path:
        destination_dir = self.completed if succeeded else self.failed
        destination = _unique_destination(destination_dir, source.name)
        _move_queue_item(source, destination)
        return destination

    def requeue(self, source: Path) -> Path:
        destination = _unique_destination(self.pending, source.name)
        _move_queue_item(source, destination)
        return destination

    @staticmethod
    def _source_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return [
            path
            for path in directory.iterdir()
            if path.is_file()
            and not path.name.startswith("~$")
            and path.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
        ]


def _ensure_delivery_stages(manifest: dict[str, Any]) -> None:
    existing = {
        str(stage.get("id") or "")
        for stage in manifest.get("stages") or []
    }
    for stage_id, label in DELIVERY_STAGES:
        if stage_id in existing:
            continue
        manifest.setdefault("stages", []).append(
            {
                "id": stage_id,
                "label": label,
                "status": "pending",
                "started_at": "",
                "finished_at": "",
                "detail": "",
                "metrics": {},
            }
        )


def _review_result_columns(
    source_columns: list[str],
    rows: list[dict[str, Any]],
) -> list[str]:
    columns = list(source_columns)
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    return columns


def _run_model_review_and_cz_candidate_sync(
    manifest: dict[str, Any],
    output_root: Path,
    *,
    policy: AutoReviewPolicy | None = None,
    cz_adapter: CzIntegrationAdapter | None = None,
) -> dict[str, Any]:
    store = AutomationRunStore(output_root)
    _ensure_delivery_stages(manifest)
    manifest.setdefault("options", {})["submit_to_cz"] = True
    manifest["options"]["sync_to_cz_review"] = True
    store.save(manifest)

    try:
        store.update_stage(
            manifest,
            "model_review",
            "running",
            "正在读取知识点并执行模型审核策略。",
        )
        topic_review_path = Path(
            str((manifest.get("artifacts") or {}).get("topic_review") or "")
        )
        if not topic_review_path.is_file():
            raise FileNotFoundError(
                f"主题审核工作簿不存在：{topic_review_path}"
            )

        source_columns, topic_rows = read_workbook_rows(
            topic_review_path,
            sheet_name="topic_review_queue",
        )
        review_policy = policy or AutoReviewPolicy.from_env()
        if review_policy.enabled:
            approved_rows = select_candidates_for_submission(
                topic_rows,
                review_policy,
            )
            _potential_approved, exception_rows = partition_auto_review_candidates(
                topic_rows,
                review_policy,
            )
        else:
            approved_rows = []
            exception_rows = [
                apply_auto_review_annotation(dict(row), review_policy)
                for row in topic_rows
            ]

        artifact_dir = Path(str(manifest["run_dir"])) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        model_review_path = artifact_dir / "model_review_results.xlsx"
        review_columns = _review_result_columns(
            source_columns,
            approved_rows + exception_rows,
        )
        write_rows_to_workbook(
            {
                "模型审核通过": (review_columns, approved_rows),
                "人工复核例外": (review_columns, exception_rows),
            },
            model_review_path,
        )
        sync_rows = approved_rows + exception_rows

        model_metrics = {
            "total_rows": len(topic_rows),
            "approved_rows": len(approved_rows),
            "exception_rows": len(exception_rows),
            "policy_enabled": review_policy.enabled,
            "deployment_ready": review_policy.deployment_ready,
            "validated_model": review_policy.validated_model,
            "validated_prompt_version": review_policy.validated_prompt_version,
        }
        manifest.setdefault("summary", {}).update(
            {
                "model_review_total_rows": len(topic_rows),
                "model_review_approved_rows": len(approved_rows),
                "model_review_exception_rows": len(exception_rows),
                "model_review_policy_enabled": review_policy.enabled,
                "model_review_deployment_ready": review_policy.deployment_ready,
            }
        )
        manifest.setdefault("artifacts", {})["model_review"] = str(
            model_review_path
        )
        store.update_stage(
            manifest,
            "model_review",
            "completed",
            "模型审核完成；通过项和人工例外项都会同步至候选价值复核。",
            model_metrics,
        )

        store.update_stage(
            manifest,
            "cz_upload",
            "running",
            "正在同步全部主题候选到答疑中台候选价值复核。",
        )
        if not sync_rows:
            sync_result = {
                "queued": 0,
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "failed": 0,
                "results": [],
                "skipped": True,
                "reason": "没有可同步的主题候选。",
            }
        else:
            adapter = cz_adapter or CzIntegrationAdapter()
            sync_result = adapter.sync_review_candidates(sync_rows)
            sync_result["skipped"] = False
            sync_result.setdefault("failed", 0)

        sync_path = artifact_dir / "cz_candidate_sync.json"
        _write_json_atomic(sync_path, sync_result)
        manifest["summary"]["cz_candidate_sync"] = sync_result
        manifest["artifacts"]["cz_candidate_sync"] = str(sync_path)
        # 兼容旧自动化查询字段；含义已从“直接建知识”调整为“同步候选复核”。
        manifest["summary"]["cz_submission"] = sync_result
        manifest["artifacts"]["cz_submission"] = str(sync_path)
        sync_metrics = {
            key: int(sync_result.get(key) or 0)
            for key in (
                "queued",
                "ready",
                "rejected",
                "reused",
                "failed",
            )
        }
        if sync_metrics["failed"]:
            detail = (
                f"CZ候选价值复核同步有 {sync_metrics['failed']} 条失败；"
                "成功项已保留，原文件移入失败队列供修复后幂等重试。"
            )
            store.update_stage(
                manifest,
                "cz_upload",
                "failed",
                detail,
                sync_metrics,
            )
            return store.fail(manifest, RuntimeError(detail))
        store.update_stage(
            manifest,
            "cz_upload",
            "completed",
            (
                "没有主题候选，已跳过同步。"
                if sync_result.get("skipped")
                else "全部主题候选已同步至答疑中台候选价值复核。"
            ),
            sync_metrics,
        )
        return manifest
    except Exception as exc:
        return store.fail(manifest, exc)


def process_automation_queue(
    queue_root: str | Path,
    standards_path: str | Path | None,
    output_root: str | Path,
    *,
    product_type: str = "",
    use_mimo: bool = True,
    clustering_mode: str = "direct_mimo",
    semantic_threshold: float = 0.84,
    cluster_review_floor: float = DEFAULT_CLUSTER_REVIEW_FLOOR,
    cluster_auto_merge_threshold: float = DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    cluster_review_limit: int = DEFAULT_CLUSTER_REVIEW_LIMIT,
    max_files: int = 10,
    retry_failed: bool = False,
    stale_after_seconds: int = 7_200,
    submit_to_cz: bool = False,
    embedding_client: EmbeddingClient | None = None,
    auto_review_policy: AutoReviewPolicy | None = None,
    cz_adapter: CzIntegrationAdapter | None = None,
) -> dict[str, Any]:
    standards = Path(standards_path) if standards_path else None
    if standards is not None and not standards.is_file():
        raise FileNotFoundError(f"标准文件不存在：{standards}")

    queue = AutomationQueue(queue_root)
    output_path = Path(output_root)
    output_path.mkdir(parents=True, exist_ok=True)
    batch_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    summary: dict[str, Any] = {
        "batch_id": batch_id,
        "status": "running",
        "started_at": _now(),
        "finished_at": "",
        "queue_root": str(queue.root),
        "output_root": str(output_path),
        "standards_path": str(standards) if standards is not None else "",
        "max_files": max(1, int(max_files)),
        "retry_failed": bool(retry_failed),
        "sync_to_cz_review": bool(submit_to_cz),
        "submit_to_cz": bool(submit_to_cz),
        "recovered": [],
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "remaining": 0,
        "results": [],
        "log_path": "",
        "error": "",
    }

    try:
        with queue.lock(stale_after_seconds=stale_after_seconds):
            summary["recovered"] = queue.recover_stale_processing(
                stale_after_seconds=stale_after_seconds
            )
            candidates = queue.candidates(retry_failed=retry_failed)
            selected = candidates[: summary["max_files"]]
            summary["remaining"] = max(0, len(candidates) - len(selected))

            for source in selected:
                original_path = str(source)
                claimed_path = queue.claim(source)
                job_metadata = read_queue_job_metadata(claimed_path)
                job_options = (
                    job_metadata.get("options")
                    if isinstance(job_metadata.get("options"), dict)
                    else {}
                )
                effective_product_type = str(
                    job_options.get("product_type", product_type) or ""
                )
                effective_use_mimo = _option_bool(
                    job_options.get("use_mimo"),
                    use_mimo,
                )
                effective_clustering_mode = str(
                    job_options.get("clustering_mode", clustering_mode)
                    or clustering_mode
                )
                effective_semantic_threshold = _option_float(
                    job_options.get("semantic_threshold"),
                    semantic_threshold,
                )
                effective_cluster_review_floor = _option_float(
                    job_options.get("cluster_review_floor"),
                    cluster_review_floor,
                )
                effective_cluster_auto_merge_threshold = _option_float(
                    job_options.get("cluster_auto_merge_threshold"),
                    cluster_auto_merge_threshold,
                )
                effective_cluster_review_limit = _option_int(
                    job_options.get("cluster_review_limit"),
                    cluster_review_limit,
                )
                requested_sync_to_cz = (
                    job_options.get("sync_to_cz_review")
                    if "sync_to_cz_review" in job_options
                    else job_options.get("submit_to_cz")
                )
                effective_submit_to_cz = _option_bool(
                    requested_sync_to_cz,
                    submit_to_cz,
                )
                if job_metadata:
                    job_metadata.update(
                        {
                            "status": "processing",
                            "updated_at": _now(),
                            "claimed_path": str(claimed_path),
                            "error": "",
                        }
                    )
                    write_queue_job_metadata(claimed_path, job_metadata)
                result: dict[str, Any] = {
                    "job_id": str(job_metadata.get("job_id") or ""),
                    "source_path": original_path,
                    "claimed_path": str(claimed_path),
                    "final_path": "",
                    "run_id": "",
                    "status": "running",
                    "error": "",
                }
                summary["attempted"] += 1

                try:
                    manifest = run_automation_pipeline(
                        source_path=claimed_path,
                        standards_path=standards,
                        output_root=output_path,
                        product_type=effective_product_type,
                        use_mimo=effective_use_mimo,
                        clustering_mode=effective_clustering_mode,
                        semantic_threshold=effective_semantic_threshold,
                        cluster_review_floor=effective_cluster_review_floor,
                        cluster_auto_merge_threshold=(
                            effective_cluster_auto_merge_threshold
                        ),
                        cluster_review_limit=effective_cluster_review_limit,
                        embedding_client=embedding_client,
                    )
                    if (
                        manifest.get("status") != "failed"
                        and effective_submit_to_cz
                    ):
                        manifest = _run_model_review_and_cz_candidate_sync(
                            manifest,
                            output_path,
                            policy=auto_review_policy,
                            cz_adapter=cz_adapter,
                        )
                    succeeded = manifest.get("status") != "failed"
                    final_path = queue.finish(claimed_path, succeeded=succeeded)
                    if job_metadata:
                        job_metadata.update(
                            {
                                "status": (
                                    "completed" if succeeded else "failed"
                                ),
                                "updated_at": _now(),
                                "finished_at": _now(),
                                "run_id": str(manifest.get("run_id") or ""),
                                "final_path": str(final_path),
                                "error": str(manifest.get("error") or ""),
                                "summary": manifest.get("summary") or {},
                                "artifacts": manifest.get("artifacts") or {},
                            }
                        )
                        write_queue_job_metadata(final_path, job_metadata)
                    manifest["queue"] = {
                        "batch_id": batch_id,
                        "source_path": original_path,
                        "claimed_path": str(claimed_path),
                        "final_path": str(final_path),
                        "disposition": "completed" if succeeded else "failed",
                    }
                    AutomationRunStore(output_path).save(manifest)
                    result.update(
                        {
                            "final_path": str(final_path),
                            "run_id": str(manifest.get("run_id") or ""),
                            "status": "completed" if succeeded else "failed",
                            "error": str(manifest.get("error") or ""),
                            "cz_candidate_sync": (
                                (manifest.get("summary") or {}).get(
                                    "cz_candidate_sync",
                                    {},
                                )
                            ),
                            "cz_submission": (
                                (manifest.get("summary") or {}).get(
                                    "cz_submission",
                                    {},
                                )
                            ),
                        }
                    )
                except Exception as exc:
                    final_path = (
                        queue.finish(claimed_path, succeeded=False)
                        if claimed_path.exists()
                        else None
                    )
                    if job_metadata and final_path is not None:
                        job_metadata.update(
                            {
                                "status": "failed",
                                "updated_at": _now(),
                                "finished_at": _now(),
                                "final_path": str(final_path),
                                "error": str(exc),
                            }
                        )
                        write_queue_job_metadata(final_path, job_metadata)
                    result.update(
                        {
                            "final_path": str(final_path) if final_path else "",
                            "status": "failed",
                            "error": str(exc),
                        }
                    )

                if result["status"] == "completed":
                    summary["succeeded"] += 1
                else:
                    summary["failed"] += 1
                summary["results"].append(result)

            if summary["failed"]:
                summary["status"] = "completed_with_errors"
            elif summary["attempted"]:
                summary["status"] = "completed"
            else:
                summary["status"] = "idle"
            summary["finished_at"] = _now()
            log_path = queue.logs / f"{batch_id}.json"
            summary["log_path"] = str(log_path)
            _write_json_atomic(log_path, summary)
    except AutomationQueueLocked as exc:
        summary["status"] = "locked"
        summary["finished_at"] = _now()
        summary["error"] = str(exc)

    return summary
