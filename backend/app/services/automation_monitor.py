from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


_PRIVATE_KEYS = {
    "api_key", "authorization", "cookie", "secret", "token", "password",
    "artifacts", "metadata", "options", "run_dir", "queue_root", "output_root",
}
_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\r\n\s'\"]+")
_UNIX_PATH = re.compile(r"(?<![\w/])/(?:app|data|home|opt|srv|tmp|var)/[^\r\n\s'\"]+")
_QUERY = re.compile(r"\?[^\s'\"]+")


class AutomationMonitorError(RuntimeError):
    def __init__(self, kind: str, status_code: int = 503, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code
        self.detail = detail


def sanitize_monitor_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_monitor_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _PRIVATE_KEYS
            and not str(key).strip().lower().endswith(
                ("_path", "_token", "_secret", "_password")
            )
        }
    if isinstance(value, list):
        return [sanitize_monitor_value(item) for item in value]
    if isinstance(value, str):
        text = _QUERY.sub("?<redacted>", value)
        text = _WINDOWS_PATH.sub("<redacted-path>", text)
        return _UNIX_PATH.sub("<redacted-path>", text)
    return value


def monitor_error_message(kind: str) -> str:
    return {
        "not_configured": "运行监管服务尚未配置，请联系管理员完成服务端配置。",
        "authentication": "运行监管服务鉴权异常，请联系管理员检查服务端配置。",
        "rejected": "当前自动化状态不允许执行该操作，请刷新后重试。",
        "invalid_request": "运行监管请求无效，请刷新页面后重试。",
        "unavailable": "暂时无法连接 Answer Hub 服务，请检查服务和网络。",
        "invalid_response": "Answer Hub 服务返回异常，请联系管理员检查服务。",
    }.get(kind, "运行监管服务暂不可用，请稍后刷新。")


def monitor_error_detail(kind: str, detail: str = "") -> str:
    """Return a safe, user-facing message for a gateway rejection."""
    if kind == "already_running":
        return "已有自动化任务正在运行，请等待当前任务完成后再执行。"
    return monitor_error_message(kind)


class AutomationMonitorGateway:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.configured:
            raise AutomationMonitorError("not_configured")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers = {"X-Answer-Hub-Key": self.api_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise AutomationMonitorError("authentication") from exc
            if exc.code == 409:
                detail = ""
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                    if isinstance(payload, dict):
                        detail = str(payload.get("detail") or payload.get("message") or "")
                except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                    pass
                lowered = detail.lower()
                kind = "already_running" if "running" in lowered or "正在运行" in detail or "并发" in detail else "rejected"
                raise AutomationMonitorError(kind, 409, detail) from exc
            if exc.code == 400:
                raise AutomationMonitorError("invalid_request", 400) from exc
            raise AutomationMonitorError("unavailable") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AutomationMonitorError("unavailable") from exc
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AutomationMonitorError("invalid_response") from exc
        if not isinstance(decoded, dict):
            raise AutomationMonitorError("invalid_response")
        return sanitize_monitor_value(decoded)


def build_monitor_overview(gateway: AutomationMonitorGateway, limit: int = 100) -> dict[str, Any]:
    empty = {
        "service": {"status": "unavailable", "message": ""},
        "control": {"installed": False, "enabled": False, "running": False},
        "summary": {"pending": 0, "running": 0, "attention": 0, "completed": 0, "cz_sync_failed": 0},
        "jobs": [], "issues": [],
    }
    if not gateway.configured:
        empty["service"] = {"status": "not_configured", "message": monitor_error_message("not_configured")}
        return empty
    try:
        health = gateway.request("GET", "/health")
        control = gateway.request("GET", "/api/v1/automation/control")
        jobs_data = gateway.request("GET", f"/api/v1/automation/jobs?limit={max(1, min(limit, 100))}")
    except AutomationMonitorError as exc:
        empty["service"] = {"status": exc.kind, "message": monitor_error_message(exc.kind)}
        return empty
    jobs = [item for item in jobs_data.get("items", []) if isinstance(item, dict)]
    summary = {"pending": 0, "running": 0, "attention": 0, "completed": 0, "cz_sync_failed": 0}
    issues: list[dict[str, Any]] = []
    for job in jobs:
        state = str(job.get("effective_status") or "")
        health_state = str(job.get("health_status") or "")
        if state == "pending": summary["pending"] += 1
        if state in {"processing", "running"}: summary["running"] += 1
        if state in {"completed", "done"}: summary["completed"] += 1
        cz_sync = job.get("cz_sync") if isinstance(job.get("cz_sync"), dict) else {}
        cz_failed = int(cz_sync.get("failed") or 0)
        summary["cz_sync_failed"] += cz_failed
        if health_state in {"failed", "stalled", "attention"} or cz_failed:
            summary["attention"] += 1
            if len(issues) < 3:
                issues.append({
                    "record_id": str(job.get("record_id") or ""),
                    "stage": str((job.get("current_stage") or {}).get("label") or "运行阶段"),
                    "status": health_state or ("cz_sync_failed" if cz_failed else "attention"),
                    "message": str(job.get("error") or ("CZ 候选同步失败。" if cz_failed else "任务需要人工处理。")),
                    "updated_at": str(job.get("updated_at") or ""),
                })
    jobs.sort(key=lambda item: (0 if str(item.get("health_status")) in {"stalled", "failed"} else 1, str(item.get("updated_at") or "")), reverse=False)
    return {"service": {"status": "online" if health.get("status") == "ok" else "unknown", "message": ""}, "control": control, "summary": summary, "jobs": jobs, "issues": issues}


def safe_record_id(record_id: str) -> str:
    return quote(record_id, safe="")
