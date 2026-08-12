from pathlib import Path

import pytest

from answer_hub.run_feedback import RunFeedbackStore, RunFeedbackStoreError


def test_run_feedback_persists_assignment_status_and_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run_feedback.db"
    store = RunFeedbackStore(path)

    saved = store.update(
        "job-001",
        status="in_progress",
        owner="小张",
        cause_type="clustering",
        note="已确认卡在主题聚类，准备从检查点恢复。",
        actor="管理员",
    )

    reloaded = RunFeedbackStore(path).get("job-001")
    assert saved["status_label"] == "处理中"
    assert reloaded is not None
    assert reloaded["owner"] == "小张"
    assert reloaded["cause_type"] == "clustering"
    assert reloaded["note"] == "已确认卡在主题聚类，准备从检查点恢复。"
    assert reloaded["history"] == [
        {
            "status": "in_progress",
            "status_label": "处理中",
            "owner": "小张",
            "cause_type": "clustering",
            "note": "已确认卡在主题聚类，准备从检查点恢复。",
            "actor": "管理员",
            "updated_at": saved["updated_at"],
        }
    ]


def test_run_feedback_refuses_to_overwrite_corrupt_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run_feedback.db"
    corrupt_payload = b"not-a-valid-sqlite-database"
    path.write_bytes(corrupt_payload)

    with pytest.raises(RunFeedbackStoreError, match="无法读取监管反馈存储"):
        RunFeedbackStore(path).update(
            "job-001",
            status="acknowledged",
            note="确认处理中。",
        )

    assert path.read_bytes() == corrupt_payload
