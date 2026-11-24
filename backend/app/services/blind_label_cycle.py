"""Offline feedback cycle: bounded discovery, reviewed replay and candidate reranking."""

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.routes import integration
from app.services.blind_label_answer_replay import AnswerReplayManifest, run_answer_replay
from app.services.blind_label_learning import (
    LearningReviewError,
    load_learning_review,
    preview_learning_samples,
)
from app.services.blind_label_replay import ReplayManifest
from app.services.blind_labeling import (
    BLIND_LABEL_NOT_REFERABLE_REASON_CODES,
    BLIND_LABEL_REFERABLE_REASON_CODES,
    BLIND_LABEL_TASK_REASON_CODES,
)
from app.services.embedding import EmbeddingServiceUnavailable


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue: Literal["ranking", "retrieval_missed", "answer_issue", "knowledge_issue", "knowledge_gap", "insufficient_evidence"]


# A production policy would additionally require an independent end-to-end evaluation.
MIN_TRAIN_CASES = 10
MIN_HOLDOUT_CASES = 10
MIN_SPLIT_TARGETS = 3
_TRACKS = {"ranking_review", "recall_review", "knowledge_review", "knowledge_gap_review", "evidence_review"}


def diagnosis_evidence(item: dict) -> dict:
    """Use an explicit allowlist, not a redacted copy of a database row."""
    def count(value) -> int:
        return value if type(value) is int and 0 <= value <= 500 else 0

    def reason_counts(values, allowed) -> dict:
        return {key: count(values[key]) for key in sorted(allowed) if key in values}

    return {
        "completed": count(item["completed_annotators"]),
        "required": count(item["required_annotators"]),
        "referable": count(item.get("referable_count")),
        "not_referable": count(item.get("not_referable_count")),
        "track": item["suggested_track"] if item["suggested_track"] in _TRACKS else "evidence_review",
        "task_reasons": reason_counts(item["task_reason_counts"], BLIND_LABEL_TASK_REASON_CODES),
        "candidates": [
            {
                "rank": count(candidate["rank"]),
                "referable": count(candidate["referable_count"]),
                "not_referable": count(candidate["not_referable_count"]),
                "reasons": reason_counts(
                    candidate["reason_counts"],
                    BLIND_LABEL_REFERABLE_REASON_CODES | BLIND_LABEL_NOT_REFERABLE_REASON_CODES,
                ),
            }
            for candidate in item["candidates"][:3]
        ],
    }


def _discover(db, *, scan_limit, cursor, category_id, diagnose, ai_limit) -> tuple[dict, list[dict]]:
    page = preview_learning_samples(db, scan_limit=scan_limit, cursor=cursor, category_id=category_id)
    suggestions, drafts = [], []
    attempted = 0
    for item in page["items"]:
        order = item["work_order_id"]
        version = item["evidence_version"]
        state = item["review_status"]
        entry = {
            "work_order_id": order, "evidence_version": version,
            "review_status": state, "rule_track": item["suggested_track"],
            "ai_status": "not_requested", "ai_issue": None,
        }
        suggestions.append(entry)
        if diagnose and state in {"pending_review", "stale"} and attempted < ai_limit:
            attempted += 1
            diagnosis = diagnose(diagnosis_evidence(item))
            entry.update(ai_status="suggested" if diagnosis else "unavailable",
                         ai_issue=diagnosis.issue if diagnosis else None)

        review = item["review"] or {}
        issue = review.get("issue_type")
        if state == "confirmed" and issue in {"knowledge_issue", "knowledge_gap"}:
            # Stable per reviewed version, suitable for deduplication in a future review inbox.
            key = hashlib.sha256(f"{order}\0{review['id']}\0{version}".encode()).hexdigest()
            drafts.append({
                "draft_id": key, "state": "needs_editor", "work_order_id": order,
                "review_id": review["id"], "evidence_version": version, "issue": issue,
                "action": "review_existing_knowledge" if issue == "knowledge_issue" else "collect_missing_knowledge",
                # The review has not attested to the precise affected article or new facts.
                "knowledge_id": None, "proposed_content": None,
            })
    return {
        "scanned": page["scanned"], "next_cursor": page["next_cursor"],
        "ai_attempted": attempted, "ai_suggested": sum(row["ai_status"] == "suggested" for row in suggestions),
        "cases": suggestions,
    }, drafts


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


def _bigrams(value: str) -> set[str]:
    cleaned = re.sub(r"[\W_]", "", _normalize(value))
    return {cleaned[i:i+2] for i in range(len(cleaned) - 1)}


@dataclass(frozen=True)
class RankedCandidate:
    knowledge_id: str
    similarity: float
    title_overlap: float


@dataclass(frozen=True)
class EvaluatedCase:
    work_order_id: str
    query_key: str
    target: str
    returned: tuple[RankedCandidate, ...]  # same origin, scope and threshold as baseline
    explicit_negatives: frozenset[str]

    @property
    def baseline_position(self) -> int | None:
        return next((index for index, row in enumerate(self.returned, 1) if row.knowledge_id == self.target), None)


def _collect_retrieval(db: Session, manifest: ReplayManifest | None) -> tuple[dict, list[EvaluatedCase]]:
    if manifest is None:
        return {"configured": False, "cases": [], "summary": {"requested": 0, "evaluated": 0}}, []
    config = None
    rows, cases = [], []
    for case in manifest.cases:
        row = {"work_order_id": case.work_order_id, "review_id": case.review_id}
        rows.append(row)
        try:
            detail = load_learning_review(db, case.work_order_id)
        except LearningReviewError as exc:
            if exc.status_code != 409:
                raise
            row["status"] = "review_changed_or_ineligible"
            continue
        review = detail["review"] or {}
        if not detail["replay_eligible"] or review.get("id") != case.review_id:
            row["status"] = "review_changed_or_ineligible"
            continue
        # This trainer never learns retrieval labels from an answer-quality judgment.
        if review["issue_type"] not in {"retrieval_missed", "ranking"}:
            row["status"] = "not_retrieval_training_issue"
            continue
        if config is None:
            config = integration.get_active_runtime_values(db)
        try:
            ranked, returned, threshold = integration.rank_standard_semantic_request(
                db, case.request.to_search_request(), runtime_config=config,
            )
        except EmbeddingServiceUnavailable:
            row["status"] = "embedding_unavailable"
            continue
        target = review["knowledge_id"]
        # If the target was outside the semantic window (or below threshold), reranking
        # cannot recover it. It remains a miss, including in holdout evaluation.
        origin = next((key for key, values in ranked.items() if any(item.id == target for item, _ in values)), None)
        query_tokens = _bigrams(case.request.normalized_question)
        candidates = tuple(
            RankedCandidate(item.id, float(score), len(query_tokens & _bigrams(item.title or "")) / max(1, len(query_tokens)))
            for item, score in returned.get(origin, ())
        )
        if not math.isfinite(threshold) or any(not math.isfinite(item.similarity) for item in candidates):
            row["status"] = "invalid_retrieval_scores"
            continue
        negatives = frozenset(
            item["knowledge_id"] for item in detail["sample"]["candidates"]
            if item["knowledge_id"] and item["knowledge_id"] != target
            and item["verdict"] == "not_referable" and item["knowledge_state"] == "snapshot_matches"
        )
        evaluated = EvaluatedCase(
            case.work_order_id, _normalize(case.request.normalized_question), target, candidates, negatives,
        )
        cases.append(evaluated)
        window_rank = next((i for i, (item, _) in enumerate(ranked.get(origin, ()), 1) if item.id == target), None)
        row.update(status="evaluated", origin=origin, window_rank=window_rank,
                   baseline_position=evaluated.baseline_position, threshold=threshold)
    outcomes = Counter(row["status"] for row in rows)
    return {"configured": True, "cases": rows, "summary": {
        "requested": len(rows), "evaluated": len(cases), "outcomes": dict(outcomes),
        "top3_hits": sum(case.baseline_position is not None and case.baseline_position <= 3 for case in cases),
    }}, cases


def _partition(cases: list[EvaluatedCase]) -> tuple[list[EvaluatedCase], list[EvaluatedCase]]:
    """Keep shared normalized queries or target articles (even transitively) in one split."""
    parent = list(range(len(cases)))
    seen: dict[tuple[str, str], int] = {}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, case in enumerate(cases):
        for key in (("query", case.query_key), ("target", case.target)):
            previous = seen.setdefault(key, i)
            parent[root(i)] = root(previous)
    groups: dict[int, list[EvaluatedCase]] = {}
    for i, case in enumerate(cases):
        groups.setdefault(root(i), []).append(case)
    # Use stable work-order IDs rather than ranking results to decide the holdout.
    buckets = sorted(groups.values(), key=lambda group: hashlib.sha256(
        min(case.work_order_id for case in group).encode()).hexdigest())
    cutoff = max(MIN_HOLDOUT_CASES, math.ceil(len(cases) * 0.3))
    train, holdout = [], []
    for group in buckets:
        (holdout if len(holdout) < cutoff else train).extend(group)
    return train, holdout


def _rank(case: EvaluatedCase, weight: float) -> int | None:
    ranked = sorted(case.returned, key=lambda row: row.similarity + weight * row.title_overlap, reverse=True)
    return next((index for index, row in enumerate(ranked, 1) if row.knowledge_id == case.target), None)


def _train(retrieval: dict, cases: list[EvaluatedCase]) -> tuple[dict, dict | None]:
    if not retrieval["configured"]:
        return {"status": "no_reviewed_manifest"}, None
    if len(cases) != retrieval["summary"]["requested"]:
        return {"status": "validation_incomplete"}, None
    train, holdout = _partition(cases)
    pairs_by_case = []
    for case in train:
        positive = next((row for row in case.returned if row.knowledge_id == case.target), None)
        if positive:
            pairs = [(positive.similarity - row.similarity, positive.title_overlap - row.title_overlap)
                     for row in case.returned if row.knowledge_id in case.explicit_negatives]
            if pairs:
                pairs_by_case.append((case.target, pairs))
    counts = {
        "training_cases": len(train), "holdout_cases": len(holdout), "training_cases_with_pairs": len(pairs_by_case),
        "holdout_target_count": len({case.target for case in holdout}),
        "training_pair_target_count": len({target for target, _ in pairs_by_case}),
    }
    if (counts["training_cases_with_pairs"] < MIN_TRAIN_CASES or len(holdout) < MIN_HOLDOUT_CASES
            or min(counts["training_pair_target_count"], counts["holdout_target_count"]) < MIN_SPLIT_TARGETS):
        return {"status": "insufficient_independent_evidence", **counts}, None

    def pair_loss(weight):
        # Fit one coefficient to independently confirmed relevance pairs. Average
        # per case to avoid one item with more negatives dominating the fit.
        return sum(
            sum(1 if s + weight * t < 0 else 0.5 if s + weight * t == 0 else 0
                for s, t in pairs) / len(pairs) for _, pairs in pairs_by_case
        )

    weight = min((i / 100 for i in range(0, 101, 5)), key=pair_loss)
    if not weight:
        return {"status": "no_training_gain", **counts}, None
    outcomes = [(case.baseline_position, _rank(case, weight)) for case in holdout]
    improved = sum(old is not None and new is not None and new < old for old, new in outcomes)
    regressed = sum(old is not None and (new is None or new > old) for old, new in outcomes)
    old_top3 = sum(old is not None and old <= 3 for old, _ in outcomes)
    new_top3 = sum(new is not None and new <= 3 for _, new in outcomes)
    metrics = {**counts, "holdout_improved": improved, "holdout_regressed": regressed,
               "baseline_holdout_top3": old_top3, "candidate_holdout_top3": new_top3}
    if new_top3 <= old_top3 or regressed:
        return {"status": "holdout_gate_failed", **metrics}, None
    return {"status": "offline_candidate", **metrics}, {
        "schema_version": 1, "type": "semantic_title_overlap_reranker", "similarity_weight": 1.0,
        "title_bigram_overlap_weight": weight, "scope": "returned_candidates_within_knowledge_origin",
        "production_approved": False,
    }


def run_learning_cycle(
    db: Session, *, retrieval_manifest: ReplayManifest | None = None,
    answer_manifest: AnswerReplayManifest | None = None,
    scan_limit: int = 50, cursor: str | None = None, category_id: str | None = None,
    diagnose: Callable[[dict], Diagnosis | None] | None = None, ai_limit: int = 5,
) -> dict:
    """Side effects: reads and local service calls only. No review, telemetry or KB writes."""
    if not 0 <= ai_limit <= 20:
        raise ValueError("AI 单次最多处理 20 条")
    discovery, drafts = _discover(db, scan_limit=scan_limit, cursor=cursor, category_id=category_id,
                                  diagnose=diagnose, ai_limit=ai_limit)
    retrieval, evaluated = _collect_retrieval(db, retrieval_manifest)
    training, candidate = _train(retrieval, evaluated)
    return {
        "schema_version": 1, "scope": "offline_only", "discovery": discovery,
        "validation": {"retrieval": retrieval, "answers": run_answer_replay(db, answer_manifest) if answer_manifest else None},
        "training": training, "candidate": candidate, "knowledge_drafts": drafts, "published": False,
    }
