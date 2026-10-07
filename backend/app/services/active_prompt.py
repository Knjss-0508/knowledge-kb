from __future__ import annotations

from app.core.database import SessionLocal
from app.models.integration import ConfidenceTrainingJob


DEFAULT_PROMPT_VERSION = "post-transcription-value-and-content-review-v10"
DEFAULT_PROMPT = """你是知识转写草稿审核标注器。必须先阅读主题、来源证据、知识草稿和推荐回复，再判断是否值得沉淀；不能只凭标题或聚类标签判断。"""


def get_active_confidence_prompt() -> tuple[str, str]:
    db = SessionLocal()
    try:
        jobs = db.query(ConfidenceTrainingJob).order_by(ConfidenceTrainingJob.updated_at.desc()).all()
        for job in jobs:
            for version in reversed(list(job.prompt_versions or [])):
                if version.get("status") == "active" and version.get("prompt"):
                    return str(version["prompt"]), str(version.get("version") or "active")
    finally:
        db.close()
    return DEFAULT_PROMPT, DEFAULT_PROMPT_VERSION
