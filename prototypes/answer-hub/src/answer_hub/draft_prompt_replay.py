"""Answer Hub-only shadow replay for candidate knowledge-draft prompts."""

from __future__ import annotations

import json
from typing import Any

from .mimo import MimoClient, MimoError


CURRENT_DRAFT_PROMPT_VERSION = "answer-hub-topic-transcription-v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _topic_is_replayable(topic: Any) -> bool:
    if not isinstance(topic, dict):
        return False
    evidence = topic.get("主题事实证据包") or topic.get("evidence_package")
    return bool(
        _text(topic.get("topic_id") or topic.get("主题ID"))
        and isinstance(evidence, dict)
        and list(
            evidence.get("facts")
            or evidence.get("representative_facts")
            or []
        )
    )


def _draft_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _text(candidate.get("title")),
        "subtitles": list(candidate.get("subtitles") or []),
        "content": candidate.get("content"),
        "recommended_reply": _text(candidate.get("recommended_reply")),
        "content_type": _text(candidate.get("content_type")),
        "standard_refs": list(candidate.get("standard_refs") or []),
        "knowledge_form": _text(candidate.get("knowledge_form")),
        "applicable_scope": _text(candidate.get("applicable_scope")),
        "applicable_brands": list(candidate.get("applicable_brands") or []),
        "applicable_models": list(candidate.get("applicable_models") or []),
        "needs_human_review": bool(candidate.get("needs_human_review")),
        "confidence": candidate.get("confidence"),
    }


def topic_review_rows_to_replay_samples(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build replay-only samples from a frozen topic-review workbook.

    The workbook is a model output, not human final truth.  This function only
    transports the complete topic evidence package for an isolated replay and
    deliberately leaves ``human_final_draft`` empty.
    """

    samples: list[dict[str, Any]] = []
    skipped_samples: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=2):
        topic_id = _text(row.get("主题ID"))
        sample_id = topic_id or f"topic-review-row-{row_index}"
        product_type = _text(row.get("产品类型")) or _text(row.get("适用范围"))
        raw_evidence = row.get("主题事实证据包")
        if isinstance(raw_evidence, dict):
            evidence = raw_evidence
        else:
            try:
                evidence = json.loads(_text(raw_evidence))
            except json.JSONDecodeError:
                evidence = None
        if not topic_id:
            reason = "缺少主题ID。"
        elif not product_type:
            reason = "缺少产品类型或适用范围。"
        elif not isinstance(evidence, dict):
            reason = "主题事实证据包不是 JSON 对象。"
        elif not list(
            evidence.get("facts")
            or evidence.get("representative_facts")
            or []
        ):
            reason = "主题事实证据包不含 facts 或 representative_facts。"
        else:
            samples.append(
                {
                    "id": sample_id,
                    "topic": {
                        "topic_id": topic_id,
                        "产品类型": product_type,
                        "回收业务层级": _text(row.get("回收业务层级")),
                        "主题问题意图": _text(row.get("主题问题意图")),
                        "主题对象/部位": _text(row.get("主题对象/部位")),
                        "主题异常现象": _text(row.get("主题异常现象")),
                        "主题解题方式": _text(row.get("主题解题方式")),
                        "主题事实证据包": evidence,
                        "主题事实引用": _text(row.get("主题事实引用")),
                        "主题证据摘要": _text(row.get("主题证据摘要")),
                        "主题图片链接": _text(row.get("主题图片链接")),
                        "主题视频链接": _text(row.get("主题视频链接")),
                    },
                    # A topic review workbook is never treated as a human-final
                    # draft. Its rows are therefore ineligible for adoption.
                    "human_final_draft": {},
                }
            )
            continue
        skipped_samples.append({"id": sample_id, "reason": reason})
    return {"samples": samples, "skipped_samples": skipped_samples}


def replay_draft_prompts(
    *,
    client: MimoClient,
    samples: list[dict[str, Any]],
    candidate_prompt: str,
    candidate_prompt_version: str,
) -> dict[str, Any]:
    """Replay frozen Answer Hub topic inputs with current and candidate prompts.

    The caller owns persistence. This helper returns only sanitized draft
    fields and never modifies a topic, a source workbook, or a live prompt.
    """

    rows: list[dict[str, Any]] = []
    skipped_samples: list[dict[str, Any]] = []
    failed_samples: list[dict[str, Any]] = []
    for sample in samples:
        sample_id = _text(sample.get("id"))
        topic = sample.get("topic")
        if not _topic_is_replayable(topic):
            skipped_samples.append(
                {
                    "id": sample_id,
                    "reason": "缺少完整 Answer Hub 主题证据包。",
                }
            )
            continue
        try:
            baseline = client.label_topic(
                dict(topic),
                [],
                use_standard_references=False,
                max_attempts=2,
            )
        except MimoError as exc:
            failed_samples.append(
                {
                    "id": sample_id,
                    "stage": "baseline",
                    "reason": str(exc),
                }
            )
            continue
        try:
            candidate = client.replay_topic_draft(
                dict(topic),
                draft_prompt=candidate_prompt,
                prompt_version=candidate_prompt_version,
            )
        except MimoError as exc:
            failed_samples.append(
                {
                    "id": sample_id,
                    "stage": "candidate",
                    "reason": str(exc),
                }
            )
            continue
        rows.append(
            {
                "id": sample_id,
                "topic_id": _text(topic.get("topic_id") or topic.get("主题ID")),
                "candidate_prompt_version": candidate_prompt_version,
                "baseline_prompt_version": _text(
                    (baseline.request_audit or {}).get("prompt_version")
                )
                or CURRENT_DRAFT_PROMPT_VERSION,
                "baseline": _draft_payload(dict(baseline.candidate or {})),
                "candidate": _draft_payload(dict(candidate.candidate or {})),
                "human_final_draft": dict(sample.get("human_final_draft") or {}),
            }
        )
    return {
        "status": "completed" if not failed_samples else "completed_with_failures",
        "replay_type": "answer_hub_draft_transcription",
        "model": _text(getattr(getattr(client, "config", None), "model", "")),
        "candidate_prompt_version": candidate_prompt_version,
        "completed_count": len(rows),
        "skipped_count": len(skipped_samples),
        "failed_count": len(failed_samples),
        "rows": rows,
        "skipped_samples": skipped_samples,
        "failed_samples": failed_samples,
    }
