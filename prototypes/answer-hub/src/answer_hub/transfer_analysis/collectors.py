from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
import hashlib
import json
import re
import time
import uuid

from .store import TransferAnalysisStore


SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
}


class CollectorConfigurationError(ValueError):
    pass


class AuthExpiredError(RuntimeError):
    pass


def _json_path_get(value: Any, path: str, default: Any = None) -> Any:
    if path in ("", "$", None):
        return value
    current = value
    for raw_part in str(path).strip("$.").split("."):
        if current is None:
            return default
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", raw_part)
        if not match:
            return default
        key, index = match.groups()
        if key:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        if index is not None:
            if not isinstance(current, list) or int(index) >= len(current):
                return default
            current = current[int(index)]
    return current


def _pick_path(value: Any, path: str | list[str] | tuple[str, ...], default: Any = "") -> Any:
    paths = path if isinstance(path, (list, tuple)) else [path]
    for candidate in paths:
        result = _json_path_get(value, str(candidate), None)
        if result not in (None, "", [], {}):
            return result
    return default


def _render_template(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_template(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    full_match = re.fullmatch(r"\{\{([a-zA-Z0-9_]+)\}\}", value)
    if full_match:
        return variables.get(full_match.group(1), "")
    rendered = value
    for key, item in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(item))
    return rendered


def _redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _json_shape(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key): _json_shape(item, depth + 1) for key, item in list(value.items())[:80]}
    if isinstance(value, list):
        return [_json_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _safe_source_id(value: Any, prefix: str, payload: Any) -> str:
    text = str(value or "").strip()
    if text:
        return text
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class EndpointOperation:
    method: str
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    items_path: str = ""
    record_path: str = ""
    total_path: str = ""
    page_size: int = 100
    page_start: int = 1
    max_pages: int = 1000
    field_map: dict[str, str | list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EndpointOperation":
        if not payload.get("path"):
            raise CollectorConfigurationError("接口操作缺少 path")
        headers = {str(k): str(v) for k, v in dict(payload.get("headers") or {}).items()}
        forbidden = [key for key in headers if key.lower() in SENSITIVE_KEYS]
        if forbidden:
            raise CollectorConfigurationError(
                f"接口模板不能保存敏感请求头：{', '.join(forbidden)}"
            )
        return cls(
            method=str(payload.get("method") or "GET").upper(),
            path=str(payload["path"]),
            query=dict(payload.get("query") or {}),
            body=dict(payload.get("body") or {}),
            headers=headers,
            items_path=str(payload.get("items_path") or ""),
            record_path=str(payload.get("record_path") or ""),
            total_path=str(payload.get("total_path") or ""),
            page_size=max(1, min(int(payload.get("page_size") or 100), 1000)),
            page_start=max(0, int(payload.get("page_start") or 1)),
            max_pages=max(1, min(int(payload.get("max_pages") or 1000), 10000)),
            field_map=dict(payload.get("field_map") or {}),
        )


@dataclass(frozen=True)
class EndpointProfile:
    system: str
    base_url: str
    login_url: str
    profile_dir: str
    operations: dict[str, EndpointOperation]
    channel: str = "chrome"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EndpointProfile":
        system = str(payload.get("system") or "").strip().lower()
        if system not in {"manhattan", "baixiaosheng"}:
            raise CollectorConfigurationError("system 仅支持 manhattan 或 baixiaosheng")
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url:
            raise CollectorConfigurationError("接口模板缺少 base_url")
        operations = {
            str(name): EndpointOperation.from_dict(dict(operation))
            for name, operation in dict(payload.get("operations") or {}).items()
        }
        if not operations:
            raise CollectorConfigurationError("接口模板至少需要一个 operation")
        return cls(
            system=system,
            base_url=base_url,
            login_url=str(payload.get("login_url") or base_url),
            profile_dir=str(
                payload.get("profile_dir")
                or f"data/browser_profiles/{system}"
            ),
            operations=operations,
            channel=str(payload.get("channel") or "chrome"),
        )

    @classmethod
    def load(cls, path: str | Path) -> "EndpointProfile":
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise CollectorConfigurationError("接口模板根节点必须是 JSON 对象")
        return cls.from_dict(payload)


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 Playwright。请执行：python -m pip install -e \".[collector]\""
        ) from exc
    return sync_playwright


def discover_network_requests(
    system: str,
    login_url: str,
    output_path: str | Path,
    profile_dir: str | Path,
    *,
    timeout_seconds: int = 900,
    channel: str = "chrome",
) -> dict[str, Any]:
    """Open a persistent browser and save sanitized JSON/XHR request shapes.

    The user completes login and closes the browser when endpoint exploration is
    finished. Response bodies are never persisted; only their JSON key shapes
    are saved.
    """

    sync_playwright = _load_playwright()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile.resolve()),
            channel=channel,
            headless=False,
        )

        def record_request(request) -> None:
            resource_type = str(request.resource_type or "")
            if resource_type not in {"xhr", "fetch"}:
                return
            post_data: Any = request.post_data
            if post_data:
                try:
                    post_data = json.loads(post_data)
                except (TypeError, json.JSONDecodeError):
                    post_data = str(post_data)[:2000]
            records.append(
                {
                    "kind": "request",
                    "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "system": system,
                    "method": request.method,
                    "url": request.url,
                    "resource_type": resource_type,
                    "post_data": _redact(post_data),
                }
            )

        def record_response(response) -> None:
            request = response.request
            resource_type = str(request.resource_type or "")
            if resource_type not in {"xhr", "fetch"}:
                return
            content_type = str(response.headers.get("content-type") or "")
            shape: Any = None
            if "json" in content_type.lower():
                try:
                    shape = _json_shape(response.json())
                except Exception:
                    shape = {"error": "response JSON shape unavailable"}
            records.append(
                {
                    "kind": "response",
                    "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "system": system,
                    "method": request.method,
                    "url": response.url,
                    "status": response.status,
                    "content_type": content_type,
                    "json_shape": shape,
                }
            )

        context.on("request", record_request)
        context.on("response", record_response)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_url, wait_until="domcontentloaded")
        deadline = time.monotonic() + max(30, int(timeout_seconds))
        while time.monotonic() < deadline:
            if not context.pages:
                break
            time.sleep(0.5)
        context.close()

    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {
        "system": system,
        "output": str(output),
        "request_records": sum(item["kind"] == "request" for item in records),
        "response_records": sum(item["kind"] == "response" for item in records),
    }


class _ConfiguredPlaywrightSession:
    def __init__(self, profile: EndpointProfile, *, headless: bool = True) -> None:
        self.profile = profile
        self.headless = headless
        self._manager = None
        self._context = None

    def __enter__(self):
        sync_playwright = _load_playwright()
        self._manager = sync_playwright().start()
        Path(self.profile.profile_dir).mkdir(parents=True, exist_ok=True)
        self._context = self._manager.chromium.launch_persistent_context(
            user_data_dir=str(Path(self.profile.profile_dir).resolve()),
            channel=self.profile.channel,
            headless=self.headless,
        )
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._context is not None:
            self._context.close()
        if self._manager is not None:
            self._manager.stop()

    def request(
        self,
        operation: EndpointOperation,
        variables: dict[str, Any],
    ) -> Any:
        if self._context is None:
            raise RuntimeError("Playwright session is not open")
        url = urljoin(
            self.profile.base_url.rstrip("/") + "/",
            str(_render_template(operation.path, variables)).lstrip("/"),
        )
        query = _render_template(operation.query, variables)
        body = _render_template(operation.body, variables)
        headers = {"Accept": "application/json", **operation.headers}
        if body:
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
        response = self._context.request.fetch(
            url,
            method=operation.method,
            params=query or None,
            data=body or None,
            headers=headers,
            fail_on_status_code=False,
            timeout=30000,
        )
        content_type = str(response.headers.get("content-type") or "")
        text = response.text()
        if response.status in {401, 403} or "text/html" in content_type.lower():
            raise AuthExpiredError(
                f"{self.profile.system} 登录态失效，请重新执行人工登录。"
            )
        if response.status >= 400:
            raise RuntimeError(
                f"{self.profile.system} API HTTP {response.status}: {text[:300]}"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{self.profile.system} API 返回非 JSON：{text[:300]}"
            ) from exc

    def fetch_items(
        self,
        operation_name: str,
        variables: dict[str, Any],
        *,
        paginate: bool = False,
    ) -> list[Any]:
        operation = self.profile.operations.get(operation_name)
        if operation is None:
            raise CollectorConfigurationError(
                f"接口模板缺少 operation：{operation_name}"
            )
        if not paginate:
            payload = self.request(operation, variables)
            items = _json_path_get(payload, operation.items_path, payload)
            if isinstance(items, list):
                return items
            if isinstance(items, dict):
                return [items]
            return []

        results: list[Any] = []
        for offset in range(operation.max_pages):
            page = operation.page_start + offset
            current_variables = {
                **variables,
                "page": page,
                "page_size": operation.page_size,
                "offset": offset * operation.page_size,
            }
            payload = self.request(operation, current_variables)
            items = _json_path_get(payload, operation.items_path, [])
            if not isinstance(items, list):
                raise CollectorConfigurationError(
                    f"{operation_name}.items_path 没有指向数组"
                )
            results.extend(items)
            total = _json_path_get(payload, operation.total_path, None)
            if not items:
                break
            if isinstance(total, (int, float)) and len(results) >= int(total):
                break
            if total in (None, "") and len(items) < operation.page_size:
                break
        return results

    def fetch_record(
        self,
        operation_name: str,
        variables: dict[str, Any],
    ) -> Any:
        operation = self.profile.operations.get(operation_name)
        if operation is None:
            raise CollectorConfigurationError(
                f"接口模板缺少 operation：{operation_name}"
            )
        payload = self.request(operation, variables)
        return _json_path_get(payload, operation.record_path, payload)


def _normalize_mapping(item: Any, field_map: dict[str, str | list[str]]) -> dict[str, Any]:
    return {
        canonical: _pick_path(item, source_path, "")
        for canonical, source_path in field_map.items()
    }


def _conversation_text(messages: Any) -> str:
    if isinstance(messages, str):
        return messages.strip()
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            lines.append(str(item))
            continue
        role = (
            item.get("role")
            or item.get("senderType")
            or item.get("speaker")
            or item.get("sender")
            or "未知"
        )
        content = (
            item.get("content")
            or item.get("text")
            or item.get("message")
            or item.get("body")
            or ""
        )
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _first_last_from_messages(messages: Any) -> tuple[str, str]:
    if not isinstance(messages, list):
        return "", ""
    first_question = ""
    last_answer = ""
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(
            item.get("role")
            or item.get("senderType")
            or item.get("speaker")
            or item.get("sender")
            or ""
        ).lower()
        content = str(
            item.get("content")
            or item.get("text")
            or item.get("message")
            or item.get("body")
            or ""
        ).strip()
        if not content:
            continue
        if not first_question and any(
            marker in role for marker in ("user", "engineer", "工程师", "用户", "human")
        ):
            first_question = content
        if any(marker in role for marker in ("assistant", "bot", "百晓生", "机器人", "ai")):
            last_answer = content
    return first_question, last_answer


def _normalize_transfer(
    item: Any,
    operation: EndpointOperation,
) -> dict[str, Any]:
    mapped = _normalize_mapping(item, operation.field_map)
    transfer_id = _safe_source_id(
        mapped.get("transfer_id") or mapped.get("conversation_id"),
        "manhattan",
        item,
    )
    return {
        "transfer_id": transfer_id,
        "work_order_id": mapped.get("work_order_id", ""),
        "conversation_id": mapped.get("conversation_id", ""),
        "event_time": mapped.get("event_time", ""),
        "engineer": mapped.get("engineer", ""),
        "transfer_reason": mapped.get("transfer_reason", ""),
        "category": mapped.get("category", ""),
        "model": mapped.get("model", ""),
        "order_status": mapped.get("order_status", ""),
        "source": item,
    }


def _normalize_conversation(
    system: str,
    item: Any,
    operation: EndpointOperation,
    variables: dict[str, Any],
) -> dict[str, Any]:
    mapped = _normalize_mapping(item, operation.field_map)
    messages = mapped.get("messages") or []
    if isinstance(messages, str):
        messages = [{"role": "unknown", "content": messages}]
    inferred_question, inferred_answer = _first_last_from_messages(messages)
    source_id = _safe_source_id(
        mapped.get("source_id") or mapped.get("conversation_id"),
        system,
        item,
    )
    return {
        "source_id": source_id,
        "work_order_id": mapped.get("work_order_id") or variables.get("work_order_id", ""),
        "engineer": mapped.get("engineer", ""),
        "started_at": mapped.get("started_at", ""),
        "ended_at": mapped.get("ended_at", ""),
        "first_question": mapped.get("first_question") or inferred_question,
        "last_answer": mapped.get("last_answer") or inferred_answer,
        "intent_result": mapped.get("intent_result", ""),
        "conversation_text": mapped.get("conversation_text") or _conversation_text(messages),
        "messages": messages,
        "retrievals": mapped.get("retrievals") or [],
        "tools": mapped.get("tools") or [],
        "attachments": mapped.get("attachments") or [],
        "source": item,
    }


def collect_with_endpoint_profile(
    profile_path: str | Path,
    store: TransferAnalysisStore,
    *,
    start_date: str = "",
    end_date: str = "",
    work_order_ids: Iterable[str] | None = None,
    transfer_ids: Iterable[str] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    profile = EndpointProfile.load(profile_path)
    run_id = f"{profile.system}-{uuid.uuid4().hex}"
    store.save_collection_run(
        run_id,
        profile.system,
        start_date,
        end_date,
        "running",
    )
    metrics = {"list_records": 0, "detail_records": 0}
    try:
        with _ConfiguredPlaywrightSession(profile, headless=headless) as session:
            if profile.system == "manhattan" and "list" in profile.operations:
                operation = profile.operations["list"]
                items = session.fetch_items(
                    "list",
                    {"start": start_date, "end": end_date},
                    paginate=True,
                )
                records = [_normalize_transfer(item, operation) for item in items]
                metrics["list_records"] = store.upsert_transfers(records)

            if profile.system == "manhattan" and transfer_ids and "detail" in profile.operations:
                operation = profile.operations["detail"]
                for transfer_id in dict.fromkeys(str(item) for item in transfer_ids if item):
                    transfer = store.get_transfer(transfer_id)
                    if transfer is None:
                        continue
                    variables = {
                        "source_id": transfer_id,
                        "transfer_id": transfer_id,
                        "conversation_id": transfer.get("conversation_id", ""),
                        "work_order_id": transfer.get("work_order_id", ""),
                    }
                    item = session.fetch_record("detail", variables)
                    detail = _normalize_conversation(
                        "manhattan",
                        item,
                        operation,
                        variables,
                    )
                    store.upsert_conversation("manhattan", detail)
                    metrics["detail_records"] += 1

            if profile.system == "baixiaosheng":
                operation_name = "lookup" if "lookup" in profile.operations else "list"
                if operation_name not in profile.operations:
                    raise CollectorConfigurationError(
                        "百晓生接口模板需要 lookup 或 list operation"
                    )
                lookup_operation = profile.operations[operation_name]
                for work_order_id in dict.fromkeys(
                    str(item) for item in (work_order_ids or []) if item
                ):
                    variables = {
                        "work_order_id": work_order_id,
                        "start": start_date,
                        "end": end_date,
                    }
                    items = session.fetch_items(
                        operation_name,
                        variables,
                        paginate=bool(lookup_operation.items_path),
                    )
                    for raw_item in items:
                        item = raw_item
                        detail_operation = lookup_operation
                        mapped = _normalize_mapping(raw_item, lookup_operation.field_map)
                        source_id = mapped.get("source_id") or mapped.get("conversation_id")
                        if source_id and "detail" in profile.operations:
                            item = session.fetch_record(
                                "detail",
                                {
                                    **variables,
                                    "source_id": source_id,
                                    "conversation_id": source_id,
                                },
                            )
                            detail_operation = profile.operations["detail"]
                        detail = _normalize_conversation(
                            "baixiaosheng",
                            item,
                            detail_operation,
                            variables,
                        )
                        detail_variables = {
                            **variables,
                            "source_id": detail["source_id"],
                            "conversation_id": detail["source_id"],
                        }
                        if "retrieval" in profile.operations:
                            retrieval_operation = profile.operations["retrieval"]
                            retrieval_payload = session.fetch_record(
                                "retrieval",
                                detail_variables,
                            )
                            retrieval_mapping = _normalize_mapping(
                                retrieval_payload,
                                retrieval_operation.field_map,
                            )
                            detail["retrievals"] = (
                                retrieval_mapping.get("retrievals")
                                or (
                                    retrieval_payload
                                    if isinstance(retrieval_payload, list)
                                    else detail["retrievals"]
                                )
                            )
                        if "tools" in profile.operations:
                            tools_operation = profile.operations["tools"]
                            tools_payload = session.fetch_record(
                                "tools",
                                detail_variables,
                            )
                            tools_mapping = _normalize_mapping(
                                tools_payload,
                                tools_operation.field_map,
                            )
                            detail["tools"] = (
                                tools_mapping.get("tools")
                                or (
                                    tools_payload
                                    if isinstance(tools_payload, list)
                                    else detail["tools"]
                                )
                            )
                        store.upsert_conversation("baixiaosheng", detail)
                        metrics["detail_records"] += 1

        store.save_collection_run(
            run_id,
            profile.system,
            start_date,
            end_date,
            "completed",
            metrics,
        )
        return {"run_id": run_id, "system": profile.system, **metrics}
    except Exception as exc:
        store.save_collection_run(
            run_id,
            profile.system,
            start_date,
            end_date,
            "failed",
            metrics,
            error=str(exc),
        )
        raise
