from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.embedding_admin import (
    EmbeddingModelVersion,
    EmbeddingRuntimeConfig,
    EmbeddingTrainingJob,
    EmbeddingTrainingRunner,
    EmbeddingTrainingSample,
)
from app.models.integration import RetrievalQualityEvent
from app.models.knowledge import (
    Knowledge,
    KnowledgeEmbedding,
    KnowledgeSearchEmbedding,
    KnowledgeStatus,
)
from app.models.user import User
from app.routes.auth import require_permission
from app.schemas.embedding_admin import (
    EmbeddingLabSearchRequest,
    EmbeddingModelDecision,
    EmbeddingRunnerClaim,
    EmbeddingRunnerComplete,
    EmbeddingRunnerFailure,
    EmbeddingRunnerHeartbeat,
    EmbeddingRunnerProgress,
    EmbeddingRuntimeConfigCreate,
    EmbeddingTrainingJobCreate,
    EmbeddingTrainingSampleCreate,
    EmbeddingTrainingSampleUpdate,
    RetrievalQualityReview,
)
from app.services.embedding import EmbeddingServiceUnavailable, embed_texts
from app.services.embedding_admin import (
    activate_runtime_config,
    build_training_dataset,
    import_deduplication_samples,
    import_retrieval_samples,
    next_runtime_config_version,
)
from app.services.embedding_runtime import (
    default_embedding_runtime_config,
    get_active_runtime_record,
    get_active_runtime_values,
)


router = APIRouter(prefix="/embedding-model", tags=["向量模型管理"])
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,255}$")
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _runner_token(
    token: str = Header(default="", alias="X-Embedding-Runner-Token"),
) -> None:
    configured = settings.EMBEDDING_TRAINING_RUNNER_TOKEN.strip()
    if len(configured) < 24:
        raise HTTPException(503, "本地 GPU Runner 密钥尚未配置")
    if not token or not hmac.compare_digest(token, configured):
        raise HTTPException(401, "GPU Runner authentication failed")


def _embedding_health_url() -> str:
    configured = settings.EMBEDDING_HEALTHCHECK_URL.strip()
    if configured:
        return configured
    base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
    base_url = base_url.removesuffix("/v1")
    return f"{base_url}/health"


def _health_snapshot() -> dict[str, Any]:
    started = datetime.utcnow()
    try:
        response = httpx.get(_embedding_health_url(), timeout=3.0)
        response.raise_for_status()
        return {
            "status": "healthy",
            "latency_ms": round((datetime.utcnow() - started).total_seconds() * 1000, 2),
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "latency_ms": None,
            "error": str(exc)[:300],
        }


def _config_payload(record: EmbeddingRuntimeConfig | None) -> dict[str, Any]:
    if not record:
        return {
            "id": "environment-default",
            "version": 0,
            "status": "environment_default",
            "config": default_embedding_runtime_config(),
            "evaluation_metrics": {},
            "change_reason": "当前使用环境变量默认值",
            "created_by": "system",
            "activated_by": None,
            "created_at": None,
            "activated_at": None,
        }
    return {
        "id": record.id,
        "version": record.version,
        "status": record.status,
        "config": record.config or {},
        "evaluation_metrics": record.evaluation_metrics or {},
        "change_reason": record.change_reason,
        "created_by": record.created_by,
        "activated_by": record.activated_by,
        "created_at": _iso(record.created_at),
        "activated_at": _iso(record.activated_at),
    }


def _sample_payload(sample: EmbeddingTrainingSample) -> dict[str, Any]:
    return {
        "id": sample.id,
        "task_type": sample.task_type,
        "query_text": sample.query_text,
        "positive_text": sample.positive_text,
        "negative_texts": sample.negative_texts or [],
        "source_type": sample.source_type,
        "source_id": sample.source_id,
        "status": sample.status,
        "reason": sample.reason,
        "metadata": sample.sample_metadata or {},
        "created_by": sample.created_by,
        "reviewed_by": sample.reviewed_by,
        "created_at": _iso(sample.created_at),
        "updated_at": _iso(sample.updated_at),
        "reviewed_at": _iso(sample.reviewed_at),
    }


def _job_payload(job: EmbeddingTrainingJob, *, include_dataset: bool = False) -> dict[str, Any]:
    payload = {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "progress": round(float(job.progress or 0.0), 2),
        "base_model": job.base_model,
        "candidate_model_name": job.candidate_model_name,
        "train_type": job.train_type,
        "training_config": job.training_config or {},
        "dataset_hash": job.dataset_hash,
        "sample_count": job.sample_count,
        "train_count": job.train_count,
        "validation_count": job.validation_count,
        "test_count": job.test_count,
        "metrics": job.metrics or {},
        "log_tail": job.log_tail,
        "artifact_uri": job.artifact_uri,
        "artifact_sha256": job.artifact_sha256,
        "runner_id": job.runner_id,
        "requested_by": job.requested_by,
        "error_message": job.error_message,
        "lease_expires_at": _iso(job.lease_expires_at),
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
    }
    if include_dataset:
        payload["dataset"] = job.dataset_payload or []
    return payload


def _runner_payload(runner: EmbeddingTrainingRunner | None) -> dict[str, Any] | None:
    if not runner:
        return None
    offline_before = datetime.utcnow() - timedelta(
        seconds=max(30, settings.EMBEDDING_TRAINING_RUNNER_OFFLINE_SECONDS)
    )
    status = runner.status
    if runner.last_seen_at < offline_before:
        status = "offline"
    return {
        "id": runner.id,
        "name": runner.name,
        "hostname": runner.hostname,
        "status": status,
        "gpu_name": runner.gpu_name,
        "gpu_memory_mb": runner.gpu_memory_mb,
        "gpu_free_memory_mb": runner.gpu_free_memory_mb,
        "cuda_version": runner.cuda_version,
        "runner_version": runner.runner_version,
        "current_job_id": runner.current_job_id,
        "metadata": runner.runner_metadata or {},
        "last_seen_at": _iso(runner.last_seen_at),
    }


def _model_payload(model: EmbeddingModelVersion) -> dict[str, Any]:
    return {
        "id": model.id,
        "model_name": model.model_name,
        "base_model": model.base_model,
        "training_job_id": model.training_job_id,
        "status": model.status,
        "dimension": model.dimension,
        "metrics": model.metrics or {},
        "artifact_uri": model.artifact_uri,
        "artifact_sha256": model.artifact_sha256,
        "release_notes": model.release_notes,
        "approved_by": model.approved_by,
        "approved_at": _iso(model.approved_at),
        "deployed_at": _iso(model.deployed_at),
        "created_at": _iso(model.created_at),
        "updated_at": _iso(model.updated_at),
    }


@router.get("/overview")
def get_embedding_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("embedding:manage")),
):
    published_query = db.query(Knowledge.id).filter(
        Knowledge.status == KnowledgeStatus.PUBLISHED
    )
    published_total = published_query.count()
    dedup_covered = (
        db.query(func.count(func.distinct(KnowledgeEmbedding.knowledge_id)))
        .join(Knowledge, Knowledge.id == KnowledgeEmbedding.knowledge_id)
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            KnowledgeEmbedding.embedding_model == settings.EMBEDDING_MODEL,
            KnowledgeEmbedding.embedding_vector.is_not(None),
        )
        .scalar()
        or 0
    )
    search_covered = (
        db.query(func.count(func.distinct(KnowledgeSearchEmbedding.knowledge_id)))
        .join(Knowledge, Knowledge.id == KnowledgeSearchEmbedding.knowledge_id)
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            KnowledgeSearchEmbedding.embedding_model == settings.EMBEDDING_MODEL,
            KnowledgeSearchEmbedding.embedding_vector.is_not(None),
        )
        .scalar()
        or 0
    )
    sample_counts = {
        status: total
        for status, total in (
            db.query(
                EmbeddingTrainingSample.status,
                func.count(EmbeddingTrainingSample.id),
            )
            .group_by(EmbeddingTrainingSample.status)
            .all()
        )
    }
    job_counts = {
        status: total
        for status, total in (
            db.query(
                EmbeddingTrainingJob.status,
                func.count(EmbeddingTrainingJob.id),
            )
            .group_by(EmbeddingTrainingJob.status)
            .all()
        )
    }
    runner = (
        db.query(EmbeddingTrainingRunner)
        .order_by(EmbeddingTrainingRunner.last_seen_at.desc())
        .first()
    )
    latest_job = (
        db.query(EmbeddingTrainingJob)
        .order_by(EmbeddingTrainingJob.created_at.desc())
        .first()
    )
    active_config = get_active_runtime_record(db)
    return {
        "model": {
            "name": settings.EMBEDDING_MODEL,
            "dimension": settings.EMBEDDING_DIMENSIONS,
            "provider": settings.EMBEDDING_PROVIDER,
            "base_url": settings.EMBEDDING_BASE_URL,
            "health": _health_snapshot(),
        },
        "vectors": {
            "published_total": published_total,
            "dedup_covered": dedup_covered,
            "search_covered": search_covered,
            "dedup_missing": max(0, published_total - dedup_covered),
            "search_missing": max(0, published_total - search_covered),
            "dedup_coverage_rate": (
                round(dedup_covered / published_total, 4) if published_total else 0
            ),
            "search_coverage_rate": (
                round(search_covered / published_total, 4) if published_total else 0
            ),
        },
        "samples": {
            "candidate": int(sample_counts.get("candidate", 0)),
            "verified": int(sample_counts.get("verified", 0)),
            "excluded": int(sample_counts.get("excluded", 0)),
        },
        "jobs": {
            "queued": int(job_counts.get("queued", 0)),
            "active": sum(
                int(job_counts.get(status, 0))
                for status in ("claimed", "running", "evaluating")
            ),
            "completed": int(job_counts.get("completed", 0)),
            "failed": int(job_counts.get("failed", 0)),
            "latest": _job_payload(latest_job) if latest_job else None,
        },
        "runner": _runner_payload(runner),
        "runner_configured": len(settings.EMBEDDING_TRAINING_RUNNER_TOKEN.strip()) >= 24,
        "active_config": _config_payload(active_config),
        "updated_at": datetime.utcnow().isoformat(),
    }


@router.get("/configs")
def list_embedding_configs(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("embedding:manage")),
):
    records = (
        db.query(EmbeddingRuntimeConfig)
        .order_by(EmbeddingRuntimeConfig.version.desc())
        .limit(50)
        .all()
    )
    return {
        "active": _config_payload(get_active_runtime_record(db)),
        "items": [_config_payload(record) for record in records],
    }


@router.post("/configs")
def create_embedding_config(
    body: EmbeddingRuntimeConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    record = EmbeddingRuntimeConfig(
        id=f"erc-{uuid.uuid4().hex[:16]}",
        version=next_runtime_config_version(db),
        status="draft",
        config=body.config.model_dump(),
        evaluation_metrics=body.evaluation_metrics,
        change_reason=body.change_reason.strip(),
        created_by=current_user.username,
    )
    db.add(record)
    activation_blocked = ""
    if body.activate:
        active_values = get_active_runtime_values(db)
        structural_keys = {"search_chunk_size", "search_chunk_overlap"}
        structural_changed = any(
            active_values.get(key) != record.config.get(key)
            for key in structural_keys
        )
        if structural_changed and not body.evaluation_metrics.get(
            "vector_rebuild_completed"
        ):
            activation_blocked = (
                "分块参数变化后必须先完成候选向量重建，当前配置已保留为草稿"
            )
        else:
            activate_runtime_config(db, record, username=current_user.username)
    db.commit()
    db.refresh(record)
    payload = _config_payload(record)
    if activation_blocked:
        payload["activation_blocked"] = True
        payload["message"] = activation_blocked
    return payload


@router.post("/configs/{config_id}/activate")
def activate_embedding_config(
    config_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    record = (
        db.query(EmbeddingRuntimeConfig)
        .filter(EmbeddingRuntimeConfig.id == config_id)
        .first()
    )
    if not record:
        raise HTTPException(404, "参数版本不存在")
    active_values = get_active_runtime_values(db)
    structural_changed = any(
        active_values.get(key) != (record.config or {}).get(key)
        for key in ("search_chunk_size", "search_chunk_overlap")
    )
    if structural_changed and not (record.evaluation_metrics or {}).get(
        "vector_rebuild_completed"
    ):
        raise HTTPException(409, "该版本改变了分块参数，必须先完成候选向量重建")
    activate_runtime_config(db, record, username=current_user.username)
    db.commit()
    db.refresh(record)
    return _config_payload(record)


@router.post("/lab/search")
def embedding_lab_search(
    body: EmbeddingLabSearchRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("embedding:manage")),
):
    try:
        query_vector = embed_texts([body.query.strip()])[0]
    except EmbeddingServiceUnavailable as exc:
        raise HTTPException(503, "Embedding 服务不可用，无法运行召回实验") from exc

    distance = KnowledgeSearchEmbedding.embedding_vector.cosine_distance(query_vector)
    query = (
        db.query(
            Knowledge,
            KnowledgeSearchEmbedding,
            distance.label("distance"),
        )
        .join(
            KnowledgeSearchEmbedding,
            KnowledgeSearchEmbedding.knowledge_id == Knowledge.id,
        )
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            KnowledgeSearchEmbedding.embedding_model == settings.EMBEDDING_MODEL,
            KnowledgeSearchEmbedding.embedding_vector.is_not(None),
        )
    )
    if body.knowledge_origin:
        query = query.filter(Knowledge.knowledge_origin == body.knowledge_origin)
    if body.business_type:
        query = query.filter(Knowledge.business_type == body.business_type)
    if body.category_id:
        query = query.filter(Knowledge.category_id == body.category_id)
    rows = query.order_by(distance).limit(body.top_k).all()
    return {
        "query": body.query,
        "model": settings.EMBEDDING_MODEL,
        "top_k": body.top_k,
        "results": [
            {
                "rank": index,
                "knowledge_id": item.id,
                "title": item.title,
                "knowledge_origin": item.knowledge_origin,
                "business_type": item.business_type,
                "category_id": item.category_id,
                "embedding_kind": embedding.embedding_kind,
                "chunk_index": embedding.chunk_index,
                "source_text": embedding.source_text,
                "score": round(max(0.0, 1.0 - float(distance_value)), 6),
            }
            for index, (item, embedding, distance_value) in enumerate(rows, start=1)
        ],
    }


@router.get("/samples")
def list_training_samples(
    status: str | None = Query(None),
    task_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("embedding:manage")),
):
    query = db.query(EmbeddingTrainingSample)
    if status:
        query = query.filter(EmbeddingTrainingSample.status == status)
    if task_type:
        query = query.filter(EmbeddingTrainingSample.task_type == task_type)
    total = query.count()
    items = (
        query.order_by(EmbeddingTrainingSample.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_sample_payload(item) for item in items],
    }


@router.post("/samples")
def create_training_sample(
    body: EmbeddingTrainingSampleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    now = datetime.utcnow()
    sample = EmbeddingTrainingSample(
        id=f"ets-{uuid.uuid4().hex[:16]}",
        task_type=body.task_type,
        query_text=body.query_text.strip(),
        positive_text=body.positive_text.strip(),
        negative_texts=body.negative_texts,
        source_type=body.source_type,
        source_id=body.source_id,
        status=body.status,
        reason=body.reason.strip(),
        sample_metadata=body.metadata,
        created_by=current_user.username,
        reviewed_by=current_user.username if body.status != "candidate" else None,
        reviewed_at=now if body.status != "candidate" else None,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return _sample_payload(sample)


@router.patch("/samples/{sample_id}")
def update_training_sample(
    sample_id: str,
    body: EmbeddingTrainingSampleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    sample = (
        db.query(EmbeddingTrainingSample)
        .filter(EmbeddingTrainingSample.id == sample_id)
        .first()
    )
    if not sample:
        raise HTTPException(404, "训练样本不存在")
    changes = body.model_dump(exclude_unset=True)
    if "metadata" in changes:
        sample.sample_metadata = changes.pop("metadata") or {}
    for key, value in changes.items():
        if key in {"query_text", "positive_text", "reason"} and isinstance(value, str):
            value = value.strip()
        setattr(sample, key, value)
    if sample.status == "verified":
        if not sample.positive_text.strip():
            raise HTTPException(422, "已确认样本必须填写正确知识内容")
        sample.reviewed_by = current_user.username
        sample.reviewed_at = datetime.utcnow()
    elif sample.status == "excluded":
        sample.reviewed_by = current_user.username
        sample.reviewed_at = datetime.utcnow()
    else:
        sample.reviewed_by = None
        sample.reviewed_at = None
    db.commit()
    db.refresh(sample)
    return _sample_payload(sample)


@router.post("/samples/import-sources")
def import_training_sample_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    retrieval = import_retrieval_samples(db, created_by=current_user.username)
    deduplication = import_deduplication_samples(
        db,
        created_by=current_user.username,
    )
    return {
        "retrieval_imported": retrieval,
        "deduplication_imported": deduplication,
        "total_imported": retrieval + deduplication,
    }


@router.post("/retrieval-events/{event_id}/review")
def review_retrieval_event(
    event_id: str,
    body: RetrievalQualityReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    event = (
        db.query(RetrievalQualityEvent)
        .filter(RetrievalQualityEvent.id == event_id)
        .first()
    )
    if not event:
        raise HTTPException(404, "召回事件不存在")
    if body.expected_knowledge_id:
        expected = (
            db.query(Knowledge)
            .filter(
                Knowledge.id == body.expected_knowledge_id,
                Knowledge.status == KnowledgeStatus.PUBLISHED,
            )
            .first()
        )
        if not expected:
            raise HTTPException(422, "指定的正确知识不存在或尚未发布")
    event.review_status = body.review_status
    event.expected_knowledge_id = body.expected_knowledge_id
    event.feedback_type = body.feedback_type
    event.failure_reason = body.failure_reason
    event.training_eligible = body.training_eligible
    event.reviewed_by = current_user.username
    event.reviewed_at = datetime.utcnow()
    metadata = dict(event.event_metadata or {})
    metadata["human_review"] = {
        "reason": body.reason.strip(),
        "reviewed_by": current_user.username,
        "reviewed_at": event.reviewed_at.isoformat(),
    }
    event.event_metadata = metadata
    db.commit()
    return {
        "status": "recorded",
        "event_id": event.id,
        "review_status": event.review_status,
        "training_eligible": event.training_eligible,
    }


@router.get("/jobs")
def list_training_jobs(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("embedding:manage")),
):
    jobs = (
        db.query(EmbeddingTrainingJob)
        .order_by(EmbeddingTrainingJob.created_at.desc())
        .limit(50)
        .all()
    )
    models = (
        db.query(EmbeddingModelVersion)
        .order_by(EmbeddingModelVersion.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "jobs": [_job_payload(job) for job in jobs],
        "models": [_model_payload(model) for model in models],
    }


@router.post("/jobs")
def create_training_job(
    body: EmbeddingTrainingJobCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    runtime = get_active_runtime_values(db)
    samples = (
        db.query(EmbeddingTrainingSample)
        .filter(
            EmbeddingTrainingSample.status == "verified",
            EmbeddingTrainingSample.task_type.in_(body.include_task_types),
        )
        .order_by(EmbeddingTrainingSample.created_at)
        .all()
    )
    minimum = int(runtime["training_min_verified_samples"])
    if len(samples) < minimum:
        raise HTTPException(
            422,
            {
                "message": "已确认训练样本不足",
                "verified_samples": len(samples),
                "minimum_required": minimum,
            },
        )
    dataset, dataset_hash, counts = build_training_dataset(samples)
    if counts["validation"] == 0 or counts["test"] == 0:
        raise HTTPException(422, "样本分布不足以生成独立验证集和测试集")
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    candidate_model_name = (
        body.candidate_model_name or f"kb-qwen3-embedding-0.6b-{timestamp}"
    ).strip()
    if not _MODEL_NAME_PATTERN.fullmatch(candidate_model_name):
        raise HTTPException(422, "候选模型名称只允许字母、数字、点、横线、下划线和斜线")
    if (
        db.query(EmbeddingTrainingJob)
        .filter(EmbeddingTrainingJob.candidate_model_name == candidate_model_name)
        .first()
    ):
        raise HTTPException(409, "候选模型名称已存在")
    default_training_config = {
        "low_memory_mode": True,
        "quant_method": "bnb",
        "quant_bits": 4,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "max_length": 256,
        "num_train_epochs": 2,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": 16,
        "learning_rate": 0.0001 if body.train_type == "lora" else 0.000006,
        "lora_rank": 8,
        "lora_alpha": 16,
        "max_negative_samples": 2,
        "evaluation_batch_size": 1,
        "min_free_gpu_memory_mb": 3200,
        "seed": 42,
        "dedup_block_threshold": runtime["dedup_block_threshold"],
        "minimum_recall_at_10": runtime["minimum_recall_at_10"],
        "maximum_false_block_rate": runtime["maximum_false_block_rate"],
    }
    default_training_config.update(body.training_config)
    job = EmbeddingTrainingJob(
        id=f"etj-{uuid.uuid4().hex[:16]}",
        status="queued",
        stage="等待本地 GPU Runner",
        progress=0.0,
        base_model=settings.EMBEDDING_MODEL,
        candidate_model_name=candidate_model_name,
        train_type=body.train_type,
        training_config=default_training_config,
        dataset_hash=dataset_hash,
        dataset_payload=dataset,
        sample_count=len(dataset),
        train_count=counts["train"],
        validation_count=counts["validation"],
        test_count=counts["test"],
        requested_by=current_user.username,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_payload(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_training_job(
    job_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("embedding:manage")),
):
    job = (
        db.query(EmbeddingTrainingJob)
        .filter(EmbeddingTrainingJob.id == job_id)
        .first()
    )
    if not job:
        raise HTTPException(404, "训练任务不存在")
    if job.status in _TERMINAL_JOB_STATUSES:
        return _job_payload(job)
    job.status = "cancelled"
    job.stage = "已取消"
    job.completed_at = datetime.utcnow()
    job.lease_expires_at = None
    runner = (
        db.query(EmbeddingTrainingRunner)
        .filter(EmbeddingTrainingRunner.id == job.runner_id)
        .first()
        if job.runner_id
        else None
    )
    if runner:
        runner.current_job_id = None
        runner.status = "online"
    db.commit()
    db.refresh(job)
    return _job_payload(job)


@router.post("/models/{model_id}/decision")
def decide_model_version(
    model_id: str,
    body: EmbeddingModelDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("embedding:manage")),
):
    model = (
        db.query(EmbeddingModelVersion)
        .filter(EmbeddingModelVersion.id == model_id)
        .first()
    )
    if not model:
        raise HTTPException(404, "候选模型不存在")
    if model.status not in {"candidate", "approved", "rejected"}:
        raise HTTPException(409, "当前模型状态不允许重新审批")
    # 候选审批只记录人工评审结论。不得在此上传模型、修改生产推理模型，
    # 或触发全量向量重建；这些操作必须分别取得用户的明确授权。
    model.status = "approved" if body.action == "approve" else "rejected"
    model.release_notes = body.release_notes.strip()
    model.approved_by = current_user.username
    model.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(model)
    return _model_payload(model)


def _upsert_runner(db: Session, body: EmbeddingRunnerHeartbeat) -> EmbeddingTrainingRunner:
    runner = (
        db.query(EmbeddingTrainingRunner)
        .filter(EmbeddingTrainingRunner.id == body.runner_id)
        .first()
    )
    if not runner:
        runner = EmbeddingTrainingRunner(
            id=body.runner_id,
            name=body.name,
        )
        db.add(runner)
    runner.name = body.name
    runner.hostname = body.hostname
    runner.status = body.status
    runner.gpu_name = body.gpu_name
    runner.gpu_memory_mb = body.gpu_memory_mb
    runner.gpu_free_memory_mb = body.gpu_free_memory_mb
    runner.cuda_version = body.cuda_version
    runner.runner_version = body.runner_version
    runner.current_job_id = body.current_job_id
    runner.runner_metadata = body.metadata
    runner.last_seen_at = datetime.utcnow()
    return runner


@router.post("/runner/heartbeat")
def runner_heartbeat(
    body: EmbeddingRunnerHeartbeat,
    db: Session = Depends(get_db),
    _: None = Depends(_runner_token),
):
    runner = _upsert_runner(db, body)
    db.commit()
    db.refresh(runner)
    return {"status": "ok", "runner": _runner_payload(runner)}


@router.post("/runner/claim")
def claim_training_job(
    body: EmbeddingRunnerClaim,
    db: Session = Depends(get_db),
    _: None = Depends(_runner_token),
):
    now = datetime.utcnow()
    expired_jobs = (
        db.query(EmbeddingTrainingJob)
        .filter(
            EmbeddingTrainingJob.status.in_(["claimed", "running", "evaluating"]),
            EmbeddingTrainingJob.lease_expires_at.is_not(None),
            EmbeddingTrainingJob.lease_expires_at < now,
        )
        .all()
    )
    for expired in expired_jobs:
        previous_runner_id = expired.runner_id
        expired.status = "queued"
        expired.stage = "Runner 离线，等待重新领取"
        expired.runner_id = None
        expired.lease_expires_at = None
        if previous_runner_id:
            previous_runner = (
                db.query(EmbeddingTrainingRunner)
                .filter(EmbeddingTrainingRunner.id == previous_runner_id)
                .first()
            )
            if previous_runner and previous_runner.current_job_id == expired.id:
                previous_runner.status = "offline"
                previous_runner.current_job_id = None

    job = (
        db.query(EmbeddingTrainingJob)
        .filter(EmbeddingTrainingJob.status == "queued")
        .order_by(EmbeddingTrainingJob.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    runner = (
        db.query(EmbeddingTrainingRunner)
        .filter(EmbeddingTrainingRunner.id == body.runner_id)
        .first()
    )
    if not runner:
        raise HTTPException(409, "请先发送 Runner 心跳")
    runner.last_seen_at = now
    if not job:
        runner.status = "online"
        runner.current_job_id = None
        db.commit()
        return {"job": None}
    lease_seconds = max(60, settings.EMBEDDING_TRAINING_JOB_LEASE_SECONDS)
    job.status = "claimed"
    job.stage = "Runner 已领取"
    job.runner_id = runner.id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    runner.status = "busy"
    runner.current_job_id = job.id
    db.commit()
    db.refresh(job)
    return {"job": _job_payload(job, include_dataset=True)}


def _owned_job(
    db: Session,
    job_id: str,
    runner_id: str,
) -> EmbeddingTrainingJob:
    job = (
        db.query(EmbeddingTrainingJob)
        .filter(EmbeddingTrainingJob.id == job_id)
        .first()
    )
    if not job:
        raise HTTPException(404, "训练任务不存在")
    if job.runner_id != runner_id:
        raise HTTPException(409, "训练任务不属于当前 Runner")
    if job.status in _TERMINAL_JOB_STATUSES:
        raise HTTPException(409, "训练任务已经结束")
    return job


@router.post("/runner/jobs/{job_id}/progress")
def update_training_progress(
    job_id: str,
    body: EmbeddingRunnerProgress,
    db: Session = Depends(get_db),
    _: None = Depends(_runner_token),
):
    job = _owned_job(db, job_id, body.runner_id)
    job.status = body.status
    job.stage = body.stage
    job.progress = body.progress
    job.log_tail = body.log_tail[-20000:]
    job.lease_expires_at = datetime.utcnow() + timedelta(seconds=body.lease_seconds)
    runner = (
        db.query(EmbeddingTrainingRunner)
        .filter(EmbeddingTrainingRunner.id == body.runner_id)
        .first()
    )
    if runner:
        runner.status = "busy"
        runner.current_job_id = job.id
        runner.last_seen_at = datetime.utcnow()
    db.commit()
    return {"status": "recorded"}


@router.post("/runner/jobs/{job_id}/complete")
def complete_training_job(
    job_id: str,
    body: EmbeddingRunnerComplete,
    db: Session = Depends(get_db),
    _: None = Depends(_runner_token),
):
    job = _owned_job(db, job_id, body.runner_id)
    if body.dimension != settings.EMBEDDING_DIMENSIONS:
        raise HTTPException(
            422,
            f"候选模型维度必须保持 {settings.EMBEDDING_DIMENSIONS}",
        )
    now = datetime.utcnow()
    job.status = "completed"
    job.stage = "训练与离线评估完成"
    job.progress = 100.0
    job.metrics = body.metrics
    job.artifact_uri = body.artifact_uri
    job.artifact_sha256 = body.artifact_sha256
    job.log_tail = body.log_tail[-20000:]
    job.error_message = ""
    job.lease_expires_at = None
    job.completed_at = now
    model = (
        db.query(EmbeddingModelVersion)
        .filter(EmbeddingModelVersion.training_job_id == job.id)
        .first()
    )
    if not model:
        model = EmbeddingModelVersion(
            id=f"emv-{uuid.uuid4().hex[:16]}",
            model_name=job.candidate_model_name,
            base_model=job.base_model,
            training_job_id=job.id,
        )
        db.add(model)
    model.status = "candidate"
    model.dimension = body.dimension
    model.metrics = body.metrics
    model.artifact_uri = body.artifact_uri
    model.artifact_sha256 = body.artifact_sha256
    runner = (
        db.query(EmbeddingTrainingRunner)
        .filter(EmbeddingTrainingRunner.id == body.runner_id)
        .first()
    )
    if runner:
        runner.status = "online"
        runner.current_job_id = None
        runner.last_seen_at = now
    db.commit()
    db.refresh(job)
    return {"status": "completed", "job": _job_payload(job)}


@router.post("/runner/jobs/{job_id}/fail")
def fail_training_job(
    job_id: str,
    body: EmbeddingRunnerFailure,
    db: Session = Depends(get_db),
    _: None = Depends(_runner_token),
):
    job = _owned_job(db, job_id, body.runner_id)
    job.error_message = body.error_message
    job.log_tail = body.log_tail[-20000:]
    job.lease_expires_at = None
    if body.retryable:
        job.status = "queued"
        job.stage = "训练失败，等待重试"
        job.runner_id = None
        job.progress = 0.0
    else:
        job.status = "failed"
        job.stage = "训练失败"
        job.completed_at = datetime.utcnow()
    runner = (
        db.query(EmbeddingTrainingRunner)
        .filter(EmbeddingTrainingRunner.id == body.runner_id)
        .first()
    )
    if runner:
        runner.status = "error" if not body.retryable else "online"
        runner.current_job_id = None
        runner.last_seen_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return {"status": job.status, "job": _job_payload(job)}
