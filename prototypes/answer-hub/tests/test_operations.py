from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os

import pytest

from answer_hub.operations import (
    OperationsPolicy,
    RedactionRiskError,
    apply_retention_cleanup,
    build_operations_snapshot,
    enforce_redaction,
    evaluate_run_sla,
    scan_redaction_rows,
)


def test_redaction_scan_blocks_sensitive_values_without_echoing_them() -> None:
    rows = [
        {
            "聊天内容": "请联系 13812345678，邮箱 user@example.com。",
            "核心问题": "测试",
        }
    ]
    report = scan_redaction_rows(rows)

    assert report["passed"] is False
    assert report["blocking_count"] == 2
    samples = " ".join(item["sample"] for item in report["findings"])
    assert "13812345678" not in samples
    assert "user@example.com" not in samples


def test_redaction_enforcement_can_be_disabled_for_controlled_migration(
    monkeypatch,
) -> None:
    rows = [{"聊天内容": "手机号 13812345678"}]
    monkeypatch.setenv("ANSWER_HUB_REDACTION_ENFORCE", "false")

    report = enforce_redaction(rows)

    assert report["blocking_count"] == 1


def test_redaction_enforcement_rejects_unredacted_input(monkeypatch) -> None:
    rows = [{"聊天内容": "手机号 13812345678"}]
    monkeypatch.setenv("ANSWER_HUB_REDACTION_ENFORCE", "true")

    with pytest.raises(RedactionRiskError):
        enforce_redaction(rows)


def test_operations_snapshot_and_sla_report_runtime_risks() -> None:
    run = {
        "run_id": "run-1",
        "status": "review_pending",
        "duration_seconds": 1200,
        "summary": {
            "source_total_rows": 100,
            "eligible_rows": 100,
            "topic_signal_fallback_rows": 30,
            "model_calls": 10,
            "model_failed_calls": 1,
            "model_estimated_cost": 12.5,
        },
    }
    policy = OperationsPolicy(
        max_seconds_per_100_rows=900,
        max_failure_rate=0.05,
        max_fallback_rate=0.20,
        max_run_cost=10,
        retention_days=30,
    )

    sla = evaluate_run_sla(run, policy)
    snapshot = build_operations_snapshot([run], policy)

    assert sla["passed"] is False
    assert len(sla["breaches"]) == 4
    assert snapshot["sla_breach_runs"] == 1
    assert snapshot["model_failure_rate"] == pytest.approx(0.1)
    assert snapshot["fallback_rate"] == pytest.approx(0.3)


def test_retention_cleanup_is_dry_run_by_default(tmp_path: Path) -> None:
    old_run = tmp_path / "old-run"
    recent_run = tmp_path / "recent-run"
    old_run.mkdir()
    recent_run.mkdir()
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(days=60)
    ).timestamp()
    os.utime(old_run, (old_timestamp, old_timestamp))

    preview = apply_retention_cleanup(
        tmp_path,
        retention_days=30,
        execute=False,
    )

    assert preview["candidate_count"] == 1
    assert old_run.is_dir()
    assert recent_run.is_dir()

    executed = apply_retention_cleanup(
        tmp_path,
        retention_days=30,
        execute=True,
    )
    assert executed["deleted"] == [str(old_run.resolve())]
    assert not old_run.exists()
    assert recent_run.exists()
