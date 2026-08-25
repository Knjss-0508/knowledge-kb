from __future__ import annotations

from datetime import datetime
import json
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SCHEDULE_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AutomationControlError(RuntimeError):
    pass


class AutomationDisabled(AutomationControlError):
    pass


class AutomationRunActive(AutomationControlError):
    pass


class AutomationControlStore:
    """Persist safe operator controls beside the automation run state."""

    def __init__(self, root: str | Path, *, stale_after_seconds: int = 7_200) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_path = self.root / "automation-control.json"
        self.run_lock_path = self.root / ".automation-control-run.lock"
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self._lock = Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "enabled": False,
            "schedule_enabled": False,
            "schedule_time": "02:00",
            "timezone": "Asia/Shanghai",
            "updated_at": "",
            "last_run": {},
            "last_scheduled_date": "",
        }

    def _read_locked(self) -> dict[str, Any]:
        data = self._defaults()
        if not self.control_path.is_file():
            return data
        try:
            loaded = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return data
        if not isinstance(loaded, dict):
            return data
        for key in data:
            if key in loaded:
                data[key] = loaded[key]
        data["enabled"] = bool(data["enabled"])
        data["schedule_enabled"] = bool(data["schedule_enabled"])
        data["schedule_time"] = self._validated_schedule_time(data["schedule_time"])
        data["timezone"] = "Asia/Shanghai"
        data["last_run"] = data["last_run"] if isinstance(data["last_run"], dict) else {}
        return data

    def _write_locked(self, data: dict[str, Any]) -> None:
        temporary = self.control_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.control_path)

    @staticmethod
    def _validated_schedule_time(value: Any) -> str:
        text = str(value or "").strip()
        if not _SCHEDULE_TIME_RE.fullmatch(text):
            return "02:00"
        return text

    def _run_is_active_locked(self) -> bool:
        if not self.run_lock_path.exists():
            return False
        try:
            age_seconds = datetime.now().timestamp() - self.run_lock_path.stat().st_mtime
        except OSError:
            return False
        if age_seconds >= self.stale_after_seconds:
            self.run_lock_path.unlink(missing_ok=True)
            return False
        return True

    @staticmethod
    def _public(data: dict[str, Any], running: bool) -> dict[str, Any]:
        return {
            "enabled": bool(data["enabled"]),
            "schedule_enabled": bool(data["schedule_enabled"]),
            "schedule_time": str(data["schedule_time"]),
            "timezone": "Asia/Shanghai",
            "running": running,
            "updated_at": str(data.get("updated_at") or ""),
            "last_run": dict(data.get("last_run") or {}),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = self._read_locked()
            return self._public(data, self._run_is_active_locked())

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {"enabled", "schedule_enabled", "schedule_time"}
        unknown = set(patch) - allowed
        if unknown:
            raise AutomationControlError("控制请求包含不支持的字段")
        with self._lock:
            data = self._read_locked()
            if "enabled" in patch:
                if not isinstance(patch["enabled"], bool):
                    raise AutomationControlError("自动化总开关必须是布尔值")
                data["enabled"] = patch["enabled"]
            if "schedule_enabled" in patch:
                if not isinstance(patch["schedule_enabled"], bool):
                    raise AutomationControlError("每日计划开关必须是布尔值")
                data["schedule_enabled"] = patch["schedule_enabled"]
            if "schedule_time" in patch:
                value = str(patch["schedule_time"] or "").strip()
                if not _SCHEDULE_TIME_RE.fullmatch(value):
                    raise AutomationControlError("计划时间必须为 HH:MM 格式")
                data["schedule_time"] = value
            data["updated_at"] = self._now()
            self._write_locked(data)
            return self._public(data, self._run_is_active_locked())

    def schedule_due(self, now: datetime | None = None) -> bool:
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        with self._lock:
            data = self._read_locked()
            if not data["enabled"] or not data["schedule_enabled"]:
                return False
            if data.get("last_scheduled_date") == current.date().isoformat():
                return False
            hour, minute = (int(part) for part in data["schedule_time"].split(":"))
            return (current.hour, current.minute) >= (hour, minute)

    def start_run(self, *, actor: str, trigger: str) -> dict[str, Any]:
        with self._lock:
            data = self._read_locked()
            if not data["enabled"]:
                raise AutomationDisabled("自动化总开关已关闭，不能创建新运行。")
            if self._run_is_active_locked():
                raise AutomationRunActive("已有自动化任务正在运行，请等待完成后再试。")
            try:
                descriptor = os.open(
                    self.run_lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError as exc:
                raise AutomationRunActive("已有自动化任务正在运行，请等待完成后再试。") from exc
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"started_at": self._now()}, ensure_ascii=False))
            started_at = self._now()
            data["last_run"] = {
                "status": "running",
                "trigger": trigger,
                "actor": actor[:80],
                "started_at": started_at,
                "finished_at": "",
                "fetched_records": 0,
                "queued_jobs": 0,
                "rejected_records": 0,
                "processed": 0,
                "failed": 0,
                "error": "",
            }
            if trigger == "schedule":
                data["last_scheduled_date"] = datetime.now(SHANGHAI_TZ).date().isoformat()
            data["updated_at"] = started_at
            self._write_locked(data)
            return self._public(data, True)

    def finish_run(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read_locked()
            last_run = dict(data.get("last_run") or {})
            last_run.update(
                {
                    "status": str(result.get("status") or "completed"),
                    "finished_at": self._now(),
                    "fetched_records": int(result.get("fetched_records") or 0),
                    "queued_jobs": int(result.get("queued_jobs") or 0),
                    "rejected_records": int(result.get("rejected_records") or 0),
                    "processed": int(result.get("processed") or 0),
                    "failed": int(result.get("failed") or 0),
                    "error": str(result.get("error") or "")[:240],
                }
            )
            data["last_run"] = last_run
            data["updated_at"] = self._now()
            self._write_locked(data)
            self.run_lock_path.unlink(missing_ok=True)
            return self._public(data, False)
