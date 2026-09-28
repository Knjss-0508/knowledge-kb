from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class TopicFact:
    """A normalized, auditable source fact used by the transcription seam."""

    fact_id: str
    source_record_id: str
    text: str
    fields: Mapping[str, str] = field(default_factory=dict)
    # Optional structured slots keep the seam compatible with the existing
    # text-first evidence package while giving later gates explicit anchors.
    object: str = ""
    condition: str = ""
    observation: str = ""
    action: str = ""
    result: str = ""
    boundary: str = ""
    confidence: float | None = None
    fact_type: str = ""
    image_state: str = ""


@dataclass(frozen=True)
class TopicEvidence:
    """Evidence package exposed to the quality gate, not to workbook callers."""

    facts: tuple[TopicFact, ...]
    representative_fact_ids: tuple[str, ...] = ()

    @property
    def source_text(self) -> str:
        return "\n".join(fact.text for fact in self.facts if fact.text)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                fact.source_record_id
                for fact in self.facts
                if fact.source_record_id
            )
        )


@dataclass(frozen=True)
class TopicDraftAudit:
    """Result of deterministic fact and logic validation for one draft."""

    status: str
    issues: tuple[str, ...] = ()
    claim_fact_refs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unsupported_claims: tuple[str, ...] = ()
    logic_conflicts: tuple[str, ...] = ()
    scope_expansion: tuple[str, ...] = ()
    historical_standard_reference_status: str = "无历史标准关联"

    @property
    def blocking(self) -> bool:
        return bool(self.issues)

    @property
    def retryable(self) -> bool:
        return bool(
            self.logic_conflicts
            or self.unsupported_claims
            or self.scope_expansion
        )

    @property
    def retry_reason(self) -> str:
        reasons = [*self.logic_conflicts, *self.unsupported_claims]
        if self.scope_expansion:
            reasons.extend(self.scope_expansion)
        return "；".join(dict.fromkeys(reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": list(self.issues),
            "claim_fact_refs": {
                claim: list(refs)
                for claim, refs in self.claim_fact_refs.items()
            },
            "unsupported_claims": list(self.unsupported_claims),
            "logic_conflicts": list(self.logic_conflicts),
            "scope_expansion": list(self.scope_expansion),
            "historical_standard_reference_status": (
                self.historical_standard_reference_status
            ),
        }
