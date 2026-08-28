from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
import json
import math
import os
import re
import shutil


TEXT_FIELDS = (
    "聊天内容",
    "历史实际回复",
    "参考话术",
    "核心问题",
    "判定结论",
    "判定依据",
    "图片证据摘要",
    "主题证据摘要",
)

BLOCKING_PATTERNS = {
    "mobile_phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "national_id": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card_like": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
}

WARNING_PATTERNS = {
    "email": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "learning_link": re.compile(r"(?i)https?://[^\s<>\"']+"),
    "address_like": re.compile(
        r"[\u4e00-\u9fff]{2,}(?:省|市|区|县|镇|乡|街道|路|街|巷|号楼|栋|单元|室)"
    ),
}

_REDACTION_METADATA_FIELDS = {
    "序号",
    "上传者",
    "分析时间",
    "工单ID",
    "回收单号",
    "回收业务层级",
    "回收业务层级编码",
    "产品类型",
    "产品类型编码",
}


class RedactionRiskError(ValueError):
    """Raised when unredacted sensitive content is found in an input batch."""


@dataclass(frozen=True)
class OperationsPolicy:
    max_seconds_per_100_rows: float = 900.0
    max_failure_rate: float = 0.05
    max_fallback_rate: float = 0.20
    max_run_cost: float = 50.0
    retention_days: int = 30

    @classmethod
    def from_env(cls) -> "OperationsPolicy":
        return cls(
            max_seconds_per_100_rows=_env_float(
                "ANSWER_HUB_SLA_SECONDS_PER_100_ROWS",
                900.0,
                minimum=60.0,
            ),
            max_failure_rate=_env_float(
                "ANSWER_HUB_SLA_MAX_FAILURE_RATE",
                0.05,
                minimum=0.0,
                maximum=1.0,
            ),
            max_fallback_rate=_env_float(
                "ANSWER_HUB_SLA_MAX_FALLBACK_RATE",
                0.20,
                minimum=0.0,
                maximum=1.0,
            ),
            max_run_cost=_env_float(
                "ANSWER_HUB_MAX_RUN_COST",
                50.0,
                minimum=0.0,
            ),
            retention_days=_env_int(
                "ANSWER_HUB_RETENTION_DAYS",
                30,
                minimum=1,
                maximum=3650,
            ),
        )


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _mask(value: str) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * min(12, len(text) - 4)}{text[-2:]}"


def scan_redaction_rows(
    rows: Iterable[dict[str, Any]],
    *,
    fields: Iterable[str] = TEXT_FIELDS,
    sample_limit: int = 20,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    first_blocking: dict[str, Any] | None = None
    first_blocking_by_row: dict[int, dict[str, Any]] = {}
    blocking_rows: set[int] = set()
    blocking_count = 0
    warning_count = 0
    row_count = 0
    finding_limit = max(1, sample_limit)
    field_names = tuple(fields)
    pattern_groups = (
        ("blocking", BLOCKING_PATTERNS),
        ("warning", WARNING_PATTERNS),
    )
    for row_index, row in enumerate(rows, start=1):
        row_count += 1
        for field in field_names:
            value = row.get(field)
            if value is None:
                continue
            text = str(value)
            for severity, patterns in pattern_groups:
                for finding_type, pattern in patterns.items():
                    for match in pattern.finditer(text):
                        finding = {
                            "severity": severity,
                            "type": finding_type,
                            "row": row_index,
                            "field": field,
                            "sample": _mask(match.group(0)),
                        }
                        if severity == "blocking":
                            blocking_count += 1
                            blocking_rows.add(row_index)
                            if first_blocking is None:
                                first_blocking = finding
                            first_blocking_by_row.setdefault(row_index, finding)
                        else:
                            warning_count += 1
                        if len(findings) < finding_limit:
                            findings.append(finding)
                        elif finding is first_blocking:
                            findings[-1] = finding
    return {
        "rows_scanned": row_count,
        "passed": blocking_count == 0,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "findings": findings,
        "first_blocking": first_blocking,
        "blocking_rows": sorted(blocking_rows),
        "first_blocking_by_row": first_blocking_by_row,
    }


def partition_redaction_rows(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Keep safe rows flowing while isolating rows blocked by redaction checks.

    Excluded rows intentionally retain only non-sensitive metadata. Their text,
    links, and model payloads are never copied into review workbooks or audit
    records.
    """
    source_rows = list(rows)
    report = scan_redaction_rows(source_rows)
    blocking_rows = set(report["blocking_rows"])
    first_blocking_by_row = report["first_blocking_by_row"]
    safe_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(source_rows, start=1):
        if row_index not in blocking_rows:
            safe_rows.append(row)
            continue
        finding = first_blocking_by_row[row_index]
        excluded = {
            field: row.get(field)
            for field in _REDACTION_METADATA_FIELDS
            if field in row
        }
        excluded["排除原因"] = (
            "未通过脱敏校验："
            f"命中{finding['type']}（字段：{finding['field']}）"
        )
        excluded["脱敏状态"] = "未通过"
        excluded_rows.append(excluded)
    report = dict(report)
    report["safe_rows"] = len(safe_rows)
    report["skipped_rows"] = len(excluded_rows)
    return safe_rows, excluded_rows, report


def enforce_redaction(rows: list[dict[str, Any]]) -> dict[str, Any]:
    report = scan_redaction_rows(rows)
    enforce = os.getenv("ANSWER_HUB_REDACTION_ENFORCE", "true").strip().lower()
    if report["blocking_count"] and enforce not in {"0", "false", "no", "off"}:
        first = report["first_blocking"]
        if first is None:
            raise RuntimeError("脱敏扫描结果缺少阻断项详情。")
        raise RedactionRiskError(
            "输入数据疑似包含未脱敏敏感信息："
            f"第{first['row']}行“{first['field']}”命中{first['type']}。"
            "请完成脱敏后重试。"
        )
    return report


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    start = parse_iso_datetime(started_at)
    finish = parse_iso_datetime(finished_at)
    if not start or not finish:
        return None
    return max(0.0, round((finish - start).total_seconds(), 3))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def evaluate_run_sla(
    manifest: dict[str, Any],
    policy: OperationsPolicy | None = None,
) -> dict[str, Any]:
    active_policy = policy or OperationsPolicy.from_env()
    summary = manifest.get("summary") or {}
    total_rows = int(summary.get("source_total_rows") or summary.get("total_rows") or 0)
    elapsed = float(manifest.get("duration_seconds") or 0.0)
    normalized_seconds = (
        elapsed * 100 / total_rows
        if elapsed and total_rows
        else None
    )
    feature_rows = int(summary.get("eligible_rows") or 0)
    fallback_rows = int(summary.get("topic_signal_fallback_rows") or 0)
    fallback_rate = fallback_rows / feature_rows if feature_rows else 0.0
    failed_calls = int(summary.get("model_failed_calls") or 0)
    total_calls = int(summary.get("model_calls") or 0)
    failure_rate = failed_calls / total_calls if total_calls else 0.0
    estimated_cost = float(summary.get("model_estimated_cost") or 0.0)
    breaches: list[str] = []
    if (
        normalized_seconds is not None
        and normalized_seconds > active_policy.max_seconds_per_100_rows
    ):
        breaches.append("处理时长超过每100条SLA")
    if failure_rate > active_policy.max_failure_rate:
        breaches.append("模型调用失败率超过SLA")
    if fallback_rate > active_policy.max_fallback_rate:
        breaches.append("规则降级率超过SLA")
    if estimated_cost > active_policy.max_run_cost:
        breaches.append("单次运行估算成本超过预算")
    return {
        "passed": not breaches,
        "breaches": breaches,
        "seconds_per_100_rows": (
            round(normalized_seconds, 3)
            if normalized_seconds is not None
            else None
        ),
        "failure_rate": round(failure_rate, 6),
        "fallback_rate": round(fallback_rate, 6),
        "estimated_cost": round(estimated_cost, 6),
        "policy": asdict(active_policy),
    }


def build_operations_snapshot(
    manifests: Iterable[dict[str, Any]],
    policy: OperationsPolicy | None = None,
) -> dict[str, Any]:
    runs = list(manifests)
    completed = [run for run in runs if run.get("status") == "review_pending"]
    failed = [run for run in runs if run.get("status") == "failed"]
    durations = [
        float(run.get("duration_seconds") or 0.0)
        for run in completed
        if float(run.get("duration_seconds") or 0.0) > 0
    ]
    total_rows = sum(
        int((run.get("summary") or {}).get("source_total_rows") or 0)
        for run in completed
    )
    total_cost = sum(
        float((run.get("summary") or {}).get("model_estimated_cost") or 0.0)
        for run in runs
    )
    model_calls = sum(
        int((run.get("summary") or {}).get("model_calls") or 0)
        for run in runs
    )
    model_failed_calls = sum(
        int((run.get("summary") or {}).get("model_failed_calls") or 0)
        for run in runs
    )
    eligible_rows = sum(
        int((run.get("summary") or {}).get("eligible_rows") or 0)
        for run in runs
    )
    fallback_rows = sum(
        int((run.get("summary") or {}).get("topic_signal_fallback_rows") or 0)
        for run in runs
    )
    sla_results = [evaluate_run_sla(run, policy) for run in completed]
    alerts = [
        {
            "run_id": run.get("run_id", ""),
            "breaches": result["breaches"],
        }
        for run, result in zip(completed, sla_results)
        if not result["passed"]
    ]
    return {
        "total_runs": len(runs),
        "completed_runs": len(completed),
        "failed_runs": len(failed),
        "running_runs": sum(run.get("status") == "running" for run in runs),
        "success_rate": len(completed) / len(runs) if runs else 0.0,
        "average_duration_seconds": mean(durations) if durations else None,
        "p95_duration_seconds": _percentile(durations, 0.95),
        "total_rows": total_rows,
        "average_rows_per_second": (
            total_rows / sum(durations)
            if durations and sum(durations) > 0
            else None
        ),
        "model_calls": model_calls,
        "model_failed_calls": model_failed_calls,
        "model_failure_rate": (
            model_failed_calls / model_calls
            if model_calls
            else 0.0
        ),
        "fallback_rate": fallback_rows / eligible_rows if eligible_rows else 0.0,
        "estimated_cost": round(total_cost, 6),
        "sla_breach_runs": len(alerts),
        "alerts": alerts,
    }


def write_operations_report(
    manifests: Iterable[dict[str, Any]],
    output_path: str | Path,
) -> dict[str, Any]:
    snapshot = build_operations_snapshot(manifests)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot


def plan_retention_cleanup(
    root: str | Path,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> list[Path]:
    base = Path(root).resolve()
    if not base.is_dir():
        return []
    days = retention_days or OperationsPolicy.from_env().retention_days
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=max(1, days))
    candidates: list[Path] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        modified = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
        resolved = child.resolve()
        if resolved.parent != base:
            continue
        if modified < cutoff:
            candidates.append(resolved)
    return sorted(candidates)


def apply_retention_cleanup(
    root: str | Path,
    *,
    retention_days: int | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    base = Path(root).resolve()
    candidates = plan_retention_cleanup(
        base,
        retention_days=retention_days,
    )
    deleted: list[str] = []
    if execute:
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.parent != base:
                raise ValueError(f"拒绝清理越界目录：{resolved}")
            shutil.rmtree(resolved)
            deleted.append(str(resolved))
    return {
        "root": str(base),
        "execute": execute,
        "retention_days": (
            retention_days or OperationsPolicy.from_env().retention_days
        ),
        "candidate_count": len(candidates),
        "candidates": [str(path) for path in candidates],
        "deleted": deleted,
    }
