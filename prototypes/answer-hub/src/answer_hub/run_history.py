from datetime import datetime
import os
from pathlib import Path
from typing import Any
import re

from .automation import AutomationRunStore
from .automation_queue import (
    JOB_METADATA_SUFFIX,
    QUEUE_DIRECTORY_NAMES,
    SUPPORTED_SOURCE_SUFFIXES,
    read_queue_job_metadata,
)


RUN_STATUS_LABELS = {
    "pending": "排队中",
    "processing": "运行中",
    "running": "运行中",
    "completed": "已完成",
    "review_pending": "待人工审核",
    "needs_confirmation": "等待人工确认",
    "failed": "运行失败",
}
RUN_HEALTH_LABELS = {
    "healthy": "正常",
    "active": "运行中",
    "stalled": "疑似卡住",
    "attention": "需要处理",
    "review_pending": "待人工审核",
    "failed": "运行失败",
    "completed": "已完成",
    "unknown": "未知",
}
_URL_QUERY_RE = re.compile(
    r"(?P<base>https?://[^\s?#]+)\?[^\s#]*",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"['\"]?\b("
    r"(?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|integration[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token|authorization|password|passwd|secret)"
    r")\b['\"]?\s*[:=]\s*['\"]?"
    r"(?:Bearer\s+)?[^'\"\s,;}\]]+['\"]?",
    re.IGNORECASE,
)


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def sanitize_run_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = _URL_QUERY_RE.sub(
        lambda match: f"{match.group('base')}?<redacted>",
        text,
    )
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )


def _safe_stage(stage: dict[str, Any]) -> dict[str, Any]:
    safe = dict(stage)
    safe["detail"] = sanitize_run_text(stage.get("detail"))
    safe["metrics"] = {
        key: sanitize_run_text(value) if isinstance(value, str) else value
        for key, value in dict(stage.get("metrics") or {}).items()
    }
    return safe


def _safe_activity(activity: dict[str, Any]) -> dict[str, Any]:
    safe = _sanitize_payload(dict(activity))
    stage_id = str(safe.get("stage_id") or safe.get("id") or "")
    safe["stage_id"] = stage_id
    safe["id"] = stage_id
    return safe


def _safe_retry_history(
    retry_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _sanitize_payload(dict(item))
        for item in retry_history
    ]


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_run_text(value)
    return value


def _current_stage(stages: list[dict[str, Any]]) -> dict[str, Any]:
    active = next(
        (
            stage
            for stage in stages
            if str(stage.get("status") or "") in {"running", "failed"}
        ),
        None,
    )
    if active:
        return dict(active)
    completed = [
        stage
        for stage in stages
        if str(stage.get("status") or "") == "completed"
    ]
    return dict(completed[-1]) if completed else {}


def _effective_status(
    queue_status: str,
    run_status: str,
) -> str:
    if run_status in {"failed", "needs_confirmation"}:
        return run_status
    if queue_status == "failed":
        return "failed"
    if queue_status == "pending":
        return "pending"
    if queue_status == "processing":
        return "running"
    return run_status or queue_status or "pending"


def _latest_timestamp(*values: Any) -> str:
    timestamps = [
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    ]
    return max(timestamps, default="")


def _timestamp_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _sla_seconds_per_100_rows() -> int:
    """单个阶段的耗时预算系数（每 100 行允许的秒数）。

    与 ANSWER_HUB_SLA_SECONDS_PER_100_ROWS 保持一致，默认 900 秒。
    用于判断「运行中」的批次是否真的超时，而不是只看最后更新时间。
    """

    try:
        return max(60, int(os.getenv("ANSWER_HUB_SLA_SECONDS_PER_100_ROWS", "900")))
    except (TypeError, ValueError):
        return 900


def _run_health(
    record: dict[str, Any],
    *,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any]:
    status = str(record.get("effective_status") or "")
    activity = record.get("current_activity") or {}
    activity_updated_at = _timestamp_datetime(
        activity.get("updated_at")
        if isinstance(activity, dict) and activity.get("updated_at")
        else ""
    )
    updated_at = _timestamp_datetime(
        record.get("updated_at") or record.get("created_at")
    )
    heartbeat_at = max(
        (value for value in (updated_at, activity_updated_at) if value is not None),
        default=None,
    )
    reference_now = now
    if reference_now.tzinfo is None:
        reference_now = reference_now.astimezone()
    stale_seconds = (
        max(0, int((reference_now - heartbeat_at).total_seconds()))
        if heartbeat_at is not None
        else 0
    )
    # 关键：不能只按「多久没更新」判卡住。
    # updated_at 只在阶段切换时更新，而「会话语义标注」这类阶段在限速 2 req/s、
    # 3000+ 条的规模下本来就要跑数小时，期间完全没有写入。按固定阈值判断必然
    # 把正常长跑误报成「疑似卡住」。
    # 因此这里改为「从当前阶段开始到现在的耗时」是否超过该阶段的合理预算：
    # 预算 = 基础阈值 + 按行数摊薄的 SLA（ANSWER_HUB_SLA_SECONDS_PER_100_ROWS）。
    current_stage = next(
        (
            stage
            for stage in record.get("stages") or []
            if str(stage.get("status") or "") == "running"
        ),
        None,
    )
    stage_started_at = _timestamp_datetime(
        current_stage.get("started_at")
        if isinstance(current_stage, dict)
        else ""
    )
    # 没有阶段信息（例如排队中、或旧版本清单）时，退回按最后更新时间判断。
    if stage_started_at is not None:
        stage_elapsed_seconds = max(
            0, int((reference_now - stage_started_at).total_seconds())
        )
    else:
        stage_elapsed_seconds = stale_seconds
    summary = record.get("summary") or {}
    row_count = 0
    for key in ("eligible_rows", "total_rows", "topic_rows"):
        try:
            value = int(summary.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            row_count = value
            break
    stage_budget_seconds = max(60, int(stale_after_seconds))
    if row_count > 0:
        stage_budget_seconds += int(row_count / 100 * _sla_seconds_per_100_rows())
    # 允许 2 倍余量，避免网络抖动/模型排队导致的正常波动被误判。
    stage_budget_seconds = int(stage_budget_seconds * 2)
    if stage_started_at is None:
        # 无阶段信息：沿用「多久没更新」的保守判定。
        is_stale = bool(
            status in {"pending", "processing", "running"}
            and heartbeat_at is not None
            and stale_seconds >= max(60, int(stale_after_seconds))
        )
    else:
        is_stale = bool(
            status in {"pending", "processing", "running"}
            and stage_elapsed_seconds >= stage_budget_seconds
        )
    if is_stale:
        health_status = "stalled"
    elif status == "failed":
        health_status = "failed"
    elif status == "needs_confirmation":
        health_status = "attention"
    elif status == "review_pending":
        health_status = "review_pending"
    elif status in {"pending", "processing", "running"}:
        health_status = "active"
    elif status == "completed":
        health_status = "completed"
    elif status:
        health_status = "healthy"
    else:
        health_status = "unknown"
    return {
        "health_status": health_status,
        "health_label": RUN_HEALTH_LABELS[health_status],
        "is_stale": is_stale,
        "stale_seconds": stale_seconds,
    }


def _queue_metadata_entries(
    queue_root: Path,
) -> list[tuple[dict[str, Any], str, Path]]:
    entries: list[tuple[dict[str, Any], str, Path]] = []
    for directory_name in QUEUE_DIRECTORY_NAMES:
        if directory_name == "logs":
            continue
        directory = queue_root / directory_name
        if not directory.is_dir():
            continue
        for metadata_path in directory.glob(f"*{JOB_METADATA_SUFFIX}"):
            source_path = metadata_path.with_name(
                metadata_path.name[: -len(JOB_METADATA_SUFFIX)]
            )
            metadata = read_queue_job_metadata(source_path)
            if not metadata:
                continue
            entries.append((metadata, directory_name, source_path))
    return entries


def _record(
    *,
    metadata: dict[str, Any] | None,
    queue_bucket: str,
    source_path: Path | None,
    manifest: dict[str, Any] | None,
    attempt_manifests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    job = metadata or {}
    run = manifest or {}
    job_id = str(job.get("job_id") or "")
    run_id = str(run.get("run_id") or job.get("run_id") or "")
    queue_status = str(job.get("status") or queue_bucket or "")
    run_status = str(run.get("status") or job.get("run_status") or "")
    effective_status = _effective_status(queue_status, run_status)
    stages = [
        _safe_stage(stage)
        for stage in list(run.get("stages") or job.get("stages") or [])
    ]
    current_activity = _safe_activity(
        dict(run.get("current_activity") or {})
    )
    activity_history = [
        _safe_activity(dict(item))
        for item in list(run.get("activity_history") or [])
        if isinstance(item, dict)
    ]
    summary = dict(run.get("summary") or job.get("summary") or {})
    artifacts = dict(run.get("artifacts") or job.get("artifacts") or {})
    options = {
        **dict(job.get("options") or {}),
        **dict(run.get("options") or {}),
    }
    queue_manifest = run.get("queue") or {}
    # `queue` 只在批次跑完后才由 automation_queue 写入；运行中或中途被终止的批次
    # 里没有这个字段，旧逻辑会掉进最后的 else 分支，被误标成「Streamlit手动验证」。
    # 队列任务的文件名恒为 `job-<时间戳>-<随机>--<原文件名>`，据此兜底识别。
    looks_like_queue_job = bool(
        str(run.get("source_name") or job.get("original_filename") or "").startswith(
            "job-"
        )
    )
    if job_id:
        source_type = "online_api"
        source_label = "线上接口"
    elif queue_manifest or (queue_bucket and source_path is not None) or looks_like_queue_job:
        source_type = "automation_queue"
        source_label = "自动化队列"
    else:
        source_type = "streamlit"
        source_label = "Streamlit手动验证"
    source_name = str(
        job.get("original_filename")
        or run.get("source_name")
        or (source_path.name if source_path is not None else "")
    )
    error = sanitize_run_text(
        run.get("error") or job.get("error") or ""
    )
    alerts = [
        sanitize_run_text(alert)
        for alert in list(run.get("alerts") or job.get("alerts") or [])
    ]
    cz_sync = _sanitize_payload(
        dict(
            summary.get("cz_candidate_sync")
            or summary.get("cz_submission")
            or {}
        )
    )
    attempts = attempt_manifests or ([run] if run else [])
    attempt_runs = [
        {
            "run_id": str(attempt.get("run_id") or ""),
            "status": str(attempt.get("status") or ""),
            "status_label": RUN_STATUS_LABELS.get(
                str(attempt.get("status") or ""),
                str(attempt.get("status") or ""),
            ),
            "created_at": str(attempt.get("created_at") or ""),
            "updated_at": str(attempt.get("updated_at") or ""),
            "error": sanitize_run_text(attempt.get("error")),
            "current_stage": (
                _safe_activity(
                    dict(attempt.get("current_activity") or {})
                )
                or _current_stage(
                    [
                        _safe_stage(stage)
                        for stage in list(attempt.get("stages") or [])
                    ]
                )
            ),
        }
        for attempt in attempts
    ]
    safe_manifest = (
        {
            **run,
            "error": error,
            "alerts": alerts,
            "stages": stages,
            "current_activity": current_activity,
            "activity_history": activity_history,
            "retry_history": _safe_retry_history(
                list(run.get("retry_history") or [])
            ),
        }
        if run
        else {}
    )
    safe_metadata = {
        **job,
        "error": sanitize_run_text(job.get("error")),
        "alerts": [
            sanitize_run_text(alert)
            for alert in list(job.get("alerts") or [])
        ],
    }
    queue_record_id = (
        f"queue:{source_path.name}"
        if queue_bucket and source_path is not None
        else ""
    )
    settle_from_date = str(
        options.get("knowledge_settle_from_date")
        or options.get("second_part_query_from_date")
        or ""
    ).strip()
    settle_to_date = str(
        options.get("knowledge_settle_to_date")
        or options.get("second_part_query_to_date")
        or ""
    ).strip()
    batch_name = (
        f"沉淀 {settle_from_date} 至 {settle_to_date}"
        if settle_from_date and settle_to_date
        else ""
    )
    return {
        "record_id": (
            job_id
            or queue_record_id
            or run_id
        ),
        "job_id": job_id,
        "run_id": run_id,
        "source_type": source_type,
        "source_label": source_label,
        "source_name": source_name,
        "batch_name": batch_name,
        "settle_from_date": settle_from_date,
        "settle_to_date": settle_to_date,
        "queue_status": queue_status,
        "run_status": run_status,
        "effective_status": effective_status,
        "status_label": RUN_STATUS_LABELS.get(
            effective_status,
            effective_status,
        ),
        "created_at": str(
            job.get("created_at") or run.get("created_at") or ""
        ),
        "updated_at": _latest_timestamp(
            job.get("updated_at"),
            run.get("updated_at"),
        ),
        "finished_at": str(job.get("finished_at") or ""),
        "sync_to_cz_review": _bool_value(
            options.get("sync_to_cz_review")
            if "sync_to_cz_review" in options
            else options.get("submit_to_cz")
        ),
        "summary": summary,
        "cz_sync": cz_sync,
        "artifacts": artifacts,
        "stages": stages,
        "current_stage": current_activity or _current_stage(stages),
        "activity_history": activity_history,
        "error": error,
        "alerts": alerts,
        "options": options,
        "queue_bucket": queue_bucket,
        "attempt_count": sum(
            max(1, int(attempt.get("attempt_count") or 1))
            for attempt in attempts
        )
        or 1,
        "attempt_runs": attempt_runs,
        "retry_history": _safe_retry_history(
            list(run.get("retry_history") or [])
        ),
        "duration_seconds": run.get("duration_seconds"),
        "run_dir": str(run.get("run_dir") or ""),
        "manifest": safe_manifest,
        "metadata": safe_metadata,
    }


def _queue_sources_without_metadata(
    queue_root: Path,
    metadata_sources: set[Path],
) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    for directory_name in QUEUE_DIRECTORY_NAMES:
        if directory_name == "logs":
            continue
        directory = queue_root / directory_name
        if not directory.is_dir():
            continue
        try:
            directory_entries = list(directory.iterdir())
        except OSError:
            continue
        for source_path in directory_entries:
            try:
                should_skip = (
                    not source_path.is_file()
                    or source_path.name.startswith("~$")
                    or source_path.suffix.lower()
                    not in SUPPORTED_SOURCE_SUFFIXES
                    or source_path.resolve() in metadata_sources
                )
            except OSError:
                continue
            if not should_skip:
                sources.append((directory_name, source_path))
    return sources


def _resolved_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return path.resolve()
    except OSError:
        return None


def list_automation_run_records(
    output_root: str | Path,
    queue_root: str | Path,
    *,
    limit: int = 100,
    stale_after_seconds: int = 7_200,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    store = AutomationRunStore(output_root)
    manifests = store.list(limit=max(100, int(limit) * 2))
    manifests_by_run_id = {
        str(manifest.get("run_id") or ""): manifest
        for manifest in manifests
        if str(manifest.get("run_id") or "")
    }
    run_id_by_queue_path: dict[Path, str] = {}
    run_ids_by_source_name: dict[str, list[str]] = {}
    for run_id, manifest in manifests_by_run_id.items():
        source_name = str(manifest.get("source_name") or "").strip()
        if source_name:
            run_ids_by_source_name.setdefault(source_name, []).append(run_id)
        queue_manifest = manifest.get("queue") or {}
        for field in ("source_path", "claimed_path", "final_path"):
            if path := _resolved_path(queue_manifest.get(field)):
                run_id_by_queue_path[path] = run_id
    records: list[dict[str, Any]] = []
    queue_path = Path(queue_root)
    metadata_entries = _queue_metadata_entries(queue_path)
    metadata_sources = {
        source_path.resolve()
        for _metadata, _queue_bucket, source_path in metadata_entries
    }
    for metadata, queue_bucket, source_path in metadata_entries:
        explicit_run_id = str(metadata.get("run_id") or "")
        job_id = str(metadata.get("job_id") or "")
        related_run_ids = {
            run_id
            for run_id, manifest in manifests_by_run_id.items()
            if job_id
            and str(manifest.get("source_name") or "").startswith(
                f"{job_id}--"
            )
        }
        if explicit_run_id in manifests_by_run_id:
            related_run_ids.add(explicit_run_id)
        if not related_run_ids and not explicit_run_id:
            queued_filename = str(
                metadata.get("queued_filename") or source_path.name
            ).strip()
            matching_run_ids = [
                candidate_run_id
                for candidate_run_id in run_ids_by_source_name.get(
                    queued_filename,
                    [],
                )
                if candidate_run_id in manifests_by_run_id
            ]
            if len(matching_run_ids) == 1:
                related_run_ids.add(matching_run_ids[0])
        related_manifests = sorted(
            (
                manifests_by_run_id[run_id]
                for run_id in related_run_ids
                if run_id in manifests_by_run_id
            ),
            key=lambda manifest: (
                str(
                    manifest.get("updated_at")
                    or manifest.get("created_at")
                    or ""
                ),
                str(manifest.get("run_id") or ""),
            ),
            reverse=True,
        )
        manifest = None
        if explicit_run_id:
            manifest = manifests_by_run_id.get(explicit_run_id)
        elif str(metadata.get("status") or "") == "processing":
            manifest = next(
                (
                    candidate
                    for candidate in related_manifests
                    if str(candidate.get("status") or "") == "running"
                ),
                related_manifests[0] if related_manifests else None,
            )
        elif str(metadata.get("status") or "") in {"completed", "failed"}:
            manifest = related_manifests[0] if related_manifests else None
        if manifest is not None:
            current_run_id = str(manifest.get("run_id") or "")
            related_manifests = [
                manifest,
                *[
                    candidate
                    for candidate in related_manifests
                    if str(candidate.get("run_id") or "")
                    != current_run_id
                ],
            ]
        for related_run_id in related_run_ids:
            manifests_by_run_id.pop(related_run_id, None)
        records.append(
            _record(
                metadata=metadata,
                queue_bucket=queue_bucket,
                source_path=source_path,
                manifest=manifest,
                attempt_manifests=related_manifests,
            )
        )
    for queue_bucket, source_path in _queue_sources_without_metadata(
        queue_path,
        metadata_sources,
    ):
        try:
            resolved_source = source_path.resolve()
            modified_at = source_path.stat().st_mtime
        except OSError:
            continue
        matched_run_id = run_id_by_queue_path.get(resolved_source, "")
        related_run_ids = {
            run_id
            for run_id in run_ids_by_source_name.get(source_path.name, [])
            if run_id in manifests_by_run_id
            and (
                bool(manifests_by_run_id[run_id].get("queue"))
                or (
                    queue_bucket == "processing"
                    and str(
                        manifests_by_run_id[run_id].get("status") or ""
                    )
                    == "running"
                )
            )
        }
        if matched_run_id:
            related_run_ids.add(matched_run_id)
        related_manifests = sorted(
            (
                manifests_by_run_id[run_id]
                for run_id in related_run_ids
                if run_id in manifests_by_run_id
            ),
            key=lambda candidate: (
                str(
                    candidate.get("updated_at")
                    or candidate.get("created_at")
                    or ""
                ),
                str(candidate.get("run_id") or ""),
            ),
            reverse=True,
        )
        manifest = None
        if matched_run_id:
            manifest = manifests_by_run_id.get(matched_run_id)
        elif queue_bucket == "processing":
            manifest = next(
                (
                    candidate
                    for candidate in related_manifests
                    if str(candidate.get("status") or "") == "running"
                ),
                None,
            )
        elif queue_bucket in {"completed", "failed"}:
            manifest = related_manifests[0] if related_manifests else None
        if manifest is not None:
            current_run_id = str(manifest.get("run_id") or "")
            related_manifests = [
                manifest,
                *[
                    candidate
                    for candidate in related_manifests
                    if str(candidate.get("run_id") or "")
                    != current_run_id
                ],
            ]
        for related_run_id in related_run_ids:
            manifests_by_run_id.pop(related_run_id, None)
        timestamp = datetime.fromtimestamp(
            modified_at
        ).astimezone().isoformat(timespec="seconds")
        records.append(
            _record(
                metadata={
                    "status": queue_bucket,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "original_filename": source_path.name,
                },
                queue_bucket=queue_bucket,
                source_path=source_path,
                manifest=manifest,
                attempt_manifests=related_manifests,
            )
        )
    for manifest in manifests_by_run_id.values():
        queue_manifest = manifest.get("queue") or {}
        records.append(
            _record(
                metadata=None,
                queue_bucket=str(
                    queue_manifest.get("disposition") or ""
                ),
                source_path=None,
                manifest=manifest,
                attempt_manifests=[manifest],
            )
        )
    reference_now = now or datetime.now().astimezone()
    for record in records:
        record.update(
            _run_health(
                record,
                now=reference_now,
                stale_after_seconds=stale_after_seconds,
            )
        )
    records.sort(
        key=lambda record: (
            record.get("updated_at") or record.get("created_at") or "",
            record.get("record_id") or "",
        ),
        reverse=True,
    )
    return records[: max(1, int(limit))]
