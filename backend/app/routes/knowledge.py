import uuid
import os
import string
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.routes.auth import get_current_user, has_permission, require_permission
from app.models.user import User
from app.models.knowledge import (
    Knowledge, KnowledgeStatus, KnowledgeLayer,
    KnowledgeTag, KnowledgeMedia, KnowledgeDeduplicationFeedback, KnowledgeChangeLog,
)
from app.core.config import settings
from app.services.embedding import EmbeddingServiceUnavailable
from app.services.knowledge_dedup import (
    DedupDecision,
    check_duplicate,
    ensure_embedding,
    ensure_search_embeddings,
    save_embedding,
    search_embeddings,
)
from app.schemas.knowledge import (
    KnowledgeCreate, KnowledgeUpdate, KnowledgeResponse,
    CandidateSubmit, DeduplicationFeedbackSubmit, FeedbackSubmit,
    SearchRequest, SearchResponse, SearchResult,
)

router = APIRouter(prefix="/knowledge", tags=["知识库管理"])

UPLOAD_DIR = Path(settings.UPLOAD_DIR) if settings.UPLOAD_DIR else Path(__file__).resolve().parents[2] / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_VIDEO = {"video/mp4", "video/webm", "video/quicktime"}
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
TEMP_UPLOAD_TTL_SECONDS = 15 * 60
TEMP_UPLOADS: dict[str, dict] = {}

ALPHA = string.ascii_uppercase  # A-Z


def _generate_knowledge_id(db: Session) -> str:
    sequence_number = db.execute(
        text("SELECT nextval('knowledge_item_number_seq')")
    ).scalar_one()
    if sequence_number > len(ALPHA) * 99999:
        raise ValueError("Knowledge ID limit reached.")
    letter_idx, number = divmod(sequence_number - 1, 99999)
    return f"{ALPHA[letter_idx]}-{number + 1:05d}"


def _normalize_content(raw):
    if raw is None:
        return {"blocks": []}
    if isinstance(raw, str):
        return {"blocks": [{"type": "text", "value": raw}]}
    if isinstance(raw, dict) and "blocks" in raw:
        return raw
    return {"blocks": [{"type": "text", "value": str(raw)}]}


def _cleanup_temp_uploads() -> None:
    cutoff = time.monotonic() - TEMP_UPLOAD_TTL_SECONDS
    for temp_id, upload in list(TEMP_UPLOADS.items()):
        if upload["created_at"] < cutoff:
            TEMP_UPLOADS.pop(temp_id, None)


async def _read_validated_upload(file: UploadFile, media_type: str) -> tuple[bytes, str]:
    allowed_types = ALLOWED_IMAGE if media_type == "image" else ALLOWED_VIDEO if media_type == "video" else None
    if not allowed_types or file.content_type not in allowed_types:
        raise HTTPException(400, "Unsupported media type.")

    data = await file.read(settings.UPLOAD_MAX_BYTES + 1)
    if len(data) > settings.UPLOAD_MAX_BYTES:
        raise HTTPException(413, "Uploaded file is too large.")
    return data, MIME_EXTENSIONS[file.content_type]


def _persist_temp_media(
    db: Session,
    knowledge_id: str,
    content: dict,
    username: str,
) -> None:
    _cleanup_temp_uploads()
    for block in content.get("blocks", []):
        temp_id = block.get("media_id")
        if not isinstance(temp_id, str) or not temp_id.startswith("temp-"):
            continue
        upload = TEMP_UPLOADS.pop(temp_id, None)
        if not upload or upload["username"] != username:
            raise HTTPException(422, "Temporary upload is unavailable.")
        filename = f"{uuid.uuid4().hex[:12]}{upload['extension']}"
        file_path = UPLOAD_DIR / filename
        file_path.write_bytes(upload["data"])
        db.add(
            KnowledgeMedia(
                id=f"media-{uuid.uuid4().hex[:8]}",
                knowledge_id=knowledge_id,
                media_type=upload["media_type"],
                filename=filename,
                original_name=upload["original_name"],
                file_path=str(file_path),
                file_size=len(upload["data"]),
                mime_type=upload["mime_type"],
                alt=block.get("alt") or upload["alt"] or filename,
                caption=block.get("caption") or upload["caption"],
            )
        )
        block["media_id"] = filename


def _discard_temp_media(content: dict, username: str) -> None:
    for block in content.get("blocks", []):
        temp_id = block.get("media_id")
        if isinstance(temp_id, str):
            upload = TEMP_UPLOADS.get(temp_id)
            if upload and upload["username"] == username:
                TEMP_UPLOADS.pop(temp_id, None)


def _sync_media_meta(db: Session, knowledge_id: str, content: dict):
    """将 content.blocks 中的 alt/caption 同步到 media 表"""
    blocks = content.get("blocks", [])
    media_map = {}
    for m in db.query(KnowledgeMedia).filter(KnowledgeMedia.knowledge_id == knowledge_id).all():
        media_map[m.filename] = m
    for b in blocks:
        if b.get("type") in ("image", "video") and b.get("media_id"):
            media_obj = media_map.get(b["media_id"])
            if media_obj:
                if b.get("alt"):
                    media_obj.alt = b["alt"]
                if b.get("caption"):
                    media_obj.caption = b["caption"]


def _deduplication_metadata(decision: DedupDecision) -> dict:
    return {
        "action": decision.action,
        "embedding_model": settings.EMBEDDING_MODEL,
        "content_hash": decision.content_hash,
        "block_threshold": settings.DEDUP_BLOCK_THRESHOLD,
        "review_threshold": settings.DEDUP_REVIEW_THRESHOLD,
        "matches": [
            {
                "knowledge_id": match.knowledge_id,
                "title": match.title,
                "status": match.status,
                "category_id": match.category_id,
                "layer": match.layer,
                "match_type": match.match_type,
                "similarity": match.similarity,
                "title_similarity": match.title_similarity,
                "content_similarity": match.content_similarity,
            }
            for match in decision.matches
        ],
    }


def _check_manual_deduplication(
    db: Session,
    *,
    title: str,
    subtitles: list[str],
    content: dict,
    scene_tags: list[str],
    exclude_knowledge_id: str | None = None,
    confirm_dedup_review: bool = False,
) -> DedupDecision:
    try:
        decision = check_duplicate(
            db,
            title=title,
            subtitles=subtitles,
            content=content,
            scene_tags=scene_tags,
            exclude_knowledge_id=exclude_knowledge_id,
        )
    except EmbeddingServiceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Embedding 服务不可用，无法完成查重，请稍后再提交审核。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if (
        decision.action == "block_duplicate"
        and decision.matches
        and decision.matches[0].match_type == "semantic"
    ):
        # Human-submitted knowledge needs a review path when semantics are uncertain.
        decision.action = "review_duplicate"

    if decision.action == "review_duplicate" and not confirm_dedup_review:
        metadata = _deduplication_metadata(decision)
        top_match = metadata["matches"][0] if metadata["matches"] else None
        message = "检测到疑似重复知识，请对比后确认是否仍要提交审核。"
        if top_match:
            message += f" 命中 {top_match['knowledge_id']}《{top_match['title']}》。"
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DUPLICATE_REVIEW_REQUIRED",
                "message": message,
                "deduplication": metadata,
            },
        )

    if decision.action == "block_duplicate":
        metadata = _deduplication_metadata(decision)
        top_match = metadata["matches"][0] if metadata["matches"] else None
        message = "检测到重复或高度相似的已有知识，未提交审核。"
        if top_match:
            message += f" 命中 {top_match['knowledge_id']}《{top_match['title']}》。"
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DUPLICATE_BLOCKED",
                "message": message,
                "deduplication": metadata,
            },
        )
    return decision


def _to_response(item: Knowledge) -> dict:
    tags = []
    for kt in item.tags:
        tv = kt.tag_value
        if tv:
            tags.append({"id": tv.id, "dimension_id": tv.dimension_id, "value": tv.value})
    media_list = []
    for m in (item.media or []):
        media_list.append({
            "id": m.id,
            "media_type": m.media_type,
            "filename": m.filename,
            "original_name": m.original_name,
            "file_path": f"/uploads/{m.filename}",
            "file_size": m.file_size,
            "mime_type": m.mime_type,
            "alt": m.alt,
            "caption": m.caption,
            "duration": m.duration,
            "sort_order": m.sort_order,
        })
    return {
        "id": item.id,
        "title": item.title,
        "subtitles": item.subtitles or [],
        "content": item.content,
        "layer": item.layer.value,
        "category_id": item.category_id,
        "status": item.status.value,
        "source": item.source,
        "quality_score": item.quality_score or 0.0,
        "applicable_scenes": item.applicable_scenes or [],
        "applicable_business_types": item.applicable_business_types or [],
        "applicable_categories": item.applicable_categories or [],
        "applicable_brands": item.applicable_brands or [],
        "applicable_models": item.applicable_models or [],
        "deduplication_metadata": item.deduplication_metadata or {},
        "is_model_personal": item.is_model_personal == "true",
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "tags": tags,
        "media": media_list,
    }


# ---- CRUD ----

@router.post("", response_model=KnowledgeResponse, status_code=201, summary="创建知识条目", description="新建一条知识条目，完成查重后直接进入待审核(review)")
def create_knowledge(
    body: KnowledgeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    normalized_content = _normalize_content(body.content)
    try:
        decision = _check_manual_deduplication(
            db,
            title=body.title,
            subtitles=body.subtitles or [],
            content=normalized_content,
            scene_tags=body.applicable_scenes or [],
            confirm_dedup_review=body.confirm_dedup_review,
        )
    except Exception:
        _discard_temp_media(normalized_content, current_user.username)
        raise
    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        subtitles=body.subtitles or [],
        content=normalized_content,
        layer=KnowledgeLayer(body.layer),
        category_id=body.category_id,
        status=KnowledgeStatus.REVIEW,
        applicable_scenes=body.applicable_scenes,
        applicable_business_types=body.applicable_business_types,
        applicable_categories=body.applicable_categories,
        applicable_brands=body.applicable_brands,
        applicable_models=body.applicable_models,
        deduplication_metadata=_deduplication_metadata(decision),
        is_model_personal="true" if body.is_model_personal else "false",
        created_by=current_user.username,
        updated_by=current_user.username,
    )
    db.add(item)
    db.flush()
    _persist_temp_media(db, item.id, item.content, current_user.username)
    if decision.embedding:
        save_embedding(
            db,
            knowledge=item,
            content_hash=decision.content_hash,
            embedding=decision.embedding,
            title_embedding=decision.title_embedding,
            content_embedding=decision.content_embedding,
        )
    ensure_search_embeddings(db, item)
    db.commit()
    db.refresh(item)
    return _to_response(item)


@router.get("", response_model=list[KnowledgeResponse], summary="查询知识条目列表", description="支持按状态、层级、分类筛选，分页查询")
def list_knowledge(
    status: str | None = Query(None, description="状态筛选"),
    layer: str | None = Query(None, description="知识层级"),
    category_id: str | None = Query(None, description="分类ID"),
    keyword: str | None = Query(None, description="标题关键词搜索"),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页条数"),
    db: Session = Depends(get_db),
    _=Depends(require_permission("knowledge:view")),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Knowledge)
    if current_user.role == "visitor":
        q = q.filter(Knowledge.status == KnowledgeStatus.PUBLISHED)
    if status:
        q = q.filter(Knowledge.status == KnowledgeStatus(status))
    if layer:
        q = q.filter(Knowledge.layer == KnowledgeLayer(layer))
    if category_id:
        q = q.filter(Knowledge.category_id == category_id)
    if keyword:
        q = q.filter(Knowledge.title.ilike(f"%{keyword}%"))
    items = q.order_by(Knowledge.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return [_to_response(i) for i in items]


@router.get("/dashboard", summary="获取知识运营总览")
def get_dashboard(
    db: Session = Depends(get_db),
    _=Depends(require_permission("knowledge:view")),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Knowledge)
    if current_user.role == "visitor":
        q = q.filter(Knowledge.status == KnowledgeStatus.PUBLISHED)

    counts = {
        status.value: 0
        for status in (
            KnowledgeStatus.DRAFT,
            KnowledgeStatus.REVIEW,
            KnowledgeStatus.PUBLISHED,
            KnowledgeStatus.DEPRECATED,
        )
    }
    for status, total in q.with_entities(
        Knowledge.status, func.count(Knowledge.id)
    ).group_by(Knowledge.status).all():
        counts[status.value] = total

    pending = q.filter(Knowledge.status == KnowledgeStatus.REVIEW).order_by(
        Knowledge.updated_at.asc()
    ).limit(5).all()
    recent_updates = (
        q.filter(Knowledge.status == KnowledgeStatus.PUBLISHED)
        .order_by(Knowledge.updated_at.desc())
        .limit(5)
        .all()
    )
    return {
        "counts": counts,
        "pending": [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status.value,
                "updated_at": item.updated_at,
                "created_by": item.created_by,
                "updated_by": item.updated_by,
            }
            for item in pending
        ],
        "recent_updates": [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status.value,
                "updated_at": item.updated_at,
                "created_by": item.created_by,
                "updated_by": item.updated_by,
            }
            for item in recent_updates
        ],
    }


def _knowledge_snapshot(item: Knowledge) -> dict:
    return {
        "title": item.title,
        "subtitles": deepcopy(item.subtitles or []),
        "content": deepcopy(item.content or {}),
        "layer": item.layer.value,
        "category_id": item.category_id,
        "status": item.status.value,
        "applicable_scenes": deepcopy(item.applicable_scenes or []),
        "applicable_business_types": deepcopy(item.applicable_business_types or []),
        "applicable_categories": deepcopy(item.applicable_categories or []),
        "applicable_brands": deepcopy(item.applicable_brands or []),
        "applicable_models": deepcopy(item.applicable_models or []),
        "is_model_personal": item.is_model_personal == "true",
    }


def _can_edit_knowledge(item: Knowledge, user: User) -> bool:
    if user.role == "super_admin":
        return True
    if item.status == KnowledgeStatus.PUBLISHED:
        return has_permission(user, "knowledge:edit_published")
    if item.status == KnowledgeStatus.REVIEW:
        return (
            has_permission(user, "knowledge:edit_review_all")
            or (
                item.created_by == user.username
                and has_permission(user, "knowledge:edit_own_review")
            )
        )
    if item.status == KnowledgeStatus.DRAFT:
        return (
            item.created_by == user.username
            and has_permission(user, "knowledge:create")
        )
    return False


@router.get("/{knowledge_id}", response_model=KnowledgeResponse, summary="获取知识条目详情")
def get_knowledge(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:view")), current_user: User = Depends(get_current_user)):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if current_user.role == "visitor" and item.status != KnowledgeStatus.PUBLISHED:
        raise HTTPException(403, "Permission denied.")
    return _to_response(item)


@router.get("/{knowledge_id}/change-logs", summary="获取已发布知识的变更日志")
def list_change_logs(
    knowledge_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permission("knowledge:view")),
    current_user: User = Depends(get_current_user),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if current_user.role == "visitor" and item.status != KnowledgeStatus.PUBLISHED:
        raise HTTPException(403, "Permission denied.")
    logs = (
        db.query(KnowledgeChangeLog)
        .filter(KnowledgeChangeLog.knowledge_id == knowledge_id)
        .order_by(KnowledgeChangeLog.created_at.desc())
        .all()
    )
    return [
        {
            "id": log.id,
            "changed_by": log.changed_by,
            "changed_fields": log.changed_fields or [],
            "before_data": log.before_data or {},
            "after_data": log.after_data or {},
            "created_at": log.created_at,
        }
        for log in logs
    ]


@router.patch("/{knowledge_id}", response_model=KnowledgeResponse, summary="更新知识条目")
def update_knowledge(
    knowledge_id: str,
    body: KnowledgeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    is_admin = current_user.role == "super_admin"
    is_owner = item.created_by == current_user.username
    if item.status == KnowledgeStatus.PUBLISHED:
        allowed = is_admin or has_permission(current_user, "knowledge:edit_published")
    elif item.status == KnowledgeStatus.REVIEW:
        allowed = (
            is_admin
            or has_permission(current_user, "knowledge:edit_review_all")
            or (is_owner and has_permission(current_user, "knowledge:edit_own_review"))
        )
    elif item.status == KnowledgeStatus.DRAFT:
        allowed = is_admin or (is_owner and has_permission(current_user, "knowledge:create"))
    else:
        allowed = is_admin
    if not allowed:
        raise HTTPException(403, "You do not have permission to edit this knowledge item.")
    was_published = item.status == KnowledgeStatus.PUBLISHED
    before_data = _knowledge_snapshot(item) if was_published else None
    updates = body.model_dump(exclude_unset=True)
    updated_fields = set(updates)
    for field, val in updates.items():
        if field == "content":
            normalized = _normalize_content(val)
            setattr(item, field, normalized)
            # 同步 content.blocks 里的 alt/caption 回 media 表
            _sync_media_meta(db, item.id, normalized)
        elif field == "status":
            setattr(item, field, KnowledgeStatus(val))
        elif field == "layer":
            setattr(item, field, KnowledgeLayer(val))
        elif field == "is_model_personal":
            setattr(item, field, "true" if val else "false")
        else:
            setattr(item, field, val)
    after_data = _knowledge_snapshot(item)
    changed_fields = [
        field for field, before_value in (before_data or {}).items()
        if before_value != after_data.get(field)
    ]
    if changed_fields:
        item.updated_by = current_user.username
    if was_published and changed_fields:
        db.add(
            KnowledgeChangeLog(
                id=f"kcl-{uuid.uuid4().hex[:12]}",
                knowledge_id=item.id,
                changed_by=current_user.username,
                changed_fields=changed_fields,
                before_data=before_data,
                after_data=after_data,
            )
        )
    if {"title", "subtitles", "content"} & updated_fields:
        ensure_embedding(db, item)
        ensure_search_embeddings(db, item)
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _to_response(item)


@router.delete("/{knowledge_id}", status_code=204, summary="删除知识条目")
def delete_knowledge(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:deprecate"))):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    db.delete(item)
    db.commit()


@router.post(
    "/{knowledge_id}/deduplication-feedback",
    summary="提交知识查重人工反馈",
)
def submit_deduplication_feedback(
    knowledge_id: str,
    body: DeduplicationFeedbackSubmit,
    db: Session = Depends(get_db),
    _: None = Depends(require_permission("knowledge:approve")),
    current_user: User = Depends(get_current_user),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    matched_item = db.query(Knowledge).filter(Knowledge.id == body.matched_knowledge_id).first()
    if not matched_item:
        raise HTTPException(404, "命中的知识条目不存在")

    metadata = item.deduplication_metadata or {}
    matches = metadata.get("matches") if isinstance(metadata, dict) else []
    if not any(match.get("knowledge_id") == body.matched_knowledge_id for match in matches or []):
        raise HTTPException(422, "该知识不包含指定的查重命中记录")

    feedback = (
        db.query(KnowledgeDeduplicationFeedback)
        .filter(
            KnowledgeDeduplicationFeedback.knowledge_id == knowledge_id,
            KnowledgeDeduplicationFeedback.matched_knowledge_id == body.matched_knowledge_id,
            KnowledgeDeduplicationFeedback.submitted_by == current_user.username,
        )
        .first()
    )
    if feedback:
        feedback.verdict = body.verdict
        feedback.reason = body.reason.strip()
    else:
        feedback = KnowledgeDeduplicationFeedback(
            id=f"dfb-{uuid.uuid4().hex[:12]}",
            knowledge_id=knowledge_id,
            matched_knowledge_id=body.matched_knowledge_id,
            verdict=body.verdict,
            reason=body.reason.strip(),
            submitted_by=current_user.username,
        )
        db.add(feedback)

    metadata = dict(metadata)
    existing_feedback = [
        entry
        for entry in metadata.get("feedback", [])
        if not (
            entry.get("matched_knowledge_id") == body.matched_knowledge_id
            and entry.get("submitted_by") == current_user.username
        )
    ]
    existing_feedback.append(
        {
            "matched_knowledge_id": body.matched_knowledge_id,
            "verdict": body.verdict,
            "reason": body.reason.strip(),
            "submitted_by": current_user.username,
            "updated_at": datetime.utcnow().isoformat(),
        }
    )
    metadata["feedback"] = existing_feedback
    item.deduplication_metadata = metadata
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return {
        "status": "recorded",
        "deduplication_metadata": item.deduplication_metadata,
    }


# ---- 审核流程 ----

@router.post("/{knowledge_id}/submit-review", response_model=KnowledgeResponse, summary="提交审核")
def submit_review(
    knowledge_id: str,
    confirm_dedup_review: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if item.status != KnowledgeStatus.DRAFT:
        raise HTTPException(400, "只有草稿状态才能提交审核")
    if current_user.role != "super_admin" and item.created_by != current_user.username:
        raise HTTPException(403, "Only the creator can submit this knowledge item for review.")
    decision = _check_manual_deduplication(
        db,
        title=item.title,
        subtitles=item.subtitles or [],
        content=item.content,
        scene_tags=item.applicable_scenes or [],
        exclude_knowledge_id=item.id,
        confirm_dedup_review=confirm_dedup_review,
    )
    item.status = KnowledgeStatus.REVIEW
    item.deduplication_metadata = _deduplication_metadata(decision)
    if decision.embedding:
        save_embedding(
            db,
            knowledge=item,
            content_hash=decision.content_hash,
            embedding=decision.embedding,
            title_embedding=decision.title_embedding,
            content_embedding=decision.content_embedding,
        )
    ensure_search_embeddings(db, item)
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _to_response(item)


@router.post("/{knowledge_id}/approve", response_model=KnowledgeResponse, summary="审批通过")
def approve_knowledge(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:approve"))):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if item.status != KnowledgeStatus.REVIEW:
        raise HTTPException(400, "只有审核中状态才能审批通过")
    ensure_embedding(db, item)
    ensure_search_embeddings(db, item)
    item.status = KnowledgeStatus.PUBLISHED
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _to_response(item)


@router.post("/{knowledge_id}/deprecate", response_model=KnowledgeResponse, summary="废弃知识条目")
def deprecate_knowledge(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:deprecate"))):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    item.status = KnowledgeStatus.DEPRECATED
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _to_response(item)


@router.post("/{knowledge_id}/restore", response_model=KnowledgeResponse, summary="重新启用废弃知识")
def restore_knowledge(
    knowledge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:deprecate")),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if item.status != KnowledgeStatus.DEPRECATED:
        raise HTTPException(400, "Only deprecated knowledge items can be restored.")
    before_data = _knowledge_snapshot(item)
    item.status = KnowledgeStatus.PUBLISHED
    item.updated_by = current_user.username
    item.updated_at = datetime.utcnow()
    after_data = _knowledge_snapshot(item)
    db.add(
        KnowledgeChangeLog(
            id=f"kcl-{uuid.uuid4().hex[:12]}",
            knowledge_id=item.id,
            changed_by=current_user.username,
            changed_fields=["status"],
            before_data=before_data,
            after_data=after_data,
        )
    )
    db.commit()
    db.refresh(item)
    return _to_response(item)


# ---- 媒体上传(图片+视频) ----

@router.post("/{knowledge_id}/media", summary="上传媒体文件", description="上传图片或视频到指定知识条目")
async def upload_media(
    knowledge_id: str,
    file: UploadFile = File(...),
    media_type: str = Form("image"),
    alt: str = Form(""),
    caption: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if not _can_edit_knowledge(item, current_user):
        raise HTTPException(403, "Permission denied.")

    content, extension = await _read_validated_upload(file, media_type)
    if media_type == "image" and file.content_type not in ALLOWED_IMAGE:
        raise HTTPException(400, f"不支持的图片格式: {file.content_type}，支持: png/jpg/gif/webp")
    if media_type == "video" and file.content_type not in ALLOWED_VIDEO:
        raise HTTPException(400, f"不支持的视频格式: {file.content_type}，支持: mp4/webm/mov")

    ext = extension
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    file_path = str(UPLOAD_DIR / filename)

    with open(file_path, "wb") as f:
        f.write(content)

    media = KnowledgeMedia(
        id=f"media-{uuid.uuid4().hex[:8]}",
        knowledge_id=knowledge_id,
        media_type=media_type,
        filename=filename,
        original_name=file.filename or filename,
        file_path=file_path,
        file_size=len(content),
        mime_type=file.content_type or ("video/mp4" if media_type == "video" else "image/png"),
        alt=alt or filename,
        caption=caption or '',
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return {
        "id": media.id,
        "media_type": media.media_type,
        "filename": media.filename,
        "original_name": media.original_name,
        "file_path": f"/uploads/{media.filename}",
        "file_size": media.file_size,
        "mime_type": media.mime_type,
        "alt": media.alt,
        "caption": media.caption,
    }


@router.post("/upload-temp", summary="临时上传媒体文件", description="未创建知识条目时的临时上传，返回文件ID供编辑器使用")
async def upload_temp(
    file: UploadFile = File(...),
    media_type: str = Form("image"),
    alt: str = Form(""),
    caption: str = Form(""),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    _cleanup_temp_uploads()
    content, extension = await _read_validated_upload(file, media_type)
    filename = f"temp-{uuid.uuid4().hex[:12]}"
    TEMP_UPLOADS[filename] = {
        "username": current_user.username,
        "media_type": media_type,
        "mime_type": file.content_type,
        "extension": extension,
        "original_name": file.filename or filename,
        "alt": alt,
        "caption": caption,
        "data": content,
        "created_at": time.monotonic(),
    }
    return {
        "id": filename,
        "filename": filename,
        "original_name": file.filename,
        "file_path": None,
        "file_size": len(content),
        "mime_type": file.content_type,
        "alt": alt,
        "caption": caption,
    }

@router.get("/{knowledge_id}/media", summary="获取知识条目的媒体文件列表")
def list_media(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:view"))):
    items = db.query(KnowledgeMedia).filter(
        KnowledgeMedia.knowledge_id == knowledge_id
    ).order_by(KnowledgeMedia.sort_order).all()
    return [{
        "id": m.id, "media_type": m.media_type,
        "filename": m.filename, "original_name": m.original_name,
        "file_path": f"/uploads/{m.filename}", "file_size": m.file_size,
        "mime_type": m.mime_type, "alt": m.alt, "caption": m.caption,
        "duration": m.duration, "sort_order": m.sort_order,
    } for m in items]


@router.patch("/{knowledge_id}/media/{media_file}", summary="更新媒体信息", description="修改图片/视频的描述和说明文字")
def update_media(knowledge_id: str, media_file: str, alt: str = Form(""), caption: str = Form(""), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if not _can_edit_knowledge(item, current_user):
        raise HTTPException(403, "Permission denied.")
    media = db.query(KnowledgeMedia).filter(
        KnowledgeMedia.filename == media_file, KnowledgeMedia.knowledge_id == knowledge_id
    ).first()
    if not media:
        raise HTTPException(404, "媒体文件不存在")
    media.alt = alt
    media.caption = caption
    db.commit()
    return {"status": "ok"}


@router.delete("/{knowledge_id}/media/{media_file}", status_code=204, summary="删除媒体文件")
def delete_media(knowledge_id: str, media_file: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if not _can_edit_knowledge(item, current_user):
        raise HTTPException(403, "Permission denied.")
    media = db.query(KnowledgeMedia).filter(
        KnowledgeMedia.filename == media_file, KnowledgeMedia.knowledge_id == knowledge_id
    ).first()
    if not media:
        raise HTTPException(404, "媒体文件不存在")
    if os.path.exists(media.file_path):
        os.remove(media.file_path)
    db.delete(media)
    db.commit()


# ---- 候选池 ----

@router.post("/candidates", response_model=KnowledgeResponse, status_code=201, summary="提交候选知识")
def submit_candidate(
    body: CandidateSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    try:
        decision = check_duplicate(
            db,
            title=body.title,
            subtitles=[],
            content=_normalize_content(body.content),
            scene_tags=body.applicable_scenes,
        )
    except EmbeddingServiceUnavailable as exc:
        raise HTTPException(503, "Embedding 服务不可用，无法完成查重") from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if decision.action == "block_duplicate":
        raise HTTPException(
            409,
            detail={
                "code": "DUPLICATE_BLOCKED",
                "deduplication": _deduplication_metadata(decision),
            },
        )

    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        content=_normalize_content(body.content),
        layer=KnowledgeLayer(body.layer),
        category_id=body.category_id,
        status=KnowledgeStatus.REVIEW,
        applicable_scenes=body.applicable_scenes,
        source=body.source,
        source_session_id=body.source_session_id,
        created_by=current_user.username,
        updated_by=current_user.username,
        deduplication_metadata=_deduplication_metadata(decision),
    )
    db.add(item)
    db.flush()
    if decision.embedding:
        save_embedding(
            db,
            knowledge=item,
            content_hash=decision.content_hash,
            embedding=decision.embedding,
            title_embedding=decision.title_embedding,
            content_embedding=decision.content_embedding,
        )
    ensure_search_embeddings(db, item)
    db.commit()
    db.refresh(item)
    return _to_response(item)


# ---- 检索 ----

@router.post("/search", response_model=SearchResponse, summary="检索知识库")
def search_knowledge(
    body: SearchRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
):
    try:
        ranked = search_embeddings(
            db,
            query=body.query,
            category_id=body.category_id,
            layer=body.layer,
            tags=body.tags,
            top_k=body.top_k,
        )
    except EmbeddingServiceUnavailable as exc:
        raise HTTPException(503, "Embedding 服务不可用，无法完成语义检索") from exc

    # Existing installations can still serve title matches while search vectors
    # are being rebuilt in the background.
    if not ranked:
        q = db.query(Knowledge).filter(Knowledge.status == KnowledgeStatus.PUBLISHED)
        if body.category_id:
            q = q.filter(Knowledge.category_id == body.category_id)
        if body.layer:
            q = q.filter(Knowledge.layer == KnowledgeLayer(body.layer))
        if body.tags:
            q = q.filter(
                Knowledge.tags.any(KnowledgeTag.tag_value_id.in_(body.tags))
            )
        q = q.filter(Knowledge.title.ilike(f"%{body.query}%"))
        ranked = [
            (item, float(item.quality_score or 0.0))
            for item in q.order_by(Knowledge.quality_score.desc()).limit(body.top_k).all()
        ]
    results = [
        SearchResult(
            id=i.id, title=i.title, content=i.content,
            score=round(score, 6), layer=i.layer.value,
            status=i.status.value, category_id=i.category_id,
        )
        for i, score in ranked
    ]
    return SearchResponse(query=body.query, total=len(results), results=results)


# ---- 反馈 ----

@router.post("/feedback", summary="提交使用反馈")
def submit_feedback(
    body: FeedbackSubmit,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
):
    item = db.query(Knowledge).filter(Knowledge.id == body.knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    from app.models.knowledge import UsageStat
    stat = db.query(UsageStat).filter(UsageStat.knowledge_id == body.knowledge_id).first()
    if not stat:
        stat = UsageStat(id=f"us-{uuid.uuid4().hex[:12]}", knowledge_id=body.knowledge_id)
        db.add(stat)
    if body.action == "useful":
        stat.click_count += 1
    stat.recommend_count += 1
    stat.last_used_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "message": "反馈已记录"}

