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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_SPREADSHEET_TOKEN = "TLxlsXMKJhPn1htD31lcdl2enKd"
DEFAULT_SHEET_ID = "w3Caff"
DEFAULT_CATEGORY_ID = "119"
DEFAULT_CATEGORY_NAME = "平板电脑"
DEFAULT_ACTOR = "model-configuration-sync"
REQUIRED_HEADERS = ("标题", "品牌ID", "品牌", "型号ID", "型号", "综合内容")
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

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "sync.lock"


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


def _safe_message(value: Any, *, spreadsheet_token: str = "") -> str:
    message = _text(value)
    if spreadsheet_token:
        message = message.replace(spreadsheet_token, "[spreadsheet-redacted]")
    message = re.sub(
        r"(?i)(access[_-]?token|refresh[_-]?token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        message,
    )
    return message[:2000]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_from_output(output: str, *, spreadsheet_token: str = "") -> Mapping[str, Any]:
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
                + _safe_message(text, spreadsheet_token=spreadsheet_token)
            )
    if not isinstance(value, Mapping):
        raise SyncSchedulerError("命令返回 JSON 根节点不是对象。")
    return value


def _run_command(
    argv: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    spreadsheet_token: str = "",
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
            + _safe_message(detail_text, spreadsheet_token=spreadsheet_token)
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

    def call(self, args: Sequence[str]) -> Mapping[str, Any]:
        output = _run_command(
            [self.config.lark_cli, *args, "--json"],
            spreadsheet_token=self.config.spreadsheet_token,
            timeout=min(self.config.command_timeout_seconds, 120),
            env=self.env,
            runner=self.runner,
        )
        response = _json_from_output(
            output.decode("utf-8", errors="replace"),
            spreadsheet_token=self.config.spreadsheet_token,
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
                + _safe_message(message or "未提供错误详情。", spreadsheet_token=self.config.spreadsheet_token)
            )
        return response

    def revision(self) -> str:
        response = self.call(
            [
                "sheets",
                "+revision-get",
                "--as",
                self.config.identity,
                "--spreadsheet-token",
                self.config.spreadsheet_token,
            ]
        )
        revision = _text((response.get("data") or {}).get("revision"))
        if not revision:
            raise SyncSchedulerError("飞书表格未返回有效 revision。")
        return revision

    def workbook_info(self) -> Mapping[str, Any]:
        response = self.call(
            [
                "sheets",
                "+workbook-info",
                "--as",
                self.config.identity,
                "--spreadsheet-token",
                self.config.spreadsheet_token,
            ]
        )
        data = response.get("data")
        warning_message = _text(data.get("warning_message")) if isinstance(data, Mapping) else ""
        if warning_message:
            raise SyncSchedulerError(
                "飞书工作簿读取返回告警，禁止同步："
                + _safe_message(
                    warning_message,
                    spreadsheet_token=self.config.spreadsheet_token,
                )
            )
        if isinstance(data, Mapping) and data.get("complete") is False:
            raise SyncSchedulerError("飞书工作簿读取不完整，禁止同步。")
        return response

    def cells(self, *, row_count: int) -> Mapping[str, Any]:
        if row_count < 2:
            raise SyncSchedulerError("飞书目标工作表没有可同步的数据行。")
        return self.call(
            [
                "sheets",
                "+cells-get",
                "--as",
                self.config.identity,
                "--spreadsheet-token",
                self.config.spreadsheet_token,
                "--sheet-id",
                self.config.sheet_id,
                "--range",
                f"A1:Q{row_count}",
                "--include",
                "value",
                "--max-chars",
                "20000000",
            ]
        )


def _find_target_sheet(workbook_response: Mapping[str, Any], sheet_id: str) -> Mapping[str, Any]:
    data = workbook_response.get("data")
    sheets = data.get("sheets") if isinstance(data, Mapping) else None
    for sheet in sheets if isinstance(sheets, list) else []:
        if isinstance(sheet, Mapping) and _text(sheet.get("sheet_id")) == sheet_id:
            try:
                row_count = int(sheet.get("row_count") or 0)
            except (TypeError, ValueError) as exc:
                raise SyncSchedulerError("飞书工作表行数不是有效数字。") from exc
            if row_count < 2:
                raise SyncSchedulerError("飞书目标工作表没有可同步的数据行。")
            return sheet
    raise SyncSchedulerError(f"未找到目标工作表：{sheet_id}")


def _cell_value(cell: Any) -> str:
    if isinstance(cell, Mapping):
        return _text(cell.get("value"))
    return _text(cell)


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
) -> dict[str, Any]:
    target_sheet = _find_target_sheet(workbook_response, sheet_id)
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
    sheet_range = ranges[0] if isinstance(ranges, list) and ranges else None
    if not isinstance(sheet_range, Mapping) or sheet_range.get("truncated"):
        raise SyncSchedulerError("飞书表格结果为空或被截断，禁止同步。")
    actual_range = _text(sheet_range.get("actual_range"))
    if not re.fullmatch(r"(?i)A1:[A-Z]+[0-9]+", actual_range):
        raise SyncSchedulerError(
            "飞书表格实际读取范围异常，禁止同步："
            + (actual_range or "未返回 actual_range")
        )
    col_indices = sheet_range.get("col_indices")
    if not isinstance(col_indices, list) or not col_indices or _text(col_indices[0]).upper() != "A":
        raise SyncSchedulerError("飞书表格未返回从 A 列开始的有效列定位，禁止同步。")
    rows = sheet_range.get("cells")
    if not isinstance(rows, list) or len(rows) < 2:
        raise SyncSchedulerError("飞书表格没有可同步的数据行。")
    row_indices = sheet_range.get("row_indices")
    if not isinstance(row_indices, list) or len(row_indices) != len(rows):
        raise SyncSchedulerError("飞书表格行定位与数据行数不一致，禁止同步。")
    for index in range(1, len(row_indices)):
        try:
            if int(row_indices[index]) <= int(row_indices[index - 1]):
                raise SyncSchedulerError("飞书表格行定位不是递增序列，禁止同步。")
        except (TypeError, ValueError) as exc:
            raise SyncSchedulerError("飞书表格行定位包含无效行号，禁止同步。") from exc

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
    missing_headers = [header for header in REQUIRED_HEADERS if header not in headers]
    if missing_headers:
        raise SyncSchedulerError(
            "飞书表格缺少必填列：" + "、".join(missing_headers)
        )

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
            for header in REQUIRED_HEADERS
        }
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
        key = (DEFAULT_CATEGORY_ID, brand_id, model_id)
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
                "来源工作表": "个性化配置信息",
                "来源行号": source_row_number,
                "品类ID": DEFAULT_CATEGORY_ID,
                "品类": DEFAULT_CATEGORY_NAME,
                "品牌ID": brand_id,
                "品牌": brand_name,
                "型号ID": model_id,
                "型号": model_name,
                "标题": required["标题"],
                "综合内容": required["综合内容"],
            }
        )
        records.append(
            {
                "source_record_id": source_record_id,
                "title": required["标题"],
                "category_id": DEFAULT_CATEGORY_ID,
                "category_name": DEFAULT_CATEGORY_NAME,
                "brand_id": brand_id,
                "brand_name": brand_name,
                "model_id": model_id,
                "model_name": model_name,
                "content": required["综合内容"],
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
        "sheet_name": "个性化配置信息",
        "revision": revision,
        "category_id": DEFAULT_CATEGORY_ID,
        "category_name": DEFAULT_CATEGORY_NAME,
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
    return hashlib.sha256(spreadsheet_token.encode("utf-8")).hexdigest()


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
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log = _make_logger(config.state_dir)
    with _single_instance_lock(config.lock_path) as acquired:
        if not acquired:
            result = {"status": "skipped", "reason": "already_running"}
            log("已有同步任务运行，本次跳过。")
            return result
        state = _load_state(config.state_path)
        expected_fingerprint = _spreadsheet_fingerprint(config.spreadsheet_token)
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
            if not config.force and last_revision == before_revision:
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
            cells = lark.cells(row_count=row_count)
            payload = build_payload(
                workbook_response=workbook,
                cells_response=cells,
                sheet_id=config.sheet_id,
                spreadsheet_token=config.spreadsheet_token,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="定时同步飞书机型配置信息。")
    parser.add_argument("--identity", choices=("user", "bot"), default=os.environ.get("MODEL_CONFIG_SYNC_IDENTITY", "bot"))
    parser.add_argument("--spreadsheet-token", default=os.environ.get("MODEL_CONFIG_SYNC_SPREADSHEET_TOKEN", DEFAULT_SPREADSHEET_TOKEN))
    parser.add_argument("--sheet-id", default=os.environ.get("MODEL_CONFIG_SYNC_SHEET_ID", DEFAULT_SHEET_ID))
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
    )


def main(argv: Sequence[str] | None = None) -> int:
    config: SchedulerConfig | None = None
    try:
        config = _config_from_args(_parser().parse_args(argv))
        result = run_once(config)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
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


