from __future__ import annotations

import tempfile
from pathlib import Path

from answer_hub.confidence_training_local import LocalConfidenceTrainingStore


def test_local_confidence_store_keeps_human_truth_and_jobs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = LocalConfidenceTrainingStore(Path(directory) / "confidence.db")
        assert store.upsert([{"id": "candidate-1", "title": "测试候选"}]) == 1
        assert store.label("candidate-1", "worthy")["human_label"] == "worthy"
        store.save_model("candidate-1", {"knowledge_value": "worthy", "confidence": 0.9})
        job_id = store.create_job(["candidate-1"])
        store.update_job(job_id, "completed", "影子评测候选已保存", {"completed": 1})
        item = store.items()[0]
        assert item["model_result"]["confidence"] == 0.9
        assert store.jobs()[0]["status"] == "completed"


def test_local_confidence_store_rejects_unknown_human_truth() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = LocalConfidenceTrainingStore(Path(directory) / "confidence.db")
        store.upsert([{"id": "candidate-1"}])
        try:
            store.label("candidate-1", "pass")
        except ValueError as exc:
            assert "human_label" in str(exc)
        else:
            raise AssertionError("unknown label was accepted")
