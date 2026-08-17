from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import copy
import json
import os
import re

from .automation_api import (
    SUPPORTED_CLUSTERING_MODES,
    AutomationJobStore,
)
from .automation_queue import (
    SUPPORTED_SOURCE_SUFFIXES,
    AutomationQueue,
    read_queue_job_metadata,
)
from .excel_io import write_rows_to_workbook
from .mimo import load_dotenv
from .workflow import SOURCE_COLUMNS


_MISSING = object()
_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SecondPartPullError(RuntimeError):
    pass


class SecondPartPageFetcher(Protocol):
    def fetch_page(
        self,
        profile: "SecondPartPullProfile",
        cursor: str,
    ) -> Any: ...


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "是"}


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _path_value(
    payload: Any,
    path: str,
    default: Any = _MISSING,
) -> Any:
    normalized_path = _text(path)
    if not normalized_path or normalized_path == "$":
        return payload
    current = payload
    for segment in normalized_path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if 0 <= index < len(current):
                current = current[index]
                continue
        return default
    return current


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _expand_environment(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        resolved = os.getenv(variable)
        if resolved is None or resolved == "":
            raise SecondPartPullError(
                f"第二部分接口配置引用的环境变量未设置：{variable}"
            )
        return resolved

    return _ENV_REFERENCE_RE.sub(replace, value)


def _normalize_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, list):
        normalized_items = [_normalize_cell(item) for item in value]
        return "\n".join(
            _text(item)
            for item in normalized_items
            if _text(item)
        )
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


@dataclass(frozen=True)
class SecondPartPullProfile:
    name: str
    url: str
    method: str
    headers: dict[str, Any]
    params: dict[str, Any]
    body: dict[str, Any]
    batch_size: int
    cursor_param: str
    limit_param: str
    items_path: str
    next_cursor_path: str
    has_more_path: str
    field_map: dict[str, Any]
    defaults: dict[str, Any]
    required_fields: tuple[str, ...]
    workflow: dict[str, Any]
    timeout_seconds: float

    @classmethod
    def load(cls, path: str | Path) -> "SecondPartPullProfile":
        profile_path = Path(path)
        if not profile_path.is_file():
            raise SecondPartPullError(
                f"第二部分拉取配置不存在：{profile_path}"
            )
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecondPartPullError(
                f"第二部分拉取配置无法读取：{exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SecondPartPullError("第二部分拉取配置必须是 JSON 对象")

        name = _text(payload.get("name")) or profile_path.stem
        url = _text(payload.get("url"))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SecondPartPullError("第二部分接口 URL 必须是有效的 HTTP/HTTPS 地址")

        method = (_text(payload.get("method")) or "GET").upper()
        if method not in {"GET", "POST"}:
            raise SecondPartPullError("第二部分接口 method 只支持 GET 或 POST")

        response = payload.get("response") or {}
        if not isinstance(response, dict):
            raise SecondPartPullError("第二部分接口 response 配置必须是对象")
        items_path = _text(response.get("items_path"))
        if not items_path:
            raise SecondPartPullError("第二部分接口配置缺少 response.items_path")

        field_map = payload.get("field_map") or {}
        if not isinstance(field_map, dict) or not field_map:
            raise SecondPartPullError("第二部分接口配置缺少 field_map")
        invalid_targets = [
            key for key in field_map if not _text(key)
        ]
        if invalid_targets:
            raise SecondPartPullError("field_map 包含空目标字段")
        invalid_sources = [
            target
            for target, source in field_map.items()
            if not (
                isinstance(source, str)
                or (
                    isinstance(source, list)
                    and source
                    and all(isinstance(item, str) for item in source)
                )
            )
        ]
        if invalid_sources:
            raise SecondPartPullError(
                "field_map 的来源路径必须是字符串或非空字符串数组："
                + "、".join(map(str, invalid_sources))
            )

        raw_required_fields = payload.get("required_fields") or []
        if not isinstance(raw_required_fields, list) or not all(
            isinstance(field, str) and _text(field)
            for field in raw_required_fields
        ):
            raise SecondPartPullError(
                "required_fields 必须是字段名组成的数组"
            )
        required_fields = tuple(
            dict.fromkeys(_text(field) for field in raw_required_fields)
        )

        headers = payload.get("headers") or {}
        params = payload.get("params") or {}
        body = payload.get("body") or {}
        defaults = payload.get("defaults") or {}
        workflow = payload.get("workflow") or {}
        for label, value in (
            ("headers", headers),
            ("params", params),
            ("body", body),
            ("defaults", defaults),
            ("workflow", workflow),
        ):
            if not isinstance(value, dict):
                raise SecondPartPullError(f"第二部分接口 {label} 配置必须是对象")

        clustering_mode = (
            _text(workflow.get("clustering_mode")) or "direct_mimo"
        )
        if clustering_mode not in SUPPORTED_CLUSTERING_MODES:
            raise SecondPartPullError(
                f"不支持的聚类模式：{clustering_mode}"
            )

        return cls(
            name=name,
            url=url,
            method=method,
            headers=dict(headers),
            params=dict(params),
            body=dict(body),
            batch_size=max(1, min(_int_value(payload.get("batch_size"), 100), 1000)),
            cursor_param=_text(payload.get("cursor_param")) or "cursor",
            limit_param=_text(payload.get("limit_param")) or "limit",
            items_path=items_path,
            next_cursor_path=_text(response.get("next_cursor_path")),
            has_more_path=_text(response.get("has_more_path")),
            field_map=dict(field_map),
            defaults=dict(defaults),
            required_fields=required_fields,
            workflow={
                **workflow,
                "use_mimo": _bool_value(workflow.get("use_mimo"), True),
                "clustering_mode": clustering_mode,
                "sync_to_cz_review": _bool_value(
                    workflow.get("sync_to_cz_review"),
                    False,
                ),
            },
            timeout_seconds=max(
                1.0,
                _float_value(payload.get("timeout_seconds"), 30.0),
            ),
        )


class UrllibSecondPartPageFetcher:
    def fetch_page(
        self,
        profile: SecondPartPullProfile,
        cursor: str,
    ) -> Any:
        headers = {
            str(key): _text(value)
            for key, value in _expand_environment(profile.headers).items()
        }
        params = copy.deepcopy(_expand_environment(profile.params))
        body = copy.deepcopy(_expand_environment(profile.body))
        if cursor:
            if profile.method == "GET":
                params[profile.cursor_param] = cursor
            else:
                body[profile.cursor_param] = cursor
        if profile.method == "GET":
            params[profile.limit_param] = profile.batch_size
            query = urlencode(params, doseq=True)
            url = f"{profile.url}{'&' if '?' in profile.url else '?'}{query}"
            data = None
        else:
            body[profile.limit_param] = profile.batch_size
            url = profile.url
            data = json.dumps(
                body,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")
        request = Request(
            url,
            data=data,
            headers=headers,
            method=profile.method,
        )
        try:
            with urlopen(
                request,
                timeout=profile.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise SecondPartPullError(
                f"第二部分接口 HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SecondPartPullError(
                f"第二部分接口连接失败：{exc}"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecondPartPullError("第二部分接口没有返回有效 JSON") from exc


def _mapping_value(item: Any, source_path: Any) -> Any:
    paths = source_path if isinstance(source_path, list) else [source_path]
    for path in paths:
        value = _path_value(item, _text(path), _MISSING)
        if value is not _MISSING and value not in (None, "", []):
            return value
    return ""


def _map_records(
    profile: SecondPartPullProfile,
    items: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    rejected_records: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            rejected_records.append(
                {
                    "source_index": index,
                    "source_record_id": "",
                    "missing_required_fields": [],
                    "reason": "record_not_object",
                }
            )
            continue
        row = {
            _text(target): _normalize_cell(
                _mapping_value(item, source_path)
            )
            for target, source_path in profile.field_map.items()
        }
        for target, default in profile.defaults.items():
            target_name = _text(target)
            if target_name and row.get(target_name) in (None, ""):
                row[target_name] = _normalize_cell(
                    _expand_environment(default)
                )
        if row.get("序号") in (None, ""):
            row["序号"] = index
        missing_required_fields = [
            field
            for field in profile.required_fields
            if row.get(field) in (None, "")
        ]
        if missing_required_fields:
            rejected_records.append(
                {
                    "source_index": index,
                    "source_record_id": _text(
                        row.get("工单ID") or item.get("id")
                    ),
                    "missing_required_fields": missing_required_fields,
                    "reason": "missing_required_fields",
                }
            )
            continue
        if not any(value not in (None, "") for value in row.values()):
            rejected_records.append(
                {
                    "source_index": index,
                    "source_record_id": _text(item.get("id")),
                    "missing_required_fields": [],
                    "reason": "empty_mapped_record",
                }
            )
            continue
        rows.append(row)
    return rows, rejected_records


def _workbook_bytes(
    profile: SecondPartPullProfile,
    rows: list[dict[str, Any]],
) -> bytes:
    extra_columns = [
        column
        for column in [
            *profile.field_map.keys(),
            *profile.defaults.keys(),
        ]
        if _text(column) and _text(column) not in SOURCE_COLUMNS
    ]
    columns = [*SOURCE_COLUMNS, *dict.fromkeys(map(_text, extra_columns))]
    with TemporaryDirectory(prefix="second-part-pull-") as temp_dir:
        path = Path(temp_dir) / "second-part.xlsx"
        write_rows_to_workbook(
            {
                "共享数据汇总": (
                    columns,
                    rows,
                )
            },
            path,
        )
        return path.read_bytes()


def _batch_key(
    profile: SecondPartPullProfile,
    cursor: str,
    next_cursor: str,
    rows: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "profile": profile.name,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "rows": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return f"sha256:{sha256(payload.encode('utf-8')).hexdigest()}"


def _find_existing_batch(
    queue: AutomationQueue,
    batch_key: str,
) -> dict[str, Any] | None:
    queue.ensure()
    for directory in (
        queue.pending,
        queue.processing,
        queue.completed,
        queue.failed,
    ):
        for source_path in directory.iterdir():
            if (
                not source_path.is_file()
                or source_path.name.startswith("~$")
                or source_path.suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES
            ):
                continue
            metadata = read_queue_job_metadata(source_path)
            if (
                _text(
                    (metadata.get("options") or {}).get(
                        "source_batch_key"
                    )
                )
                == batch_key
            ):
                return metadata
    return None


def _safe_filename_fragment(value: Any) -> str:
    fragment = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        _text(value),
    ).strip("-._")
    return fragment[:64] or "second-part"


def _write_rejection_report(
    output_root: str | Path,
    profile: SecondPartPullProfile,
    cursor: str,
    next_cursor: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    report_dir = Path(output_root) / "second-part-pull-rejections"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "profile": profile.name,
        "source_cursor": cursor,
        "source_next_cursor": next_cursor,
        "records": records,
    }
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path = report_dir / (
        f"{_safe_filename_fragment(profile.name)}-"
        f"{sha256(encoded).hexdigest()}.json"
    )
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"path": str(path), "records": len(records)}


def _load_state(
    path: Path,
    profile_name: str,
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "profile": profile_name,
            "cursor": "",
            "updated_at": "",
            "last_batch_key": "",
            "last_job_id": "",
        }
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecondPartPullError(
            f"第二部分拉取状态无法读取：{exc}"
        ) from exc
    if not isinstance(state, dict):
        raise SecondPartPullError("第二部分拉取状态必须是 JSON 对象")
    existing_profile = _text(state.get("profile"))
    if existing_profile and existing_profile != profile_name:
        raise SecondPartPullError(
            "拉取状态属于其他第二部分 profile，请使用独立 state 文件"
        )
    return {
        **state,
        "profile": profile_name,
        "cursor": _text(state.get("cursor")),
    }


def _save_state(
    path: Path,
    *,
    profile_name: str,
    cursor: str,
    batch_key: str = "",
    job_id: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profile": profile_name,
        "cursor": cursor,
        "updated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "last_batch_key": batch_key,
        "last_job_id": job_id,
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def pull_second_part_to_queue(
    profile_path: str | Path,
    *,
    queue_root: str | Path,
    output_root: str | Path,
    state_path: str | Path,
    max_pages: int = 10,
    fetcher: SecondPartPageFetcher | None = None,
) -> dict[str, Any]:
    load_dotenv()
    profile = SecondPartPullProfile.load(profile_path)
    queue = AutomationQueue(queue_root)
    job_store = AutomationJobStore(queue_root, output_root)
    state_file = Path(state_path)
    state = _load_state(state_file, profile.name)
    cursor = _text(state.get("cursor"))
    page_fetcher = fetcher or UrllibSecondPartPageFetcher()
    page_limit = max(1, int(max_pages))
    summary: dict[str, Any] = {
        "status": "idle",
        "profile": profile.name,
        "profile_path": str(Path(profile_path)),
        "state_path": str(state_file),
        "queue_root": str(queue.root),
        "output_root": str(Path(output_root)),
        "started_cursor": cursor,
        "cursor": cursor,
        "fetched_pages": 0,
        "fetched_records": 0,
        "queued_jobs": 0,
        "reused_jobs": 0,
        "rejected_records": 0,
        "rejection_reports": [],
        "jobs": [],
    }

    for _page_index in range(page_limit):
        try:
            response = page_fetcher.fetch_page(profile, cursor)
        except SecondPartPullError:
            raise
        except Exception as exc:
            raise SecondPartPullError(
                f"第二部分接口拉取失败：{exc}"
            ) from exc
        summary["fetched_pages"] += 1

        items = _path_value(
            response,
            profile.items_path,
            _MISSING,
        )
        if not isinstance(items, list):
            raise SecondPartPullError(
                "第二部分接口响应中的 items_path 不是数组"
            )
        next_cursor_value = (
            _path_value(
                response,
                profile.next_cursor_path,
                "",
            )
            if profile.next_cursor_path
            else ""
        )
        next_cursor = _text(next_cursor_value)
        has_more_value = (
            _path_value(
                response,
                profile.has_more_path,
                _MISSING,
            )
            if profile.has_more_path
            else _MISSING
        )
        has_more = (
            _bool_value(has_more_value)
            if has_more_value is not _MISSING
            else bool(next_cursor and next_cursor != cursor)
        )
        summary["fetched_records"] += len(items)

        if items:
            rows, rejected_records = _map_records(profile, items)
            if rejected_records:
                summary["rejected_records"] += len(rejected_records)
                summary["rejection_reports"].append(
                    _write_rejection_report(
                        output_root,
                        profile,
                        cursor,
                        next_cursor,
                        rejected_records,
                    )
                )
        else:
            rows = []

        if rows:
            batch_key = _batch_key(
                profile,
                cursor,
                next_cursor,
                rows,
            )
            existing = _find_existing_batch(queue, batch_key)
            if existing is not None:
                job_id = _text(existing.get("job_id"))
                summary["reused_jobs"] += 1
                result_status = "reused"
            else:
                workbook = _workbook_bytes(profile, rows)
                filename = (
                    f"{_safe_filename_fragment(profile.name)}-"
                    f"{datetime.now():%Y%m%d-%H%M%S}-"
                    f"{batch_key.split(':', 1)[-1][:10]}.xlsx"
                )
                options = {
                    **profile.workflow,
                    "submit_to_cz": profile.workflow[
                        "sync_to_cz_review"
                    ],
                    "source_system": profile.name,
                    "source_batch_key": batch_key,
                    "source_cursor": cursor,
                    "source_next_cursor": next_cursor,
                }
                metadata = job_store.create(
                    filename,
                    workbook,
                    options,
                )
                job_id = _text(metadata.get("job_id"))
                summary["queued_jobs"] += 1
                result_status = "queued"
            summary["jobs"].append(
                {
                    "job_id": job_id,
                    "status": result_status,
                    "source_batch_key": batch_key,
                    "records": len(rows),
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                }
            )
        else:
            batch_key = ""
            job_id = ""

        if has_more and (
            not next_cursor
            or next_cursor == cursor
        ):
            raise SecondPartPullError(
                "第二部分接口标记 has_more=true，但没有返回可推进的 next_cursor"
            )

        if next_cursor and next_cursor != cursor:
            cursor = next_cursor
        _save_state(
            state_file,
            profile_name=profile.name,
            cursor=cursor,
            batch_key=batch_key,
            job_id=job_id,
        )
        summary["cursor"] = cursor

        if not has_more:
            break

    if summary["queued_jobs"]:
        summary["status"] = "queued"
    elif summary["reused_jobs"]:
        summary["status"] = "reused"
    elif summary["rejected_records"]:
        summary["status"] = "rejected"
    return summary
