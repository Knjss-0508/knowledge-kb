from __future__ import annotations

import math
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.integration import ConfidenceTrainingSettingsRecord


MODEL_NAME = "deepseek-flash"
EVALUATION_SCOPE = "shadow_only"
CONFIDENCE_BANDS = (
    ("B0", 0.00, 0.70),
    ("B1", 0.70, 0.85),
    ("B2", 0.85, 0.95),
    ("B3", 0.95, 1.00),
)

SETTINGS_RECORD_ID = "confidence-training-global"
DEFAULT_SETTINGS = {
    "training_candidate_collection_enabled": True,
    "training_snapshot_enabled": False,
    # A DeepSeek inference endpoint is not a model-training platform.
    # Keep the legacy field false for API compatibility, but never make it
    # user-switchable until the company training-service contract exists.
    "auto_training_job_enabled": False,
    # This is deliberately not user-switchable in the preview implementation.
    "production_auto_review_enabled": False,
}


def confidence_band(value: Any) -> str | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= score <= 1:
        return None
    for name, lower, upper in CONFIDENCE_BANDS:
        if (lower <= score < upper) or (name == "B3" and score == 1.0):
            return name
    return None


def confidence_band_label(name: str) -> str:
    for band, lower, upper in CONFIDENCE_BANDS:
        if band == name:
            right = "]" if band == "B3" else ")"
            return f"{band} [{lower:.2f}, {upper:.2f}{right}"
    return name


def _review_metadata(item) -> dict[str, Any]:
    metadata = dict(item.review_metadata or {})
    value = metadata.get("confidence_training")
    return dict(value) if isinstance(value, dict) else {}


def _model_review(item) -> dict[str, Any]:
    payload = dict(item.candidate_payload or {})
    model = payload.get("model_review")
    if isinstance(model, dict) and model:
        return model
    metadata = dict(item.review_metadata or {})
    model = metadata.get("model_review")
    return dict(model) if isinstance(model, dict) else {}


def _human_review(item) -> dict[str, Any]:
    payload = dict(item.candidate_payload or {})
    human = payload.get("human_review")
    if isinstance(human, dict) and human:
        return human
    metadata = dict(item.review_metadata or {})
    human = metadata.get("human_review")
    return dict(human) if isinstance(human, dict) else {}


def model_label(item) -> str | None:
    model = _model_review(item)
    value = model.get("knowledge_value") or model.get("decision")
    return str(value).strip() or None


def human_label(item) -> str | None:
    value = _human_review(item).get("knowledge_value")
    value = str(value).strip()
    return value if value in {"worthy", "unworthy"} else None


def human_draft_disposition(item) -> str | None:
    human = _human_review(item)
    if human_label(item) == "unworthy":
        return "not_applicable"
    value = str(human.get("draft_disposition") or "").strip()
    return value if value in {"approved", "revision_required", "hold_for_evidence"} else None


def model_draft_disposition(item) -> str | None:
    model = _model_review(item)
    action = str(model.get("suggested_action") or "").strip()
    if action == "submit_for_human_review":
        return "approved"
    if action == "return_for_revision":
        return "revision_required"
    if action == "hold_for_evidence":
        return "hold_for_evidence"
    quality = str(model.get("draft_quality") or "").strip()
    return {
        "合格": "approved",
        "需修改": "revision_required",
        "待补证据": "hold_for_evidence",
    }.get(quality)


def _derived_evaluation(item) -> dict[str, Any]:
    """Compare the post-transcription model result with the one human review.

    Human reviewers never reconfirm a value in this module.  The comparison is
    derived from their candidate-review conclusion and draft disposition.
    """
    truth = human_label(item)
    human_draft = human_draft_disposition(item)
    model = model_label(item)
    model_draft = model_draft_disposition(item)
    if not truth or (truth == "worthy" and not human_draft) or not model:
        return {"model_correctness": "not_evaluable", "strict_correct": None, "acceptable_correct": None}
    same_value = truth == model
    same_draft = truth == "unworthy" or model_draft is None or model_draft == human_draft
    strict = same_value and same_draft
    return {
        "model_correctness": "strict_correct" if strict else "wrong",
        "strict_correct": strict,
        "acceptable_correct": same_value,
    }


def item_payload(item) -> dict[str, Any]:
    model = _model_review(item)
    human = _human_review(item)
    evaluation = _review_metadata(item)
    raw_confidence = model.get("confidence")
    band = evaluation.get("confidence_band") or confidence_band(raw_confidence)
    label = model_label(item)
    truth = human_label(item)
    human_draft = human_draft_disposition(item)
    model_draft = model_draft_disposition(item)
    derived = _derived_evaluation(item)
    confirmed = bool(truth) and (truth == "unworthy" or bool(human_draft))
    training_recommended = (
        "recommended"
        if str(human.get("training_eligible") or "").strip() == "是" and confirmed
        else "not_recommended"
    )
    return {
        "id": item.id,
        "event_id": item.event_id,
        "title": str((item.candidate_payload or {}).get("knowledge", {}).get("title") or ""),
        "model_label": label,
        "model_confidence": raw_confidence,
        "confidence_band": band,
        "model_version": model.get("model_name") or model.get("provider") or MODEL_NAME,
        "prompt_version": model.get("prompt_version"),
        # The candidate review is the sole source of truth. The legacy
        # confidence-training label is only retained for audit compatibility.
        "human_label": truth,
        "human_draft_disposition": human_draft,
        "model_draft_disposition": model_draft,
        "truth_status": ("confirmed" if confirmed else "pending"),
        "model_correctness": evaluation.get("model_correctness") or derived["model_correctness"],
        "strict_correct": evaluation.get("strict_correct", derived["strict_correct"]),
        "acceptable_correct": evaluation.get("acceptable_correct", derived["acceptable_correct"]),
        "error_type": list(evaluation.get("error_type") or []),
        "error_severity": evaluation.get("error_severity"),
        "review_note": str(evaluation.get("review_note") or ""),
        "correction_status": evaluation.get("correction_status") or "not_started",
        "correction_round": int(evaluation.get("correction_round") or 0),
        "corrected_model_review": evaluation.get("corrected_model_review"),
        "training_candidate_status": evaluation.get("training_candidate_status") or training_recommended,
        "dataset_split": evaluation.get("dataset_split"),
        "review_status": item.review_status or "pending",
    }


def _wilson_lower(correct: int, total: int) -> float | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = correct / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return round(max(0.0, (centre - spread) / denominator), 4)


def _accuracy_group(items: list[dict[str, Any]], *, split: str, band: str) -> dict[str, Any]:
    group = [
        item for item in items
        if (item.get("dataset_split") or "unassigned") == split
        and item.get("confidence_band") == band
    ]
    valid = [item for item in group if item.get("truth_status") == "confirmed"]
    strict = [item for item in valid if item.get("strict_correct") is True]
    acceptable = [item for item in valid if item.get("acceptable_correct") is True]
    return {
        "dataset_split": split,
        "split_label": {"train": "问题发现集", "validation": "验证集", "test": "测试集", "unassigned": "未分区"}.get(split, split),
        "band": band,
        "label": confidence_band_label(band),
        "n_scored": len(group),
        "n_valid_truth": len(valid),
        "strict_correct_count": len(strict),
        "strict_accuracy": round(len(strict) / len(valid), 4) if valid else None,
        "acceptable_accuracy": round(len(acceptable) / len(valid), 4) if valid else None,
        "accuracy_ci95_lower": _wilson_lower(len(strict), len(valid)),
        "coverage": round(len(valid) / len(group), 4) if group else 0,
        "not_evaluable_rate": round(sum(item.get("truth_status") != "confirmed" for item in group) / len(group), 4) if group else 0,
        "fatal_error_rate": round(sum(item.get("error_severity") == "fatal" for item in valid) / len(valid), 4) if valid else 0,
        "major_error_rate": round(sum(item.get("error_severity") == "major" for item in valid) / len(valid), 4) if valid else 0,
        "sample_warning": "样本不足，仅供观察" if 0 < len(valid) < 10 else ("暂无样本" if not group else ""),
    }


def aggregate(items: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scored = [item for item in items if item.get("confidence_band")]
    valid = [item for item in scored if item.get("truth_status") == "confirmed"]
    strict = [item for item in valid if item.get("strict_correct") is True]
    acceptable = [item for item in valid if item.get("acceptable_correct") is True]
    fatal = [item for item in valid if item.get("error_severity") == "fatal"]
    bands = []
    for band, lower, upper in CONFIDENCE_BANDS:
        group = [item for item in scored if item.get("confidence_band") == band]
        group_valid = [item for item in group if item.get("truth_status") == "confirmed"]
        group_strict = [item for item in group_valid if item.get("strict_correct") is True]
        group_acceptable = [item for item in group_valid if item.get("acceptable_correct") is True]
        bands.append({
            "band": band,
            "label": confidence_band_label(band),
            "n_scored": len(group),
            "n_valid_truth": len(group_valid),
            "strict_correct_count": len(group_strict),
            "strict_accuracy": round(len(group_strict) / len(group_valid), 4) if group_valid else None,
            "acceptable_accuracy": round(len(group_acceptable) / len(group_valid), 4) if group_valid else None,
            "accuracy_ci95_lower": _wilson_lower(len(group_strict), len(group_valid)),
            "coverage": round(len(group_valid) / len(group), 4) if group else 0,
            "not_evaluable_rate": round(sum(i.get("truth_status") != "confirmed" for i in group) / len(group), 4) if group else 0,
            "fatal_error_rate": round(sum(i.get("error_severity") == "fatal" for i in group_valid) / len(group_valid), 4) if group_valid else 0,
            "major_error_rate": round(sum(i.get("error_severity") == "major" for i in group_valid) / len(group_valid), 4) if group_valid else 0,
            "recommended_action": "全检（影子评测不放行）",
        })
    split_names = ("train", "validation", "test", "unassigned")
    partitioned_bands = [
        _accuracy_group(items, split=split, band=band)
        for split in split_names
        for band, _, _ in CONFIDENCE_BANDS
    ]
    split_summary = []
    for split in split_names:
        split_items = [item for item in scored if (item.get("dataset_split") or "unassigned") == split]
        split_valid = [item for item in split_items if item.get("truth_status") == "confirmed"]
        split_strict = [item for item in split_valid if item.get("strict_correct") is True]
        split_acceptable = [item for item in split_valid if item.get("acceptable_correct") is True]
        split_summary.append({
            "dataset_split": split,
            "split_label": {"train": "问题发现集", "validation": "验证集", "test": "测试集", "unassigned": "未分区"}[split],
            "sample_count": len(split_items),
            "n_valid_truth": len(split_valid),
            "strict_accuracy": round(len(split_strict) / len(split_valid), 4) if split_valid else None,
            "acceptable_accuracy": round(len(split_acceptable) / len(split_valid), 4) if split_valid else None,
            "accuracy_ci95_lower": _wilson_lower(len(split_strict), len(split_valid)),
            "sample_warning": "样本不足，仅供观察" if 0 < len(split_valid) < 10 else ("暂无样本" if not split_items else ""),
        })
    overview = {
        "total": len(items),
        "scored": len(scored),
        "annotated": sum(bool(item.get("human_label")) for item in items),
        "valid_truth": len(valid),
        "pending": len(items) - len(valid),
        "strict_accuracy": round(len(strict) / len(valid), 4) if valid else None,
        "acceptable_accuracy": round(len(acceptable) / len(valid), 4) if valid else None,
        "coverage": round(len(valid) / len(scored), 4) if scored else 0,
        "not_evaluable_rate": round((len(scored) - len(valid)) / len(scored), 4) if scored else 0,
        "fatal_error_rate": round(len(fatal) / len(valid), 4) if valid else 0,
        "training_candidate_count": sum(item.get("training_candidate_status") in {"recommended", "qualified"} for item in items),
        "model": MODEL_NAME,
        "evaluation_scope": EVALUATION_SCOPE,
        "production_auto_review_enabled": False,
        "split_summary": split_summary,
        "partitioned_bands": partitioned_bands,
    }
    return overview, bands


def _stored_settings(db: Session | None) -> dict[str, Any]:
    if db is None:
        return {}
    try:
        record = db.query(ConfidenceTrainingSettingsRecord).filter(
            ConfidenceTrainingSettingsRecord.id == SETTINGS_RECORD_ID
        ).first()
    except SQLAlchemyError:
        # Rolling deployments may briefly use the pre-migration schema. The
        # default stays fail-closed and the caller can retry after migration.
        db.rollback()
        return {}
    return dict(record.settings or {}) if record else {}


def settings_snapshot(db: Session | None = None) -> dict[str, Any]:
    stored = _stored_settings(db)
    return {
        **DEFAULT_SETTINGS,
        **{
            "training_candidate_collection_enabled": bool(
                stored.get("training_candidate_collection_enabled", DEFAULT_SETTINGS["training_candidate_collection_enabled"])
            ),
            "training_snapshot_enabled": bool(
                stored.get("training_snapshot_enabled", DEFAULT_SETTINGS["training_snapshot_enabled"])
            ),
        },
        "auto_training_job_enabled": False,
        "production_auto_review_enabled": False,
        "training_task_available": False,
        "training_task_reason": "尚未接入公司内部训练平台；DeepSeek-flash 仅用于标注和纠错，不会自动训练权重。",
        "model_name": MODEL_NAME,
        "evaluation_scope": EVALUATION_SCOPE,
    }


def update_settings(
    db: Session,
    values: dict[str, Any],
    *,
    updated_by: str,
) -> dict[str, Any]:
    requested = {
        "training_candidate_collection_enabled": bool(
            values.get("training_candidate_collection_enabled", True)
        ),
        "training_snapshot_enabled": bool(values.get("training_snapshot_enabled", False)),
        # Never accept a request to enable production automation or a training
        # job that has no backing service.
        "auto_training_job_enabled": False,
        "production_auto_review_enabled": False,
    }
    record = db.query(ConfidenceTrainingSettingsRecord).filter(
        ConfidenceTrainingSettingsRecord.id == SETTINGS_RECORD_ID
    ).first()
    if record is None:
        record = ConfidenceTrainingSettingsRecord(
            id=SETTINGS_RECORD_ID,
            settings=requested,
            updated_by=updated_by,
        )
        db.add(record)
    else:
        record.settings = requested
        record.updated_by = updated_by
    db.commit()
    db.refresh(record)
    return settings_snapshot(db)


def update_item(item, values: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(item.review_metadata or {})
    evaluation = _review_metadata(item)
    values = dict(values)
    # Human truth is written only by candidate value review. Ignore legacy
    # form fields here so the evaluation page cannot create a second truth.
    values.pop("human_label", None)
    values.pop("truth_status", None)
    values.pop("truth_reviewer", None)
    values.pop("truth_reviewed_at", None)
    evaluation.update(values)
    evaluation["confidence_band"] = evaluation.get("confidence_band") or confidence_band(_model_review(item).get("confidence"))
    derived = _derived_evaluation(item)
    if evaluation.get("model_correctness") in (None, "not_evaluable"):
        evaluation.update(derived)
    truth = human_label(item)
    evaluation["truth_status"] = "confirmed" if truth and (truth == "unworthy" or human_draft_disposition(item)) else "pending"
    metadata["confidence_training"] = evaluation
    item.review_metadata = metadata
    return item_payload(item)
