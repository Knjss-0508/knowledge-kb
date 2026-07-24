from __future__ import annotations

from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable
import hmac
import json
import os
import uuid

from flask import Flask, jsonify, request, send_file

from .automation import AutomationRunStore
from .automation_queue import (
    JOB_METADATA_SUFFIX,
    SUPPORTED_SOURCE_SUFFIXES,
    AutomationQueue,
    queue_job_metadata_path,
    read_queue_job_metadata,
    write_queue_job_metadata,
)
from .mimo import load_dotenv


MAX_UPLOAD_BYTES = 40 * 1024 * 1024
SUPPORTED_CLUSTERING_MODES = {
    "direct_mimo",
    "semantic_mimo",
    "semantic",
    "rule",
}


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


def _source_for_metadata(metadata_path: Path) -> Path:
    name = metadata_path.name
    if not name.endswith(JOB_METADATA_SUFFIX):
        raise ValueError(f"无效任务元数据路径：{metadata_path}")
    return metadata_path.with_name(name[: -len(JOB_METADATA_SUFFIX)])


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

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "service": "answer-hub-automation-api",
                "authentication_configured": bool(configured_key),
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

    @app.get("/api/v1/automation/jobs/<job_id>")
    @require_api_key
    def get_job(job_id: str):
        metadata = job_store.get(job_id)
        if metadata is None:
            return jsonify({"error": "job not found"}), 404
        return jsonify(job_payload(metadata))

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
