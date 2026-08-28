from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import json
import math
import os
import shutil
import threading
import time
import uuid

from .embedding import EmbeddingClient
from .mimo import MimoClient, MimoError
from .operations import duration_seconds, evaluate_run_sla
from .terminology import ensure_terminology_loaded
from .version import AUTOMATION_MANIFEST_VERSION, release_metadata
from .workflow import (
    DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD,
    DEFAULT_CLUSTER_REVIEW_FLOOR,
    DEFAULT_CLUSTER_REVIEW_LIMIT,
    initial_label_from_workbook,
    resolve_cluster_media_policy,
)


AUTOMATION_STAGES = [
    ("intake", "接收会话输入"),
    ("load_input", "读取并校验输入"),
    ("preprocess", "清洗与证据分流"),
    ("semantic_label", "会话语义标注"),
    ("topic_build", "主题处理（兼容汇总）"),
    ("topic_cluster", "原子问题与主题聚类"),
    ("topic_enrichment", "聚类准入、历史归并与价值分类"),
    ("knowledge_transcription", "知识转写与内容初审"),
    ("export_review", "生成待审核队列"),
]

TOPIC_SUBSTAGE_IDS = (
    "topic_cluster",
    "topic_enrichment",
    "knowledge_transcription",
)

AUTOMATION_RUN_STATUSES = {
    "running": "运行中",
    "review_pending": "待人工审核",
    "needs_confirmation": "等待人工确认",
    "failed": "运行失败",
}

DEFAULT_CLUSTER_FAILURE_ABORT_RATIO = 0.5
CLUSTER_FAILURE_ABORT_RATIO_ENV = "ANSWER_HUB_CLUSTER_FAILURE_ABORT_RATIO"

AutomationProgressCallback = Callable[[dict[str, Any]], None]
_JSON_WRITE_LOCK = threading.Lock()
_JSON_REPLACE_ATTEMPTS = 7
_JSON_REPLACE_BACKOFF_SECONDS = 0.05


class MimoPreflightError(RuntimeError):
    """MiMo is configured but unavailable before generation starts."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = Path(name or fallback).name.strip()
    return cleaned or fallback


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.tmp"
    )
    with _JSON_WRITE_LOCK:
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            for attempt in range(_JSON_REPLACE_ATTEMPTS):
                try:
                    temporary_path.replace(path)
                    return
                except PermissionError:
                    if attempt + 1 >= _JSON_REPLACE_ATTEMPTS:
                        raise
                    # Windows scanners/readers may briefly block atomic replacement.
                    time.sleep(_JSON_REPLACE_BACKOFF_SECONDS * (2**attempt))
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


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
        terminology = ensure_terminology_loaded()
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        timestamp = _now()
        manifest = {
            "manifest_version": AUTOMATION_MANIFEST_VERSION,
            "release": release_metadata(),
            "run_id": run_id,
            "status": "running",
            "status_label": AUTOMATION_RUN_STATUSES["running"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "source_name": source_name,
            "standards_name": standards_name,
            "run_dir": str(run_dir),
            "options": dict(options),
            "terminology": terminology,
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
            "current_activity": {},
            "activity_history": [],
            "summary": {},
            "artifacts": {},
            "error": "",
            "attempt_count": 1,
            "retry_history": [],
            "duration_seconds": 0.0,
            "sla": {},
            "alerts": [],
        }
        self.save(manifest)
        return manifest

    def save(self, manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["updated_at"] = _now()
        if manifest.get("status") in {
            "review_pending",
            "needs_confirmation",
            "failed",
        }:
            elapsed = duration_seconds(
                manifest.get("created_at"),
                manifest.get("updated_at"),
            )
            if elapsed is not None:
                manifest["duration_seconds"] = elapsed
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
        if status in {"completed", "failed", "interrupted"}:
            if not stage["started_at"]:
                stage["started_at"] = timestamp
            stage["finished_at"] = timestamp
            elapsed = duration_seconds(stage["started_at"], stage["finished_at"])
            if elapsed is not None:
                stage["duration_seconds"] = elapsed
        stage["status"] = status
        stage["detail"] = detail
        stage["metrics"] = dict(metrics or {})
        stage["updated_at"] = timestamp
        activity = {
            "stage_id": stage_id,
            "label": stage.get("label", ""),
            "status": status,
            "detail": detail,
            "metrics": dict(metrics or {}),
            "updated_at": timestamp,
        }
        manifest["current_activity"] = activity
        history = manifest.setdefault("activity_history", [])
        history.append(activity)
        if len(history) > 200:
            del history[:-200]
        return self.save(manifest)

    def fail(self, manifest: dict[str, Any], error: Exception) -> dict[str, Any]:
        active_stage_id = str(
            (manifest.get("current_activity") or {}).get("stage_id") or ""
        )
        running_stages = [
            stage
            for stage in manifest["stages"]
            if stage["status"] == "running"
        ]
        running_stages.sort(
            key=lambda stage: str(stage.get("id") or "") == active_stage_id
        )
        active_label = next(
            (
                str(stage.get("label") or "")
                for stage in running_stages
                if str(stage.get("id") or "") == active_stage_id
            ),
            "当前阶段",
        )
        for running_stage in running_stages:
            running_stage_id = str(running_stage.get("id") or "")
            is_active = running_stage_id == active_stage_id
            interrupted = not is_active and running_stage_id != "topic_build"
            self.update_stage(
                manifest,
                running_stage_id,
                "interrupted" if interrupted else "failed",
                detail=(
                    f"因{active_label}失败而中断。"
                    if interrupted
                    else str(error)
                ),
            )
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        self.save(manifest)
        manifest["sla"] = evaluate_run_sla(manifest)
        manifest["alerts"] = list(manifest["sla"].get("breaches") or [])
        return self.save(manifest)


def _topic_substage_id(
    detail: str,
    metrics: dict[str, Any],
) -> str:
    explicit_phase = str(metrics.get("pipeline_phase") or "").strip()
    if explicit_phase in TOPIC_SUBSTAGE_IDS:
        return explicit_phase
    metric_names = set(metrics)
    if any(
        name.startswith(("atomic_", "direct_cluster_", "direct_reconcile_"))
        for name in metric_names
    ):
        return "topic_cluster"
    if "转写" in detail or "内容初审" in detail:
        return "knowledge_transcription"
    if any(word in detail for word in ("准入", "历史", "价值分类")):
        return "topic_enrichment"
    return "topic_cluster"


def _ensure_topic_substages(manifest: dict[str, Any]) -> None:
    stages = manifest.setdefault("stages", [])
    existing_ids = {
        str(stage.get("id") or "")
        for stage in stages
        if isinstance(stage, dict)
    }
    labels = dict(AUTOMATION_STAGES)
    insert_at = next(
        (
            index
            for index, stage in enumerate(stages)
            if str(stage.get("id") or "") == "export_review"
        ),
        len(stages),
    )
    for substage_id in TOPIC_SUBSTAGE_IDS:
        if substage_id in existing_ids:
            continue
        stages.insert(
            insert_at,
            {
                "id": substage_id,
                "label": labels[substage_id],
                "status": "pending",
                "started_at": "",
                "finished_at": "",
                "detail": "",
                "metrics": {},
            },
        )
        insert_at += 1
    manifest.setdefault("current_activity", {})
    manifest.setdefault("activity_history", [])


def _update_workflow_progress(
    store: AutomationRunStore,
    manifest: dict[str, Any],
    stage_id: str,
    status: str,
    detail: str,
    metrics: dict[str, Any],
) -> None:
    if stage_id == "topic_build":
        _ensure_topic_substages(manifest)
    store.update_stage(
        manifest,
        stage_id,
        status,
        detail=detail,
        metrics=metrics,
    )
    if stage_id != "topic_build":
        return
    if status == "running":
        substage_id = _topic_substage_id(detail, metrics)
        if substage_id != "topic_cluster":
            cluster_stage = next(
                (
                    stage
                    for stage in manifest.get("stages") or []
                    if str(stage.get("id") or "") == "topic_cluster"
                ),
                None,
            )
            if cluster_stage and cluster_stage.get("status") == "running":
                store.update_stage(
                    manifest,
                    "topic_cluster",
                    "completed",
                    detail="原子问题拆分与主题聚类完成。",
                    metrics=dict(cluster_stage.get("metrics") or {}),
                )
        store.update_stage(
            manifest,
            substage_id,
            "running",
            detail=detail,
            metrics=metrics,
        )
        return
    if status != "completed":
        return
    cluster_only = bool((manifest.get("options") or {}).get("cluster_only"))
    completion_details = {
        "topic_cluster": "原子问题拆分与主题聚类完成。",
        "topic_enrichment": (
            "仅聚类模式已跳过聚类准入、历史归并与价值分类。"
            if cluster_only
            else "聚类准入、历史归并与价值分类完成。"
        ),
        "knowledge_transcription": (
            "仅聚类模式已跳过知识转写与内容初审。"
            if cluster_only
            else "知识转写与内容初审完成。"
        ),
    }
    for substage_id in TOPIC_SUBSTAGE_IDS:
        substage_metrics = dict(metrics)
        if cluster_only and substage_id != "topic_cluster":
            substage_metrics["skipped"] = True
        store.update_stage(
            manifest,
            substage_id,
            "completed",
            detail=completion_details[substage_id],
            metrics=substage_metrics,
        )


def list_automation_runs(
    output_root: str | Path,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return AutomationRunStore(output_root).list(limit=limit)


def run_mimo_preflight() -> dict[str, Any]:
    client = MimoClient.from_env()
    if client is None:
        raise MimoPreflightError("MiMo 未配置：请检查 MIMO_API_KEY、MIMO_BASE_URL 和 MIMO_MODEL。")
    try:
        return client.check_availability()
    except MimoError as exc:
        raise MimoPreflightError(str(exc)) from exc


def _mimo_preflight_required(use_mimo: bool, clustering_mode: str) -> bool:
    return bool(use_mimo) and clustering_mode.strip().lower() != "rule"


def _mimo_confirmation_alert(error: str) -> str:
    return (
        f"MiMo API 预检失败：{error}。"
        "已停止自动生成，请人工确认是否修复配置后重跑，"
        "或明确允许规则兜底生成。"
    )


def _cluster_failure_abort_ratio() -> float:
    """Return the guarded failure ratio used before downstream delivery."""
    raw_value = os.getenv(
        CLUSTER_FAILURE_ABORT_RATIO_ENV,
        str(DEFAULT_CLUSTER_FAILURE_ABORT_RATIO),
    )
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = DEFAULT_CLUSTER_FAILURE_ABORT_RATIO
    if not math.isfinite(value) or value <= 0:
        value = DEFAULT_CLUSTER_FAILURE_ABORT_RATIO
    return max(0.0, min(value, 1.0))


def _cluster_failure_guard(summary: dict[str, Any]) -> dict[str, Any]:
    """Detect a systemic clustering failure before creating external candidates."""
    threshold = _cluster_failure_abort_ratio()
    direct_calls = int(summary.get("direct_cluster_calls") or 0)
    direct_failed = int(summary.get("direct_cluster_failed") or 0)
    atomic_calls = int(summary.get("atomic_extraction_calls") or 0)
    atomic_failed = int(summary.get("atomic_extraction_failed") or 0)
    direct_ratio = direct_failed / direct_calls if direct_calls else 0.0
    atomic_ratio = atomic_failed / atomic_calls if atomic_calls else 0.0
    reasons: list[str] = []
    if direct_calls and direct_ratio >= threshold:
        reasons.append(
            f"direct_mimo 聚类失败 {direct_failed}/{direct_calls} "
            f"（{direct_ratio:.1%}）"
        )
    if atomic_calls and atomic_ratio >= threshold:
        reasons.append(
            f"原子问题提取失败 {atomic_failed}/{atomic_calls} "
            f"（{atomic_ratio:.1%}）"
        )
    return {
        "cluster_failure_guard_triggered": bool(reasons),
        "cluster_failure_ratio": round(max(direct_ratio, atomic_ratio), 4),
        "cluster_failure_guard_threshold": threshold,
        "cluster_failure_guard_reason": "；".join(reasons),
        "cz_candidate_sync_blocked": bool(reasons),
    }


def _apply_cluster_failure_guard(
    manifest: dict[str, Any],
    store: AutomationRunStore,
    guard: dict[str, Any],
    *,
    cluster_only: bool,
) -> bool:
    if not guard["cluster_failure_guard_triggered"] or cluster_only:
        return False
    guard_reason = str(guard["cluster_failure_guard_reason"])
    guard_error = (
        "聚类失败保护已触发："
        f"{guard_reason}。本批次不会生成或同步 CZ 候选，"
        "请修复 MiMo/网络配置后使用 retry-run 重试。"
    )
    _prepare_cluster_checkpoint_for_retry(manifest, guard)
    manifest["status"] = "failed"
    manifest["error"] = guard_error
    manifest.setdefault("alerts", []).append(guard_error)
    store.update_stage(
        manifest,
        "topic_cluster",
        "failed",
        guard_error,
        guard,
    )
    return True


def _prepare_cluster_checkpoint_for_retry(
    manifest: dict[str, Any],
    guard: dict[str, Any],
) -> None:
    """Make retry-run execute topic clustering again instead of restoring failures."""
    run_dir = Path(str(manifest.get("run_dir") or ""))
    checkpoint_path = run_dir / "artifacts" / "workflow_checkpoint.json"
    if not checkpoint_path.is_file():
        checkpoint_path = run_dir / "workflow_checkpoint.json"
    if not checkpoint_path.is_file():
        return
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(checkpoint, dict):
        return
    checkpoint["stage"] = "semantic_label"
    checkpoint["updated_at"] = _now()
    checkpoint["cluster_failure_guard"] = guard
    checkpoint.pop("topic_summary", None)
    checkpoint.pop("cluster_summary", None)
    _write_json_atomic(checkpoint_path, checkpoint)


def _persist_cluster_failure_guard(
    manifest: dict[str, Any],
    store: AutomationRunStore,
    notify: Callable[[], None],
) -> None:
    manifest["sla"] = evaluate_run_sla(manifest)
    manifest["alerts"] = list(
        dict.fromkeys(
            list(manifest.get("alerts") or [])
            + list(manifest["sla"].get("breaches") or [])
        )
    )
    store.save(manifest)
    notify()


def automation_run_succeeded(manifest: dict[str, Any]) -> bool:
    return str(manifest.get("status") or "") not in {
        "failed",
        "needs_confirmation",
    }


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
    continue_on_mimo_unavailable: bool = False,
    cluster_only: bool = False,
    source_row_limit: int | None = None,
    direct_mimo_progress_path: str | Path | None = None,
    cluster_media_policy: str | None = None,
) -> dict[str, Any]:
    source = Path(source_path)
    standards = Path(standards_path) if standards_path else None
    if not source.is_file():
        raise FileNotFoundError(f"会话文件不存在：{source}")
    if standards is not None and not standards.is_file():
        raise FileNotFoundError(f"标准文件不存在：{standards}")
    if source_row_limit is not None:
        if source_row_limit < 1:
            raise ValueError("source_row_limit 必须是正整数")
        if not cluster_only:
            raise ValueError("source_row_limit 仅允许用于仅聚类小样本验证")
    use_standard_references = standards is not None

    effective_cluster_media_policy = resolve_cluster_media_policy(
        cluster_media_policy,
        cluster_only=cluster_only,
        clustering_mode=clustering_mode,
    )
    options = {
        "product_type": product_type,
        "use_mimo": use_mimo,
        "clustering_mode": clustering_mode,
        "semantic_threshold": semantic_threshold,
        "cluster_review_floor": cluster_review_floor,
        "cluster_auto_merge_threshold": cluster_auto_merge_threshold,
        "cluster_review_limit": cluster_review_limit,
        "use_standard_references": use_standard_references,
        "continue_on_mimo_unavailable": bool(continue_on_mimo_unavailable),
        "cluster_only": bool(cluster_only),
        "source_row_limit": source_row_limit or 0,
        "enforce_cluster_admission": not bool(cluster_only),
        "direct_mimo_progress_path": str(direct_mimo_progress_path or ""),
        "cluster_media_policy": effective_cluster_media_policy,
        "cluster_failure_abort_ratio": _cluster_failure_abort_ratio(),
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
        _update_workflow_progress(
            store,
            manifest,
            stage_id,
            status,
            detail,
            metrics,
        )
        notify()

    try:
        effective_use_mimo = use_mimo
        effective_clustering_mode = clustering_mode
        preflight_summary: dict[str, Any] | None = None
        if _mimo_preflight_required(use_mimo, clustering_mode):
            try:
                preflight_summary = run_mimo_preflight()
            except MimoPreflightError as exc:
                preflight_summary = {
                    "passed": False,
                    "error": str(exc),
                    "continued_with_rule_fallback": bool(
                        continue_on_mimo_unavailable
                    ),
                }
                if not continue_on_mimo_unavailable:
                    alert = _mimo_confirmation_alert(str(exc))
                    manifest["status"] = "needs_confirmation"
                    manifest["error"] = alert
                    manifest["summary"] = {"mimo_preflight": preflight_summary}
                    manifest["alerts"] = [
                        alert,
                        (
                            "确认继续后，请使用 --continue-on-mimo-unavailable "
                            "或队列任务选项 continue_on_mimo_unavailable=true。"
                        ),
                    ]
                    store.save(manifest)
                    notify()
                    return manifest
                effective_use_mimo = False
                effective_clustering_mode = "rule"
                manifest["alerts"].append(
                    _mimo_confirmation_alert(str(exc)).replace(
                        "已停止自动生成",
                        "已按人工确认继续",
                    )
                )

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
            use_mimo=effective_use_mimo,
            clustering_mode=effective_clustering_mode,
            semantic_threshold=semantic_threshold,
            cluster_review_floor=cluster_review_floor,
            cluster_auto_merge_threshold=cluster_auto_merge_threshold,
            cluster_review_limit=cluster_review_limit,
            embedding_client=embedding_client,
            progress_callback=workflow_progress,
            use_standard_references=use_standard_references,
            cluster_only=cluster_only,
            source_row_limit=source_row_limit,
            direct_mimo_progress_path=(
                Path(direct_mimo_progress_path)
                if direct_mimo_progress_path
                else None
            ),
            cluster_media_policy=effective_cluster_media_policy,
            enforce_cluster_admission=bool(
                options["enforce_cluster_admission"]
            ),
        )
        if preflight_summary is not None:
            summary["mimo_preflight"] = preflight_summary
        cluster_failure_guard = _cluster_failure_guard(summary)
        summary.update(cluster_failure_guard)
        _write_json_atomic(artifact_dir / "summary.json", summary)
        if summary.get("cluster_only"):
            artifacts = {
                "cluster_result": str(Path(summary["output_file"])),
                "summary": str(artifact_dir / "summary.json"),
                "audit_db": str(summary.get("audit_db") or ""),
            }
        else:
            artifacts = {
                "record_review": str(Path(summary["output_file"])),
                "topic_review": str(Path(summary["topic_review_file"])),
                "candidate_knowledge": str(Path(summary["candidate_output_file"])),
                "summary": str(artifact_dir / "summary.json"),
                "audit_db": str(summary.get("audit_db") or ""),
            }
        manifest["summary"] = summary
        manifest["artifacts"] = artifacts
        if _apply_cluster_failure_guard(
            manifest,
            store,
            cluster_failure_guard,
            cluster_only=cluster_only,
        ):
            _persist_cluster_failure_guard(manifest, store, notify)
            return manifest
        manifest["status"] = "review_pending"
        store.save(manifest)
        existing_alerts = list(manifest.get("alerts") or [])
        manifest["sla"] = evaluate_run_sla(manifest)
        manifest["alerts"] = list(
            dict.fromkeys(existing_alerts + list(manifest["sla"].get("breaches") or []))
        )
        store.save(manifest)
        notify()
        return manifest
    except Exception as exc:
        store.fail(manifest, exc)
        notify()
        return manifest


def resume_automation_pipeline(
    output_root: str | Path,
    run_id: str,
    *,
    embedding_client: EmbeddingClient | None = None,
    progress_callback: AutomationProgressCallback | None = None,
    allow_interrupted_running: bool = False,
) -> dict[str, Any]:
    current_terminology = ensure_terminology_loaded()
    store = AutomationRunStore(output_root)
    manifest = store.load(run_id)
    status = str(manifest.get("status") or "")
    if status != "failed" and not (
        allow_interrupted_running and status == "running"
    ):
        raise ValueError(
            "只有失败的自动化运行可以从检查点恢复；"
            "Ctrl+C 中断留下的 running 运行需显式允许恢复。"
        )

    run_dir = Path(str(manifest.get("run_dir") or ""))
    if not run_dir.is_dir():
        raise FileNotFoundError(f"运行目录不存在：{run_dir}")
    input_dir = run_dir / "inputs"
    artifact_dir = run_dir / "artifacts"
    source_path = input_dir / _safe_filename(
        str(manifest.get("source_name") or ""),
        "source.xlsx",
    )
    standards_name = str(manifest.get("standards_name") or "")
    standards_path = (
        input_dir / _safe_filename(standards_name, "standards.xlsx")
        if standards_name
        else None
    )
    if not source_path.is_file():
        raise FileNotFoundError(f"运行输入快照不存在：{source_path}")
    if standards_path is not None and not standards_path.is_file():
        raise FileNotFoundError(f"标准输入快照不存在：{standards_path}")

    failed_stage_index = next(
        (
            index
            for index, stage in enumerate(manifest.get("stages") or [])
            if stage.get("status") == "failed"
        ),
        -1,
    )
    if failed_stage_index < 0:
        failed_stage_index = next(
            (
                index
                for index, stage in enumerate(manifest.get("stages") or [])
                if stage.get("status") == "running"
            ),
            0,
        )
    previous_error = str(manifest.get("error") or "")
    manifest.setdefault("retry_history", []).append(
        {
            "attempt": int(manifest.get("attempt_count") or 1),
            "failed_at": manifest.get("updated_at"),
            "failed_stage": (
                (manifest.get("stages") or [{}])[failed_stage_index].get("id", "")
            ),
            "error": previous_error or "人工中断后从检查点恢复",
        }
    )
    manifest["attempt_count"] = int(manifest.get("attempt_count") or 1) + 1
    manifest["status"] = "running"
    manifest["error"] = ""
    manifest["terminology"] = current_terminology
    manifest["alerts"] = []
    manifest["sla"] = {}
    for index, stage in enumerate(manifest.get("stages") or []):
        if index >= failed_stage_index:
            stage.update(
                {
                    "status": "pending",
                    "started_at": "",
                    "finished_at": "",
                    "duration_seconds": 0.0,
                    "detail": "",
                    "metrics": {},
                }
            )
    store.save(manifest)

    def notify() -> None:
        if progress_callback:
            try:
                progress_callback(deepcopy(manifest))
            except Exception:
                pass

    def workflow_progress(
        stage_id: str,
        status: str,
        detail: str,
        metrics: dict[str, Any],
    ) -> None:
        _update_workflow_progress(
            store,
            manifest,
            stage_id,
            status,
            detail,
            metrics,
        )
        notify()

    options = manifest.get("options") or {}
    try:
        summary = initial_label_from_workbook(
            source_path=source_path,
            standards_path=standards_path,
            output_dir=artifact_dir,
            product_type=str(options.get("product_type") or ""),
            use_mimo=bool(options.get("use_mimo", True)),
            clustering_mode=str(options.get("clustering_mode") or "direct_mimo"),
            semantic_threshold=float(options.get("semantic_threshold") or 0.84),
            cluster_review_floor=float(
                options.get("cluster_review_floor")
                or DEFAULT_CLUSTER_REVIEW_FLOOR
            ),
            cluster_auto_merge_threshold=float(
                options.get("cluster_auto_merge_threshold")
                or DEFAULT_CLUSTER_AUTO_MERGE_THRESHOLD
            ),
            cluster_review_limit=int(
                options.get("cluster_review_limit")
                or DEFAULT_CLUSTER_REVIEW_LIMIT
            ),
            embedding_client=embedding_client,
            progress_callback=workflow_progress,
            use_standard_references=bool(
                options.get("use_standard_references", standards_path is not None)
            ),
            cluster_only=bool(options.get("cluster_only", False)),
            direct_mimo_progress_path=(
                Path(str(options["direct_mimo_progress_path"]))
                if options.get("direct_mimo_progress_path")
                else None
            ),
            cluster_media_policy=(
                str(options.get("cluster_media_policy") or "")
                or None
            ),
            enforce_cluster_admission=bool(
                options.get(
                    "enforce_cluster_admission",
                    not bool(options.get("cluster_only", False)),
                )
            ),
            resume=True,
        )
        manifest["summary"] = summary
        if summary.get("cluster_only"):
            manifest["artifacts"] = {
                "cluster_result": str(Path(summary["output_file"])),
                "summary": str(artifact_dir / "summary.json"),
                "audit_db": str(summary.get("audit_db") or ""),
            }
        else:
            manifest["artifacts"] = {
                "record_review": str(Path(summary["output_file"])),
                "topic_review": str(Path(summary["topic_review_file"])),
                "candidate_knowledge": str(Path(summary["candidate_output_file"])),
                "summary": str(artifact_dir / "summary.json"),
                "audit_db": str(summary.get("audit_db") or ""),
            }
        cluster_failure_guard = _cluster_failure_guard(summary)
        summary.update(cluster_failure_guard)
        _write_json_atomic(artifact_dir / "summary.json", summary)
        manifest["summary"] = summary
        if _apply_cluster_failure_guard(
            manifest,
            store,
            cluster_failure_guard,
            cluster_only=bool(options.get("cluster_only", False)),
        ):
            _persist_cluster_failure_guard(manifest, store, notify)
            return manifest
        manifest["status"] = "review_pending"
        store.save(manifest)
        manifest["sla"] = evaluate_run_sla(manifest)
        manifest["alerts"] = list(manifest["sla"].get("breaches") or [])
        store.save(manifest)
        notify()
        return manifest
    except Exception as exc:
        store.fail(manifest, exc)
        notify()
        return manifest
