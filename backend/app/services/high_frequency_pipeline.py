from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.core.database import SessionLocal
from app.models.high_frequency import (
    HighFrequencyAtomicQuestion,
    HighFrequencyProcessingTask,
    HighFrequencySourceRecord,
    HighFrequencyStandardQuestion,
)
from app.services.high_frequency import (
    _date_value,
    _period_start,
    clean,
    expand_laptop_component_verification_issues,
    normalize_atomic_issue,
    normalize_product_category,
    normalize_standard_question,
    parse_source_record,
    split_atomic_issues,
)
from app.services.high_frequency_classifier import (
    _classifier_model_id,
    _deterministic_classification,
    classify_incremental_question,
    split_ambiguous_questions,
)


PROMPT_VERSION = "high-frequency-incremental-v2"
RULE_VERSION = "high-frequency-rules-v10"
TASK_LEASE_SECONDS = 300
MAX_ATTEMPTS = 3


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _source_hash(raw: dict[str, Any]) -> str:
    serialized = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _stable_record_id(parsed: dict[str, Any], source_hash: str) -> str:
    return clean(parsed.get("record_id")) or f"content-{source_hash[:32]}"


def _as_day(value: str) -> date | None:
    return _date_value(value)


def _apply_source_fields(
    source: HighFrequencySourceRecord,
    parsed: dict[str, Any],
    raw: dict[str, Any],
    source_hash: str,
) -> None:
    source.source_hash = source_hash
    source.analyzed_at = clean(parsed.get("analyzed_at"))
    source.analyzed_on = _as_day(source.analyzed_at)
    source.category = clean(parsed.get("category")) or "未分类"
    source.level1 = clean(parsed.get("level1"))
    source.level2 = clean(parsed.get("level2"))
    source.chat_content = clean(parsed.get("chat_content"))
    source.core_problem = clean(parsed.get("core_problem"))
    source.judgment = clean(parsed.get("judgment"))
    source.basis = clean(parsed.get("basis"))
    source.reference_reply = clean(parsed.get("reference_reply"))
    source.raw_payload = raw
    source.processing_status = "queued"
    source.model_name = _classifier_model_id()
    source.prompt_version = PROMPT_VERSION
    source.rule_version = RULE_VERSION
    source.error_message = ""
    source.processed_at = None


def _apply_source_fetch_metadata(
    source: HighFrequencySourceRecord,
    *,
    source_possibly_truncated: bool,
    source_request_count: int,
) -> None:
    source.source_possibly_truncated = bool(source_possibly_truncated)
    source.source_request_count = max(1, int(source_request_count))


def ingest_records(
    db: Session,
    records: list[dict[str, Any]],
    *,
    source_possibly_truncated: bool = False,
    source_request_count: int = 1,
) -> dict[str, int]:
    """Persist new/changed work orders and queue only their new versions."""

    normalized_request_count = max(1, int(source_request_count))
    summary = {
        "fetched": len(records),
        "inserted": 0,
        "unchanged": 0,
        "changed": 0,
        "requeued": 0,
        "queued": 0,
        "rule_reconciled": 0,
        "category_reconciled": 0,
        "rule_requeued": 0,
    }
    seen: set[str] = set()
    touched_source_ids: list[str] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        parsed = parse_source_record(raw)
        source_hash = _source_hash(raw)
        record_id = _stable_record_id(parsed, source_hash)
        if record_id in seen:
            continue
        seen.add(record_id)
        source = (
            db.query(HighFrequencySourceRecord)
            .filter(HighFrequencySourceRecord.record_id == record_id)
            .one_or_none()
        )
        if source and source.source_hash == source_hash:
            summary["unchanged"] += 1
            _apply_source_fetch_metadata(
                source,
                source_possibly_truncated=source_possibly_truncated,
                source_request_count=normalized_request_count,
            )
            canonical_category = normalize_product_category(parsed.get("category"))
            if source.category != canonical_category:
                source.category = canonical_category
                summary["category_reconciled"] += 1
            stale_rule_mapping = _has_stale_deterministic_mapping(db, source)
            if source.processing_status == "failed" or stale_rule_mapping:
                (
                    db.query(HighFrequencyProcessingTask)
                    .filter(
                        HighFrequencyProcessingTask.source_record_id == source.id,
                        HighFrequencyProcessingTask.status.in_(
                            ("queued", "running", "failed")
                        ),
                    )
                    .update({"status": "superseded"}, synchronize_session=False)
                )
                source.processing_status = "queued"
                source.error_message = ""
                source.processed_at = None
                db.add(
                    HighFrequencyProcessingTask(
                        id=_id("hft"),
                        source_record_id=source.id,
                        source_hash=source_hash,
                        status="queued",
                        next_attempt_at=datetime.utcnow(),
                    )
                )
                if stale_rule_mapping:
                    summary["rule_requeued"] += 1
                else:
                    summary["requeued"] += 1
                summary["queued"] += 1
            touched_source_ids.append(source.id)
            continue
        if source is None:
            source = HighFrequencySourceRecord(id=_id("hfs"), record_id=record_id)
            db.add(source)
            summary["inserted"] += 1
        else:
            summary["changed"] += 1
            (
                db.query(HighFrequencyProcessingTask)
                .filter(
                    HighFrequencyProcessingTask.source_record_id == source.id,
                    HighFrequencyProcessingTask.status.in_(("queued", "running")),
                )
                .update({"status": "superseded"}, synchronize_session=False)
            )
            db.query(HighFrequencyAtomicQuestion).filter(
                HighFrequencyAtomicQuestion.source_record_id == source.id
            ).delete(synchronize_session=False)
        _apply_source_fields(source, parsed, raw, source_hash)
        _apply_source_fetch_metadata(
            source,
            source_possibly_truncated=source_possibly_truncated,
            source_request_count=normalized_request_count,
        )
        db.flush()
        touched_source_ids.append(source.id)
        db.add(
            HighFrequencyProcessingTask(
                id=_id("hft"),
                source_record_id=source.id,
                source_hash=source_hash,
                status="queued",
                next_attempt_at=datetime.utcnow(),
            )
        )
        summary["queued"] += 1
    summary["rule_reconciled"] = _reconcile_deterministic_mappings(
        db,
        touched_source_ids,
    )
    db.commit()
    return summary


def _has_stale_deterministic_mapping(
    db: Session,
    source: HighFrequencySourceRecord,
) -> bool:
    rows = (
        db.query(HighFrequencyAtomicQuestion)
        .filter(
            HighFrequencyAtomicQuestion.source_record_id == source.id,
            HighFrequencyAtomicQuestion.mapping_source == "deterministic",
        )
        .all()
    )
    for atomic in rows:
        classification = _deterministic_classification(
            {
                "category": source.category,
                "issue": atomic.atomic_question,
                "level1": source.level1,
                "level2": source.level2,
            }
        )
        if not classification:
            return True
        expected = normalize_atomic_issue(
            normalize_standard_question(
                source.category,
                clean(classification.get("standard_question")),
            )
        )
        if (
            not atomic.standard_question
            or atomic.standard_question.category != source.category
            or atomic.standard_question.normalized_question != expected
        ):
            return True
    return False


def _existing_atomic_mapping(
    db: Session,
    source: HighFrequencySourceRecord,
    normalized_question: str,
) -> HighFrequencyStandardQuestion | None:
    row = (
        db.query(HighFrequencyAtomicQuestion)
        .join(HighFrequencySourceRecord)
        .join(HighFrequencyStandardQuestion)
        .filter(
            HighFrequencySourceRecord.category == source.category,
            HighFrequencySourceRecord.processing_status == "ready",
            HighFrequencyAtomicQuestion.processing_status == "ready",
            HighFrequencyAtomicQuestion.normalized_question == normalized_question,
        )
        .order_by(HighFrequencyAtomicQuestion.updated_at.desc())
        .first()
    )
    return row.standard_question if row else None


def _candidate_standard_questions(
    db: Session,
    source: HighFrequencySourceRecord,
    normalized_question: str,
) -> list[dict[str, str]]:
    tokens = [
        token
        for token in (
            normalized_question[index : index + 2]
            for index in range(max(0, len(normalized_question) - 1))
        )
        if token and token not in {"如何", "怎么", "是否", "问题", "异常", "判定"}
    ][:12]
    query = db.query(HighFrequencyStandardQuestion).filter(
        HighFrequencyStandardQuestion.category == source.category
    )
    if tokens:
        query = query.filter(
            or_(
                *[
                    HighFrequencyStandardQuestion.normalized_question.contains(token)
                    for token in dict.fromkeys(tokens)
                ]
            )
        )
    candidates = (
        query.order_by(HighFrequencyStandardQuestion.updated_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "business_theme": standard.business_theme,
            "standard_question": standard.standard_question,
        }
        for standard in candidates
    ]


def _get_or_create_standard_question(
    db: Session,
    source: HighFrequencySourceRecord,
    classification: dict[str, str],
) -> HighFrequencyStandardQuestion:
    title = normalize_standard_question(
        source.category,
        clean(classification.get("standard_question")),
    )
    normalized = normalize_atomic_issue(title)
    standard = (
        db.query(HighFrequencyStandardQuestion)
        .filter(
            HighFrequencyStandardQuestion.category == source.category,
            HighFrequencyStandardQuestion.normalized_question == normalized,
            HighFrequencyStandardQuestion.prompt_version == PROMPT_VERSION,
        )
        .one_or_none()
    )
    if standard:
        return standard
    standard = HighFrequencyStandardQuestion(
        id=_id("hfsq"),
        category=source.category,
        normalized_question=normalized,
        standard_question=title,
        business_theme=clean(classification.get("business_theme")) or "其他",
        model_name=_classifier_model_id(),
        prompt_version=PROMPT_VERSION,
        rule_version=RULE_VERSION,
    )
    db.add(standard)
    db.flush()
    return standard


def _reconcile_deterministic_mappings(
    db: Session,
    source_record_ids: list[str],
) -> int:
    """Apply newly approved rules to existing mappings without model calls."""

    if not source_record_ids:
        return 0
    rows = (
        db.query(HighFrequencyAtomicQuestion, HighFrequencySourceRecord)
        .join(HighFrequencySourceRecord)
        .filter(HighFrequencyAtomicQuestion.source_record_id.in_(source_record_ids))
        .all()
    )
    updated = 0
    for atomic, source in rows:
        classification = _deterministic_classification(
            {
                "category": source.category,
                "issue": atomic.atomic_question,
                "level1": source.level1,
                "level2": source.level2,
            }
        )
        if not classification and atomic.standard_question:
            if atomic.standard_question.category != source.category:
                classification = {
                    "business_theme": atomic.standard_question.business_theme,
                    "standard_question": atomic.standard_question.standard_question,
                }
        if not classification:
            continue
        standard = _get_or_create_standard_question(db, source, classification)
        if (
            atomic.standard_question_id != standard.id
            or atomic.mapping_source != "deterministic"
        ):
            atomic.standard_question_id = standard.id
            atomic.mapping_source = "deterministic"
            atomic.model_name = _classifier_model_id()
            atomic.prompt_version = PROMPT_VERSION
            atomic.rule_version = RULE_VERSION
            updated += 1
    return updated


def _atomic_issues_for_source(
    source: HighFrequencySourceRecord,
) -> tuple[list[str], str, str | None]:
    issues, split_status = split_atomic_issues(source.core_problem)
    if split_status == "缺少核心问题":
        return [], split_status, None
    if split_status != "待拆分":
        expanded = [
            value
            for issue in issues
            for value in expand_laptop_component_verification_issues(source.category, issue)
        ]
        return expanded, split_status, None
    results, error = split_ambiguous_questions(
        [
            {
                "id": source.id,
                "category": source.category,
                "level1": source.level1,
                "level2": source.level2,
                "core_problem": source.core_problem,
            }
        ]
    )
    result = results.get(source.id)
    if error:
        return [], "拆分失败", error
    if not result or result.get("needs_review") or not result.get("questions"):
        return [], "AI标记需自动排除", None
    expanded = [
        value
        for issue in result["questions"]
        for value in expand_laptop_component_verification_issues(source.category, issue)
    ]
    status = "DeepSeek确认单问题" if len(expanded) == 1 else "DeepSeek已拆分"
    return expanded, status, None


def _process_source(db: Session, source: HighFrequencySourceRecord) -> tuple[str, str]:
    # A failed attempt may have written a partial mapping before a later
    # atomic question failed. A retry must replace that attempt as one unit.
    db.query(HighFrequencyAtomicQuestion).filter(
        HighFrequencyAtomicQuestion.source_record_id == source.id
    ).delete(synchronize_session=False)
    issues, split_status, split_error = _atomic_issues_for_source(source)
    if split_error:
        return "failed", split_error
    if not issues:
        source.processing_status = "review"
        source.error_message = (
            "缺少可统计的 AI 核心问题。"
            if split_status == "缺少核心问题"
            else "AI 无法可靠拆分，已自动排除在高频排行外。"
        )
        source.processed_at = datetime.utcnow()
        return "completed", ""

    unique_issues: dict[str, str] = {}
    for issue in issues:
        normalized = normalize_atomic_issue(issue)
        if normalized:
            unique_issues.setdefault(normalized, clean(issue))
    for atomic_index, (normalized, issue) in enumerate(unique_issues.items(), 1):
        deterministic = _deterministic_classification(
            {
                "category": source.category,
                "issue": issue,
                "level1": source.level1,
                "level2": source.level2,
            }
        )
        reused = None
        mapping_source = "deterministic" if deterministic else "database"
        if deterministic:
            reused = _get_or_create_standard_question(
                db,
                source,
                deterministic,
            )
        else:
            reused = _existing_atomic_mapping(db, source, normalized)
        if reused is None:
            candidates = _candidate_standard_questions(db, source, normalized)
            classification, error = classify_incremental_question(
                {
                    "category": source.category,
                    "issue": issue,
                    "level1": source.level1,
                    "level2": source.level2,
                },
                candidates,
            )
            if error or not classification:
                if error == "DeepSeek 高频问题增量归类返回不完整。":
                    db.flush()
                    db.query(HighFrequencyAtomicQuestion).filter(
                        HighFrequencyAtomicQuestion.source_record_id == source.id
                    ).delete(synchronize_session=False)
                    source.processing_status = "review"
                    source.error_message = (
                        "DeepSeek 未能稳定生成标准标题，已自动排除在高频排行外。"
                    )
                    source.processed_at = datetime.utcnow()
                    return "completed", ""
                return "failed", error or "DeepSeek 高频问题归类未返回结果。"
            reused = _get_or_create_standard_question(db, source, classification)
            mapping_source = clean(classification.get("_mapping_source")) or "model"
        db.add(
            HighFrequencyAtomicQuestion(
                id=_id("hfaq"),
                source_record_id=source.id,
                atomic_index=atomic_index,
                atomic_question=issue,
                normalized_question=normalized,
                split_status=split_status,
                processing_status="ready",
                standard_question_id=reused.id,
                mapping_source=mapping_source,
                model_name=_classifier_model_id(),
                prompt_version=PROMPT_VERSION,
                rule_version=RULE_VERSION,
            )
        )
    source.processing_status = "ready"
    source.error_message = ""
    source.processed_at = datetime.utcnow()
    return "completed", ""


def process_next_high_frequency_task() -> bool:
    """Claim and process one durable task.  Returns false when no work exists."""

    now = datetime.utcnow()
    with SessionLocal() as db:
        task = (
            db.query(HighFrequencyProcessingTask)
            .filter(
                or_(
                    and_(
                        HighFrequencyProcessingTask.status == "queued",
                        HighFrequencyProcessingTask.next_attempt_at <= now,
                    ),
                    and_(
                        HighFrequencyProcessingTask.status == "running",
                        HighFrequencyProcessingTask.lease_expires_at.is_not(None),
                        HighFrequencyProcessingTask.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(HighFrequencyProcessingTask.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not task:
            return False
        task.status = "running"
        task.attempt_count += 1
        task.started_at = now
        task.lease_expires_at = now + timedelta(seconds=TASK_LEASE_SECONDS)
        source = (
            db.query(HighFrequencySourceRecord)
            .options(joinedload(HighFrequencySourceRecord.atomic_questions))
            .filter(HighFrequencySourceRecord.id == task.source_record_id)
            .one_or_none()
        )
        if not source or source.source_hash != task.source_hash:
            task.status = "superseded"
            task.completed_at = datetime.utcnow()
            task.lease_expires_at = None
            db.commit()
            return True
        source.processing_status = "running"
        db.commit()
        try:
            with db.begin():
                status, error = _process_source(db, source)
                task.status = status
                task.error_message = error
                task.lease_expires_at = None
                task.completed_at = datetime.utcnow()
                if status == "failed":
                    source.processing_status = "failed"
                    source.error_message = error
                    if task.attempt_count < MAX_ATTEMPTS:
                        task.status = "queued"
                        task.next_attempt_at = datetime.utcnow() + timedelta(
                            seconds=task.attempt_count * 10
                        )
                        task.completed_at = None
        except Exception as exc:
            db.rollback()
            source = (
                db.query(HighFrequencySourceRecord)
                .filter(HighFrequencySourceRecord.id == task.source_record_id)
                .one_or_none()
            )
            task = (
                db.query(HighFrequencyProcessingTask)
                .filter(HighFrequencyProcessingTask.id == task.id)
                .one()
            )
            error = str(exc)[:1000]
            task.error_message = error
            task.lease_expires_at = None
            if source:
                source.processing_status = "failed"
                source.error_message = error
            if task.attempt_count < MAX_ATTEMPTS:
                task.status = "queued"
                task.next_attempt_at = datetime.utcnow() + timedelta(
                    seconds=task.attempt_count * 10
                )
            else:
                task.status = "failed"
                task.completed_at = datetime.utcnow()
            db.commit()
        return True


async def run_high_frequency_worker(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        processed = await asyncio.to_thread(process_next_high_frequency_task)
        if not processed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass


def process_available_high_frequency_tasks(max_tasks: int = 1000) -> int:
    """Test-only and local-admin helper; production workers claim one at a time."""

    processed = 0
    while processed < max_tasks and process_next_high_frequency_task():
        processed += 1
    return processed


def _source_matches(
    query,
    from_date: str,
    to_date: str,
    category: str,
    level1: str,
    level2: str,
):
    start = _as_day(from_date)
    end = _as_day(to_date)
    if start:
        query = query.filter(HighFrequencySourceRecord.analyzed_on >= start)
    if end:
        query = query.filter(HighFrequencySourceRecord.analyzed_on <= end)
    if category:
        query = query.filter(HighFrequencySourceRecord.category == category)
    if level1:
        query = query.filter(HighFrequencySourceRecord.level1 == level1)
    if level2:
        query = query.filter(HighFrequencySourceRecord.level2 == level2)
    return query


def build_persisted_dashboard(
    db: Session,
    *,
    from_date: str = "",
    to_date: str = "",
    category: str = "",
    level1: str = "",
    level2: str = "",
    business_theme: str = "",
    high_frequency_only: bool = True,
    period: str = "week",
) -> dict[str, Any]:
    source_query = _source_matches(
        db.query(HighFrequencySourceRecord),
        from_date,
        to_date,
        category,
        level1,
        level2,
    )
    sources = source_query.order_by(HighFrequencySourceRecord.analyzed_at.desc()).all()
    source_by_id = {source.id: source for source in sources}
    counts = Counter(source.processing_status for source in sources)
    categories = sorted({source.category for source in sources if source.category})
    level1_options = sorted({source.level1 for source in sources if source.level1})
    level2_options = sorted({source.level2 for source in sources if source.level2})
    ready_ids = [
        source.id for source in sources if source.processing_status == "ready"
    ]
    atomic_questions = []
    if ready_ids:
        atomic_questions = (
            db.query(HighFrequencyAtomicQuestion)
            .options(joinedload(HighFrequencyAtomicQuestion.standard_question))
            .filter(
                HighFrequencyAtomicQuestion.source_record_id.in_(ready_ids),
                HighFrequencyAtomicQuestion.processing_status == "ready",
                HighFrequencyAtomicQuestion.standard_question_id.is_not(None),
            )
            .all()
        )

    groups: dict[str, list[HighFrequencyAtomicQuestion]] = defaultdict(list)
    for atomic in atomic_questions:
        standard = atomic.standard_question
        if standard:
            groups[standard.id].append(atomic)
    all_rows: list[dict[str, Any]] = []
    for standard_id, atoms in groups.items():
        standard = atoms[0].standard_question
        per_source: dict[str, list[HighFrequencyAtomicQuestion]] = defaultdict(list)
        for atomic in atoms:
            per_source[atomic.source_record_id].append(atomic)
        cases = []
        for source_id, source_atoms in sorted(
            per_source.items(),
            key=lambda pair: source_by_id[pair[0]].analyzed_at,
            reverse=True,
        ):
            source = source_by_id[source_id]
            cases.append(
                {
                    "工单ID": source.record_id or "-",
                    "分析时间": source.analyzed_at or "-",
                    "品类": source.category,
                    "业务主题": standard.business_theme,
                    "原始问题": source.chat_content or "-",
                    "AI分析问题": "；".join(
                        atomic.atomic_question for atomic in source_atoms
                    ),
                    "原始AI核心问题": source.core_problem,
                    "原子问题": source_atoms[0].atomic_question,
                    "拆分状态": source_atoms[0].split_status,
                    "判定结论": source.judgment or "-",
                    "判定依据": source.basis or "-",
                    "参考话术": source.reference_reply or "",
                }
            )
        buckets = sorted(
            {
                _period_start(source_by_id[source_id].analyzed_on, period).isoformat()
                for source_id in per_source
                if source_by_id[source_id].analyzed_on
            }
        )
        all_rows.append(
            {
                "品类": standard.category,
                "业务主题": standard.business_theme,
                "原子问题": standard.standard_question,
                "出现次数": len(cases),
                "出现占比": 0,
                "工单数": len(cases),
                "有话术": f"{sum(bool(case['参考话术']) for case in cases)}/{len(cases)}",
                "统计周期": "、".join(buckets),
                "最近出现时间": max(
                    (case["分析时间"] for case in cases if case["分析时间"] != "-"),
                    default="-",
                ),
                "案例": cases,
            }
        )
    if business_theme:
        all_rows = [row for row in all_rows if row["业务主题"] == business_theme]
    selected_count = sum(row["出现次数"] for row in all_rows)
    for row in all_rows:
        row["出现占比"] = round(row["出现次数"] / selected_count, 4) if selected_count else 0
    high_frequency_count = sum(row["出现次数"] >= 2 for row in all_rows)
    rows = [
        row for row in all_rows if not high_frequency_only or row["出现次数"] >= 2
    ]
    rows.sort(key=lambda row: (-row["出现次数"], row["品类"], row["原子问题"]))
    for index, row in enumerate(rows, 1):
        row["排名"] = index

    trend: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"atomic_count": 0, "work_orders": set()}
    )
    selected_standard_ids = {
        atoms[0].standard_question_id
        for standard_id, atoms in groups.items()
        if any(row["原子问题"] == atoms[0].standard_question.standard_question for row in all_rows)
    }
    for atomic in atomic_questions:
        if atomic.standard_question_id not in selected_standard_ids:
            continue
        source = source_by_id[atomic.source_record_id]
        if source.analyzed_on:
            bucket = _period_start(source.analyzed_on, period).isoformat()
            trend[bucket]["atomic_count"] += 1
            trend[bucket]["work_orders"].add(source.id)

    pending_cases = [
        {
            "工单ID": source.record_id or "-",
            "分析时间": source.analyzed_at or "-",
            "品类": source.category,
            "原始问题": source.chat_content or "-",
            "AI分析问题": source.core_problem,
            "原始AI核心问题": source.core_problem,
            "拆分状态": "AI自动排除",
            "判定结论": source.judgment or "-",
            "判定依据": source.basis or "-",
            "参考话术": source.reference_reply or "",
        }
        for source in sources
        if source.processing_status == "review"
    ]
    latest_sync = max(
        (source.updated_at for source in sources if source.updated_at),
        default=None,
    )
    source_request_count = max(
        (source.source_request_count or 1 for source in sources),
        default=0,
    )
    possibly_truncated = any(
        source.source_possibly_truncated for source in sources
    )
    return {
        "summary": {
            "total_records": len(sources),
            "ready_record_count": counts["ready"],
            "queued_record_count": counts["queued"],
            "running_record_count": counts["running"],
            "failed_record_count": counts["failed"],
            "review_record_count": counts["review"],
            "atomic_issue_count": selected_count,
            "pending_split_records": len(pending_cases),
            "missing_core_problem_records": 0,
            "standard_question_count": len(all_rows),
            "high_frequency_count": high_frequency_count,
            "single_occurrence_count": sum(row["出现次数"] == 1 for row in all_rows),
            "categories": len(categories),
        },
        "categories": categories,
        "level1_options": level1_options,
        "level2_options": level2_options,
        "business_theme_options": sorted(
            {row["业务主题"] for row in all_rows if row["业务主题"]}
        ),
        "rows": rows,
        "pending_cases": pending_cases,
        "trend": [
            {
                "period": bucket,
                "atomic_count": values["atomic_count"],
                "work_order_count": len(values["work_orders"]),
            }
            for bucket, values in sorted(trend.items())
        ],
        "meta": {
            "query_limit": 0,
            "possibly_truncated": possibly_truncated,
            "source_request_count": source_request_count,
            "grouping_mode": "same_category_persisted_standard_question",
            "standard_question_mode": "deepseek_incremental_persisted_mapping",
            "classification_error": "",
            "split_error": "",
            "split_pending": bool(counts["queued"] or counts["running"]),
            "classification_pending": bool(counts["queued"] or counts["running"]),
            "uncached_classification_count": 0,
            "uncached_split_count": 0,
            "needs_manual_review": bool(pending_cases),
            "worker_state": (
                "failed"
                if counts["failed"]
                else "running"
                if counts["queued"] or counts["running"]
                else "completed"
            ),
            "worker_error": "",
            "quality_status": (
                "failed"
                if counts["failed"]
                else "incomplete"
                if possibly_truncated
                else "processing"
                if counts["queued"] or counts["running"]
                else "review"
                if counts["review"]
                else "ready"
            ),
            "high_frequency_only": high_frequency_only,
            "last_sync_at": latest_sync.isoformat() if latest_sync else "",
        },
    }
