from __future__ import annotations

from datetime import datetime

from app.core.database import SessionLocal
from app.models.integration import ConfidenceTrainingJob


DEFAULT_PROMPT_VERSION = "post-transcription-value-and-content-review-v11"
DEFAULT_PROMPT = """你是知识转写草稿审核标注器。必须先阅读主题、来源证据、知识草稿和推荐回复，再判断是否值得沉淀；不能只凭标题或聚类标签判断。

只返回 JSON 对象，字段包括：knowledge_value、draft_disposition、reason、evidence_used、limitations、confidence。
knowledge_value 只能是 worthy、unworthy、pending。
draft_disposition 只能是 approved、revision_required、hold_for_evidence、not_applicable。
若 knowledge_value=unworthy，draft_disposition 必须为 not_applicable；若 knowledge_value=pending，draft_disposition 必须为 hold_for_evidence；approved 或 revision_required 只能对应 worthy。"""


def _as_naive_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _approved_review_prompts(db) -> list[tuple[datetime, str, str]]:
    jobs = db.query(ConfidenceTrainingJob).order_by(
        ConfidenceTrainingJob.created_at.desc()
    ).all()
    approved: list[tuple[datetime, str, str]] = []
    for job in jobs:
        if str((job.analysis_result or {}).get("optimization_target") or "review") != "review":
            continue
        for version in job.prompt_versions or []:
            if version.get("status") not in {
                "approved_for_future_candidates",
                "active",
            }:
                continue
            approved_at = _as_naive_datetime(
                version.get("approved_at")
                or version.get("decided_at")
                or version.get("created_at")
            )
            prompt = str(version.get("prompt") or "").strip()
            if approved_at and prompt:
                approved.append(
                    (
                        approved_at,
                        prompt,
                        str(version.get("version") or "approved-active"),
                    )
                )
    return approved


def get_active_confidence_prompt(*, db=None) -> tuple[str, str]:
    """Return the newest human-approved review Prompt for newly created work."""
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        approved = _approved_review_prompts(db)
        if approved:
            _, prompt, version = max(approved, key=lambda item: item[0])
            return prompt, version
    finally:
        if owns_session:
            db.close()
    return DEFAULT_PROMPT, DEFAULT_PROMPT_VERSION


def get_confidence_prompt_for_candidate(
    candidate_created_at: datetime | None,
    *,
    db=None,
) -> tuple[str, str]:
    """Use an approved Prompt only for candidates created after its approval."""
    created_at = _as_naive_datetime(candidate_created_at)
    if created_at is None:
        return get_active_confidence_prompt(db=db)

    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        eligible = [
            item
            for item in _approved_review_prompts(db)
            if created_at >= item[0]
        ]
        if eligible:
            _, prompt, version = max(eligible, key=lambda item: item[0])
            return prompt, version or "approved-for-future-candidates"
    finally:
        if owns_session:
            db.close()
    return DEFAULT_PROMPT, DEFAULT_PROMPT_VERSION
