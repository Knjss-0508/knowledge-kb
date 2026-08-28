from __future__ import annotations

from datetime import datetime
from functools import wraps
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
import hmac
import json
import os
import re
import threading
import uuid

from flask import Flask, jsonify, request, send_file

from .automation import AutomationRunStore
from .automation_control import (
    AutomationTaskControlError,
    AutomationTaskController,
    read_automation_log_tail,
)
from .automation_queue import (
    JOB_METADATA_SUFFIX,
    SUPPORTED_SOURCE_SUFFIXES,
    AutomationQueue,
    queue_job_metadata_path,
    read_queue_job_metadata,
    write_queue_job_metadata,
)
from .excel_io import write_rows_to_workbook
from .mimo import load_dotenv
from .run_feedback import (
    RunFeedbackStore,
    RunFeedbackStoreError,
    default_run_feedback,
)
from .run_history import list_automation_run_records, sanitize_run_text
from .workflow import SOURCE_COLUMNS


MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_SECOND_PART_BATCH_ITEMS = 100
SUPPORTED_CLUSTERING_MODES = {
    "direct_mimo",
    "semantic_mimo",
    "semantic",
    "rule",
}
PUBLIC_RUN_SUMMARY_FIELDS = {
    "source_total_rows",
    "source_rows",
    "selected_rows",
    "eligible_rows",
    "excluded_rows",
    "feature_rows",
    "model_labeled_rows",
    "topic_rows",
    "topic_stage_classified_rows",
    "topic_worthy_rows",
    "topic_unworthy_rows",
    "topic_transcribed_rows",
    "topic_transcription_skipped_rows",
    "evidence_gap_rows",
    "pending_cluster_rows",
    "pending_historical_topic_review",
    "cluster_rows",
    "cluster_admission_admitted_topics",
    "cluster_admission_pending_topics",
    "model_calls",
    "model_failed_calls",
    "model_total_tokens",
    "model_estimated_cost",
    "model_key_switches",
    "topic_signal_fallback_rows",
    "redaction_warning_findings",
}
_PATH_FIELD_NAMES = {
    "audit_db",
    "candidate_file",
    "candidate_output_file",
    "claimed_path",
    "direct_mimo_progress_path",
    "final_path",
    "output_file",
    "queue_path",
    "run_dir",
    "source_file",
    "standard_file",
    "topic_review_file",
}
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\r\n]*"
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize_second_part_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(
            _text(item)
            for item in value
            if _text(item)
        )
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _second_part_workbook_bytes(rows: list[dict[str, Any]]) -> bytes:
    with TemporaryDirectory(prefix="answer-hub-second-part-") as temp_dir:
        workbook_path = Path(temp_dir) / "second-part.xlsx"
        write_rows_to_workbook(
            {"共享数据汇总": (SOURCE_COLUMNS, rows)},
            workbook_path,
        )
        return workbook_path.read_bytes()


def _second_part_payload_fingerprint(
    source_system: str,
    items: list[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        {
            "source_system": source_system,
            "items": items,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"


def _source_for_metadata(metadata_path: Path) -> Path:
    name = metadata_path.name
    if not name.endswith(JOB_METADATA_SUFFIX):
        raise ValueError(f"无效任务元数据路径：{metadata_path}")
    return metadata_path.with_name(name[: -len(JOB_METADATA_SUFFIX)])


def _public_text(value: Any) -> str:
    return _WINDOWS_ABSOLUTE_PATH_RE.sub(
        "<redacted-path>",
        sanitize_run_text(value),
    )


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _PATH_FIELD_NAMES
            and not str(key).strip().lower().endswith(
                ("_path", "_dir", "_file")
            )
        }
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        return _public_text(value)
    return value


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _public_value(value)
        for key, value in summary.items()
        if key in PUBLIC_RUN_SUMMARY_FIELDS
    }


def _public_stage(stage: dict[str, Any]) -> dict[str, Any]:
    return _public_value(
        {
            key: stage.get(key)
            for key in (
                "id",
                "stage_id",
                "label",
                "status",
                "started_at",
                "finished_at",
                "updated_at",
                "duration_seconds",
                "detail",
                "metrics",
            )
            if key in stage
        }
    )


class AutomationJobStore:
    def __init__(
        self,
        queue_root: str | Path,
        output_root: str | Path,
    ) -> None:
        self.queue = AutomationQueue(queue_root)
        self.output_root = Path(output_root)
        self.queue.ensure()
        self.output_root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        filename: str,
        payload: bytes,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        safe_name = Path(filename or "second-part.xlsx").name.strip()
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_SOURCE_SUFFIXES:
            allowed = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
            raise ValueError(f"只支持第二部分 Excel 文件：{allowed}")
        if not payload:
            raise ValueError("上传文件不能为空")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise ValueError("文件过大，单次上传上限为 40MB")

        job_id = f"job-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        queued_name = f"{job_id}--{safe_name}"
        source_path = self.queue.pending / queued_name
        temporary_path = source_path.with_suffix(f"{source_path.suffix}.tmp")
        temporary_path.write_bytes(payload)
        temporary_path.replace(source_path)
        timestamp = _now()
        metadata = {
            "job_id": job_id,
            "status": "pending",
            "created_at": timestamp,
            "updated_at": timestamp,
            "finished_at": "",
            "original_filename": safe_name,
            "queued_filename": queued_name,
            "queue_path": str(source_path),
            "claimed_path": "",
            "final_path": "",
            "run_id": "",
            "options": dict(options),
            "summary": {},
            "artifacts": {},
            "error": "",
        }
        write_queue_job_metadata(source_path, metadata)
        return metadata

    def locate(
        self,
        job_id: str,
    ) -> tuple[Path, dict[str, Any]] | None:
        for directory in (
            self.queue.pending,
            self.queue.processing,
            self.queue.completed,
            self.queue.failed,
        ):
            for metadata_path in directory.glob(f"*{JOB_METADATA_SUFFIX}"):
                source_path = _source_for_metadata(metadata_path)
                metadata = read_queue_job_metadata(source_path)
                if str(metadata.get("job_id") or "") == job_id:
                    return source_path, metadata
        return None

    def get(self, job_id: str) -> dict[str, Any] | None:
        located = self.locate(job_id)
        if located is None:
            return None
        _source_path, metadata = located
        run_id = str(metadata.get("run_id") or "")
        if run_id:
            try:
                manifest = AutomationRunStore(self.output_root).load(run_id)
            except (OSError, json.JSONDecodeError):
                manifest = {}
            if manifest:
                metadata = {
                    **metadata,
                    "run_status": manifest.get("status"),
                    "stages": manifest.get("stages") or [],
                    "summary": manifest.get("summary") or {},
                    "artifacts": manifest.get("artifacts") or {},
                    "error": manifest.get("error") or metadata.get("error") or "",
                }
        return metadata

    def find_by_source_batch_key(
        self,
        source_batch_key: str,
    ) -> dict[str, Any] | None:
        if not source_batch_key:
            return None
        self.queue.ensure()
        for directory in (
            self.queue.pending,
            self.queue.processing,
            self.queue.completed,
            self.queue.failed,
        ):
            for metadata_path in directory.glob(f"*{JOB_METADATA_SUFFIX}"):
                source_path = _source_for_metadata(metadata_path)
                metadata = read_queue_job_metadata(source_path)
                options = metadata.get("options") or {}
                if (
                    isinstance(options, dict)
                    and _text(options.get("source_batch_key"))
                    == source_batch_key
                ):
                    return metadata
        return None

    def retry(self, job_id: str) -> dict[str, Any]:
        located = self.locate(job_id)
        if located is None:
            raise FileNotFoundError(f"任务不存在：{job_id}")
        source_path, metadata = located
        if metadata.get("status") != "failed":
            raise ValueError("只有失败任务可以重新入队")
        pending_path = self.queue.requeue(source_path)
        metadata.update(
            {
                "status": "pending",
                "updated_at": _now(),
                "finished_at": "",
                "queue_path": str(pending_path),
                "claimed_path": "",
                "final_path": "",
                "run_id": "",
                "summary": {},
                "artifacts": {},
                "error": "",
            }
        )
        write_queue_job_metadata(pending_path, metadata)
        return metadata


def _current_stage(stages: list[dict[str, Any]]) -> dict[str, Any]:
    active = next(
        (
            stage
            for stage in stages
            if stage.get("status") in {"running", "failed"}
        ),
        None,
    )
    if active:
        return active
    completed = [
        stage for stage in stages if stage.get("status") == "completed"
    ]
    return completed[-1] if completed else {}


def create_automation_api_app(
    *,
    api_key: str | None = None,
    queue_root: str | Path | None = None,
    output_root: str | Path | None = None,
    feedback_path: str | Path | None = None,
    # Legacy hooks retained for callers that embedded the original in-process
    # worker before the Windows task-controller boundary was introduced.
    run_worker: Callable[[str], dict[str, Any]] | None = None,
    start_scheduler: Callable[..., Any] | None = None,
    task_controller: Any | None = None,
    project_root: str | Path | None = None,
) -> Flask:
    load_dotenv()
    configured_key = (
        api_key
        if api_key is not None
        else os.getenv("ANSWER_HUB_API_KEY", "").strip()
    )
    job_store = AutomationJobStore(
        queue_root
        or os.getenv(
            "ANSWER_HUB_AUTOMATION_QUEUE",
            "data/automation-queue",
        ),
        output_root
        or os.getenv(
            "ANSWER_HUB_AUTOMATION_OUTPUT",
            "outputs/automation-runs",
        ),
    )
    feedback_store = RunFeedbackStore(
        feedback_path
        or os.getenv("ANSWER_HUB_RUN_FEEDBACK_PATH", "").strip()
        or (job_store.queue.root / "run_feedback.db")
    )
    if task_controller is not None:
        automation_controller = task_controller
    elif run_worker is not None:
        class _LegacyController:
            def __init__(self) -> None:
                self.enabled = False
                self.running = False
                self.last_run: dict[str, Any] = {}
                self._lock = threading.Lock()

            def status(self) -> dict[str, Any]:
                with self._lock:
                    return {
                        "installed": True,
                        "enabled": self.enabled,
                        "running": self.running,
                        "last_run": dict(self.last_run),
                    }

            def set_enabled(self, enabled: bool) -> dict[str, Any]:
                with self._lock:
                    self.enabled = bool(enabled)
                return {"enabled": self.enabled, "installed": True}

            def run_now(self) -> dict[str, Any]:
                with self._lock:
                    if self.running:
                        raise AutomationTaskControlError("已有自动化任务正在运行。")
                    self.running = True

                def worker() -> None:
                    try:
                        result = run_worker("管理员")
                        with self._lock:
                            self.last_run = dict(result or {})
                            self.last_run.setdefault("status", "completed")
                    except Exception as exc:  # pragma: no cover - legacy hook boundary
                        with self._lock:
                            self.last_run = {"status": "failed", "error": str(exc)}
                    finally:
                        with self._lock:
                            self.running = False

                threading.Thread(target=worker, daemon=True).start()
                return {"status": "accepted"}

            def retry_failed(self, _: Path) -> dict[str, Any]:
                return self.run_now()

        automation_controller = _LegacyController()
    else:
        automation_controller = AutomationTaskController()
    automation_project_root = Path(project_root or Path(__file__).resolve().parents[2])
    automation_plan_path = Path(
        os.getenv("ANSWER_HUB_AUTOMATION_PLAN_PATH", "data/automation-plan.json")
    )
    if not automation_plan_path.is_absolute():
        automation_plan_path = automation_project_root / automation_plan_path

    def read_automation_plan() -> dict[str, Any]:
        try:
            payload = json.loads(automation_plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def write_automation_plan(payload: dict[str, Any]) -> None:
        automation_plan_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = automation_plan_path.with_suffix(automation_plan_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(automation_plan_path)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    def require_api_key(
        view: Callable[..., Any],
    ) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            provided = request.headers.get("X-Answer-Hub-Key", "")
            if not configured_key or not hmac.compare_digest(
                provided,
                configured_key,
            ):
                return jsonify({"error": "unauthorized"}), 401
            return view(*args, **kwargs)

        return wrapped

    def job_payload(metadata: dict[str, Any]) -> dict[str, Any]:
        stages = list(metadata.get("stages") or [])
        active = _current_stage(stages)
        job_id = str(metadata.get("job_id") or "")
        artifacts = metadata.get("artifacts") or {}
        return {
            **metadata,
            "current_stage": {
                "id": active.get("id", ""),
                "label": active.get("label", ""),
                "status": active.get("status", ""),
                "detail": active.get("detail", ""),
            },
            "status_url": f"{request.url_root.rstrip('/')}/api/v1/automation/jobs/{job_id}",
            "retry_url": f"{request.url_root.rstrip('/')}/api/v1/automation/jobs/{job_id}/retry",
            "artifact_urls": {
                name: (
                    f"{request.url_root.rstrip('/')}/api/v1/automation/jobs/"
                    f"{job_id}/artifacts/{name}"
                )
                for name, path in artifacts.items()
                if str(path or "").strip()
            },
        }

    def public_run_record(
        record: dict[str, Any],
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        record_id = str(record.get("record_id") or "")
        artifact_names = sorted(
            str(name)
            for name, path in dict(record.get("artifacts") or {}).items()
            if str(path or "").strip()
        )
        return {
            "record_id": record_id,
            "job_id": str(record.get("job_id") or ""),
            "run_id": str(record.get("run_id") or ""),
            "source_type": str(record.get("source_type") or ""),
            "source_label": _public_text(record.get("source_label")),
            "source_name": _public_text(record.get("source_name")),
            "queue_status": str(record.get("queue_status") or ""),
            "run_status": str(record.get("run_status") or ""),
            "effective_status": str(
                record.get("effective_status") or ""
            ),
            "status_label": _public_text(record.get("status_label")),
            "health_status": str(record.get("health_status") or ""),
            "health_label": _public_text(record.get("health_label")),
            "is_stale": bool(record.get("is_stale")),
            "stale_seconds": int(record.get("stale_seconds") or 0),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "finished_at": str(record.get("finished_at") or ""),
            "sync_to_cz_review": bool(record.get("sync_to_cz_review")),
            "summary": _public_summary(dict(record.get("summary") or {})),
            "cz_sync": _public_value(dict(record.get("cz_sync") or {})),
            "stages": [
                _public_stage(dict(stage))
                for stage in list(record.get("stages") or [])
                if isinstance(stage, dict)
            ],
            "current_stage": _public_stage(
                dict(record.get("current_stage") or {})
            ),
            "activity_history": [
                _public_stage(dict(activity))
                for activity in list(record.get("activity_history") or [])
                if isinstance(activity, dict)
            ],
            "error": _public_text(record.get("error")),
            "alerts": _public_value(list(record.get("alerts") or [])),
            "attempt_count": int(record.get("attempt_count") or 1),
            "attempt_runs": _public_value(
                list(record.get("attempt_runs") or [])
            ),
            "retry_history": _public_value(
                list(record.get("retry_history") or [])
            ),
            "duration_seconds": record.get("duration_seconds"),
            "artifact_names": artifact_names,
            "feedback": _public_value(feedback),
        }

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "service": "answer-hub-automation-api",
                "authentication_configured": bool(configured_key),
            }
        )

    def automation_control_snapshot() -> dict[str, Any]:
        try:
            snapshot = dict(automation_controller.status())
        except AutomationTaskControlError as exc:
            return {
                "installed": False,
                "enabled": False,
                "running": False,
                "message": f"无法读取自动化计划任务状态：{exc}",
                "available": False,
            }
        snapshot["available"] = bool(snapshot.get("installed"))
        plan = read_automation_plan()
        snapshot["plan"] = {
            "second_part_query_from_date": str(plan.get("second_part_query_from_date") or ""),
            "second_part_query_to_date": str(plan.get("second_part_query_to_date") or ""),
            "knowledge_settle_from_date": str(
                plan.get("knowledge_settle_from_date")
                or plan.get("second_part_query_from_date")
                or ""
            ),
            "knowledge_settle_to_date": str(
                plan.get("knowledge_settle_to_date")
                or plan.get("second_part_query_to_date")
                or ""
            ),
            "timezone": str(plan.get("timezone") or "Asia/Shanghai"),
        }
        # Keep the legacy root-level field for older clients.
        snapshot["timezone"] = snapshot["plan"]["timezone"]
        snapshot["schedule_enabled"] = bool(plan.get("schedule_enabled"))
        snapshot["schedule_time"] = str(plan.get("schedule_time") or "02:00")
        return snapshot

    @app.get("/api/v1/automation/control")
    @require_api_key
    def get_automation_control():
        return jsonify(automation_control_snapshot())

    @app.patch("/api/v1/automation/control")
    @require_api_key
    def update_automation_control():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return jsonify({"error": "enabled 必须是布尔值。"}), 400
        from_date = str(body.get("second_part_query_from_date") or "").strip()
        to_date = str(body.get("second_part_query_to_date") or "").strip()
        from_date = str(
            body.get("knowledge_settle_from_date") or from_date
        ).strip()
        to_date = str(
            body.get("knowledge_settle_to_date") or to_date
        ).strip()
        if bool(from_date) != bool(to_date):
            return jsonify({"error": "第二部分采集开始日期和结束日期必须同时填写。"}), 400
        if from_date and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_date) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_date) or from_date > to_date):
            return jsonify({"error": "第二部分采集日期范围无效，请使用 YYYY-MM-DD 且开始日期不晚于结束日期。"}), 400
        schedule_enabled = body.get("schedule_enabled")
        if schedule_enabled is not None and not isinstance(schedule_enabled, bool):
            return jsonify({"error": "schedule_enabled 必须是布尔值。"}), 400
        schedule_time = str(body.get("schedule_time") or "02:00").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
            return jsonify({"error": "schedule_time 必须是 HH:MM 格式。"}), 400
        try:
            result = automation_controller.set_enabled(body["enabled"])
        except AutomationTaskControlError as exc:
            allow_uninstalled = _bool_value(
                os.getenv("ANSWER_HUB_AUTOMATION_ALLOW_UNINSTALLED_CONTROL"),
                False,
            )
            if not allow_uninstalled:
                return jsonify({"error": _public_text(str(exc))}), 409
            result = {
                "enabled": bool(body["enabled"]),
                "message": "本地测试模式：未安装 Windows 计划任务，仅保存控制状态和采集范围。",
                "installed": False,
            }
        plan = read_automation_plan()
        plan.update({
            "second_part_query_from_date": from_date,
            "second_part_query_to_date": to_date,
            "knowledge_settle_from_date": from_date,
            "knowledge_settle_to_date": to_date,
            "schedule_enabled": bool(schedule_enabled) if schedule_enabled is not None else bool(plan.get("schedule_enabled")),
            "schedule_time": schedule_time,
            "timezone": str(body.get("timezone") or plan.get("timezone") or "Asia/Shanghai"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        write_automation_plan(plan)
        snapshot = automation_control_snapshot()
        return jsonify({
            **result,
            **{
                key: snapshot.get(key)
                for key in (
                    "enabled",
                    "running",
                    "schedule_enabled",
                    "schedule_time",
                    "timezone",
                    "last_run",
                )
                if key in snapshot
            },
            "control": snapshot,
        })

    @app.post("/api/v1/automation/runs")
    @require_api_key
    def start_controlled_automation_run():
        control = automation_control_snapshot()
        if not control.get("enabled"):
            return jsonify({"error": "自动化总开关已关闭，请先启用后再立即执行。"}), 409
        if control.get("running"):
            return jsonify({"error": "已有自动化任务正在运行。"}), 409
        records = list_automation_run_records(
            job_store.output_root,
            job_store.queue.root,
            limit=100,
        )
        if any(
            str(record.get("effective_status") or "")
            in {"processing", "running"}
            for record in records
        ):
            return jsonify({"error": "已有自动化任务正在运行，请等待完成后再试。"}), 409
        try:
            result = automation_controller.run_now()
        except AutomationTaskControlError as exc:
            return jsonify({"error": _public_text(str(exc))}), 409
        return jsonify({"status": "accepted", **result}), 202

    @app.post("/api/v1/automation/retry-failed")
    @require_api_key
    def retry_failed_automation_runs():
        control = automation_control_snapshot()
        if not control.get("enabled"):
            return jsonify({"error": "自动化已暂停，不能重试失败任务。"}), 409
        if control.get("running"):
            return jsonify({"error": "已有自动化任务正在运行。"}), 409
        try:
            result = automation_controller.retry_failed(automation_project_root)
        except AutomationTaskControlError as exc:
            return jsonify({"error": _public_text(str(exc))}), 409
        return jsonify({"status": "accepted", **result}), 202

    @app.get("/api/v1/automation/logs/latest")
    @require_api_key
    def get_latest_automation_log():
        lines = max(1, min(_int_value(request.args.get("lines"), 120), 200))
        log = read_automation_log_tail(automation_project_root, lines=lines)
        return jsonify(
            {
                "name": _public_text(log.get("name")),
                "content": _public_text(log.get("content")),
            }
        )

    @app.post("/api/v1/automation/jobs")
    @require_api_key
    def create_job():
        upload = request.files.get("source_file") or request.files.get("source")
        if upload is None or not upload.filename:
            return jsonify({"error": "请上传第二部分 Excel 文件"}), 400
        clustering_mode = (
            request.form.get("clustering_mode") or "direct_mimo"
        ).strip()
        if clustering_mode not in SUPPORTED_CLUSTERING_MODES:
            return jsonify({"error": "不支持的聚类模式"}), 400
        sync_to_cz_review = _bool_value(
            request.form.get("sync_to_cz_review")
            if request.form.get("sync_to_cz_review") is not None
            else request.form.get("submit_to_cz"),
            False,
        )
        options = {
            "product_type": (request.form.get("product_type") or "").strip(),
            "use_mimo": _bool_value(request.form.get("use_mimo"), True),
            "clustering_mode": clustering_mode,
            "semantic_threshold": _float_value(
                request.form.get("semantic_threshold"),
                0.84,
            ),
            "cluster_review_floor": _float_value(
                request.form.get("cluster_review_floor"),
                0.75,
            ),
            "cluster_auto_merge_threshold": _float_value(
                request.form.get("cluster_auto_merge_threshold"),
                0.92,
            ),
            "cluster_review_limit": _int_value(
                request.form.get("cluster_review_limit"),
                100,
            ),
            "continue_on_mimo_unavailable": _bool_value(
                request.form.get("continue_on_mimo_unavailable"),
                False,
            ),
            "sync_to_cz_review": sync_to_cz_review,
            "submit_to_cz": sync_to_cz_review,
        }
        try:
            metadata = job_store.create(
                upload.filename,
                upload.read(),
                options,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(job_payload(metadata)), 202

    @app.post("/api/v1/automation/second-part/records:batch")
    @require_api_key
    def receive_second_part_records():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "请求体必须是 JSON 对象"}), 400
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return jsonify({"error": "items 必须是非空数组"}), 400
        if len(items) > MAX_SECOND_PART_BATCH_ITEMS:
            return jsonify(
                {
                    "error": (
                        "单次第二部分数据提交最多 "
                        f"{MAX_SECOND_PART_BATCH_ITEMS} 条。"
                    )
                }
            ), 400

        source_system = _text(body.get("source_system")) or "second-part"
        if len(source_system) > 100:
            return jsonify({"error": "source_system 不能超过 100 个字符"}), 400
        idempotency_key = _text(body.get("idempotency_key"))
        if not idempotency_key:
            return jsonify({"error": "idempotency_key 不能为空"}), 400
        if len(idempotency_key) > 200:
            return jsonify({"error": "idempotency_key 不能超过 200 个字符"}), 400
        raw_options = body.get("options") or {}
        if not isinstance(raw_options, dict):
            return jsonify({"error": "options 必须是 JSON 对象"}), 400
        clustering_mode = _text(
            raw_options.get("clustering_mode") or "direct_mimo"
        )
        if clustering_mode not in SUPPORTED_CLUSTERING_MODES:
            return jsonify({"error": "不支持的聚类模式"}), 400

        rows: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                return jsonify(
                    {"error": f"items[{index}] 必须是 JSON 对象"}
                ), 400
            if _text(item.get("redaction_status")).lower() != "redacted":
                return jsonify(
                    {
                        "error": (
                            f"items[{index}] 未标记为已脱敏，"
                            "不允许进入自动化队列。"
                        )
                    }
                ), 400
            record = item.get("record")
            if not isinstance(record, dict) or not record:
                return jsonify(
                    {"error": f"items[{index}].record 必须是非空 JSON 对象"}
                ), 400
            row = {
                column: _normalize_second_part_cell(record.get(column))
                for column in SOURCE_COLUMNS
            }
            if row["序号"] in (None, ""):
                row["序号"] = index
            if row["上传者"] in (None, ""):
                row["上传者"] = source_system
            if not any(value not in (None, "") for value in row.values()):
                return jsonify(
                    {"error": f"items[{index}].record 不能是空记录"}
                ), 400
            rows.append(row)

        source_batch_key = (
            "sha256:"
            + sha256(
                f"{source_system}\x00{idempotency_key}".encode("utf-8")
            ).hexdigest()
        )
        payload_fingerprint = _second_part_payload_fingerprint(
            source_system,
            items,
        )
        existing = job_store.find_by_source_batch_key(source_batch_key)
        if existing is not None:
            existing_options = existing.get("options") or {}
            if (
                isinstance(existing_options, dict)
                and _text(existing_options.get("source_payload_fingerprint"))
                and _text(existing_options.get("source_payload_fingerprint"))
                != payload_fingerprint
            ):
                return jsonify(
                    {
                        "error": (
                            "幂等键已被其他数据批次使用，"
                            "请为变更后的数据使用新的 idempotency_key。"
                        )
                    }
                ), 409
            response = job_payload(existing)
            response.update(
                {
                    "reused": True,
                    "accepted_records": len(rows),
                }
            )
            return jsonify(response), 200

        sync_to_cz_review = _bool_value(
            raw_options.get("sync_to_cz_review")
            if "sync_to_cz_review" in raw_options
            else raw_options.get("submit_to_cz"),
            False,
        )
        options = {
            "product_type": _text(raw_options.get("product_type")),
            "use_mimo": _bool_value(raw_options.get("use_mimo"), True),
            "clustering_mode": clustering_mode,
            "semantic_threshold": _float_value(
                raw_options.get("semantic_threshold"),
                0.84,
            ),
            "cluster_review_floor": _float_value(
                raw_options.get("cluster_review_floor"),
                0.75,
            ),
            "cluster_auto_merge_threshold": _float_value(
                raw_options.get("cluster_auto_merge_threshold"),
                0.92,
            ),
            "cluster_review_limit": _int_value(
                raw_options.get("cluster_review_limit"),
                100,
            ),
            "continue_on_mimo_unavailable": _bool_value(
                raw_options.get("continue_on_mimo_unavailable"),
                False,
            ),
            "sync_to_cz_review": sync_to_cz_review,
            "submit_to_cz": sync_to_cz_review,
            "source_system": source_system,
            "source_batch_key": source_batch_key,
            "source_idempotency_key": idempotency_key,
            "source_payload_fingerprint": payload_fingerprint,
            "source_record_count": len(rows),
        }
        source_name = re.sub(
            r"[^A-Za-z0-9._-]+",
            "-",
            source_system,
        ).strip("-._") or "second-part"
        try:
            metadata = job_store.create(
                f"{source_name}-records.xlsx",
                _second_part_workbook_bytes(rows),
                options,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        response = job_payload(metadata)
        response.update(
            {
                "reused": False,
                "accepted_records": len(rows),
            }
        )
        return jsonify(response), 202

    @app.get("/api/v1/automation/jobs")
    @require_api_key
    def list_jobs():
        limit = max(
            1,
            min(_int_value(request.args.get("limit"), 100), 500),
        )
        stale_after_seconds = max(
            60,
            _int_value(
                request.args.get("stale_after_seconds"),
                _int_value(
                    os.getenv("ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS"),
                    7_200,
                ),
            ),
        )
        records = list_automation_run_records(
            job_store.output_root,
            job_store.queue.root,
            limit=limit,
            stale_after_seconds=stale_after_seconds,
        )
        record_ids = [
            str(record.get("record_id") or "")
            for record in records
        ]
        try:
            feedback_by_record = feedback_store.get_many(record_ids)
        except RunFeedbackStoreError as exc:
            return jsonify({"error": str(exc)}), 503
        items = [
            public_run_record(
                record,
                feedback_by_record.get(record_id)
                or default_run_feedback(record_id),
            )
            for record, record_id in zip(records, record_ids)
        ]
        return jsonify({"count": len(items), "items": items})

    @app.get("/api/v1/automation/jobs/<job_id>")
    @require_api_key
    def get_job(job_id: str):
        metadata = job_store.get(job_id)
        if metadata is None:
            return jsonify({"error": "job not found"}), 404
        return jsonify(job_payload(metadata))

    @app.patch("/api/v1/automation/jobs/<record_id>/feedback")
    @require_api_key
    def update_job_feedback(record_id: str):
        records = list_automation_run_records(
            job_store.output_root,
            job_store.queue.root,
            limit=500,
        )
        if not any(
            str(record.get("record_id") or "") == record_id
            for record in records
        ):
            return jsonify({"error": "job not found"}), 404
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "JSON对象格式不正确"}), 400
        try:
            previous = feedback_store.get(record_id) or default_run_feedback(
                record_id
            )
            feedback = feedback_store.update(
                record_id,
                status=str(body.get("status") or previous["status"]),
                owner=str(body.get("owner", previous["owner"]) or ""),
                cause_type=str(
                    body.get("cause_type", previous["cause_type"]) or ""
                ),
                note=str(body.get("note", previous["note"]) or ""),
                actor=str(body.get("actor", previous["actor"]) or ""),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RunFeedbackStoreError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"record_id": record_id, "feedback": feedback})

    @app.post("/api/v1/automation/jobs/<job_id>/retry")
    @require_api_key
    def retry_job(job_id: str):
        try:
            metadata = job_store.retry(job_id)
        except FileNotFoundError:
            return jsonify({"error": "job not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(job_payload(metadata)), 202

    @app.get("/api/v1/automation/jobs/<job_id>/artifacts/<artifact_name>")
    @require_api_key
    def download_artifact(job_id: str, artifact_name: str):
        metadata = job_store.get(job_id)
        if metadata is None:
            return jsonify({"error": "job not found"}), 404
        run_id = str(metadata.get("run_id") or "")
        artifact_value = str(
            (metadata.get("artifacts") or {}).get(artifact_name) or ""
        )
        if not run_id or not artifact_value:
            return jsonify({"error": "artifact not found"}), 404
        run_dir = (job_store.output_root / run_id).resolve()
        artifact_path = Path(artifact_value)
        if not artifact_path.is_absolute():
            artifact_path = Path.cwd() / artifact_path
        artifact_path = artifact_path.resolve()
        if not artifact_path.is_relative_to(run_dir) or not artifact_path.is_file():
            return jsonify({"error": "artifact not found"}), 404
        return send_file(
            artifact_path,
            as_attachment=True,
            download_name=artifact_path.name,
        )

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "文件过大，单次上传上限为 40MB"}), 413

    return app


def main() -> None:
    load_dotenv()
    api_key = os.getenv("ANSWER_HUB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANSWER_HUB_API_KEY 未配置；请通过环境变量设置 API 鉴权密钥。"
        )
    host = os.getenv("ANSWER_HUB_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _int_value(os.getenv("ANSWER_HUB_API_PORT"), 8780)
    create_automation_api_app(api_key=api_key).run(
        host=host,
        port=port,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
