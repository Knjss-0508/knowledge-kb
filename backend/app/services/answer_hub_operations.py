from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_PRIVATE_FIELD_NAMES = {
    "artifacts",
    "manifest",
    "metadata",
    "options",
    "queue_root",
    "output_root",
    "run_dir",
    "profile_path",
    "state_path",
    "log_path",
    "source_conversation_url",
}
_PRIVATE_FIELD_SUFFIXES = ("_path", "_url", "_token", "_secret", "_password")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s'\"]+")
_UNIX_PATH_RE = re.compile(r"(?<![\w/])/(?:app|data|home|opt|srv|tmp|var)/[^\s'\"]+")
_QUERY_RE = re.compile(r"\?[^\s'\"]+")


class AnswerHubGatewayError(RuntimeError):
    def __init__(self, kind: str, *, status_code: int = 503) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


def sanitize_answer_hub_payload(value: Any) -> Any:
    """Remove data that must never cross the CZ browser boundary."""

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower()
            if (
                normalized in _PRIVATE_FIELD_NAMES
                or normalized.endswith(_PRIVATE_FIELD_SUFFIXES)
                or normalized in {"api_key", "authorization", "cookie", "secret"}
            ):
                continue
            sanitized[key] = sanitize_answer_hub_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_answer_hub_payload(item) for item in value]
    if isinstance(value, str):
        text = _QUERY_RE.sub("?<redacted>", value)
        text = _WINDOWS_PATH_RE.sub("<redacted-path>", text)
        return _UNIX_PATH_RE.sub("<redacted-path>", text)
    return value


class AnswerHubGateway:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.opener = opener

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise AnswerHubGatewayError("not_configured")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers = {"X-Answer-Hub-Key": self.api_key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise AnswerHubGatewayError("authentication") from exc
            if exc.code == 409:
                raise AnswerHubGatewayError("rejected", status_code=409) from exc
            if exc.code == 400:
                raise AnswerHubGatewayError("invalid_request", status_code=400) from exc
            raise AnswerHubGatewayError("unavailable") from exc
        except (URLError, TimeoutError, OSError):
            raise AnswerHubGatewayError("unavailable")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AnswerHubGatewayError("invalid_response") from exc
        if not isinstance(parsed, dict):
            raise AnswerHubGatewayError("invalid_response")
        return sanitize_answer_hub_payload(parsed)


def answer_hub_error_message(kind: str) -> str:
    return {
        "not_configured": "Answer Hub 服务尚未配置，请由运维在服务端补齐配置。",
        "authentication": "Answer Hub 服务鉴权异常，请由运维检查服务端配置。",
        "rejected": "Answer Hub 拒绝了本次操作，请先刷新页面确认当前运行状态。",
        "invalid_request": "本次自动化控制参数无效，请刷新页面后重试。",
        "unavailable": "暂时无法连接 Answer Hub 服务，请检查服务和网络。",
        "invalid_response": "Answer Hub 服务返回异常，请由运维检查服务。",
    }.get(kind, "Answer Hub 服务暂不可用，请稍后刷新。")


def build_answer_hub_overview(
    gateway: AnswerHubGateway,
    *,
    waiting_review: int,
) -> dict[str, Any]:
    if not gateway.configured:
        return {
            "service": {"status": "not_configured", "label": "未配置"},
            "control": {"enabled": False, "schedule_enabled": False, "schedule_time": "02:00", "timezone": "Asia/Shanghai", "running": False, "last_run": {}},
            "metrics": {"queue_tasks": 0, "processing": 0, "failed": 0, "waiting_review": waiting_review, "stalled": 0, "recent_pull_records": 0, "recent_queued_records": 0, "recent_quarantined_records": 0},
            "jobs": [],
            "problems": [{"type": "configuration", "message": answer_hub_error_message("not_configured"), "suggestion": "请联系运维完成服务端配置。"}],
        }
    try:
        health = gateway.request("GET", "/health")
        control = gateway.request("GET", "/api/v1/automation/control")
        jobs_response = gateway.request("GET", "/api/v1/automation/jobs?limit=100")
    except AnswerHubGatewayError as exc:
        return {
            "service": {"status": exc.kind, "label": "不可用"},
            "control": {"enabled": False, "schedule_enabled": False, "schedule_time": "02:00", "timezone": "Asia/Shanghai", "running": False, "last_run": {}},
            "metrics": {"queue_tasks": 0, "processing": 0, "failed": 0, "waiting_review": waiting_review, "stalled": 0, "recent_pull_records": 0, "recent_queued_records": 0, "recent_quarantined_records": 0},
            "jobs": [],
            "problems": [{"type": exc.kind, "message": answer_hub_error_message(exc.kind), "suggestion": "请检查服务端状态后重试。"}],
        }

    jobs = jobs_response.get("items") if isinstance(jobs_response.get("items"), list) else []
    jobs = [job for job in jobs if isinstance(job, dict)][:100]
    processing = sum(
        str(job.get("effective_status") or "") in {"pending", "processing", "running"}
        for job in jobs
    )
    failed = sum(str(job.get("health_status") or "") == "failed" for job in jobs)
    stalled = sum(str(job.get("health_status") or "") == "stalled" for job in jobs)
    problems = []
    for job in jobs:
        health_status = str(job.get("health_status") or "")
        error = str(job.get("error") or "").strip()
        if health_status not in {"failed", "stalled", "attention"} and not error:
            continue
        problems.append(
            {
                "job_id": str(job.get("job_id") or job.get("record_id") or ""),
                "stage": str((job.get("current_stage") or {}).get("label") or "运行阶段"),
                "type": health_status or "error",
                "message": error or ("任务超过阈值未更新，疑似卡住。" if health_status == "stalled" else "任务需要人工处理。"),
                "suggestion": "检查服务状态后，可由超级管理员发起失败重试。",
            }
        )
        if len(problems) >= 10:
            break
    last_run = control.get("last_run") if isinstance(control.get("last_run"), dict) else {}
    return {
        "service": {
            "status": "online" if health.get("status") == "ok" else "unknown",
            "label": "在线" if health.get("status") == "ok" else "状态未知",
        },
        "control": control,
        "metrics": {
            "queue_tasks": len(jobs),
            "processing": processing,
            "failed": failed,
            "waiting_review": waiting_review,
            "stalled": stalled,
            "recent_pull_records": int(last_run.get("fetched_records") or 0),
            "recent_queued_records": int(last_run.get("queued_jobs") or 0),
            "recent_quarantined_records": int(last_run.get("rejected_records") or 0),
        },
        "jobs": jobs,
        "problems": problems,
    }
