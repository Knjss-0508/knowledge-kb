import csv
import asyncio
import hashlib
import io
import json
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.models.integration import ConfidenceTrainingJob, IntegrationIngestion
from app.models.user import User
from app.routes.auth import require_permission
from app.schemas.confidence_training import (
    ConfidenceRegressionReviewUpdate,
    ConfidenceTrainingItem,
    ConfidenceTrainingJob as ConfidenceTrainingJobSchema,
    ConfidenceTrainingOverview,
    ConfidenceTrainingSettings,
    ConfidenceTrainingSettingsUpdate,
    ConfidenceTrainingUpdate,
)
from app.services.confidence_training import (
    EVALUATION_SCOPE,
    MODEL_NAME,
    aggregate,
    item_payload,
    settings_snapshot,
    update_item,
    update_settings,
)
from app.services.model_annotation import (
    ModelAnnotationError,
    analyze_confidence_training_prompt,
    correct_with_deepseek_flash,
    revise_confidence_training_prompt,
    shadow_rerun_transcribed_candidate,
    test_deepseek_flash_connection,
)


router = APIRouter(prefix="/confidence-training", tags=["置信度训练"])


def _rows(db: Session) -> list[IntegrationIngestion]:
    return (
        db.query(IntegrationIngestion)
        .filter(
            IntegrationIngestion.review_status.isnot(None),
            IntegrationIngestion.source_system != "excel",
        )
        .order_by(IntegrationIngestion.created_at.desc())
        .all()
    )


def _training_job_payload(job: ConfidenceTrainingJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "model_name": job.model_name,
        "evaluation_scope": job.evaluation_scope,
        "dataset_hash": job.dataset_hash,
        "sample_count": job.sample_count,
        "train_count": job.train_count,
        "validation_count": job.validation_count,
        "test_count": job.test_count,
        "requested_by": job.requested_by,
        "error_message": job.error_message,
        "analysis_result": dict(job.analysis_result or {}),
        "candidate_prompt": job.candidate_prompt,
        "resolved_model_version": job.resolved_model_version,
    "shadow_evaluation": dict(job.shadow_evaluation or {}),
        "regression_reviews": dict(job.regression_reviews or {}),
        "prompt_versions": list(job.prompt_versions or []),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _manual_training_dataset(rows: list[IntegrationIngestion]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    confirmed_rows = [
        row for row in rows
        if item_payload(row)["truth_status"] == "confirmed"
    ]
    confirmed_rows.sort(key=lambda row: row.id)
    dataset: list[dict[str, Any]] = []
    counts = {"train": 0, "validation": 0, "test": 0}
    total = len(confirmed_rows)
    test_count = max(1, total // 10) if total >= 3 else 0
    validation_count = max(1, total // 10) if total >= 3 else (1 if total == 2 else 0)
    train_count = total - validation_count - test_count
    for index, row in enumerate(confirmed_rows):
        item = item_payload(row)
        knowledge = dict((row.candidate_payload or {}).get("knowledge") or {})
        split = (
            "train" if index < train_count
            else ("validation" if index < train_count + validation_count else "test")
        )
        counts[split] += 1
        dataset.append({
            "id": row.id,
            "split": split,
            "title": str(knowledge.get("title") or item["title"]),
            "content": knowledge.get("content"),
            "recommended_reply": knowledge.get("recommended_reply"),
            "model_label": item["model_label"],
            "human_label": item["human_label"],
            "model_draft_disposition": item["model_draft_disposition"],
            "human_draft_disposition": item["human_draft_disposition"],
            "evaluation_scope": EVALUATION_SCOPE,
            "not_for_weight_training": True,
        })
    return dataset, counts


def _prompt_optimization_snapshot(dataset: list[dict[str, Any]]) -> dict[str, Any]:
    def text_excerpt(value: Any, limit: int) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return str(text).strip()[:limit]

    mismatches = [
        item for item in dataset
        if item.get("model_label") != item.get("human_label")
        or item.get("model_draft_disposition") != item.get("human_draft_disposition")
    ]
    # Keep the first real analysis bounded: error-first samples are more useful
    # than sending an oversized snapshot that can time out at the internal API.
    selected = (mismatches + [item for item in dataset if item not in mismatches])[:8]
    return {
        "evaluation_scope": EVALUATION_SCOPE,
        "current_prompt_version": "post-transcription-value-and-content-review-v10",
        "current_policy": {
            "knowledge_value": ["worthy", "unworthy", "pending"],
            "draft_disposition": ["approved", "revision_required", "hold_for_evidence"],
            "rule": "沉淀价值和草稿处理必须分别判断。",
        },
        "population": {
            "total_confirmed_truth": len(dataset),
            "model_human_value_mismatches": sum(item.get("model_label") != item.get("human_label") for item in dataset),
            "model_human_draft_mismatches": sum(item.get("model_draft_disposition") != item.get("human_draft_disposition") for item in dataset),
        },
        "representative_cases": [
            {
                "id": item["id"],
                "title": item["title"],
                "content_excerpt": text_excerpt(item.get("content"), 360),
                "recommended_reply_excerpt": text_excerpt(item.get("recommended_reply"), 180),
                "model_label": item.get("model_label"),
                "human_label": item.get("human_label"),
                "model_draft_disposition": item.get("model_draft_disposition"),
                "human_draft_disposition": item.get("human_draft_disposition"),
            }
            for item in selected
        ],
    }


CURRENT_REVIEW_PROMPT_VERSION = "post-transcription-value-and-content-review-v10"
CURRENT_REVIEW_PROMPT = """你是知识转写草稿审核标注器。必须先阅读主题、来源证据、知识草稿和推荐回复，再判断是否值得沉淀；不能只凭标题或聚类标签判断。

只返回 JSON 对象，字段包括：knowledge_value、draft_disposition、reason、evidence_used、limitations、confidence。
knowledge_value 只能是 worthy、unworthy、pending。
draft_disposition 只能是 approved、revision_required、hold_for_evidence、not_applicable。
若 knowledge_value=unworthy，draft_disposition 必须为 not_applicable；若 knowledge_value=pending，draft_disposition 必须为 hold_for_evidence；approved 或 revision_required 只能对应 worthy。"""


def _shadow_candidate(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": sample.get("title"),
        "content": sample.get("content"),
        "recommended_reply": sample.get("recommended_reply"),
        "evidence_excerpt": sample.get("evidence_excerpt"),
    }


def _shadow_value(result: dict[str, Any]) -> str | None:
    value = str(result.get("knowledge_value") or result.get("decision") or "").strip()
    return value if value in {"worthy", "unworthy", "pending"} else None


def _shadow_draft(result: dict[str, Any]) -> str | None:
    value = str(result.get("draft_disposition") or "").strip()
    if value in {"approved", "revision_required", "hold_for_evidence", "not_applicable"}:
        return value
    action = str(result.get("suggested_action") or "").strip()
    return {
        "submit_for_human_review": "approved",
        "return_for_revision": "revision_required",
        "hold_for_evidence": "hold_for_evidence",
        "discard": "not_applicable",
    }.get(action)


def _wilson_lower_bound(correct: int, total: int) -> float | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = correct / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5
    return round(max(0.0, (centre - spread) / denominator), 4)


def _shadow_metrics(rows: list[dict[str, Any]], result_key: str) -> dict[str, Any]:
    total = len(rows)
    value_correct = sum(row[result_key]["value_correct"] for row in rows)
    draft_correct = sum(row[result_key]["draft_correct"] for row in rows)
    strict_correct = sum(row[result_key]["strict_correct"] for row in rows)
    return {
        "n": total,
        "value_accuracy": round(value_correct / total, 4) if total else None,
        "draft_accuracy": round(draft_correct / total, 4) if total else None,
        "strict_accuracy": round(strict_correct / total, 4) if total else None,
        "strict_accuracy_ci95_lower": _wilson_lower_bound(strict_correct, total),
        "value_correct_count": value_correct,
        "draft_correct_count": draft_correct,
        "strict_correct_count": strict_correct,
    }


def _shadow_comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = _shadow_metrics(rows, "baseline")
    candidate = _shadow_metrics(rows, "candidate")
    improved = sum(
        not row["baseline"]["strict_correct"] and row["candidate"]["strict_correct"]
        for row in rows
    )
    regressed = sum(
        row["baseline"]["strict_correct"] and not row["candidate"]["strict_correct"]
        for row in rows
    )
    delta = round((candidate["strict_accuracy"] or 0) - (baseline["strict_accuracy"] or 0), 4)
    gate_passed = (
        delta > 0
        and candidate["strict_accuracy_ci95_lower"] is not None
        and candidate["strict_accuracy_ci95_lower"] >= (baseline["strict_accuracy_ci95_lower"] or 0)
        and regressed == 0
    )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "strict_accuracy_delta": delta,
        "improved_count": improved,
        "regressed_count": regressed,
        "gate_passed": gate_passed,
        "recommended_action": (
            "人工审核后可进入下一轮影子运行" if gate_passed
            else "保持当前 Prompt；人工审核错例后继续修改候选 Prompt"
        ),
    }


def _regressed_rows(job: ConfidenceTrainingJob) -> list[dict[str, Any]]:
    return [
        row for row in list((job.shadow_evaluation or {}).get("rows") or [])
        if bool((row.get("baseline") or {}).get("strict_correct"))
        and not bool((row.get("candidate") or {}).get("strict_correct"))
    ]


def _revision_preconditions(job: ConfidenceTrainingJob) -> tuple[bool, str]:
    evaluation = dict(job.shadow_evaluation or {})
    if evaluation.get("status") != "completed":
        return False, "请先完成本轮候选 Prompt 影子复跑。"
    regressed = _regressed_rows(job)
    if not regressed:
        return False, "当前没有可用于修订的已确认退化样本。"
    reviews = dict(job.regression_reviews or {})
    missing = [str(row.get("id")) for row in regressed if not reviews.get(str(row.get("id")))]
    if missing:
        return False, f"还有 {len(missing)} 条退化样本未完成人工归因。"
    truth_correction = [
        item for item in reviews.values()
        if item.get("classification") == "human_truth_correction_required"
    ]
    if truth_correction:
        return False, "存在“人工真值需修正”的退化样本，请先回候选价值复核修正并重新冻结评测。"
    if not any(bool(item.get("keep_as_regression_case")) for item in reviews.values()):
        return False, "至少保留一条退化样本作为回归测试用例。"
    return True, ""


def _prompt_revision_snapshot(job: ConfidenceTrainingJob) -> dict[str, Any]:
    versions = list(job.prompt_versions or [])
    base = versions[-1] if versions else {
        "version": "v1-candidate",
        "prompt": job.candidate_prompt,
    }
    reviews = dict(job.regression_reviews or {})
    cases = []
    for row in _regressed_rows(job):
        review = dict(reviews.get(str(row.get("id"))) or {})
        if not review.get("keep_as_regression_case"):
            continue
        cases.append({
            "id": row.get("id"),
            "title": row.get("title"),
            "human_truth": {
                "knowledge_value": row.get("human_label"),
                "draft_disposition": row.get("human_draft_disposition"),
            },
            "baseline_output": row.get("baseline"),
            "candidate_output": row.get("candidate"),
            "human_classification": review.get("classification"),
            "human_note": review.get("note"),
        })
    return {
        "evaluation_scope": EVALUATION_SCOPE,
        "base_prompt_version": base.get("version", "v1-candidate"),
        "base_prompt": base.get("prompt", job.candidate_prompt),
        "regression_cases": cases,
        "constraints": [
            "不得修改人工真值，也不得要求系统自动修改人工真值。",
            "不得自动启用候选 Prompt、不得切换生产路由、不得提交模型权重训练。",
            "修订后必须保留 unworthy 与 pending 的安全边界，且不得使回归用例退化。",
        ],
    }


def _run_shadow_rerun(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if not job or not job.candidate_prompt:
            return
        stored_evaluation = dict(job.shadow_evaluation or {})
        rows: list[dict[str, Any]] = list(stored_evaluation.get("rows") or [])
        completed_ids = {str(row.get("id")) for row in rows if row.get("id")}
        samples = list(job.dataset_payload or [])
        for index, sample in enumerate(samples, start=1):
            if str(sample.get("id")) in completed_ids:
                continue
            candidate = _shadow_candidate(sample)
            def call_with_retry(*, review_prompt: str, prompt_version: str, label: str) -> dict[str, Any]:
                last_error: ModelAnnotationError | None = None
                for attempt in range(1, 4):
                    try:
                        result = shadow_rerun_transcribed_candidate(
                            review_prompt=review_prompt,
                            candidate=candidate,
                            prompt_version=prompt_version,
                        )
                        # The existing group-model configuration is limited to
                        # one request per second. Keep paired baseline/candidate
                        # calls from immediately tripping the gateway limiter.
                        time.sleep(1.05)
                        return result
                    except ModelAnnotationError as exc:
                        last_error = exc
                        if not exc.retryable or attempt >= 3:
                            raise ModelAnnotationError(
                                f"第 {index} 条 {label} 调用失败（重试 {attempt}/3）：{exc}",
                                f"SHADOW_{label.upper()}_{exc.error_code}",
                                False,
                            ) from exc
                        time.sleep(2 if attempt == 1 else 5)
                raise last_error or ModelAnnotationError("未知模型调用错误。", "UNKNOWN")

            try:
                baseline_result = call_with_retry(
                    review_prompt=CURRENT_REVIEW_PROMPT,
                    prompt_version=CURRENT_REVIEW_PROMPT_VERSION,
                    label="baseline",
                )
                candidate_result = call_with_retry(
                    review_prompt=job.candidate_prompt,
                    prompt_version="candidate-from-" + job.id,
                    label="candidate",
                )
            except ModelAnnotationError as exc:
                job.status = "shadow_rerun_failed"
                job.stage = "候选 Prompt 影子复跑失败"
                job.error_message = f"{exc.error_code}：{exc}"
                job.shadow_evaluation = {
                    **dict(job.shadow_evaluation or {}),
                    "status": "failed",
                    "failed_sample_index": index,
                    "failed_sample_id": sample.get("id"),
                    "error_code": exc.error_code,
                    "error_message": str(exc),
                    "rows": rows,
                }
                db.add(job)
                db.commit()
                return
            truth_value = sample.get("human_label")
            truth_draft = sample.get("human_draft_disposition")
            def evaluation(result: dict[str, Any]) -> dict[str, Any]:
                value = _shadow_value(result)
                draft = _shadow_draft(result)
                value_correct = value == truth_value
                draft_correct = draft == truth_draft
                return {
                    "knowledge_value": value,
                    "draft_disposition": draft,
                    "confidence": result.get("confidence"),
                    "reason": str(result.get("reason") or "")[:1200],
                    "value_correct": value_correct,
                    "draft_correct": draft_correct,
                    "strict_correct": value_correct and draft_correct,
                    "resolved_model_version": result.get("resolved_model_version"),
                }
            rows.append({
                "id": sample.get("id"),
                "title": sample.get("title"),
                "split": sample.get("split"),
                "human_label": truth_value,
                "human_draft_disposition": truth_draft,
                "baseline": evaluation(baseline_result),
                "candidate": evaluation(candidate_result),
            })
            job.shadow_evaluation = {
                "status": "running",
                "completed_samples": index,
                "total_samples": len(samples),
                "evaluation_scope": EVALUATION_SCOPE,
                "rows": rows,
            }
            job.stage = f"影子复跑中：{index}/{len(samples)} 条"
            db.add(job)
            db.commit()
        comparison = _shadow_comparison(rows)
        job.status = "shadow_rerun_review_pending"
        job.stage = "新旧 Prompt 影子复跑完成，等待人工验收"
        job.shadow_evaluation = {
            "status": "completed",
            "evaluation_scope": EVALUATION_SCOPE,
            "baseline_prompt_version": CURRENT_REVIEW_PROMPT_VERSION,
            "candidate_prompt_version": "candidate-from-" + job.id,
            "rows": rows,
            "comparison": comparison,
        }
        job.error_message = (
            "shadow_only：复跑结果仅用于人工验收，不自动启用候选 Prompt，"
            "不改变生产路由或模型权重。"
        )
        db.add(job)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if job:
            job.status = "shadow_rerun_failed"
            job.stage = "候选 Prompt 影子复跑异常中断"
            job.error_message = f"影子复跑异常：{type(exc).__name__}"
            db.add(job)
            db.commit()
    finally:
        db.close()


def recover_confidence_shadow_reruns_after_startup() -> None:
    """Return unfinished local work to the durable queue after a restart."""
    db = SessionLocal()
    try:
        jobs = (
            db.query(ConfidenceTrainingJob)
            .filter(ConfidenceTrainingJob.status == "shadow_rerun_running")
            .all()
        )
        for job in jobs:
            job.status = "shadow_rerun_queued"
            job.stage = "本地服务重启后已恢复排队，将从已完成样本继续"
            evaluation = dict(job.shadow_evaluation or {})
            evaluation["status"] = "queued"
            job.shadow_evaluation = evaluation
            job.error_message = ""
            db.add(job)
        if jobs:
            db.commit()
    finally:
        db.close()


async def run_confidence_shadow_rerun_worker(stop_event: asyncio.Event) -> None:
    """Process one durable shadow rerun at a time without tying it to HTTP."""
    recover_confidence_shadow_reruns_after_startup()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            job = (
                db.query(ConfidenceTrainingJob)
                .filter(ConfidenceTrainingJob.status == "shadow_rerun_queued")
                .order_by(ConfidenceTrainingJob.created_at)
                .first()
            )
            if job:
                job_id = job.id
                job.status = "shadow_rerun_running"
                job.stage = "候选 Prompt 影子复跑启动中"
                evaluation = dict(job.shadow_evaluation or {})
                evaluation["status"] = "running"
                job.shadow_evaluation = evaluation
                db.add(job)
                db.commit()
                await asyncio.to_thread(_run_shadow_rerun, job_id)
                continue
        finally:
            db.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


def _run_prompt_optimization_analysis(job_id: str) -> None:
    """Run the slow internal-model call after the create response is returned."""
    db = SessionLocal()
    try:
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if not job:
            return
        try:
            analysis = analyze_confidence_training_prompt(
                _prompt_optimization_snapshot(list(job.dataset_payload or []))
            )
            job.status = "prompt_review_pending"
            job.stage = "DeepSeek-flash 已完成错误分析，等待人工审核候选提示词"
            job.analysis_result = analysis
            job.candidate_prompt = str(analysis.get("candidate_prompt") or "")
            job.resolved_model_version = str(
                analysis.get("resolved_model_version") or analysis.get("model_name") or ""
            )
            job.error_message = (
                "shadow_only：候选提示词仅用于后续影子复跑，未修改当前生产提示词或模型权重。"
            )
            job.prompt_versions = [{
                "version": "v1-candidate",
                "prompt": job.candidate_prompt,
                "status": "diagnostic_pending",
                "source": "initial_error_analysis",
                "created_at": datetime.utcnow().isoformat(),
                "diagnostic_job_id": job.id,
            }]
        except ModelAnnotationError as exc:
            job.status = "analysis_failed"
            job.stage = "DeepSeek-flash 提示词分析失败"
            job.error_message = str(exc)
        db.add(job)
        db.commit()
    finally:
        db.close()


@router.get("/overview", response_model=ConfidenceTrainingOverview)
def confidence_training_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    items = [item_payload(row) for row in _rows(db)]
    overview, bands = aggregate(items)
    overview["bands"] = bands
    overview["settings"] = settings_snapshot(db)
    return overview


@router.get("/items", response_model=list[ConfidenceTrainingItem])
def list_confidence_training_items(
    batch_id: str = Query("", max_length=128),
    confidence_band: str = Query("", max_length=8),
    annotation_status: str = Query("", pattern="^(|pending|annotated)$"),
    training_status: str = Query("", max_length=32),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    items = [item_payload(row) for row in _rows(db)]
    if batch_id.strip():
        items = [item for item in items if batch_id.strip() in item["event_id"]]
    if confidence_band.strip():
        items = [item for item in items if item["confidence_band"] == confidence_band.strip()]
    if annotation_status == "annotated":
        items = [item for item in items if item.get("human_label")]
    elif annotation_status == "pending":
        items = [item for item in items if not item.get("human_label")]
    if training_status.strip():
        items = [item for item in items if item["training_candidate_status"] == training_status.strip()]
    return items[:limit]


@router.patch("/items/{ingestion_id}", response_model=ConfidenceTrainingItem)
def update_confidence_training_item(
    ingestion_id: str,
    body: ConfidenceTrainingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    item = (
        db.query(IntegrationIngestion)
        .filter(IntegrationIngestion.id == ingestion_id)
        .first()
    )
    if not item:
        raise HTTPException(404, "候选评测记录不存在。")
    if body.human_label is not None or body.truth_status is not None:
        raise HTTPException(409, "人工真值由候选价值复核产生，不能在置信度评测页重复输入。")
    values = body.model_dump(exclude_unset=True)
    values["truth_reviewer"] = current_user.username
    values["truth_reviewed_at"] = __import__("datetime").datetime.utcnow().isoformat()
    payload = update_item(item, values)
    db.add(item)
    db.commit()
    db.refresh(item)
    return payload


@router.post("/items/{ingestion_id}/correct")
def correct_confidence_training_item(
    ingestion_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    """Run a shadow correction while preserving the initial model output and human truth."""
    item = (
        db.query(IntegrationIngestion)
        .filter(IntegrationIngestion.id == ingestion_id)
        .first()
    )
    if not item:
        raise HTTPException(404, "候选评测记录不存在。")
    metadata = dict(item.review_metadata or {})
    evaluation = dict(metadata.get("confidence_training") or {})
    human = dict((item.candidate_payload or {}).get("human_review") or {})
    human.update(dict(metadata.get("human_review") or {}))
    human_label = human.get("knowledge_value")
    if not human_label:
        raise HTTPException(409, "请先完成并确认人工真值，再让 DeepSeek-flash 纠错。")
    model_review = dict((item.candidate_payload or {}).get("model_review") or metadata.get("model_review") or {})
    candidate = {
        "title": str((item.candidate_payload or {}).get("knowledge", {}).get("title") or ""),
        "draft": {
            "content": (item.candidate_payload or {}).get("knowledge", {}).get("content"),
            "recommended_reply": (item.candidate_payload or {}).get("knowledge", {}).get("recommended_reply"),
        },
        "evidence_excerpt": str((item.candidate_payload or {}).get("knowledge", {}).get("evidence_excerpt") or ""),
        "model_review": model_review,
        "human_truth": {
            "knowledge_value": human_label,
            "draft_disposition": human.get("draft_disposition"),
        },
        "review_note": str(evaluation.get("review_note") or ""),
    }
    try:
        corrected = correct_with_deepseek_flash(candidate)
    except ModelAnnotationError as exc:
        raise HTTPException(503, str(exc)) from exc
    history = list(evaluation.get("correction_history") or [])
    round_number = len(history) + 1
    history.append({
        "correction_round": round_number,
        "model_name": corrected.get("model_name", "deepseek-flash"),
        "prompt_version": corrected.get("prompt_version"),
        "result": corrected,
        "requested_by": current_user.username,
    })
    evaluation.update({
        "correction_round": round_number,
        "correction_status": "pending_human_confirmation",
        "corrected_model_review": corrected,
        "correction_history": history,
    })
    metadata["confidence_training"] = evaluation
    item.review_metadata = metadata
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "status": "pending_human_confirmation",
        "model": corrected.get("model_name", "deepseek-flash"),
        "correction_round": round_number,
        "corrected_model_review": corrected,
        "item": item_payload(item),
    }


@router.get("/settings", response_model=ConfidenceTrainingSettings)
def get_confidence_training_settings(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    return settings_snapshot(db)


@router.patch("/settings", response_model=ConfidenceTrainingSettings)
def patch_confidence_training_settings(
    body: ConfidenceTrainingSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    return update_settings(
        db,
        body.model_dump(),
        updated_by=current_user.username,
    )


@router.post("/model-connection-test")
def test_confidence_training_model_connection(
    _: User = Depends(require_permission("knowledge:submit")),
):
    """Use a fixed non-business payload to verify the internal model route."""
    try:
        result = test_deepseek_flash_connection()
    except ModelAnnotationError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "status": "connected",
        "requested_model": result.get("requested_model"),
        "resolved_model_version": result.get("resolved_model_version"),
        "prompt_version": result.get("prompt_version"),
    }


@router.get("/jobs", response_model=list[ConfidenceTrainingJobSchema])
def list_confidence_training_jobs(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    jobs = (
        db.query(ConfidenceTrainingJob)
        .order_by(ConfidenceTrainingJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [_training_job_payload(job) for job in jobs]


@router.post("/jobs", response_model=ConfidenceTrainingJobSchema)
def create_confidence_training_job(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    if not settings_snapshot(db)["training_candidate_collection_enabled"]:
        raise HTTPException(409, "请先开启“允许收集训练候选”，再人工发起训练任务。")
    dataset, counts = _manual_training_dataset(_rows(db))
    if len(dataset) < 3:
        raise HTTPException(
            422,
            "至少需要 3 条已确认的人工真值，才能生成独立训练集、验证集和测试集。",
        )
    serialized = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    job = ConfidenceTrainingJob(
        id=f"ctj-{uuid.uuid4().hex[:16]}",
        status="analyzing_errors",
        stage="DeepSeek-flash 正在分析人工真值与模型错误",
        model_name=MODEL_NAME,
        evaluation_scope=EVALUATION_SCOPE,
        dataset_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        dataset_payload=dataset,
        sample_count=len(dataset),
        train_count=counts["train"],
        validation_count=counts["validation"],
        test_count=counts["test"],
        requested_by=current_user.username,
        error_message="",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(_run_prompt_optimization_analysis, job.id)
    return _training_job_payload(job)


@router.post("/jobs/{job_id}/shadow-rerun", response_model=ConfidenceTrainingJobSchema)
def start_confidence_training_shadow_rerun(
    job_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    if job.status not in {
        "prompt_review_pending",
        "shadow_rerun_interrupted",
        "shadow_rerun_failed",
    } or not job.candidate_prompt:
        raise HTTPException(409, "当前任务没有可继续的候选 Prompt 影子复跑。")
    job.status = "shadow_rerun_queued"
    job.stage = "候选 Prompt 影子复跑排队中"
    previous_rows = list((job.shadow_evaluation or {}).get("rows") or [])
    job.shadow_evaluation = {
        "status": "queued",
        "completed_samples": 0,
        "total_samples": len(job.dataset_payload or []),
        "evaluation_scope": EVALUATION_SCOPE,
        "resumed_rows": len(previous_rows),
        "rows": previous_rows,
    }
    db.add(job)
    db.commit()
    db.refresh(job)
    return _training_job_payload(job)


@router.get("/jobs/{job_id}/regressions")
def list_confidence_training_regressions(
    job_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    return {
        "job_id": job.id,
        "rows": _regressed_rows(job),
        "reviews": dict(job.regression_reviews or {}),
        "next_action": "review_regressions",
    }


@router.patch("/jobs/{job_id}/regressions/{ingestion_id}")
def review_confidence_training_regression(
    job_id: str,
    ingestion_id: str,
    body: ConfidenceRegressionReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    if (job.shadow_evaluation or {}).get("status") != "completed":
        raise HTTPException(409, "只有影子复跑完成后才能复核退化样本。")
    row = next((item for item in _regressed_rows(job) if str(item.get("id")) == ingestion_id), None)
    if not row:
        raise HTTPException(404, "该样本不在当前候选 Prompt 的退化集合中。")
    reviews = dict(job.regression_reviews or {})
    reviews[ingestion_id] = {
        "classification": body.classification,
        "note": body.note.strip(),
        "keep_as_regression_case": body.keep_as_regression_case,
        "reviewed_by": current_user.username,
        "reviewed_at": datetime.utcnow().isoformat(),
        "next_action": "candidate_review" if body.classification == "human_truth_correction_required" else "prompt_revision",
    }
    job.regression_reviews = reviews
    job.stage = (
        "退化样本已复核，需回候选价值复核修正真值"
        if body.classification == "human_truth_correction_required"
        else "退化样本已复核，可生成下一版候选 Prompt"
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _training_job_payload(job)


def _run_prompt_revision(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if not job:
            return
        try:
            result = revise_confidence_training_prompt(_prompt_revision_snapshot(job))
            revision = {
                "version": "v1.1-candidate",
                "prompt": str(result.get("candidate_prompt") or ""),
                "status": "validation_pending",
                "source": "regression_review",
                "created_at": datetime.utcnow().isoformat(),
                "diagnostic_job_id": job.id,
                "analysis_summary": result.get("analysis_summary"),
                "prompt_changes": result.get("prompt_changes") or [],
                "expected_risks": result.get("expected_risks") or [],
                "recommended_next_action": result.get("recommended_next_action") or "validation_shadow_rerun",
            }
            versions = list(job.prompt_versions or [])
            for old in versions:
                if old.get("version") == "v1-candidate" and old.get("status") == "diagnostic_pending":
                    old["status"] = "diagnostic_failed"
                    comparison = dict((job.shadow_evaluation or {}).get("comparison") or {})
                    old["evaluation_summary"] = {
                        "improved_count": comparison.get("improved_count", 0),
                        "regressed_count": comparison.get("regressed_count", 0),
                        "gate_passed": comparison.get("gate_passed", False),
                    }
            versions.append(revision)
            job.prompt_versions = versions
            job.status = "validation_pending"
            job.stage = "v1.1 候选 Prompt 已生成，等待验证集影子复跑"
            job.error_message = "shadow_only：v1.1 仍未启用，下一步只允许 validation/test 分阶段影子验收。"
            job.resolved_model_version = str(result.get("resolved_model_version") or job.resolved_model_version or "")
        except ModelAnnotationError as exc:
            job.status = "prompt_revision_failed"
            job.stage = "v1.1 候选 Prompt 生成失败"
            job.error_message = str(exc)
        db.add(job)
        db.commit()
    finally:
        db.close()


@router.post("/jobs/{job_id}/prompt-revisions", response_model=ConfidenceTrainingJobSchema)
def create_confidence_prompt_revision(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    ok, reason = _revision_preconditions(job)
    if not ok:
        raise HTTPException(409, reason)
    if job.status == "prompt_revision_generating":
        return _training_job_payload(job)
    job.status = "prompt_revision_generating"
    job.stage = "DeepSeek-flash 正在根据退化归因生成 v1.1 候选 Prompt"
    job.error_message = ""
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(_run_prompt_revision, job.id)
    return _training_job_payload(job)


@router.get("/export")
def export_confidence_training(
    format: str = Query("evaluation", pattern="^(review|evaluation|training|errors)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    items = [item_payload(row) for row in _rows(db)]
    if format == "training":
        rows: list[dict[str, Any]] = [
            {
                "id": item["id"],
                "input": item["title"],
                "model_label": item["model_label"],
                "human_label": item["human_label"],
                "dataset_split": item["dataset_split"] or "train",
            }
            for item in items
            if item.get("training_candidate_status") in {"recommended", "qualified"}
            and item.get("truth_status") == "confirmed"
        ]
        content = "\n".join(json.dumps({**row, "evaluation_scope": EVALUATION_SCOPE, "not_for_weight_training": True}, ensure_ascii=False) for row in rows) + ("\n" if rows else "")
        return Response(content, media_type="application/jsonl", headers={"Content-Disposition": 'attachment; filename="shadow-training-candidates.jsonl"'})
    if format == "errors":
        items = [item for item in items if item.get("model_correctness") == "wrong" or item.get("error_severity")]
    if format == "evaluation":
        overview, bands = aggregate(items)
        payload: Any = {"notice": "shadow_only; 不改变生产路由、不进入正式训练真值", "overview": overview, "bands": bands, "items": items}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="confidence-evaluation-report.json"'})
    output = io.StringIO()
    fields = ["id", "event_id", "title", "model_label", "model_confidence", "confidence_band", "human_label", "truth_status", "model_correctness", "strict_correct", "acceptable_correct", "error_type", "error_severity", "training_candidate_status", "dataset_split", "review_status"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in items:
        writer.writerow({key: json.dumps(item.get(key), ensure_ascii=False) if isinstance(item.get(key), list) else item.get(key, "") for key in fields})
    filename = "confidence-review-workbook.csv" if format == "review" else "confidence-errors.csv"
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
