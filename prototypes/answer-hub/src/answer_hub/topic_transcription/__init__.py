"""Deep internal seam for topic-level transcription quality checks.

The public workflow still owns workbook orchestration.  This package keeps
fact/claim auditing and high-risk logic checks in one small, testable module
so callers do not need to know how the checks are implemented.
"""

from .contracts import TopicDraftAudit, TopicEvidence, TopicFact
from .service import audit_topic_draft

__all__ = [
    "TopicDraftAudit",
    "TopicEvidence",
    "TopicFact",
    "audit_topic_draft",
]
