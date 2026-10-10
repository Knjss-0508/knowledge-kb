#!/usr/bin/env python3
"""定时检查飞书机型配置表并幂等同步到知识库中台。

该脚本运行在安装了 lark-cli 的调度主机（Windows 或 Linux）上，
可以通过 SSH 把同步请求送到知识库服务器的 backend 容器：

* 用 lark-cli 读取文档 revision，未变化时不读取整表；
* 变化后读取完整工作表，先做全表校验，再调用 backend 同步命令；
* 同步前后再次检查 revision，避免把编辑中的半张表写入知识库；
* 只有同步成功后才更新 checkpoint；默认不因源表删行而废弃旧知识。

脚本只依赖 Python 标准库、lark-cli 和 Docker CLI，不保存或打印飞书应用密钥。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_SPREADSHEET_TOKEN = "TLxlsXMKJhPn1htD31lcdl2enKd"
DEFAULT_SHEET_ID = "w3Caff"
DEFAULT_CATEGORY_ID = "119"
DEFAULT_CATEGORY_NAME = "平板电脑"
DEFAULT_ACTOR = "model-configuration-sync"
DEFAULT_TARGET_SHEET_NAME = "个性化配置信息"
REQUIRED_IDENTITY_HEADERS = ("标题", "品牌ID", "品牌", "型号ID", "型号")
CONTENT_HEADER_ALIASES = ("综合内容", "综合信息")
IGNORED_SOURCE_HEADERS = (
    "是否有卡槽",
    "Home键",
    "指纹识别",
    "3D面容",
    "内置手写笔",
    "闪光灯",
    "蜂窝网络",
    "光线传感器",
)
CELLS_GET_CONTRACT_GUIDANCE = (
    "处理 ranges[n].cells 之前，必须先查看顶层 has_more，以及每个 range 的 "
    "actual_range / row_indices / col_indices。定位真实行号时用 row_indices[i]，"
    "定位真实列字母时用 col_indices[j]，不要按二维数组下标自己数行列；"
    "skip_hidden=true、skip_filter=true 或结果被截断时，这样会错位。"
)


class SyncSchedulerError(RuntimeError):
    """可直接展示给运维人员的同步错误。"""


@dataclass(frozen=True)
class SchedulerConfig:
    identity: str
    spreadsheet_token: str
    sheet_id: str
    state_dir: Path
    lark_cli: str
    docker_cli: str
    backend_container: str
    target: str
    ssh_cli: str
    ssh_host: str
    ssh_user: str
    ssh_key: Path | None
    actor: str
    max_revision_retries: int
    command_timeout_seconds: int
    force: bool
    check_only: bool
    # 单表模式沿用历史默认值；Wiki 模式会由来源清单覆盖这些字段。
    category_id: str = DEFAULT_CATEGORY_ID
    category_name: str = DEFAULT_CATEGORY_NAME
    sheet_name: str = DEFAULT_TARGET_SHEET_NAME
    wiki_root: str = ""
    source_config_path: Path | None = None
    wiki_max_pages: int = 200

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "sync.lock"


@dataclass(frozen=True)
class SourceMapping:
    """Wiki 自动发现时，工作簿到知识库品类的显式映射。"""

    category_id: str
    category_name: str
    spreadsheet_token: str = ""
    sheet_id: str = ""
    wiki_node_token: str = ""
    workbook_title: str = ""
    sheet_name: str = ""
    custom: bool = False


@dataclass(frozen=True)
class DiscoveredSource:
    """一个可独立校验、独立写库的飞书工作表来源。"""

    spreadsheet_token: str
    sheet_id: str
    sheet_name: str
    workbook_title: str
    wiki_node_token: str
    category_id: str
    category_name: str

    @property
    def state_key(self) -> str:
        raw = f"{self.spreadsheet_token}\0{self.sheet_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _identity_text(value: Any) -> str:
    """清除品牌、型号和外部 ID 中不可见的 Unicode 格式字符。"""
    return "".join(
        character
        for character in _text(value)
        if unicodedata.category(character) != "Cf"
    ).strip()


def _column_label(number: int) -> str:
    """把从 1 开始的列号转换为 A、Z、AA 形式。"""
    if number < 1:
        raise SyncSchedulerError("飞书工作表列数必须大于 0。")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _safe_message(
    value: Any,
    *,
    spreadsheet_token: str = "",
    sensitive_values: Iterable[str] = (),
) -> str:
    message = _text(value)
    redactions = ((spreadsheet_token, "[spreadsheet-redacted]"),)
    redactions += tuple(
        (sensitive_value, "[sensitive-value-redacted]")
        for sensitive_value in sensitive_values
        if sensitive_value
    )
    for sensitive_value, replacement in redactions:
        if sensitive_value:
            message = message.replace(sensitive_value, replacement)
    message = re.sub(
        r"(?i)(access[_-]?token|refresh[_-]?token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        message,
    )
    return message[:2000]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_from_output(
    output: str,
    *,
    spreadsheet_token: str = "",
    sensitive_values: Iterable[str] = (),
) -> Mapping[str, Any]:
    text = output.strip()
    if not text:
        raise SyncSchedulerError("命令没有返回 JSON。")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # 某些 CLI 版本可能在 JSON 前输出一行提示；只接受最后一个 JSON 对象。
        decoder = json.JSONDecoder()
        for index in range(len(text) - 1, -1, -1):
            if text[index] != "{":
                continue
            try:
                value, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if text[index + end :].strip():
                continue
            break
        else:
            raise SyncSchedulerError(
                "命令返回内容不是有效 JSON："
                + _safe_message(
                    text,
                    spreadsheet_token=spreadsheet_token,
                    sensitive_values=sensitive_values,
                )
            )
    if not isinstance(value, Mapping):
        raise SyncSchedulerError("命令返回 JSON 根节点不是对象。")
    return value


def _run_command(
    argv: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    spreadsheet_token: str = "",
    sensitive_values: Iterable[str] = (),
    timeout: float = 120.0,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bytes:
    try:
        run_kwargs: dict[str, Any] = {
            "input": input_bytes,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "check": False,
            "timeout": timeout,
        }
        if env is not None:
            run_kwargs["env"] = dict(env)
        completed = runner(list(argv), **run_kwargs)
    except FileNotFoundError as exc:
        raise SyncSchedulerError(f"找不到命令：{argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncSchedulerError(
            f"命令执行超过 {int(timeout)} 秒，已终止：{argv[0]}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or b""
        try:
            detail_text = detail.decode("utf-8", errors="replace")
        except AttributeError:
            detail_text = str(detail)
        try:
            error_json = _json_from_output(
                detail_text,
                spreadsheet_token=spreadsheet_token,
                sensitive_values=sensitive_values,
            )
            detail_text = _text(
                (error_json.get("error") or {}).get("message")
                if isinstance(error_json.get("error"), Mapping)
                else error_json.get("message")
            ) or detail_text
        except SyncSchedulerError:
            pass
        raise SyncSchedulerError(
            f"命令执行失败（退出码 {completed.returncode}）："
            + _safe_message(
                detail_text,
                spreadsheet_token=spreadsheet_token,
                sensitive_values=sensitive_values,
            )
        )
    return completed.stdout or b""


class LarkCli:
    def __init__(
        self,
        config: SchedulerConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.config = config
        self.runner = runner
        self.env = os.environ.copy()
        self.env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        self.env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

    def call(
        self,
        args: Sequence[str],
        *,
        spreadsheet_token: str | None = None,
        sensitive_values: Iterable[str] = (),
    ) -> Mapping[str, Any]:
        redaction_token = spreadsheet_token or self.config.spreadsheet_token
        output = _run_command(
            [self.config.lark_cli, *args, "--json"],
            spreadsheet_token=redaction_token,
            sensitive_values=sensitive_values,
            timeout=min(self.config.command_timeout_seconds, 120),
            env=self.env,
            runner=self.runner,
        )
        response = _json_from_output(
            output.decode("utf-8", errors="replace"),
            spreadsheet_token=redaction_token,
            sensitive_values=sensitive_values,
        )
        if response.get("ok") is not True:
            error = response.get("error")
            message = (
                error.get("message")
                if isinstance(error, Mapping)
                else response.get("message")
            )
            raise SyncSchedulerError(
                "飞书接口读取失败："
                + _safe_message(
                    message or "未提供错误详情。",
                    spreadsheet_token=redaction_token,
                    sensitive_values=sensitive_values,
                )
            )
        return response

    def revision(self, *, spreadsheet_token: str | None = None) -> str:
        token = spreadsheet_token or self.config.spreadsheet_token
        response = self.call(
            [
                "sheets",
                "+revision-get",
                "--as",
                self.config.identity,
                "--spreadsheet-token",
                token,
            ],
            spreadsheet_token=token,
        )
        revision = _text((response.get("data") or {}).get("revision"))
        if not revision:
            raise SyncSchedulerError("飞书表格未返回有效 revision。")
        return revision

    def workbook_info(
        self,
        *,
        spreadsheet_token: str | None = None,
    ) -> Mapping[str, Any]:
        token = spreadsheet_token or self.config.spreadsheet_token
        response = self.call(
            [
                "sheets",
                "+workbook-info",
                "--as",
                self.config.identity,
                "--spreadsheet-token",
                token,
            ],
            spreadsheet_token=token,
        )
        data = response.get("data")
        warning_message = _text(data.get("warning_message")) if isinstance(data, Mapping) else ""
        if warning_message:
            raise SyncSchedulerError(
                "飞书工作簿读取返回告警，禁止同步："
                + _safe_message(
                    warning_message,
                    spreadsheet_token=token,
                )
            )
        if isinstance(data, Mapping) and data.get("complete") is False:
            raise SyncSchedulerError("飞书工作簿读取不完整，禁止同步。")
        return response

    def cells(
        self,
        *,
        row_count: int,
        column_count: int,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
    ) -> Mapping[str, Any]:
        if row_count < 2:
            raise SyncSchedulerError("飞书目标工作表没有可同步的数据行。")
        if column_count < 1:
            raise SyncSchedulerError("飞书目标工作表没有有效列。")
        token = spreadsheet_token or self.config.spreadsheet_token
        target_sheet_id = sheet_id or self.config.sheet_id
        return self.call(
            [
                "sheets",
                "+cells-get",
                "--as",
                self.config.identity,
                "--spreadsheet-token",
                token,
                "--sheet-id",
                target_sheet_id,
                "--range",
                f"A1:{_column_label(column_count)}{row_count}",
                "--include",
                "value",
                "--max-chars",
                "20000000",
            ],
            spreadsheet_token=token,
        )

    def wiki_node(self, node_token: str) -> Mapping[str, Any]:
        response = self.call(
            [
                "wiki",
                "+node-get",
                "--as",
                self.config.identity,
                "--node-token",
                node_token,
            ],
            sensitive_values=(node_token,),
        )
        data = response.get("data")
        node = data.get("node") if isinstance(data, Mapping) else None
        if not isinstance(node, Mapping):
            # 兼容少数 lark-cli 版本将 shortcut 数据直接置于 data 的行为。
            node = data
        if not isinstance(node, Mapping):
            raise SyncSchedulerError("飞书 Wiki 节点读取结果缺少 node。")
        return node

    def wiki_children(
        self,
        *,
        space_id: str,
        parent_node_token: str,
        max_pages: int,
    ) -> list[Mapping[str, Any]]:
        if max_pages < 1:
            raise SyncSchedulerError("Wiki 最大分页数必须大于 0。")
        result: list[Mapping[str, Any]] = []
        page_token = ""
        for _ in range(max_pages):
            args = [
                "wiki",
                "+node-list",
                "--as",
                self.config.identity,
                "--space-id",
                space_id,
                "--parent-node-token",
                parent_node_token,
                "--page-size",
                "50",
            ]
            if page_token:
                args.extend(("--page-token", page_token))
            response = self.call(
                args,
                sensitive_values=(parent_node_token, page_token),
            )
            data = response.get("data")
            if not isinstance(data, Mapping):
                raise SyncSchedulerError("飞书 Wiki 子节点读取结果缺少 data。")
            # lark-cli 1.0.87 的 Wiki shortcut 返回 data.nodes；保留 items
            # 兼容较早/不同封装版本，二者均缺失时才 fail closed。
            nodes = data.get("nodes")
            if not isinstance(nodes, list):
                nodes = data.get("items")
            if not isinstance(nodes, list):
                raise SyncSchedulerError("飞书 Wiki 子节点读取结果缺少 nodes/items。")
            result.extend(node for node in nodes if isinstance(node, Mapping))
            if not data.get("has_more"):
                return result
            next_token = _text(data.get("page_token"))
            if not next_token:
                raise SyncSchedulerError("飞书 Wiki 子节点分页缺少 page_token。")
            page_token = next_token
        raise SyncSchedulerError(
            f"飞书 Wiki 子节点分页超过上限 {max_pages}，为避免漏读已停止同步。"
        )


def _workbook_data(workbook_response: Mapping[str, Any]) -> Mapping[str, Any]:
    data = workbook_response.get("data")
    if not isinstance(data, Mapping):
        raise SyncSchedulerError("飞书工作簿读取结果缺少 data 节点。")
    return data


def _workbook_title(workbook_response: Mapping[str, Any]) -> str:
    data = workbook_response.get("data")
    if not isinstance(data, Mapping):
        return ""
    for key in ("title", "name", "spreadsheet_title", "workbook_title"):
        value = _text(data.get(key))
        if value:
            return value
    return ""


def _find_target_sheet(
    workbook_response: Mapping[str, Any],
    sheet_id: str,
    *,
    sheet_name: str = "",
) -> Mapping[str, Any]:
    data = _workbook_data(workbook_response)
    sheets = data.get("sheets")
    for sheet in sheets if isinstance(sheets, list) else []:
        if not isinstance(sheet, Mapping):
            continue
        current_id = _text(sheet.get("sheet_id"))
        current_name = _text(
            sheet.get("title") or sheet.get("name") or sheet.get("sheet_name")
        )
        # 提供 Sheet ID 时，ID 是唯一信源；只有没有 ID 时才按名称兜底。
        matches = (
            current_id == sheet_id
            if sheet_id
            else bool(sheet_name) and current_name == sheet_name
        )
        if matches:
            try:
                row_count = int(sheet.get("row_count") or 0)
            except (TypeError, ValueError) as exc:
                raise SyncSchedulerError("飞书工作表行数不是有效数字。") from exc
            if row_count < 2:
                raise SyncSchedulerError(
                    f"飞书目标工作表没有可同步的数据行：{current_name or current_id}"
                )
            return sheet
    raise SyncSchedulerError(
        f"未找到目标工作表：{sheet_name or sheet_id}"
    )


def _cell_value(cell: Any) -> str:
    if isinstance(cell, Mapping):
        return _text(cell.get("value"))
    return _text(cell)


def _column_number(value: Any) -> int:
    """把 A、Z、AA 形式的列名转成从 1 开始的列号。"""
    text = _text(value).upper()
    if not re.fullmatch(r"[A-Z]+", text):
        raise SyncSchedulerError(f"飞书表格列定位无效：{text or '<empty>'}")
    result = 0
    for character in text:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _parse_actual_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?i)A1:([A-Z]+)([0-9]+)", value)
    if not match:
        raise SyncSchedulerError(
            "飞书表格实际读取范围异常，禁止同步：" + (value or "未返回 actual_range")
        )
    try:
        end_column = _column_number(match.group(1))
        end_row = int(match.group(2))
    except (TypeError, ValueError) as exc:
        raise SyncSchedulerError("飞书表格实际读取范围包含无效行列。") from exc
    if end_row < 1 or end_column < 1:
        raise SyncSchedulerError("飞书表格实际读取范围为空。")
    return end_column, end_row


def _row_value(row: Sequence[Any], headers: Mapping[str, int], name: str) -> str:
    index = headers.get(name)
    if index is None or index >= len(row):
        return ""
    return _cell_value(row[index])


def _is_contract_guidance_warning(message: str) -> bool:
    """识别 cells-get 每次返回的固定读取契约提示，而非真实数据告警。"""
    normalized = re.sub(r"\s+", " ", message).strip()
    expected = re.sub(r"\s+", " ", CELLS_GET_CONTRACT_GUIDANCE).strip()
    return normalized == expected


def build_payload(
    *,
    workbook_response: Mapping[str, Any],
    cells_response: Mapping[str, Any],
    sheet_id: str,
    spreadsheet_token: str,
    category_id: str = DEFAULT_CATEGORY_ID,
    category_name: str = DEFAULT_CATEGORY_NAME,
    sheet_name: str = DEFAULT_TARGET_SHEET_NAME,
    workbook_title: str = "",
) -> dict[str, Any]:
    category_id = _identity_text(category_id)
    category_name = _identity_text(category_name)
    if not category_id or not category_name:
        raise SyncSchedulerError("同步来源缺少有效的品类 ID 或品类名称。")
    target_sheet = _find_target_sheet(
        workbook_response,
        sheet_id,
        sheet_name=sheet_name,
    )
    data = cells_response.get("data")
    if not isinstance(data, Mapping):
        raise SyncSchedulerError("飞书表格读取结果缺少 data 节点，禁止同步。")
    warning_message = _text(data.get("warning_message"))
    if warning_message and not _is_contract_guidance_warning(warning_message):
        raise SyncSchedulerError(
            "飞书表格读取返回告警，禁止把可能不完整的数据写入知识库："
            + _safe_message(warning_message, spreadsheet_token=spreadsheet_token)
        )
    if data.get("complete") is False or data.get("has_more"):
        raise SyncSchedulerError("飞书表格读取不完整（has_more=true），禁止同步。")
    ranges = data.get("ranges")
    if not isinstance(ranges, list) or len(ranges) != 1:
        raise SyncSchedulerError("飞书表格返回了多个或缺失读取范围，禁止同步。")
    sheet_range = ranges[0]
    if not isinstance(sheet_range, Mapping) or sheet_range.get("truncated"):
        raise SyncSchedulerError("飞书表格结果为空或被截断，禁止同步。")
    actual_range = _text(sheet_range.get("actual_range"))
    actual_end_column, actual_end_row = _parse_actual_range(actual_range)
    try:
        expected_row_count = int(target_sheet.get("row_count") or 0)
        expected_column_count = int(target_sheet.get("column_count") or 0)
    except (TypeError, ValueError) as exc:
        raise SyncSchedulerError("飞书目标工作表行数或列数不是有效数字。") from exc
    if expected_row_count < 2 or actual_end_row != expected_row_count:
        raise SyncSchedulerError(
            "飞书表格实际读取范围未覆盖目标工作表全部行，且未严格匹配目标工作表行数，禁止同步："
            f"actual_end_row={actual_end_row}, expected_row_count={expected_row_count}"
        )
    if expected_column_count < 1 or actual_end_column != expected_column_count:
        raise SyncSchedulerError(
            "飞书表格实际读取范围未严格匹配目标工作表列数，禁止同步："
            f"actual_end_column={actual_end_column}, expected_column_count={expected_column_count}"
        )
    col_indices = sheet_range.get("col_indices")
    if not isinstance(col_indices, list) or not col_indices or _text(col_indices[0]).upper() != "A":
        raise SyncSchedulerError("飞书表格未返回从 A 列开始的有效列定位，禁止同步。")
    column_numbers = [_column_number(value) for value in col_indices]
    expected_column_numbers = list(range(1, actual_end_column + 1))
    if column_numbers != expected_column_numbers:
        raise SyncSchedulerError(
            "飞书表格列定位未从 A 连续覆盖 actual_range，禁止同步。"
        )
    rows = sheet_range.get("cells")
    if not isinstance(rows, list) or len(rows) < 2:
        raise SyncSchedulerError("飞书表格没有可同步的数据行。")
    row_indices = sheet_range.get("row_indices")
    if not isinstance(row_indices, list) or len(row_indices) != len(rows):
        raise SyncSchedulerError("飞书表格行定位与数据行数不一致，禁止同步。")
    try:
        row_numbers = [int(value) for value in row_indices]
    except (TypeError, ValueError) as exc:
        raise SyncSchedulerError("飞书表格行定位包含无效行号，禁止同步。") from exc
    expected_row_numbers = list(range(1, expected_row_count + 1))
    if row_numbers != expected_row_numbers:
        raise SyncSchedulerError(
            "飞书表格行定位未严格覆盖 1..目标行数，禁止同步。"
        )

    header_row = rows[0] if isinstance(rows[0], list) else []
    if len(header_row) > len(col_indices):
        raise SyncSchedulerError("飞书表格列定位与表头宽度不一致，禁止同步。")
    headers: dict[str, int] = {}
    for index, cell in enumerate(header_row):
        header = _cell_value(cell)
        if not header:
            continue
        if header in headers:
            raise SyncSchedulerError(f"飞书表格存在重复表头：{header}")
        headers[header] = index
    missing_headers = [
        header for header in REQUIRED_IDENTITY_HEADERS if header not in headers
    ]
    content_header = next(
        (header for header in CONTENT_HEADER_ALIASES if header in headers),
        "",
    )
    if not content_header:
        missing_headers.append("综合内容（兼容综合信息）")
    if missing_headers:
        raise SyncSchedulerError(
            "飞书表格缺少必填列：" + "、".join(missing_headers)
        )
    max_required_column = max(
        *(headers[header] for header in REQUIRED_IDENTITY_HEADERS),
        headers[content_header],
    ) + 1
    if max_required_column > len(column_numbers) or max_required_column > actual_end_column:
        raise SyncSchedulerError("飞书表格必填列未被完整读取，禁止同步。")

    records: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for row_index, raw_row in enumerate(rows[1:], start=1):
        row = raw_row if isinstance(raw_row, list) else []
        source_row_number = (
            _text(row_indices[row_index])
            if row_index < len(row_indices) and _text(row_indices[row_index])
            else str(row_index + 1)
        )
        required = {
            header: _row_value(row, headers, header)
            for header in REQUIRED_IDENTITY_HEADERS
        }
        content = _row_value(row, headers, content_header)
        required[content_header] = content
        if not any(required.values()):
            continue
        missing = [header for header, value in required.items() if not value]
        if missing:
            raise SyncSchedulerError(
                f"飞书表格第 {source_row_number} 行缺少必填字段："
                + "、".join(missing)
            )

        brand_id = _identity_text(required["品牌ID"])
        brand_name = _identity_text(required["品牌"])
        model_id = _identity_text(required["型号ID"])
        model_name = _identity_text(required["型号"])
        if not all((brand_id, brand_name, model_id, model_name)):
            raise SyncSchedulerError(
                f"飞书表格第 {source_row_number} 行的品牌或型号包含无效格式字符。"
            )
        key = (category_id, brand_id, model_id)
        if key in seen_keys:
            raise SyncSchedulerError(
                "飞书表格品类/品牌/型号ID组合重复："
                + "/".join(key)
            )
        seen_keys.add(key)

        source_fields: dict[str, str] = {}
        for header in headers:
            if header in IGNORED_SOURCE_HEADERS:
                continue
            value = _row_value(row, headers, header)
            if value:
                source_fields[header] = value
        source_record_id = ""
        for source_id_header in ("来源知识ID", "知识ID", "记录ID"):
            source_record_id = _identity_text(
                _row_value(row, headers, source_id_header)
            )
            if source_record_id:
                break
        source_fields.update(
            {
                "来源工作表": sheet_name or DEFAULT_TARGET_SHEET_NAME,
                "来源行号": source_row_number,
                "品类ID": category_id,
                "品类": category_name,
                "品牌ID": brand_id,
                "品牌": brand_name,
                "型号ID": model_id,
                "型号": model_name,
                "标题": required["标题"],
                "综合内容": content,
            }
        )
        records.append(
            {
                "source_record_id": source_record_id,
                "title": required["标题"],
                "category_id": category_id,
                "category_name": category_name,
                "brand_id": brand_id,
                "brand_name": brand_name,
                "model_id": model_id,
                "model_name": model_name,
                "content": content,
                "source_fields": source_fields,
            }
        )
    if not records:
        raise SyncSchedulerError("飞书表格没有可同步的有效记录。")

    revision = _text(data.get("revision"))
    if not revision:
        raise SyncSchedulerError("飞书表格读取结果缺少 revision。")
    return {
        "schema_version": 1,
        "source": "feishu_sheet",
        "spreadsheet_token": spreadsheet_token,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name or DEFAULT_TARGET_SHEET_NAME,
        "revision": revision,
        "category_id": category_id,
        "category_name": category_name,
        "workbook_title": workbook_title,
        "records": records,
    }


def payload_hash(payload: Mapping[str, Any]) -> str:
    relevant = dict(payload)
    relevant.pop("revision", None)
    encoded = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


DEFAULT_MULTI_SOURCE_MAPPINGS: tuple[SourceMapping, ...] = (
    SourceMapping("1100000016", "笔记本", sheet_id="4XMCyB", workbook_title="大模型【笔记本】知识库"),
    SourceMapping("101", "手机", sheet_id="BpZlyl", workbook_title="大模型【手机】知识库"),
    SourceMapping("1100000180", "相机镜头", sheet_id="2SCTAn", workbook_title="大模型【相机镜头】知识库"),
    SourceMapping("1100000170", "手表", sheet_id="2TrbJP", workbook_title="大模型【手表】知识库"),
    SourceMapping("1100000170", "手表", sheet_id="2TrbJP", workbook_title="智能手表"),
    SourceMapping("119", "平板电脑", sheet_id="w3Caff", workbook_title="大模型【平板电脑】知识库"),
    SourceMapping("1100001166", "学习机", sheet_id="1sYMRM", workbook_title="大模型【学习机】知识库"),
    SourceMapping("1100000182", "单电微单机身", sheet_id="2STsOR", workbook_title="大模型【单电微单机身】知识库"),
    SourceMapping("1100000182", "单电微单机身", sheet_id="2STsOR", workbook_title="单电/微单机身"),
    SourceMapping("1100000186", "耳机", sheet_id="2mnOSv", workbook_title="大模型【耳机】知识库"),
    SourceMapping("1100000186", "耳机", sheet_id="2mnOSv", workbook_title="耳机/耳麦"),
    SourceMapping("1100000179", "单反机身", sheet_id="2waXxL", workbook_title="大模型【单反机身】知识库"),
    SourceMapping("1100000040", "手写笔", sheet_id="2MFmvW", workbook_title="大模型【手写笔】知识库"),
)


def _mapping_from_value(value: Any, *, source: str) -> SourceMapping:
    if not isinstance(value, Mapping):
        raise SyncSchedulerError(f"来源映射配置 {source} 的每项必须是对象。")
    category_id = _identity_text(value.get("category_id"))
    category_name = _identity_text(value.get("category_name"))
    if not category_id or not category_name:
        raise SyncSchedulerError(
            f"来源映射配置 {source} 缺少 category_id 或 category_name。"
        )
    mapping = SourceMapping(
        category_id=category_id,
        category_name=category_name,
        spreadsheet_token=_identity_text(value.get("spreadsheet_token")),
        sheet_id=_identity_text(value.get("sheet_id")),
        wiki_node_token=_identity_text(value.get("wiki_node_token")),
        workbook_title=_identity_text(value.get("workbook_title")),
        sheet_name=_identity_text(value.get("sheet_name")),
        custom=True,
    )
    if not any(
        (
            mapping.spreadsheet_token,
            mapping.sheet_id,
            mapping.wiki_node_token,
            mapping.workbook_title,
        )
    ):
        raise SyncSchedulerError(
            f"来源映射配置 {source} 至少需指定 spreadsheet_token、sheet_id、"
            "wiki_node_token 或 workbook_title 之一；不能仅按 sheet_name 全局匹配。"
        )
    return mapping


def _load_source_mappings(config: SchedulerConfig) -> tuple[list[SourceMapping], str]:
    """加载自定义映射，并允许它覆盖内置的已知来源映射。"""
    if config.source_config_path is None:
        return list(DEFAULT_MULTI_SOURCE_MAPPINGS), DEFAULT_TARGET_SHEET_NAME
    try:
        document = json.loads(config.source_config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SyncSchedulerError(
            f"无法读取来源映射配置：{config.source_config_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SyncSchedulerError(
            f"来源映射配置不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列。"
        ) from exc
    if not isinstance(document, Mapping):
        raise SyncSchedulerError("来源映射配置根节点必须是对象。")
    raw_sources = document.get("sources", [])
    if not isinstance(raw_sources, list):
        raise SyncSchedulerError("来源映射配置的 sources 必须是数组。")
    custom = [
        _mapping_from_value(value, source=f"sources[{index}]")
        for index, value in enumerate(raw_sources)
    ]
    include_defaults = document.get("include_default_mappings", True)
    if not isinstance(include_defaults, bool):
        raise SyncSchedulerError(
            "来源映射配置的 include_default_mappings 必须是布尔值。"
        )
    target_sheet_name = _identity_text(document.get("target_sheet_name"))
    if not target_sheet_name:
        target_sheet_name = config.sheet_name or DEFAULT_TARGET_SHEET_NAME
    defaults = list(DEFAULT_MULTI_SOURCE_MAPPINGS) if include_defaults else []
    custom_locator_keys: set[tuple[str, str]] = set()
    for mapping in custom:
        keys = _mapping_locator_keys(mapping)
        overlap = custom_locator_keys.intersection(keys)
        if overlap:
            raise SyncSchedulerError(
                "来源映射配置存在重复定位键：" + ", ".join(f"{key[0]}={key[1]}" for key in overlap)
            )
        custom_locator_keys.update(keys)
    # 自定义项优先覆盖同一定位键的内置项；避免一个工作簿同时命中两个品类。
    defaults = [
        mapping
        for mapping in defaults
        if not custom_locator_keys.intersection(_mapping_locator_keys(mapping))
    ]
    mappings = custom + defaults
    if not mappings:
        raise SyncSchedulerError("来源映射配置没有可用来源。")
    return mappings, target_sheet_name


def _mapping_locator_keys(mapping: SourceMapping) -> set[tuple[str, str]]:
    return {
        (field, value)
        for field, value in (
            ("spreadsheet_token", mapping.spreadsheet_token),
            ("sheet_id", mapping.sheet_id),
            ("wiki_node_token", mapping.wiki_node_token),
            ("workbook_title", mapping.workbook_title),
        )
        if value
    }


def _mapping_matches(
    mapping: SourceMapping,
    *,
    spreadsheet_token: str,
    wiki_node_token: str,
    workbook_title: str,
    sheet_name: str,
    sheet_id: str = "",
    match_sheet_name: bool = True,
) -> bool:
    # 生产映射一旦提供 spreadsheet_token（或 Wiki 节点），就把它作为强身份。
    # 不能因另一工作簿偶然复用 Sheet ID/标题而回退命中错误品类。没有强身份的
    # 旧映射才保留 Sheet ID/标题任一命中的兼容逻辑。
    if mapping.spreadsheet_token:
        locator_matches = mapping.spreadsheet_token == spreadsheet_token
    elif mapping.wiki_node_token:
        locator_matches = mapping.wiki_node_token == wiki_node_token
    else:
        locator_matches = any(
            (
                bool(mapping.sheet_id) and mapping.sheet_id == sheet_id,
                bool(mapping.workbook_title)
                and mapping.workbook_title == workbook_title,
            )
        )
    if not locator_matches:
        return False
    return (
        not match_sheet_name
        or not mapping.sheet_name
        or mapping.sheet_name == sheet_name
    )


def _resolve_source_mapping(
    mappings: Sequence[SourceMapping],
    *,
    spreadsheet_token: str,
    wiki_node_token: str,
    workbook_title: str,
    sheet_name: str,
    sheet_id: str = "",
) -> SourceMapping | None:
    matches = [
        mapping
        for mapping in mappings
        if _mapping_matches(
            mapping,
            spreadsheet_token=spreadsheet_token,
            wiki_node_token=wiki_node_token,
            workbook_title=workbook_title,
            sheet_name=sheet_name,
            sheet_id=sheet_id,
        )
    ]
    if not matches:
        return None
    custom_matches = [mapping for mapping in matches if mapping.custom]
    if custom_matches:
        matches = custom_matches
    first = matches[0]
    conflicts = [
        mapping
        for mapping in matches[1:]
        if (mapping.category_id, mapping.category_name)
        != (first.category_id, first.category_name)
    ]
    if conflicts:
        raise SyncSchedulerError(
            "来源映射存在歧义：同一个飞书工作表匹配了多个不同品类。"
        )
    return first


def _sheet_title(sheet: Mapping[str, Any]) -> str:
    return _text(sheet.get("title") or sheet.get("name") or sheet.get("sheet_name"))


def _discover_wiki_nodes(
    lark: LarkCli,
    *,
    wiki_root: str,
    max_pages: int,
) -> list[Mapping[str, Any]]:
    root = lark.wiki_node(wiki_root)
    root_token = _text(root.get("node_token"))
    root_space_id = _text(root.get("space_id"))
    if not root_token or not root_space_id:
        raise SyncSchedulerError("飞书 Wiki 根节点缺少 node_token 或 space_id。")
    nodes: list[Mapping[str, Any]] = []
    queue = [root]
    visited_nodes: set[str] = set()
    while queue:
        node = queue.pop(0)
        node_token = _text(node.get("node_token"))
        if not node_token or node_token in visited_nodes:
            continue
        visited_nodes.add(node_token)
        nodes.append(node)
        if not node.get("has_child"):
            continue
        space_id = _text(node.get("space_id")) or root_space_id
        queue.extend(
            lark.wiki_children(
                space_id=space_id,
                parent_node_token=node_token,
                max_pages=max_pages,
            )
        )
    return nodes


def _node_audit_fields(wiki_node_token: str) -> dict[str, str]:
    """返回可用于排障的节点标识，但绝不暴露可访问 Wiki 的原始 token。"""
    if not wiki_node_token:
        return {}
    return {"node_fingerprint": _token_fingerprint(wiki_node_token)}


def discover_wiki_sources(
    config: SchedulerConfig,
    *,
    lark: LarkCli,
) -> tuple[list[DiscoveredSource], list[dict[str, str]], list[dict[str, str]]]:
    """递归遍历 Wiki，找出每本表中的指定 Sheet，并按映射绑定品类。"""
    if not config.wiki_root:
        raise SyncSchedulerError("未配置 Wiki 根节点。")
    mappings, target_sheet_name = _load_source_mappings(config)
    sources: list[DiscoveredSource] = []
    skipped: list[dict[str, str]] = []
    ignored: list[dict[str, str]] = []
    seen_sheets: set[tuple[str, str]] = set()
    for node in _discover_wiki_nodes(
        lark,
        wiki_root=config.wiki_root,
        max_pages=config.wiki_max_pages,
    ):
        if _text(node.get("obj_type")) != "sheet":
            continue
        spreadsheet_token = _text(node.get("obj_token"))
        wiki_node_token = _text(node.get("node_token"))
        node_title = _text(node.get("title"))
        if not spreadsheet_token:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": node_title,
                    "reason": "missing_spreadsheet_token",
                }
            )
            continue
        try:
            workbook = lark.workbook_info(spreadsheet_token=spreadsheet_token)
        except Exception as exc:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": node_title,
                    "reason": "workbook_read_failed",
                    "message": _safe_message(
                        str(exc),
                        spreadsheet_token=spreadsheet_token,
                        sensitive_values=(wiki_node_token,),
                    ),
                }
            )
            continue
        workbook_title = _workbook_title(workbook) or node_title
        try:
            workbook_data = _workbook_data(workbook)
        except Exception as exc:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": workbook_title,
                    "reason": "workbook_metadata_invalid",
                    "message": _safe_message(
                        str(exc),
                        spreadsheet_token=spreadsheet_token,
                        sensitive_values=(wiki_node_token,),
                    ),
                }
            )
            continue
        sheets = workbook_data.get("sheets")
        if not isinstance(sheets, list):
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": workbook_title,
                    "reason": "workbook_sheets_missing",
                }
            )
            continue
        allowed_sheet_names = {target_sheet_name}
        allowed_sheet_names.update(
            mapping.sheet_name
            for mapping in mappings
            if mapping.sheet_name
        )
        matching_sheets = [
            sheet
            for sheet in sheets
            if isinstance(sheet, Mapping) and _sheet_title(sheet) in allowed_sheet_names
        ]
        if not matching_sheets:
            configured = any(
                _mapping_matches(
                    mapping,
                    spreadsheet_token=spreadsheet_token,
                    wiki_node_token=wiki_node_token,
                    workbook_title=workbook_title,
                    sheet_name="",
                    sheet_id=_text(candidate.get("sheet_id")),
                    match_sheet_name=False,
                )
                for mapping in mappings
                for candidate in sheets
                if isinstance(candidate, Mapping)
            )
            item = {
                **_node_audit_fields(wiki_node_token),
                "workbook_title": workbook_title,
                "reason": "target_sheet_missing",
                "expected_sheet_name": target_sheet_name,
            }
            (skipped if configured else ignored).append(item)
            continue
        if len(matching_sheets) > 1:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": workbook_title,
                    "reason": "duplicate_target_sheet_name",
                }
            )
            continue
        sheet = matching_sheets[0]
        sheet_id = _text(sheet.get("sheet_id"))
        if not sheet_id:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": workbook_title,
                    "reason": "missing_sheet_id",
                }
            )
            continue
        try:
            mapping = _resolve_source_mapping(
                mappings,
                spreadsheet_token=spreadsheet_token,
                wiki_node_token=wiki_node_token,
                workbook_title=workbook_title,
                sheet_name=_sheet_title(sheet),
                sheet_id=sheet_id,
            )
        except Exception as exc:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": workbook_title,
                    "sheet_id": sheet_id,
                    "reason": "category_mapping_invalid",
                    "message": _safe_message(
                        str(exc),
                        spreadsheet_token=spreadsheet_token,
                        sensitive_values=(wiki_node_token,),
                    ),
                }
            )
            continue
        if mapping is None:
            skipped.append(
                {
                    **_node_audit_fields(wiki_node_token),
                    "workbook_title": workbook_title,
                    "sheet_id": sheet_id,
                    "reason": "category_mapping_missing",
                }
            )
            continue
        unique_key = (spreadsheet_token, sheet_id)
        if unique_key in seen_sheets:
            continue
        seen_sheets.add(unique_key)
        sources.append(
            DiscoveredSource(
                spreadsheet_token=spreadsheet_token,
                sheet_id=sheet_id,
                sheet_name=_sheet_title(sheet),
                workbook_title=workbook_title,
                wiki_node_token=wiki_node_token,
                category_id=mapping.category_id,
                category_name=mapping.category_name,
            )
        )
    sources.sort(
        key=lambda item: (item.category_id, item.workbook_title, item.spreadsheet_token)
    )
    return sources, skipped, ignored


class _SourceLark:
    """把通用 lark-cli 客户端收窄为一个发现到的工作簿/Sheet。"""

    def __init__(self, lark: LarkCli, source: DiscoveredSource) -> None:
        self.lark = lark
        self.source = source

    def revision(self) -> str:
        return self.lark.revision(spreadsheet_token=self.source.spreadsheet_token)

    def workbook_info(self) -> Mapping[str, Any]:
        return self.lark.workbook_info(spreadsheet_token=self.source.spreadsheet_token)

    def cells(
        self,
        *,
        row_count: int,
        column_count: int,
    ) -> Mapping[str, Any]:
        return self.lark.cells(
            row_count=row_count,
            column_count=column_count,
            spreadsheet_token=self.source.spreadsheet_token,
            sheet_id=self.source.sheet_id,
        )


def _default_state_dir() -> Path:
    override = _text(os.environ.get("MODEL_CONFIG_SYNC_STATE_DIR"))
    if override:
        return Path(override)
    if os.name == "nt":
        base = _text(os.environ.get("ProgramData")) or str(Path.cwd())
        return Path(base) / "knowledge-kb-model-configuration-sync"
    return Path("/var/lib/knowledge-kb/model-configuration-sync")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncSchedulerError(f"checkpoint 文件损坏：{path}") from exc
    if not isinstance(value, dict):
        raise SyncSchedulerError(f"checkpoint 根节点必须是对象：{path}")
    return value


def _spreadsheet_fingerprint(spreadsheet_token: str) -> str:
    return _token_fingerprint(spreadsheet_token)


def _token_fingerprint(token: str) -> str:
    """以不可逆摘要用于审计，不把飞书 token 写入日志或 checkpoint。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _source_definition_fingerprint(config: SchedulerConfig) -> str:
    """配置改变时，即便 revision 未变也必须重新核对该来源。"""
    definition = {
        "sheet_id": config.sheet_id,
        "sheet_name": config.sheet_name,
        "category_id": config.category_id,
        "category_name": config.category_name,
        "spreadsheet_token_sha256": _spreadsheet_fingerprint(
            config.spreadsheet_token
        ),
    }
    encoded = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)


@contextlib.contextmanager
def _single_instance_lock(path: Path) -> Iterable[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                handle.write("0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        yield acquired
    finally:
        if acquired and os.name != "nt":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _log(message: str, *, level: str = "INFO") -> None:
    stream = sys.stderr if level == "ERROR" else sys.stdout
    print(f"{_now()} [{level}] {message}", file=stream, flush=True)


def _make_logger(state_dir: Path) -> Callable[..., None]:
    log_path = state_dir / "sync.log"

    def log(message: str, *, level: str = "INFO") -> None:
        line = f"{_now()} [{level}] {message}"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # 日志写入失败不能改变同步结果；仍输出到标准流供调度器采集。
            pass
        stream = sys.stderr if level == "ERROR" else sys.stdout
        print(line, file=stream, flush=True)

    return log


def _sync_backend(config: SchedulerConfig, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    docker_args = [
        "exec",
        "-i",
        config.backend_container,
        "python",
        "-m",
        "app.scripts.sync_model_configurations",
        "-",
        "--actor",
        config.actor,
    ]
    if config.target == "local":
        command = [config.docker_cli, *docker_args]
    else:
        if not config.ssh_host or not config.ssh_key:
            raise SyncSchedulerError(
                "target=ssh 时必须配置 ssh_host 和 ssh_key。"
            )
        if not config.ssh_key.exists():
            raise SyncSchedulerError(f"SSH 私钥不存在：{config.ssh_key}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", config.backend_container):
            raise SyncSchedulerError("backend 容器名包含非法字符。")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", config.ssh_user):
            raise SyncSchedulerError("SSH 用户名包含非法字符。")
        if not re.fullmatch(r"[A-Za-z0-9_.:@-]+", config.ssh_host):
            raise SyncSchedulerError("SSH 主机地址包含非法字符。")
        remote_command = shlex.join([config.docker_cli, *docker_args])
        command = [
            config.ssh_cli,
            "-i",
            str(config.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"{config.ssh_user}@{config.ssh_host}",
            remote_command,
        ]
    output = _run_command(
        command,
        input_bytes=body,
        timeout=config.command_timeout_seconds,
        spreadsheet_token=config.spreadsheet_token,
        runner=subprocess.run,
    )
    result = _json_from_output(
        output.decode("utf-8", errors="replace"),
        spreadsheet_token=config.spreadsheet_token,
    )
    if result.get("status") != "success":
        raise SyncSchedulerError(
            "服务端同步返回失败："
            + _safe_message(
                result.get("message") or result,
                spreadsheet_token=config.spreadsheet_token,
            )
        )
    return result


def run_once(
    config: SchedulerConfig,
    *,
    lark: LarkCli | None = None,
    backend_sync: Callable[[SchedulerConfig, Mapping[str, Any]], Mapping[str, Any]] = _sync_backend,
) -> dict[str, Any]:
    if config.wiki_root:
        return _run_wiki_once(
            config,
            lark=lark,
            backend_sync=backend_sync,
        )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log = _make_logger(config.state_dir)
    with _single_instance_lock(config.lock_path) as acquired:
        if not acquired:
            result = {"status": "skipped", "reason": "already_running"}
            log("已有同步任务运行，本次跳过。")
            return result
        state = _load_state(config.state_path)
        expected_fingerprint = _spreadsheet_fingerprint(config.spreadsheet_token)
        expected_source_definition = _source_definition_fingerprint(config)
        stored_fingerprint = _text(state.get("spreadsheet_token_sha256"))
        stored_sheet_id = _text(state.get("sheet_id"))
        if stored_fingerprint and stored_fingerprint != expected_fingerprint:
            raise SyncSchedulerError(
                "checkpoint 对应的飞书工作簿与当前配置不同，已停止同步；请更换 state-dir 或人工确认。"
            )
        if (
            not stored_fingerprint
            and _text(state.get("last_success_revision"))
            and not config.force
        ):
            raise SyncSchedulerError(
                "旧 checkpoint 缺少工作簿指纹，已停止复用；请先使用 --force 成功同步一次以初始化 checkpoint。"
            )
        if stored_sheet_id and stored_sheet_id != config.sheet_id:
            raise SyncSchedulerError(
                "checkpoint 对应的工作表与当前配置不同，已停止同步；请更换 state-dir 或人工确认。"
            )
        lark = lark or LarkCli(config)
        for attempt in range(1, config.max_revision_retries + 1):
            before_revision = lark.revision()
            last_revision = _text(state.get("last_success_revision"))
            stored_source_definition = _text(state.get("source_definition_sha256"))
            if (
                not config.force
                and last_revision == before_revision
                and (
                    not stored_source_definition
                    or stored_source_definition == expected_source_definition
                )
            ):
                result = {
                    "status": "skipped",
                    "reason": "revision_unchanged",
                    "revision": before_revision,
                }
                log(f"revision={before_revision} 未变化，跳过整表读取。")
                return result

            workbook = lark.workbook_info()
            target_sheet = _find_target_sheet(workbook, config.sheet_id)
            row_count = int(target_sheet.get("row_count") or 0)
            column_count = int(target_sheet.get("column_count") or 0)
            cells = lark.cells(
                row_count=row_count,
                column_count=column_count,
            )
            payload = build_payload(
                workbook_response=workbook,
                cells_response=cells,
                sheet_id=config.sheet_id,
                spreadsheet_token=config.spreadsheet_token,
                category_id=config.category_id,
                category_name=config.category_name,
                sheet_name=config.sheet_name,
            )
            exported_revision = _text(payload.get("revision"))
            after_revision = lark.revision()
            if (
                before_revision != after_revision
                or exported_revision != after_revision
            ):
                log(
                    "表格在读取期间发生变化 "
                    f"（开始={before_revision}，导出={exported_revision}，结束={after_revision}），"
                    f"第 {attempt}/{config.max_revision_retries} 次重试。",
                    level="WARN",
                )
                if attempt == config.max_revision_retries:
                    raise SyncSchedulerError(
                        "飞书表格持续变化，无法形成一致快照；本次未写入知识库。"
                    )
                time.sleep(min(attempt, 3))
                continue

            digest = payload_hash(payload)
            if not config.force and _text(state.get("last_payload_sha256")) == digest:
                state.update(
                    {
                        "schema_version": 1,
                        "spreadsheet_token_sha256": expected_fingerprint,
                        "sheet_id": config.sheet_id,
                        "sheet_name": config.sheet_name,
                        "category_id": config.category_id,
                        "category_name": config.category_name,
                        "source_definition_sha256": expected_source_definition,
                        "last_success_revision": after_revision,
                        "last_payload_sha256": digest,
                        "last_success_at_utc": _now(),
                        "last_result": {"status": "content_unchanged"},
                        "last_error": None,
                    }
                )
                _write_state(config.state_path, state)
                result = {
                    "status": "skipped",
                    "reason": "content_unchanged",
                    "revision": after_revision,
                    "payload_sha256": digest,
                }
                log(f"revision={after_revision} 内容未变化，跳过写库。")
                return result

            if config.check_only:
                result = {
                    "status": "validated",
                    "revision": after_revision,
                    "payload_sha256": digest,
                    "records": len(payload["records"]),
                }
                log(
                    f"检查通过：revision={after_revision}，records={len(payload['records'])}，未写库。"
                )
                return result

            sync_result = backend_sync(config, payload)
            state.update(
                {
                    "schema_version": 1,
                    "spreadsheet_token_sha256": expected_fingerprint,
                    "sheet_id": config.sheet_id,
                    "sheet_name": config.sheet_name,
                    "category_id": config.category_id,
                    "category_name": config.category_name,
                    "source_definition_sha256": expected_source_definition,
                    "last_success_revision": after_revision,
                    "last_payload_sha256": digest,
                    "last_success_at_utc": _now(),
                    "last_result": sync_result,
                    "last_error": None,
                }
            )
            _write_state(config.state_path, state)
            result = {
                "status": "success",
                "revision": after_revision,
                "payload_sha256": digest,
                "result": sync_result,
            }
            log(f"同步成功：revision={after_revision}，records={len(payload['records'])}。")
            return result
        raise SyncSchedulerError("达到最大重试次数，未完成同步。")


def _source_state_dir(config: SchedulerConfig, source: DiscoveredSource) -> Path:
    return config.state_dir / "sources" / source.state_key


def _missing_previous_sources(
    config: SchedulerConfig,
    sources: Sequence[DiscoveredSource],
) -> list[dict[str, str]]:
    """返回上轮已记录、但本轮 Wiki 发现不到的来源；绝不删除对应知识。"""
    sources_dir = config.state_dir / "sources"
    if not sources_dir.exists():
        return []
    current_keys = {source.state_key for source in sources}
    missing: list[dict[str, str]] = []
    try:
        state_paths = sorted(
            (
                child / "state.json"
                for child in sources_dir.iterdir()
                if child.is_dir() and (child / "state.json").is_file()
            ),
            key=lambda path: path.parent.name,
        )
    except OSError as exc:
        return [
            {
                "reason": "previous_sources_scan_failed",
                "message": _safe_message(str(exc)),
            }
        ]
    for state_path in state_paths:
        state_key = state_path.parent.name
        if state_key in current_keys:
            continue
        try:
            state = _load_state(state_path)
        except SyncSchedulerError as exc:
            missing.append(
                {
                    "source_state_key": state_key,
                    "reason": "previous_checkpoint_invalid",
                    "message": _safe_message(str(exc)),
                }
            )
            continue
        missing.append(
            {
                "source_state_key": state_key,
                "workbook_title": _text(state.get("workbook_title")),
                "sheet_id": _text(state.get("sheet_id")),
                "reason": "missing_previous_source",
            }
        )
    return missing


def _record_source_error(
    config: SchedulerConfig,
    source: DiscoveredSource,
    message: str,
) -> str | None:
    state_dir = _source_state_dir(config, source)
    state_path = state_dir / "state.json"
    try:
        state = _load_state(state_path)
    except SyncSchedulerError as exc:
        # checkpoint 可能保存了上一轮可审计的状态；损坏时绝不能用空对象覆盖它。
        return "来源 checkpoint 损坏，未覆盖原文件：" + _safe_message(str(exc))
    state.update(
        {
            "schema_version": 2,
            "spreadsheet_token_sha256": _spreadsheet_fingerprint(
                source.spreadsheet_token
            ),
            "sheet_id": source.sheet_id,
            "category_id": source.category_id,
            "category_name": source.category_name,
            "workbook_title": source.workbook_title,
            "wiki_node_token_sha256": _token_fingerprint(source.wiki_node_token),
            "last_error": {"at_utc": _now(), "message": message},
        }
    )
    _write_state(state_path, state)
    return None


def _run_source_once(
    config: SchedulerConfig,
    source: DiscoveredSource,
    *,
    lark: LarkCli,
    backend_sync: Callable[[SchedulerConfig, Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    source_config = replace(
        config,
        spreadsheet_token=source.spreadsheet_token,
        sheet_id=source.sheet_id,
        category_id=source.category_id,
        category_name=source.category_name,
        sheet_name=source.sheet_name,
        state_dir=_source_state_dir(config, source),
        wiki_root="",
    )
    return run_once(
        source_config,
        lark=_SourceLark(lark, source),
        backend_sync=backend_sync,
    )


def _run_wiki_once(
    config: SchedulerConfig,
    *,
    lark: LarkCli | None,
    backend_sync: Callable[[SchedulerConfig, Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """发现并处理所有来源；每来源失败只影响自身 checkpoint。"""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log = _make_logger(config.state_dir)
    with _single_instance_lock(config.lock_path) as acquired:
        if not acquired:
            result = {"status": "skipped", "reason": "already_running"}
            log("已有多来源同步任务运行，本次跳过。")
            return result
        lark_client = lark or LarkCli(config)
        sources, skipped, ignored_non_target_sources = discover_wiki_sources(
            config, lark=lark_client
        )
        missing_previous_sources = _missing_previous_sources(config, sources)
        all_skipped = [*skipped, *missing_previous_sources]
        if not sources:
            if all_skipped:
                details = "; ".join(
                    f"{item.get('workbook_title') or item.get('node_fingerprint')}:"
                    f"{item.get('reason')}"
                    for item in all_skipped[:10]
                )
                raise SyncSchedulerError(
                    "Wiki 下没有发现可同步的个性化配置信息 Sheet。" + details
                )
            return {
                "status": "skipped",
                "reason": "no_target_sheets",
                "sources": [],
                "missing_previous_sources": [],
                "ignored_non_target_sources": ignored_non_target_sources,
            }

        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for source in sources:
            try:
                result = _run_source_once(
                    config,
                    source,
                    lark=lark_client,
                    backend_sync=backend_sync,
                )
            except Exception as exc:
                message = _safe_message(
                    str(exc), spreadsheet_token=source.spreadsheet_token
                )
                if not message:
                    message = f"{type(exc).__name__}"
                try:
                    checkpoint_error = _record_source_error(config, source, message)
                    if checkpoint_error:
                        message = f"{message}；{checkpoint_error}"
                except Exception as checkpoint_exc:
                    message = (
                        f"{message}；来源失败 checkpoint 写入失败："
                        + _safe_message(str(checkpoint_exc))
                    )
                failures.append(
                    {
                        "workbook_title": source.workbook_title,
                        "sheet_id": source.sheet_id,
                        "reason": message,
                    }
                )
                log(
                    f"来源失败（{source.workbook_title}/{source.sheet_id}）：{message}",
                    level="ERROR",
                )
                continue
            results.append(
                {
                    "workbook_title": source.workbook_title,
                    "sheet_id": source.sheet_id,
                    "category_id": source.category_id,
                    "category_name": source.category_name,
                    "result": result,
                }
            )

        summary = {
            "status": "success" if not failures and not all_skipped else "partial",
            "sources": results,
            "failed_sources": failures,
            "skipped_sources": all_skipped,
            "missing_previous_sources": missing_previous_sources,
            "ignored_non_target_sources": ignored_non_target_sources,
        }
        if failures or all_skipped:
            log(
                "多来源同步部分完成："
                f"success={len(results)} failed={len(failures)} skipped={len(all_skipped)}。",
                level="ERROR",
            )
            return summary
        log(f"多来源同步成功：sources={len(results)}。")
        return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="定时同步飞书机型配置信息。")
    parser.add_argument("--identity", choices=("user", "bot"), default=os.environ.get("MODEL_CONFIG_SYNC_IDENTITY", "bot"))
    parser.add_argument("--spreadsheet-token", default=os.environ.get("MODEL_CONFIG_SYNC_SPREADSHEET_TOKEN", DEFAULT_SPREADSHEET_TOKEN))
    parser.add_argument("--sheet-id", default=os.environ.get("MODEL_CONFIG_SYNC_SHEET_ID", DEFAULT_SHEET_ID))
    parser.add_argument("--category-id", default=os.environ.get("MODEL_CONFIG_SYNC_CATEGORY_ID", DEFAULT_CATEGORY_ID))
    parser.add_argument("--category-name", default=os.environ.get("MODEL_CONFIG_SYNC_CATEGORY_NAME", DEFAULT_CATEGORY_NAME))
    parser.add_argument("--sheet-name", default=os.environ.get("MODEL_CONFIG_SYNC_SHEET_NAME", DEFAULT_TARGET_SHEET_NAME))
    parser.add_argument(
        "--wiki-root",
        default=os.environ.get("MODEL_CONFIG_SYNC_WIKI_ROOT", ""),
        help="Wiki 根节点 URL/token；配置后启用全量多工作簿发现模式。",
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        default=Path(os.environ["MODEL_CONFIG_SYNC_SOURCE_CONFIG"])
        if os.environ.get("MODEL_CONFIG_SYNC_SOURCE_CONFIG")
        else None,
        help="可选的工作簿到品类映射 JSON。",
    )
    parser.add_argument(
        "--wiki-max-pages",
        type=int,
        default=int(os.environ.get("MODEL_CONFIG_SYNC_WIKI_MAX_PAGES", "200")),
    )
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir())
    default_lark_cli = "lark-cli.cmd" if os.name == "nt" else "lark-cli"
    parser.add_argument("--lark-cli", default=os.environ.get("MODEL_CONFIG_SYNC_LARK_CLI", default_lark_cli))
    parser.add_argument("--docker-cli", default=os.environ.get("MODEL_CONFIG_SYNC_DOCKER_CLI", "docker"))
    parser.add_argument("--backend-container", default=os.environ.get("MODEL_CONFIG_SYNC_BACKEND_CONTAINER", "kb-backend"))
    parser.add_argument("--target", choices=("ssh", "local"), default=os.environ.get("MODEL_CONFIG_SYNC_TARGET", "ssh"))
    parser.add_argument("--ssh-cli", default=os.environ.get("MODEL_CONFIG_SYNC_SSH_CLI", "ssh"))
    parser.add_argument("--ssh-host", default=os.environ.get("MODEL_CONFIG_SYNC_SSH_HOST", ""))
    parser.add_argument("--ssh-user", default=os.environ.get("MODEL_CONFIG_SYNC_SSH_USER", "root"))
    parser.add_argument("--ssh-key", type=Path, default=Path(os.environ["MODEL_CONFIG_SYNC_SSH_KEY"]) if os.environ.get("MODEL_CONFIG_SYNC_SSH_KEY") else None)
    parser.add_argument("--actor", default=os.environ.get("MODEL_CONFIG_SYNC_ACTOR", DEFAULT_ACTOR))
    parser.add_argument("--max-revision-retries", type=int, default=int(os.environ.get("MODEL_CONFIG_SYNC_MAX_RETRIES", "3")))
    parser.add_argument("--command-timeout-seconds", type=int, default=int(os.environ.get("MODEL_CONFIG_SYNC_COMMAND_TIMEOUT_SECONDS", "900")))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> SchedulerConfig:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", args.actor):
        raise SyncSchedulerError("actor 只能包含字母、数字、点、下划线和连字符。")
    if not 1 <= args.max_revision_retries <= 5:
        raise SyncSchedulerError("max-revision-retries 必须在 1 到 5 之间。")
    if not 30 <= args.command_timeout_seconds <= 3600:
        raise SyncSchedulerError("command-timeout-seconds 必须在 30 到 3600 之间。")
    if not _identity_text(args.category_id) or not _identity_text(args.category_name):
        raise SyncSchedulerError("category-id 和 category-name 不能为空。")
    if args.wiki_max_pages < 1 or args.wiki_max_pages > 10000:
        raise SyncSchedulerError("wiki-max-pages 必须在 1 到 10000 之间。")
    return SchedulerConfig(
        identity=args.identity,
        spreadsheet_token=args.spreadsheet_token,
        sheet_id=args.sheet_id,
        state_dir=args.state_dir,
        lark_cli=args.lark_cli,
        docker_cli=args.docker_cli,
        backend_container=args.backend_container,
        target=args.target,
        ssh_cli=args.ssh_cli,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_key=args.ssh_key,
        actor=args.actor,
        max_revision_retries=args.max_revision_retries,
        command_timeout_seconds=args.command_timeout_seconds,
        force=args.force,
        check_only=args.check_only,
        category_id=_identity_text(args.category_id),
        category_name=_identity_text(args.category_name),
        sheet_name=_identity_text(args.sheet_name) or DEFAULT_TARGET_SHEET_NAME,
        wiki_root=_text(args.wiki_root),
        source_config_path=args.source_config,
        wiki_max_pages=args.wiki_max_pages,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config: SchedulerConfig | None = None
    try:
        config = _config_from_args(_parser().parse_args(argv))
        result = run_once(config)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 2 if result.get("status") == "partial" else 0
    except SyncSchedulerError as exc:
        if config is not None:
            message = _safe_message(
                str(exc),
                spreadsheet_token=config.spreadsheet_token,
            )
            log = _make_logger(config.state_dir)
            log(message, level="ERROR")
            try:
                state = _load_state(config.state_path)
                state["last_error"] = {
                    "at_utc": _now(),
                    "message": message,
                }
                _write_state(config.state_path, state)
            except SyncSchedulerError as state_error:
                log(
                    "无法写入失败 checkpoint："
                    + _safe_message(str(state_error)),
                    level="ERROR",
                )
        _log(
            _safe_message(
                str(exc),
                spreadsheet_token=(config.spreadsheet_token if config else ""),
            ),
            level="ERROR",
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": _safe_message(
                        str(exc),
                        spreadsheet_token=(config.spreadsheet_token if config else ""),
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1
    except KeyboardInterrupt:
        _log("收到中断信号，本次未完成同步。", level="ERROR")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


