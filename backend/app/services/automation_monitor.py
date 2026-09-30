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


# ---------------------------------------------------------------------------
# Answer Hub HTTP 409 的分类
#
# Answer Hub 的冲突报文把原因统一放在 ``error`` 字段里，而不是 ``detail`` 或
# ``message``；原因文本还可能是操作系统本地化文案（例如 schtasks 的中文报错），
# 甚至在编码不一致时整句退化成 U+FFFD 替换字符。所以分类顺序固定为：
#   1) 响应体里的结构化字段（kind/code/... 优先，其次是 error/detail/message/...）
#   2) 文本关键词（中英文都覆盖）
#   3) 「计划任务相关标记 + 不可解码文本」兜底
# 这样「用户看得懂」不依赖某一台机器的语言与编码。
# ---------------------------------------------------------------------------
_CONFLICT_TEXT_FIELDS = (
    "error", "detail", "message", "reason", "description",
    "error_description", "error_message", "status_message",
)
_CONFLICT_KIND_FIELDS = (
    "kind", "category", "error_code", "code", "reason_code", "type", "status",
)
_STRUCTURED_CONFLICT_KINDS = {
    "already_running": "already_running",
    "running": "already_running",
    "concurrent_run": "already_running",
    "concurrent_running": "already_running",
    "queue_blocked": "queue_blocked",
    "queue_unresolved_failed": "queue_blocked",
    "unresolved_failed": "queue_blocked",
    "failed_queue_not_resolved": "queue_blocked",
    "paused": "paused",
    "automation_paused": "paused",
    "scheduler_unavailable": "scheduler_unavailable",
    "task_not_installed": "scheduler_unavailable",
    "scheduler_not_installed": "scheduler_unavailable",
    "not_installed": "scheduler_unavailable",
}
# 原实现用 "running" 子串判定 already_running，这里刻意保留同样的容错度。
_ALREADY_RUNNING_MARKERS = ("running", "正在运行", "并发", "已有自动化任务")
_QUEUE_BLOCKED_MARKERS = (
    "未解决的 failed", "未解决的失败", "存在未解决", "存在未处理的失败",
    "failed 文件", "失败文件", "unresolved failed", "unresolved_failure",
    "queue blocked", "blocked by failed",
)
_SCHEDULER_MISSING_MARKERS = (
    "不存在指定的任务名", "找不到指定的任务名", "不存在指定的任务",
    "系统找不到指定的", "尚未安装", "未安装", "未返回详细错误",
    "does not exist in the system", "not exist in the system", "does not exist",
    "cannot find", "can not find", "can't find", "no such file", "not installed",
)
_PAUSED_MARKERS = ("自动化已暂停", "请先启用", "paused")
_SCHEDULER_CONTEXT_MARKERS = (
    "answerhubautomationqueue", "answer-hub-queue.timer", "answer-hub-queue.service",
    "schtasks", "systemctl", "计划任务", "任务计划程序",
)
_REPLACEMENT_CHAR = "\ufffd"
_MAX_DETAIL_LENGTH = 2000


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


def _text_variants(text: str) -> list[str]:
    """Return the raw text plus a best-effort repair of UTF-8-as-GBK mojibake.

    Answer Hub 在 Windows 上用 UTF-8 解码本地化命令输出，会把 GBK 中文变成
    替换字符；能救回来的片段（例如「系统」）拼回去后仍可用于关键词匹配。
    """
    variants = [text]
    try:
        repaired = text.encode("utf-8", "ignore").decode("gbk", "ignore")
    except (UnicodeError, LookupError):
        repaired = ""
    if repaired and repaired != text:
        variants.append(repaired)
    return variants


def _collect_conflict_fields(payload: Any) -> tuple[str, str]:
    """Flatten an Answer Hub conflict body into (structured kind, combined text)."""
    kinds: list[str] = []
    texts: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key).strip().lower()
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
                    continue
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    continue
                text = str(value).strip()
                if not text:
                    continue
                if name in _CONFLICT_KIND_FIELDS:
                    kinds.append(text)
                if name in _CONFLICT_TEXT_FIELDS:
                    texts.append(text)
            return
        if isinstance(node, list):
            for item in node[:8]:
                walk(item, depth + 1)

    walk(payload, 0)
    return " ".join(kinds), " | ".join(texts)


def classify_conflict(payload: Any, raw_body: str = "") -> tuple[str, str]:
    """Classify an Answer Hub HTTP 409 into a user-understandable error kind.

    Returns ``(kind, detail)``. ``detail`` only feeds logging and the finer
    wording decision inside :func:`monitor_error_detail`; it is never echoed to
    the user verbatim, so an upstream path or secret cannot leak.
    """
    kind_field, text_field = _collect_conflict_fields(payload)
    detail = (text_field or raw_body or "").strip()[:_MAX_DETAIL_LENGTH]
    normalized_kind = re.sub(r"[^a-z0-9]+", "_", kind_field.strip().lower()).strip("_")
    mapped = _STRUCTURED_CONFLICT_KINDS.get(normalized_kind)
    if mapped:
        return mapped, detail

    haystack = " ".join(_text_variants(detail)).lower()
    if any(marker in haystack for marker in _ALREADY_RUNNING_MARKERS):
        return "already_running", detail
    if any(marker in haystack for marker in _QUEUE_BLOCKED_MARKERS):
        return "queue_blocked", detail
    if any(marker in haystack for marker in _SCHEDULER_MISSING_MARKERS):
        return "scheduler_unavailable", detail
    if any(marker in haystack for marker in _PAUSED_MARKERS):
        return "paused", detail
    # 计划任务命令按操作系统语言输出；Answer Hub 把本地化文本按 UTF-8 解码后
    # 整句会退化成替换字符，任何关键词都命中不了。此时只要报文里还留着计划任务
    # 的稳定 ASCII 标记（计划任务名 / schtasks / systemd 单元名），就说明这是
    # 计划任务不可用，而不是业务状态冲突。
    if _REPLACEMENT_CHAR in detail and any(
        marker in haystack for marker in _SCHEDULER_CONTEXT_MARKERS
    ):
        return "scheduler_unavailable", detail
    return "rejected", detail


def monitor_error_message(kind: str) -> str:
    return {
        "not_configured": "运行监管服务尚未配置，请联系管理员完成服务端配置。",
        "authentication": "运行监管服务的访问密钥无效或缺失，请联系管理员检查服务端配置。",
        "rejected": "当前自动化状态不允许这个操作。请先点「刷新」拉取最新状态，再重试。",
        "invalid_request": "运行监管请求无效，请刷新页面后重试。",
        "unavailable": "暂时无法连接 Answer Hub 服务，请检查服务和网络。",
        "invalid_response": "Answer Hub 服务返回异常，请联系管理员检查服务。",
        "queue_blocked": (
            "自动化队列里还有没处理完的失败文件，必须先处理这些失败项才能继续；"
            "请点「查看运行日志」确认失败原因，处理完再执行。"
        ),
        "scheduler_unavailable": (
            "无法在运行 Answer Hub 的电脑上操作自动化计划任务，计划任务可能没有安装或已被删除；"
            "请联系管理员在该电脑上确认自动化计划任务后再试。"
        ),
        "paused": "自动化当前是「已停止」状态，请先点「开始自动化流程」，再执行这个操作。",
    }.get(kind, "运行监管服务暂不可用，请稍后刷新。")


def monitor_error_detail(kind: str, detail: str = "") -> str:
    """Return a safe, user-facing message for a gateway rejection."""
    if kind == "already_running":
        return "已有自动化任务正在运行，请等待当前任务完成后再执行。"
    if kind == "scheduler_unavailable":
        lowered = " ".join(_text_variants(detail)).lower()
        if any(marker in lowered for marker in _SCHEDULER_MISSING_MARKERS):
            return (
                "运行 Answer Hub 的电脑上还没有安装这个自动化计划任务，自动化无法开始或停止；"
                "请联系管理员在该电脑上安装自动化计划任务后再试。"
            )
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
                raw_body = ""
                try:
                    raw_body = exc.read().decode("utf-8", "replace")
                except OSError:
                    raw_body = ""
                payload: Any = None
                if raw_body.strip():
                    try:
                        payload = json.loads(raw_body)
                    except json.JSONDecodeError:
                        payload = None
                kind, detail = classify_conflict(payload, raw_body)
                raise AutomationMonitorError(
                    kind, 409, str(sanitize_monitor_value(detail))
                ) from exc
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
        settle_from = str(job.get("settle_from_date") or "").strip()
        settle_to = str(job.get("settle_to_date") or "").strip()
        if not str(job.get("batch_name") or "").strip():
            job["batch_name"] = (
                f"{settle_from} 至 {settle_to}"
                if settle_from and settle_to
                else "历史批次（未记录沉淀范围）"
            )
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
                    "message": str(
                        job.get("error")
                        or (job.get("current_stage") or {}).get("detail")
                        or ("CZ 候选同步失败。" if cz_failed else "任务需要人工处理。")
                    ),
                    "updated_at": str(job.get("updated_at") or ""),
                })
    jobs.sort(key=lambda item: (0 if str(item.get("health_status")) in {"stalled", "failed"} else 1, str(item.get("updated_at") or "")), reverse=False)
    return {"service": {"status": "online" if health.get("status") == "ok" else "unknown", "message": ""}, "control": control, "summary": summary, "jobs": jobs, "issues": issues}


def safe_record_id(record_id: str) -> str:
    return quote(record_id, safe="")
