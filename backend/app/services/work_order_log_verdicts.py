"""从 zzdy 反代访问日志判定工单号真实性（无需 Cookie）。

签名（2026-10-08 与 2026-10-10 两次在生产日志上实测）::

  * GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<n> -> 响应体 >= 300 B
    表示上游确实有这个工单，``<n>`` 是真工单号；123/124 B 表示「查不到」。
  * GET /nmhtapi/im/history?conversationId=<n> -> 响应体 >= 300 B
    表示 ``<n>`` 是被当作会话号使用的（有聊天记录）。

判定规则（与 ``docs/blind-label-work-order-number-audit-20261010.md`` 同一口径）::

  * 见过 questionFormId 有数据          -> ``real``
  * 只在 conversationId 上有数据        -> ``session``（盲标池拒绝建单）
  * 只有「查不到」或日志里没有记录      -> 不下结论（fail-open，放行）
  * 同一号码两者都有数据                -> ``real``（按真工单号放行）

为什么不用 Cookie：曼哈顿会话很短命，``NMHT_COOKIE`` 线上为空、运行时 Cookie
只存在内存里，容器一重启就失效；而访问日志是上游真实流量的旁证，只读、无需登录，
容器重启与重新部署后仍然可用。两者互为补充：Cookie 复核（
``app.services.work_order_verification``）能判定时优先采用，判定不了时才看这里。

实现要点：

* 增量扫描：状态文件记录 ``offset``/``inode``，只读新增字节；日志被轮转（inode
  变化或文件变小）时从头上重扫，已判定的结论保留在状态文件里。
* 结论文件：``<backend>/data/work_order_log_verdicts.json``（容器内 ``/app/data``，
  权限 0600、tmp + ``os.replace`` 原子写），读取按 mtime/size 缓存。
* 扫描只读日志、不调上游接口，因此 Cookie 门禁休眠时本门禁仍然生效。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

VERDICT_REAL = "real"
VERDICT_SESSION = "session"

#: 上游工单详情接口在这个体量以上算「有数据」（实测 534~1128 B）。
DETAIL_PRESENT_MIN_BYTES = 300
#: 上游工单详情接口在这个体量以下算「查不到」（实测 123/124 B）。
DETAIL_MISSING_MAX_BYTES = 128
#: 聊天历史接口在这个体量以上算「有聊天数据」（实测空会话 68 B）。
CHAT_PRESENT_MIN_BYTES = 300

SIGNAL_DETAIL_PRESENT = "detail-present"
SIGNAL_DETAIL_MISSING = "detail-missing"
SIGNAL_CHAT_PRESENT = "chat-present"

READ_CHUNK_BYTES = 4 * 1024 * 1024
MAX_TRACKED_NUMBERS = 200_000
STATE_VERSION = 1

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
DEFAULT_STATE_FILE = os.path.join(DATA_DIR, "work_order_log_verdicts.json")

_REQUEST_RE = re.compile(r'"(?:GET|POST) (/[^"]*)" (\d{3}) (\d+)')
_QUESTION_FORM_RE = re.compile(r"[?&]questionFormId=(\d{1,64})")
_CONVERSATION_RE = re.compile(r"[?&]conversationId=(\d{1,64})")

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"path": None, "mtime": None, "size": None, "verdicts": {}}
_MISSING_LOGGED = False


def default_state_file() -> str:
    """Return the configured state file, falling back to the runtime data dir."""

    configured = str(getattr(settings, "WORK_ORDER_LOG_STATE_FILE", "") or "").strip()
    return configured or DEFAULT_STATE_FILE


def classify_log_line(line: str) -> tuple[str, str] | None:
    """Return ``(number, signal)`` for an interesting access-log line.

    ``None`` means the line carries no usable evidence (unrelated request, a
    response too small to distinguish, or an unparsable line).
    """

    if "questionFormId=" not in line and "conversationId=" not in line:
        return None
    match = _REQUEST_RE.search(line)
    if not match:
        return None
    url = match.group(1)
    size_text = match.group(3)
    if not size_text.isdigit():
        return None
    size = int(size_text)
    if "questionFormId=" in url:
        found = _QUESTION_FORM_RE.search(url)
        if not found:
            return None
        number = found.group(1)
        if size >= DETAIL_PRESENT_MIN_BYTES:
            return number, SIGNAL_DETAIL_PRESENT
        if size <= DETAIL_MISSING_MAX_BYTES:
            return number, SIGNAL_DETAIL_MISSING
        return None
    found = _CONVERSATION_RE.search(url)
    if not found:
        return None
    number = found.group(1)
    if size >= CHAT_PRESENT_MIN_BYTES:
        return number, SIGNAL_CHAT_PRESENT
    return None


def apply_signal(verdicts: dict[str, str], number: str, signal: str) -> bool:
    """Merge one signal into ``verdicts``; return whether the verdict changed."""

    if signal == SIGNAL_DETAIL_PRESENT:
        new_verdict = VERDICT_REAL
    elif signal == SIGNAL_CHAT_PRESENT:
        new_verdict = VERDICT_SESSION
    else:
        # "查不到" alone is not proof of anything: a brand new work order may
        # simply not have been queried yet.  Keep failing open.
        return False
    current = verdicts.get(number)
    if current == new_verdict:
        return False
    if current == VERDICT_REAL and new_verdict == VERDICT_SESSION:
        # A number that really served as a work order stays a work order even if
        # it is also used as a chat conversation.
        return False
    verdicts[number] = new_verdict
    return True


def _read_state(state_file: str) -> dict[str, Any]:
    try:
        with open(state_file, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {"offset": 0, "inode": 0, "size": 0, "verdicts": {}, "updated_at": ""}
    except (OSError, ValueError):
        logger.warning("工单号日志判定状态文件损坏，重新从日志开头扫描：%s", state_file, exc_info=True)
        return {"offset": 0, "inode": 0, "size": 0, "verdicts": {}, "updated_at": ""}
    verdicts = raw.get("verdicts")
    if not isinstance(verdicts, dict):
        verdicts = {}
    cleaned = {
        str(number): str(verdict)
        for number, verdict in verdicts.items()
        if verdict in (VERDICT_REAL, VERDICT_SESSION)
    }
    try:
        offset = max(0, int(raw.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        inode = int(raw.get("inode") or 0)
    except (TypeError, ValueError):
        inode = 0
    return {
        "offset": offset,
        "inode": inode,
        "size": int(raw.get("size") or 0),
        "verdicts": cleaned,
        "updated_at": str(raw.get("updated_at") or ""),
    }


def _write_state(state_file: str, state: dict[str, Any]) -> None:
    directory = os.path.dirname(state_file) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "offset": int(state.get("offset") or 0),
        "inode": int(state.get("inode") or 0),
        "size": int(state.get("size") or 0),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "verdicts": state.get("verdicts") or {},
    }
    temporary = f"{state_file}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    try:
        os.chmod(temporary, 0o600)
    except OSError:  # pragma: no cover - Windows / exotic filesystems
        pass
    os.replace(temporary, state_file)


def _trim(verdicts: dict[str, str]) -> None:
    overflow = len(verdicts) - MAX_TRACKED_NUMBERS
    if overflow <= 0:
        return
    for number in list(verdicts)[:overflow]:
        verdicts.pop(number, None)


def _consume_lines(handle, offset: int, max_bytes: int, on_line: Callable[[str], None]) -> int:
    """Feed complete lines after ``offset`` to ``on_line``; return the new offset.

    The trailing partial line (log writes are not line-atomic) is left for the
    next run, so a half-written line can never produce a wrong verdict.
    """

    handle.seek(offset)
    consumed = offset
    pending = b""
    while True:
        if max_bytes:
            remaining = max_bytes - (consumed - offset)
            if remaining <= 0:
                break
            size = min(READ_CHUNK_BYTES, remaining)
        else:
            size = READ_CHUNK_BYTES
        chunk = handle.read(size)
        if not chunk:
            break
        consumed += len(chunk)
        pending += chunk
        start = 0
        while True:
            index = pending.find(b"\n", start)
            if index == -1:
                break
            on_line(pending[start:index].decode("utf-8", errors="replace"))
            start = index + 1
        pending = pending[start:]
    return max(offset, consumed - len(pending))


def scan_log(
    *,
    path: str | None = None,
    state_file: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Scan new log bytes and persist the updated verdict state.

    Returns a stats dict with ``new_real``/``new_session`` (verdicts learned in
    this run) and ``changed`` (``{number: verdict}``, bounded by
    ``WORK_ORDER_LOG_BACKFILL_LIMIT``) so the caller can backfill telemetry rows.
    """

    global _MISSING_LOGGED
    log_path = str(path or getattr(settings, "WORK_ORDER_LOG_PATH", "") or "").strip()
    state_path = str(state_file or default_state_file())
    stats: dict[str, Any] = {
        "path": log_path,
        "state_file": state_path,
        "scanned_lines": 0,
        "matched_lines": 0,
        "missing": False,
        "rotation_reset": False,
        "new_real": 0,
        "new_session": 0,
        "verdicts": 0,
        "offset": 0,
        "elapsed_seconds": 0.0,
        "changed": {},
    }
    if not log_path:
        stats["missing"] = True
        return stats
    try:
        info = os.stat(log_path)
    except OSError:
        stats["missing"] = True
        if not _MISSING_LOGGED:
            _MISSING_LOGGED = True
            logger.warning("工单号日志判定：访问日志不可读（门禁暂不生效）：%s", log_path)
        return stats

    state = _read_state(state_path)
    verdicts: dict[str, str] = state["verdicts"]
    offset = int(state["offset"])
    if offset and (int(state["inode"]) != info.st_ino or info.st_size < offset):
        # The log rotated or was truncated: rescan it from the beginning.  The
        # verdicts already learned stay valid, so history is not lost.
        offset = 0
        stats["rotation_reset"] = True
    budget = max_bytes if max_bytes is not None else int(getattr(settings, "WORK_ORDER_LOG_MAX_BYTES_PER_RUN", 0) or 0)

    changed: dict[str, str] = {}

    def on_line(line: str) -> None:
        stats["scanned_lines"] += 1
        classified = classify_log_line(line)
        if classified is None:
            return
        number, signal = classified
        stats["matched_lines"] += 1
        if apply_signal(verdicts, number, signal):
            change = verdicts[number]
            changed[number] = change
            if change == VERDICT_REAL:
                stats["new_real"] += 1
            else:
                stats["new_session"] += 1

    started = time.time()
    with open(log_path, "rb") as handle:
        new_offset = _consume_lines(handle, offset, budget, on_line)
    _trim(verdicts)
    _write_state(
        state_path,
        {"offset": new_offset, "inode": info.st_ino, "size": info.st_size, "verdicts": verdicts},
    )
    stats["offset"] = new_offset
    stats["verdicts"] = len(verdicts)
    stats["elapsed_seconds"] = round(time.time() - started, 3)
    limit = int(getattr(settings, "WORK_ORDER_LOG_BACKFILL_LIMIT", 0) or 0)
    if limit > 0:
        stats["changed"] = dict(list(changed.items())[:limit])
    else:
        stats["changed"] = {}
    return stats


def _cached_verdicts(state_file: str | None = None) -> dict[str, str]:
    state_path = str(state_file or default_state_file())
    try:
        info = os.stat(state_path)
    except OSError:
        return {}
    with _CACHE_LOCK:
        if (
            _CACHE["path"] == state_path
            and _CACHE["mtime"] == info.st_mtime_ns
            and _CACHE["size"] == info.st_size
        ):
            return dict(_CACHE["verdicts"])
    verdicts = _read_state(state_path)["verdicts"]
    with _CACHE_LOCK:
        _CACHE.update(
            path=state_path,
            mtime=info.st_mtime_ns,
            size=info.st_size,
            verdicts=verdicts,
        )
    return dict(verdicts)


def verdict_for(number: str | None, *, state_file: str | None = None) -> str | None:
    """Return ``"real"``/``"session"`` for a number, or ``None`` when unknown."""

    cleaned = str(number or "").strip()
    if not cleaned:
        return None
    return _cached_verdicts(state_file).get(cleaned)


def verdict_stats(*, state_file: str | None = None) -> dict[str, Any]:
    """Diagnostics for operators: persisted offset and verdict distribution."""

    state_path = str(state_file or default_state_file())
    state = _read_state(state_path)
    verdicts = state["verdicts"]
    counts = {VERDICT_REAL: 0, VERDICT_SESSION: 0}
    for verdict in verdicts.values():
        counts[verdict] = counts.get(verdict, 0) + 1
    return {
        "state_file": state_path,
        "offset": state["offset"],
        "inode": state["inode"],
        "size": state["size"],
        "updated_at": state["updated_at"],
        "counts": counts,
        "tracked": len(verdicts),
    }


def reset_cache() -> None:
    """Drop the in-process verdict cache (used by tests and after a rescan)."""

    with _CACHE_LOCK:
        _CACHE.update(path=None, mtime=None, size=None, verdicts={})


def backfill_verdicts(
    changed: dict[str, str],
    *,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    """Persist fresh verdicts onto already stored telemetry rows.

    Rows already marked with the same verdict are left untouched; only rows whose
    ``work_order_verified`` is NULL (or contradictory) are updated, so a number
    that later turns out to be a real work order is unblocked again.
    """

    cleaned = {
        str(number): verdict
        for number, verdict in (changed or {}).items()
        if str(number).isdigit() and verdict in (VERDICT_REAL, VERDICT_SESSION)
    }
    if not cleaned:
        return 0
    from sqlalchemy import update

    from app.models.integration import RetrievalQualityEvent

    if session_factory is None:
        from app.core.database import SessionLocal as session_factory  # noqa: N813

    session = session_factory()
    updated = 0
    try:
        for verdict, verified in ((VERDICT_SESSION, False), (VERDICT_REAL, True)):
            numbers = [number for number, value in cleaned.items() if value == verdict]
            if not numbers:
                continue
            result = session.execute(
                update(RetrievalQualityEvent)
                .where(
                    RetrievalQualityEvent.question_form_id.in_(numbers),
                    RetrievalQualityEvent.work_order_verified.is_not(verified),
                )
                .values(work_order_verified=verified)
            )
            updated += int(result.rowcount or 0)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return updated


async def run_work_order_log_verdict_worker(stop_event: asyncio.Event) -> None:
    """Periodically scan new access-log bytes and backfill fresh verdicts."""

    poll_seconds = max(5.0, float(settings.WORK_ORDER_LOG_POLL_SECONDS))
    while not stop_event.is_set():
        try:
            stats = await asyncio.to_thread(scan_log)
            if not stats.get("missing") and (stats.get("new_real") or stats.get("new_session")):
                logger.info(
                    "工单号日志判定：本次新增 real=%s session=%s（日志 %s 行，用时 %ss）",
                    stats.get("new_real"),
                    stats.get("new_session"),
                    stats.get("scanned_lines"),
                    stats.get("elapsed_seconds"),
                )
            changed = stats.get("changed") or {}
            if changed:
                updated = await asyncio.to_thread(backfill_verdicts, changed)
                if updated:
                    logger.info("工单号日志判定：回写 %s 条遥测记录。", updated)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("工单号日志判定扫描失败。")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue


__all__ = [
    "CHAT_PRESENT_MIN_BYTES",
    "DEFAULT_STATE_FILE",
    "DETAIL_MISSING_MAX_BYTES",
    "DETAIL_PRESENT_MIN_BYTES",
    "VERDICT_REAL",
    "VERDICT_SESSION",
    "apply_signal",
    "backfill_verdicts",
    "classify_log_line",
    "default_state_file",
    "reset_cache",
    "run_work_order_log_verdict_worker",
    "scan_log",
    "verdict_for",
    "verdict_stats",
]
