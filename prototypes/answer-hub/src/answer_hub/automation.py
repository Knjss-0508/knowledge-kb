from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import json
import shutil
import uuid

from .embedding import EmbeddingClient
from .workflow import (
    DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    DEFAULT_CLUSTER_REVIEW_FLOOR,
    DEFAULT_CLUSTER_REVIEW_LIMIT,
    initial_label_from_workbook,
)


AUTOMATION_STAGES = [
    ("intake", "接收会话输入"),
    ("load_input", "读取并校验输入"),
    ("preprocess", "清洗与证据分流"),
    ("semantic_label", "会话语义标注"),
    ("topic_build", "主题聚类与知识转写"),
    ("export_review", "生成待审核队列"),
]

AUTOMATION_RUN_STATUSES = {
    "running": "运行中",
    "review_pending": "待人工审核",
    "failed": "运行失败",
}

AutomationProgressCallback = Callable[[dict[str, Any]], None]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = Path(name or fallback).name.strip()
    return cleaned or fallback


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary_path.replace(path)


class AutomationRunStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        source_name: str,
        standards_name: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        timestamp = _now()
        manifest = {
            "run_id": run_id,
            "status": "running",
            "status_label": AUTOMATION_RUN_STATUSES["running"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "source_name": source_name,
            "standards_name": standards_name,
            "run_dir": str(run_dir),
            "options": dict(options),
            "stages": [
                {
                    "id": stage_id,
                    "label": label,
                    "status": "pending",
                    "started_at": "",
                    "finished_at": "",
                    "detail": "",
                    "metrics": {},
                }
                for stage_id, label in AUTOMATION_STAGES
            ],
            "summary": {},
            "artifacts": {},
            "error": "",
        }
        self.save(manifest)
        return manifest

    def save(self, manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["updated_at"] = _now()
        manifest["status_label"] = AUTOMATION_RUN_STATUSES.get(
            str(manifest.get("status") or ""),
            str(manifest.get("status") or ""),
        )
        _write_json_atomic(self.manifest_path(manifest["run_id"]), manifest)
        return manifest

    def manifest_path(self, run_id: str) -> Path:
        return self.root / run_id / "automation_run.json"

    def load(self, run_id: str) -> dict[str, Any]:
        return json.loads(self.manifest_path(run_id).read_text(encoding="utf-8"))

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        for path in sorted(
            self.root.glob("*/automation_run.json"),
            key=lambda item: item.parent.name,
            reverse=True,
        ):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
            if len(manifests) >= max(1, limit):
                break
        return manifests

    def update_stage(
        self,
        manifest: dict[str, Any],
        stage_id: str,
        status: str,
        detail: str = "",
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stage = next(
            (item for item in manifest["stages"] if item["id"] == stage_id),
            None,
        )
        if stage is None:
            raise ValueError(f"Unknown automation stage: {stage_id}")
        timestamp = _now()
        if status == "running" and not stage["started_at"]:
            stage["started_at"] = timestamp
        if status in {"completed", "failed"}:
            if not stage["started_at"]:
                stage["started_at"] = timestamp
            stage["finished_at"] = timestamp
        stage["status"] = status
        stage["detail"] = detail
        stage["metrics"] = dict(metrics or {})
        return self.save(manifest)

    def fail(self, manifest: dict[str, Any], error: Exception) -> dict[str, Any]:
        running_stage = next(
            (stage for stage in manifest["stages"] if stage["status"] == "running"),
            None,
        )
        if running_stage:
            self.update_stage(
                manifest,
                running_stage["id"],
                "failed",
                detail=str(error),
            )
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        return self.save(manifest)


def list_automation_runs(
    output_root: str | Path,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return AutomationRunStore(output_root).list(limit=limit)


def run_automation_pipeline(
    source_path: str | Path,
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
    embedding_client: EmbeddingClient | None = None,
    progress_callback: AutomationProgressCallback | None = None,
) -> dict[str, Any]:
    source = Path(source_path)
    standards = Path(standards_path) if standards_path else None
    if not source.is_file():
        raise FileNotFoundError(f"会话文件不存在：{source}")
    if standards is not None and not standards.is_file():
        raise FileNotFoundError(f"标准文件不存在：{standards}")
    use_standard_references = standards is not None

    options = {
        "product_type": product_type,
        "use_mimo": use_mimo,
        "clustering_mode": clustering_mode,
        "semantic_threshold": semantic_threshold,
        "cluster_review_floor": cluster_review_floor,
        "cluster_auto_merge_threshold": cluster_auto_merge_threshold,
        "cluster_review_limit": cluster_review_limit,
        "use_standard_references": use_standard_references,
    }
    store = AutomationRunStore(output_root)
    manifest = store.create(
        source.name,
        standards.name if standards is not None else "",
        options,
    )
    run_dir = Path(manifest["run_dir"])
    input_dir = run_dir / "inputs"
    artifact_dir = run_dir / "artifacts"
    input_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    copied_source = input_dir / _safe_filename(source.name, "source.xlsx")
    copied_standards = (
        input_dir / _safe_filename(standards.name, "standards.xlsx")
        if standards is not None
        else None
    )

    def notify() -> None:
        if progress_callback:
            try:
                progress_callback(deepcopy(manifest))
            except Exception:
                # Progress rendering is observational and must never stop the workflow.
                pass

    def workflow_progress(
        stage_id: str,
        status: str,
        detail: str,
        metrics: dict[str, Any],
    ) -> None:
        store.update_stage(
            manifest,
            stage_id,
            status,
            detail=detail,
            metrics=metrics,
        )
        notify()

    try:
        store.update_stage(manifest, "intake", "running", "正在保存本次输入快照。")
        notify()
        shutil.copy2(source, copied_source)
        if standards is not None and copied_standards is not None:
            shutil.copy2(standards, copied_standards)
        store.update_stage(
            manifest,
            "intake",
            "completed",
            "输入文件已保存，后续处理可完整追溯。",
            {
                "source_bytes": copied_source.stat().st_size,
                "standards_bytes": (
                    copied_standards.stat().st_size
                    if copied_standards is not None
                    else 0
                ),
                "standard_references_enabled": use_standard_references,
            },
        )
        notify()

        summary = initial_label_from_workbook(
            source_path=copied_source,
            standards_path=copied_standards,
            output_dir=artifact_dir,
            product_type=product_type,
            use_mimo=use_mimo,
            clustering_mode=clustering_mode,
            semantic_threshold=semantic_threshold,
            cluster_review_floor=cluster_review_floor,
            cluster_auto_merge_threshold=cluster_auto_merge_threshold,
            cluster_review_limit=cluster_review_limit,
            embedding_client=embedding_client,
            progress_callback=workflow_progress,
            use_standard_references=use_standard_references,
        )
        artifacts = {
            "record_review": str(Path(summary["output_file"])),
            "topic_review": str(Path(summary["topic_review_file"])),
            "candidate_knowledge": str(Path(summary["candidate_output_file"])),
            "summary": str(artifact_dir / "summary.json"),
            "audit_db": str(summary.get("audit_db") or ""),
        }
        manifest["summary"] = summary
        manifest["artifacts"] = artifacts
        manifest["status"] = "review_pending"
        store.save(manifest)
        notify()
        return manifest
    except Exception as exc:
        store.fail(manifest, exc)
        notify()
        return manifest
