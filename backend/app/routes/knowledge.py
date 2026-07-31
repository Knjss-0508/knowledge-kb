import json
import re
import uuid
import logging
import string
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy import cast, func, or_, text
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from starlette.concurrency import run_in_threadpool

from app.core.database import SessionLocal, get_db
from app.routes.auth import get_current_user, has_permission, require_permission
from app.models.user import User
from app.models.knowledge import (
    Category, Knowledge, KnowledgeStatus,
    KnowledgeTag, KnowledgeMedia, MediaUploadStaging,
    KnowledgeDeduplicationFeedback, KnowledgeChangeLog, KnowledgeImportTask,
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
from app.services.knowledge_excel import (
    DEPRECATED_SOURCE_STATUS,
    MAX_IMPORT_FILE_BYTES,
    KnowledgeExcelError,
    IMPORTABLE_SOURCE_STATUS,
    build_knowledge_export_workbook,
    build_knowledge_import_template,
    parse_knowledge_workbook,
)
from app.services.media_deletion import (
    delete_media_immediately_or_enqueue,
    enqueue_media_deletion,
)
from app.services.media_storage import MediaStorageError, get_media_storage
from app.schemas.knowledge import (
    KnowledgeCreate, KnowledgeUpdate, KnowledgeResponse,
    CandidateSubmit, DeduplicationFeedbackSubmit, FeedbackSubmit,
    ExcelImportRowResult, KnowledgeImportTaskListResponse,
    KnowledgeImportTaskResponse,
    KnowledgeBatchApprove, KnowledgeBatchApproveResponse, KnowledgeBatchApproveResult,
    KnowledgeReviewSelectionResponse,
    SearchRequest, SearchResponse, SearchResult,
)

router = APIRouter(prefix="/knowledge", tags=["知识库管理"])
logger = logging.getLogger(__name__)
media_storage = get_media_storage()

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


def _jsonb_text_match(column, json_path: str, keyword: str):
    """在 JSON 中匹配已解码的文本，避免 JSON 转义破坏中文 ILIKE 搜索。"""
    escaped_keyword = json.dumps(re.escape(keyword), ensure_ascii=False)
    path_expression = f'{json_path} ? (@ like_regex {escaped_keyword} flag "i")'
    return func.jsonb_path_exists(
        cast(column, JSONB),
        cast(path_expression, JSONPATH),
    )


def _auto_publish_approved_source_excel(
    item: Knowledge,
    *,
    source_status: str,
    current_user: User,
) -> bool:
    """将源表已生效知识直接发布，疑似重复仍必须人工确认。"""
    if source_status != IMPORTABLE_SOURCE_STATUS:
        return False
    if (item.deduplication_metadata or {}).get("action") == "review_duplicate":
        return False
    item.status = KnowledgeStatus.PUBLISHED
    item.updated_by = current_user.username
    return True


MANHATTAN_APPLICABILITY_CATEGORY_IDS = {
    "cat-qc-standard",
    "cat-qc-process",
}


def _require_manual_applicable_category(
    *,
    source: str,
    category_id: str | None,
    applicable_categories,
) -> None:
    """质检知识的人工维护至少绑定一个适用类目，允许选择多个。"""
    if source != "manual" or category_id not in MANHATTAN_APPLICABILITY_CATEGORY_IDS:
        return
    selected = [
        str(value).strip()
        for value in (applicable_categories or [])
        if str(value).strip()
    ]
    if not selected:
        raise HTTPException(
            status_code=422,
            detail="适用类目至少选择一项，可多选。",
        )


class SourceKnowledgeMatchError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _source_identifier_values(row) -> list[tuple[str, str]]:
    return [
        ("知识键", (row.source_knowledge_key or "").strip()),
        ("主题键", (row.source_topic_key or "").strip()),
        ("记录ID", (row.source_record_id or "").strip()),
    ]


def _find_source_knowledge(db: Session, row) -> Knowledge:
    identifier_columns = {
        "知识键": Knowledge.source_knowledge_key,
        "主题键": Knowledge.source_topic_key,
        "记录ID": Knowledge.source_record_id,
    }
    ambiguous_identifiers: list[tuple[str, str]] = []
    for label, value in _source_identifier_values(row):
        if not value:
            continue
        matches = db.query(Knowledge).filter(identifier_columns[label] == value).all()
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ambiguous_identifiers.append((label, value))
    if ambiguous_identifiers:
        label, value = ambiguous_identifiers[0]
        raise SourceKnowledgeMatchError(
            "SOURCE_IDENTIFIER_AMBIGUOUS",
            f"来源{label}“{value}”匹配到多条知识，未执行废弃操作。",
        )
    raise SourceKnowledgeMatchError(
        "SOURCE_KNOWLEDGE_NOT_FOUND",
        "未根据知识键、主题键或记录ID找到原知识，未执行废弃操作。",
    )


def _bind_source_identifiers(item: Knowledge, row) -> list[str]:
    changed_fields: list[str] = []
    for field in (
        "source_topic_key",
        "source_record_id",
        "source_knowledge_key",
    ):
        value = (getattr(row, field, "") or "").strip() or None
        if value and getattr(item, field) != value:
            setattr(item, field, value)
            changed_fields.append(field)
    source_fields = getattr(row, "source_fields", None) or {}
    if source_fields and getattr(item, "source_fields", None) != source_fields:
        item.source_fields = source_fields
        changed_fields.append("source_fields")
    return changed_fields


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
    item: Knowledge,
    content: dict,
    username: str,
) -> None:
    pending: dict[str, list[dict]] = {}
    for block in content.get("blocks", []):
        temp_id = block.get("media_id")
        if not isinstance(temp_id, str) or not temp_id.startswith("temp-"):
            continue
        pending.setdefault(temp_id, []).append(block)

    if not pending:
        return

    temp_ids = list(pending)
    lock_order = sorted(temp_ids)
    now = datetime.utcnow()
    staged_uploads = (
        db.query(MediaUploadStaging)
        .filter(
            MediaUploadStaging.id.in_(lock_order),
            MediaUploadStaging.username == username,
            MediaUploadStaging.expires_at > now,
            MediaUploadStaging.status == "ready",
            MediaUploadStaging.storage_backend == media_storage.backend,
        )
        .order_by(MediaUploadStaging.id)
        .with_for_update()
        .all()
    )
    staged_by_id = {staged.id: staged for staged in staged_uploads}
    unavailable = [
        temp_id
        for temp_id in temp_ids
        if temp_id not in staged_by_id
    ]
    if unavailable:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TEMP_UPLOAD_UNAVAILABLE",
                "message": "临时媒体已过期或不可用",
                "temp_ids": unavailable,
            },
        )

    type_mismatches = [
        temp_id
        for temp_id in temp_ids
        if any(
            block.get("type") != staged_by_id[temp_id].media_type
            for block in pending[temp_id]
        )
    ]
    if type_mismatches:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TEMP_UPLOAD_TYPE_MISMATCH",
                "message": "临时媒体类型与内容块不一致",
                "temp_ids": type_mismatches,
            },
        )

    for temp_id in temp_ids:
        staged = staged_by_id[temp_id]
        blocks = pending[temp_id]
        primary_block = blocks[0]
        db.add(
            KnowledgeMedia(
                id=f"media-{uuid.uuid4().hex[:8]}",
                knowledge_id=item.id,
                media_type=staged.media_type,
                filename=staged.filename,
                original_name=staged.original_name,
                file_path=staged.storage_key,
                file_size=staged.file_size,
                mime_type=staged.mime_type,
                alt=primary_block.get("alt") or staged.alt or staged.filename,
                caption=primary_block.get("caption") or staged.caption,
            )
        )
        for block in blocks:
            block["media_id"] = staged.filename
        db.delete(staged)
    flag_modified(item, "content")


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


def _referenced_media_filenames(content: dict) -> set[str]:
    filenames: set[str] = set()
    for block in content.get("blocks", []):
        media_id = block.get("media_id")
        if (
            block.get("type") in ("image", "video")
            and isinstance(media_id, str)
            and media_id
        ):
            filenames.add(media_id.replace("/uploads/", "", 1))
    return filenames


def _deduplication_metadata(
    decision: DedupDecision,
    *,
    confirmed_by: str | None = None,
) -> dict:
    metadata = {
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
                "match_type": match.match_type,
                "similarity": match.similarity,
                "title_similarity": match.title_similarity,
                "content_similarity": match.content_similarity,
            }
            for match in decision.matches
        ],
    }
    if decision.action == "review_duplicate" and confirmed_by:
        metadata["review_confirmation"] = {
            "confirmed_by": confirmed_by,
            "confirmed_at": datetime.utcnow().isoformat(),
        }
    return metadata


def _pending_deduplication_matches(item: Knowledge) -> list[dict]:
    """返回尚未填写“确实不同”原因的疑似重复命中。"""
    metadata = getattr(item, "deduplication_metadata", None) or {}
    if not isinstance(metadata, dict) or metadata.get("action") != "review_duplicate":
        return []
    matches = metadata.get("matches")
    if not isinstance(matches, list) or not matches:
        # 数据异常时保持发布门禁，避免缺失查重证据被当作已确认。
        return [{}]
    confirmed_ids = {
        str(entry.get("matched_knowledge_id"))
        for entry in metadata.get("feedback", [])
        if isinstance(entry, dict)
        and entry.get("verdict") == "different"
        and str(entry.get("reason") or "").strip()
    }
    return [
        match
        for match in matches
        if not isinstance(match, dict)
        or not match.get("knowledge_id")
        or str(match["knowledge_id"]) not in confirmed_ids
    ]


def _deduplication_confirmation_message(pending_matches: list[dict]) -> str:
    knowledge_ids = [
        str(match.get("knowledge_id"))
        for match in pending_matches
        if isinstance(match, dict) and match.get("knowledge_id")
    ]
    suffix = f"（{'、'.join(knowledge_ids[:3])}）" if knowledge_ids else ""
    return f"请先在“对比详情”中确认疑似重复知识确实不同并填写原因，再发布{suffix}。"


def _check_manual_deduplication(
    db: Session,
    *,
    title: str,
    subtitles: list[str],
    content: dict,
    scene_tags: list[str],
    exclude_knowledge_id: str | None = None,
    confirm_dedup_review: bool = False,
    allow_duplicate_review: bool = False,
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

    if (
        decision.action == "review_duplicate"
        and not confirm_dedup_review
        and not allow_duplicate_review
    ):
        metadata = _deduplication_metadata(decision)
        top_match = metadata["matches"][0] if metadata["matches"] else None
        message = "检测到疑似重复知识，请对比后确认是否仍要提交审核。"
        if top_match:
            if top_match["match_type"] == "title_exact":
                message = "检测到标题完全相同但正文不同的知识，请对比后确认是否仍要提交审核。"
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


def _import_review_metadata(item: Knowledge) -> dict[str, str]:
    """只向审核界面暴露 Excel 原始行中与人工复核直接相关的字段。"""
    if item.source != "excel" or item.status != KnowledgeStatus.REVIEW:
        return {}
    source_fields = item.source_fields or {}
    if not isinstance(source_fields, dict):
        return {}

    normalized = {
        re.sub(r"[（(].*?[）)]", "", str(key))
        .replace("*", "")
        .replace(" ", "")
        .strip(): str(value or "").strip()
        for key, value in source_fields.items()
    }
    metadata = {
        "validation_remark": normalized.get("校验备注", ""),
        "source_trace": normalized.get("来源追溯", ""),
    }
    return {key: value for key, value in metadata.items() if value}


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
        "category_id": item.category_id,
        "status": item.status.value,
        "source": item.source,
        "quality_score": item.quality_score or 0.0,
        "applicable_scenes": item.applicable_scenes or [],
        "applicable_categories": item.applicable_categories or [],
        "applicable_brands": item.applicable_brands or [],
        "applicable_models": item.applicable_models or [],
        "related_standard_items": item.related_standard_items or [],
        "source_topic_key": item.source_topic_key,
        "source_record_id": item.source_record_id,
        "source_knowledge_key": item.source_knowledge_key,
        "import_review_metadata": _import_review_metadata(item),
        "deduplication_metadata": item.deduplication_metadata or {},
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "tags": tags,
        "media": media_list,
    }


# ---- CRUD ----

def _create_knowledge_item(
    body: KnowledgeCreate,
    db: Session,
    current_user: User,
    *,
    source: str = "manual",
    allow_duplicate_review: bool = False,
) -> Knowledge:
    _require_manual_applicable_category(
        source=source,
        category_id=body.category_id,
        applicable_categories=body.applicable_categories,
    )
    if not db.query(Category.id).filter(Category.id == body.category_id).first():
        raise HTTPException(status_code=422, detail="所属分类不存在。")

    normalized_content = _normalize_content(body.content)
    decision = _check_manual_deduplication(
        db,
        title=body.title,
        subtitles=body.subtitles or [],
        content=normalized_content,
        scene_tags=body.applicable_scenes or [],
        confirm_dedup_review=body.confirm_dedup_review,
        allow_duplicate_review=allow_duplicate_review,
    )
    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        subtitles=body.subtitles or [],
        content=normalized_content,
        category_id=body.category_id,
        status=KnowledgeStatus.REVIEW,
        source=source,
        applicable_scenes=body.applicable_scenes,
        applicable_categories=body.applicable_categories,
        applicable_brands=body.applicable_brands,
        applicable_models=body.applicable_models,
        related_standard_items=body.related_standard_items,
        source_topic_key=(body.source_topic_key or "").strip() or None,
        source_record_id=(body.source_record_id or "").strip() or None,
        source_knowledge_key=(body.source_knowledge_key or "").strip() or None,
        source_fields=body.source_fields or {},
        deduplication_metadata=_deduplication_metadata(
            decision,
            confirmed_by=(
                current_user.username if body.confirm_dedup_review else None
            ),
        ),
        created_by=current_user.username,
        updated_by=current_user.username,
    )
    db.add(item)
    db.flush()
    _persist_temp_media(
        db,
        item,
        item.content,
        current_user.username,
    )
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
    return item


@router.post("", response_model=KnowledgeResponse, status_code=201, summary="创建知识条目", description="新建一条知识条目，完成查重后直接进入待审核(review)")
def create_knowledge(
    body: KnowledgeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    try:
        item = _create_knowledge_item(body, db, current_user)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(item)
    return _to_response(item)


@router.get("/import/template", summary="下载知识批量导入模板")
def download_knowledge_import_template(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:create")),
):
    categories = db.query(Category).order_by(Category.level, Category.sort_order).all()
    payload = build_knowledge_import_template(categories)
    return StreamingResponse(
        BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="knowledge-import-template.xlsx"'
        },
    )


@router.post(
    "/import/excel",
    response_model=KnowledgeImportTaskResponse,
    status_code=202,
    summary="上传 Excel 并创建后台导入任务",
)
async def import_knowledge_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="仅支持 .xlsx 文件。")

    data = await file.read(MAX_IMPORT_FILE_BYTES + 1)
    if len(data) > MAX_IMPORT_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"导入文件不能超过 {MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB。",
        )

    task = KnowledgeImportTask(
        id=f"import-{uuid.uuid4().hex}",
        created_by=current_user.username,
        original_filename=filename[:256],
        file_size=len(data),
        file_sha256=hashlib.sha256(data).hexdigest(),
        file_content=data,
        status="queued",
        next_attempt_at=datetime.utcnow(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _to_import_task_response(task)


def _to_import_task_response(
    task: KnowledgeImportTask,
    *,
    include_results: bool = False,
    result_limit: int = 100,
) -> KnowledgeImportTaskResponse:
    return KnowledgeImportTaskResponse(
        id=task.id,
        original_filename=task.original_filename,
        file_size=task.file_size,
        status=task.status,
        total_rows=task.total_rows,
        processed_rows=task.processed_rows,
        imported=task.imported,
        review_required=task.review_required,
        pending_review=task.pending_review,
        deprecated=task.deprecated,
        failed=task.failed,
        error_message=task.error_message or "",
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        results=[
            ExcelImportRowResult.model_validate(result)
            for result in (task.results or [])[:max(1, min(result_limit, 500))]
        ]
        if include_results
        else [],
    )


def _can_view_import_task(task: KnowledgeImportTask, current_user: User) -> bool:
    return (
        current_user.role == "super_admin"
        or task.created_by == current_user.username
    )


@router.get(
    "/import/tasks",
    response_model=KnowledgeImportTaskListResponse,
    summary="查看 Excel 后台导入任务",
)
def list_knowledge_import_tasks(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    query = db.query(KnowledgeImportTask)
    if current_user.role != "super_admin":
        query = query.filter(KnowledgeImportTask.created_by == current_user.username)
    tasks = (
        query.order_by(KnowledgeImportTask.created_at.desc())
        .limit(limit)
        .all()
    )
    return KnowledgeImportTaskListResponse(
        tasks=[_to_import_task_response(task) for task in tasks]
    )


@router.get(
    "/import/tasks/{task_id}",
    response_model=KnowledgeImportTaskResponse,
    summary="查看 Excel 后台导入任务详情",
)
def get_knowledge_import_task(
    task_id: str,
    include_results: bool = Query(False, description="是否返回逐行处理结果"),
    result_limit: int = Query(100, ge=1, le=500, description="最多返回的逐行结果数"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    task = db.query(KnowledgeImportTask).filter(
        KnowledgeImportTask.id == task_id
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="导入任务不存在。")
    if not _can_view_import_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权查看该导入任务。")
    return _to_import_task_response(
        task,
        include_results=include_results,
        result_limit=result_limit,
    )


def _task_lease_expiry(now: datetime) -> datetime:
    return now + timedelta(
        seconds=max(30, settings.KNOWLEDGE_IMPORT_LEASE_SECONDS)
    )


def _append_import_task_result(
    task: KnowledgeImportTask,
    result: ExcelImportRowResult,
    *,
    now: datetime,
) -> None:
    results = list(task.results or [])
    results.append(result.model_dump(mode="json"))
    task.results = results
    task.processed_rows = (task.processed_rows or 0) + 1
    if result.status in {"imported", "review_pending", "review_required"}:
        task.imported = (task.imported or 0) + 1
    if result.status == "review_required":
        task.review_required = (task.review_required or 0) + 1
    if result.status == "review_pending":
        task.pending_review = (task.pending_review or 0) + 1
    if result.status == "deprecated":
        task.deprecated = (task.deprecated or 0) + 1
    if result.status == "failed":
        task.failed = (task.failed or 0) + 1
    task.lease_expires_at = _task_lease_expiry(now)
    task.updated_at = now


def _excel_row_failure_result(row, exc: Exception) -> ExcelImportRowResult:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        error_code = "IMPORT_REJECTED"
        if isinstance(detail, dict):
            error_code = str(detail.get("code") or error_code)
            error_message = str(detail.get("message") or detail)
        else:
            error_message = str(detail)
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            error_code=error_code,
            error_message=error_message,
            deduplication=(
                detail.get("deduplication")
                if isinstance(detail, dict)
                else None
            ),
        )
    if isinstance(exc, IntegrityError):
        logger.exception(
            "Excel knowledge import hit a database constraint at row %s",
            row.row_number,
        )
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            error_code="INVALID_ROW",
            error_message="数据校验失败，请检查分类和字段格式。",
        )
    if isinstance(exc, SourceKnowledgeMatchError):
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            error_code=exc.code,
            error_message=str(exc),
        )
    if isinstance(exc, ValueError):
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            error_code="INVALID_ROW",
            error_message=str(exc),
        )
    logger.exception("Excel knowledge import failed at row %s", row.row_number)
    return ExcelImportRowResult(
        row=row.row_number,
        title=row.title,
        status="failed",
        error_code="IMPORT_FAILED",
        error_message="导入失败，请检查服务日志。",
    )


def _process_excel_import_row(
    db: Session,
    row,
    current_user: User,
) -> ExcelImportRowResult:
    if not row.is_valid:
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            error_code=row.error_code,
            error_message=row.error_message,
        )

    if row.source_status == DEPRECATED_SOURCE_STATUS:
        item = _find_source_knowledge(db, row)
        before_data = _knowledge_snapshot(item)
        changed_fields = _bind_source_identifiers(item, row)
        if item.status != KnowledgeStatus.DEPRECATED:
            item.status = KnowledgeStatus.DEPRECATED
            changed_fields.append("status")
        item.updated_by = current_user.username
        item.updated_at = datetime.utcnow()
        after_data = _knowledge_snapshot(item)
        if changed_fields:
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
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="deprecated",
            knowledge_id=item.id,
            error_message=(
                "原知识已是废弃状态。"
                if not changed_fields
                else "已按来源标识同步为废弃状态。"
            ),
        )

    body = KnowledgeCreate(
        title=row.title,
        subtitles=row.subtitles or [],
        content=row.content,
        category_id=row.category_id,
        applicable_scenes=row.applicable_scenes or [],
        applicable_categories=row.applicable_categories or [],
        applicable_brands=row.applicable_brands or [],
        applicable_models=row.applicable_models or [],
        related_standard_items=row.related_standard_items or [],
        source_topic_key=row.source_topic_key or None,
        source_record_id=row.source_record_id or None,
        source_knowledge_key=row.source_knowledge_key or None,
        source_fields=row.source_fields,
    )
    item = _create_knowledge_item(
        body,
        db,
        current_user,
        source="excel",
        allow_duplicate_review=True,
    )
    _auto_publish_approved_source_excel(
        item,
        source_status=row.source_status,
        current_user=current_user,
    )
    deduplication = item.deduplication_metadata or {}
    is_review_required = deduplication.get("action") == "review_duplicate"
    is_pending_review = item.status == KnowledgeStatus.REVIEW
    return ExcelImportRowResult(
        row=row.row_number,
        title=row.title,
        status=(
            "review_required"
            if is_review_required
            else "review_pending"
            if is_pending_review
            else "imported"
        ),
        knowledge_id=item.id,
        error_code=(
            "DUPLICATE_REVIEW_REQUIRED"
            if is_review_required
            else None
        ),
        error_message=(
            "已进入知识待发布审核，请确认是否与命中知识重复。"
            if is_review_required
            else None
        ),
        deduplication=deduplication if is_review_required else None,
    )


def _mark_import_task_failed(
    task: KnowledgeImportTask,
    message: str,
    *,
    now: datetime,
) -> None:
    task.status = "failed"
    task.error_message = message[:2000]
    task.lease_expires_at = None
    task.completed_at = now
    task.updated_at = now


def _mark_import_task_retry(
    task: KnowledgeImportTask,
    message: str,
    *,
    now: datetime,
) -> None:
    task.status = "queued"
    task.error_message = message[:2000]
    task.lease_expires_at = None
    task.next_attempt_at = now + timedelta(seconds=5)
    task.updated_at = now


def process_knowledge_import_task(
    task_id: str,
    *,
    session_factory=SessionLocal,
) -> None:
    """Process a persisted task from the first uncommitted Excel row onward."""

    db = session_factory()
    try:
        task = db.query(KnowledgeImportTask).filter(
            KnowledgeImportTask.id == task_id
        ).first()
        if not task or task.status != "running":
            return

        categories = db.query(Category).order_by(
            Category.level,
            Category.sort_order,
        ).all()
        try:
            rows = parse_knowledge_workbook(task.file_content, categories)
        except KnowledgeExcelError as exc:
            _mark_import_task_failed(task, str(exc), now=datetime.utcnow())
            db.commit()
            return

        if task.total_rows and task.total_rows != len(rows):
            _mark_import_task_failed(
                task,
                "导入文件解析结果发生变化，任务已停止以避免重复写入。",
                now=datetime.utcnow(),
            )
            db.commit()
            return
        if task.processed_rows > len(rows):
            _mark_import_task_failed(
                task,
                "导入进度超出文件行数，任务已停止以避免重复写入。",
                now=datetime.utcnow(),
            )
            db.commit()
            return

        task.total_rows = len(rows)
        task.error_message = ""
        task.lease_expires_at = _task_lease_expiry(datetime.utcnow())
        db.commit()

        background_user = SimpleNamespace(username=task.created_by)
        for row in rows[task.processed_rows:]:
            try:
                result = _process_excel_import_row(db, row, background_user)
            except Exception as exc:
                db.rollback()
                task = db.query(KnowledgeImportTask).filter(
                    KnowledgeImportTask.id == task_id
                ).first()
                if not task or task.status != "running":
                    return
                result = _excel_row_failure_result(row, exc)

            _append_import_task_result(task, result, now=datetime.utcnow())
            db.commit()

        task = db.query(KnowledgeImportTask).filter(
            KnowledgeImportTask.id == task_id
        ).first()
        if not task or task.status != "running":
            return
        task.status = "completed_with_errors" if task.failed else "completed"
        task.error_message = ""
        task.lease_expires_at = None
        task.completed_at = datetime.utcnow()
        task.updated_at = task.completed_at
        db.commit()
    except Exception as exc:
        db.rollback()
        task = db.query(KnowledgeImportTask).filter(
            KnowledgeImportTask.id == task_id
        ).first()
        if task and task.status == "running":
            _mark_import_task_retry(
                task,
                f"后台处理异常，将自动重试：{str(exc) or type(exc).__name__}",
                now=datetime.utcnow(),
            )
            db.commit()
        logger.exception("Knowledge import task %s crashed.", task_id)
    finally:
        db.close()


def process_next_knowledge_import_task(
    *,
    session_factory=SessionLocal,
) -> bool:
    """Claim one queued or expired task, then process it outside the claim lock."""

    db = session_factory()
    task_id = ""
    now = datetime.utcnow()
    try:
        task = (
            db.query(KnowledgeImportTask)
            .filter(
                or_(
                    (
                        (KnowledgeImportTask.status == "queued")
                        & (KnowledgeImportTask.next_attempt_at <= now)
                    ),
                    (
                        (KnowledgeImportTask.status == "running")
                        & (KnowledgeImportTask.lease_expires_at <= now)
                    ),
                )
            )
            .order_by(KnowledgeImportTask.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not task:
            return False
        task.status = "running"
        task.attempt_count = (task.attempt_count or 0) + 1
        task.started_at = task.started_at or now
        task.lease_expires_at = _task_lease_expiry(now)
        task.updated_at = now
        task_id = task.id
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to claim a knowledge import task.")
        return False
    finally:
        db.close()

    process_knowledge_import_task(task_id, session_factory=session_factory)
    return True


def _filtered_knowledge_query(
    db: Session,
    current_user: User,
    *,
    status: str | None = None,
    category_id: str | None = None,
    applicable_category_ids: list[str] | None = None,
    brand_ids: list[str] | None = None,
    model_ids: list[str] | None = None,
    keyword: str | None = None,
):
    q = db.query(Knowledge)
    applicable_category_ids = [
        value.strip()
        for value in (applicable_category_ids or [])
        if value and value.strip()
    ]
    brand_ids = [
        value.strip()
        for value in (brand_ids or [])
        if value and value.strip()
    ]
    model_ids = [
        value.strip()
        for value in (model_ids or [])
        if value and value.strip()
    ]
    if current_user.role == "visitor":
        q = q.filter(Knowledge.status == KnowledgeStatus.PUBLISHED)
    if status:
        q = q.filter(Knowledge.status == KnowledgeStatus(status))
    if category_id:
        q = q.filter(Knowledge.category_id == category_id)
    if applicable_category_ids:
        q = q.filter(
            or_(
                *[
                    cast(Knowledge.applicable_categories, JSONB).contains([value])
                    for value in dict.fromkeys(applicable_category_ids)
                    if value
                ]
            )
        )
    if brand_ids:
        q = q.filter(
            or_(
                *[
                    cast(Knowledge.applicable_brands, JSONB).contains([value])
                    for value in dict.fromkeys(brand_ids)
                    if value
                ]
            )
        )
    if model_ids:
        q = q.filter(
            or_(
                *[
                    cast(Knowledge.applicable_models, JSONB).contains([value])
                    for value in dict.fromkeys(model_ids)
                    if value
                ]
            )
        )
    keyword = (keyword or "").strip()
    if keyword:
        keyword_pattern = f"%{keyword}%"
        q = q.filter(
            or_(
                Knowledge.title.ilike(keyword_pattern),
                _jsonb_text_match(Knowledge.subtitles, "$[*]", keyword),
                _jsonb_text_match(Knowledge.content, "$.blocks[*].value", keyword),
                _jsonb_text_match(Knowledge.related_standard_items, "$[*]", keyword),
                _jsonb_text_match(Knowledge.applicable_scenes, "$[*]", keyword),
                Knowledge.category.has(Category.name.ilike(keyword_pattern)),
            )
        )
    return q


def _has_knowledge_export_filter(
    *,
    status: str | None,
    category_id: str | None,
    applicable_category_ids: list[str] | None,
    brand_ids: list[str] | None,
    model_ids: list[str] | None,
    keyword: str | None,
) -> bool:
    """Avoid accidentally exporting the full knowledge base without a filter."""
    return bool(
        status
        or category_id
        or any(applicable_category_ids or [])
        or any(brand_ids or [])
        or any(model_ids or [])
        or (keyword or "").strip()
    )


@router.get("/export/excel", summary="导出知识库 Excel")
def export_knowledge_excel(
    status: str | None = Query(None, description="状态筛选"),
    category_id: str | None = Query(None, description="分类ID"),
    applicable_category_ids: list[str] | None = Query(None, description="适用类目ID，可多选"),
    brand_ids: list[str] | None = Query(None, description="适用品牌ID，可多选"),
    model_ids: list[str] | None = Query(None, description="适用机型ID，可多选"),
    keyword: str | None = Query(
        None,
        description="主标题、副标题、正文、关联标准项、场景标签或分类名称关键词搜索",
    ),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
    current_user: User = Depends(get_current_user),
):
    if not _has_knowledge_export_filter(
        status=status,
        category_id=category_id,
        applicable_category_ids=applicable_category_ids,
        brand_ids=brand_ids,
        model_ids=model_ids,
        keyword=keyword,
    ):
        raise HTTPException(
            status_code=422,
            detail="请至少设置一项筛选条件后再导出，避免误导出全部知识。",
        )
    query = _filtered_knowledge_query(
        db,
        current_user,
        status=status,
        category_id=category_id,
        applicable_category_ids=applicable_category_ids,
        brand_ids=brand_ids,
        model_ids=model_ids,
        keyword=keyword,
    )
    items = (
        query.options(joinedload(Knowledge.category))
        .order_by(Knowledge.id.asc())
        .all()
    )
    filename = f"知识库导出_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        BytesIO(build_knowledge_export_workbook(items)),
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(filename)
            )
        },
    )


@router.get(
    "",
    response_model=list[KnowledgeResponse],
    responses={
        200: {
            "headers": {
                "X-Total-Count": {
                    "description": "符合当前筛选条件的知识总数",
                    "schema": {"type": "integer"},
                }
            }
        }
    },
    summary="查询知识条目列表",
    description="支持按状态、知识分类、适用类目、品牌和机型筛选，分页查询",
)
def list_knowledge(
    response: Response,
    status: str | None = Query(None, description="状态筛选"),
    category_id: str | None = Query(None, description="分类ID"),
    applicable_category_ids: list[str] | None = Query(None, description="适用类目ID，可多选"),
    brand_ids: list[str] | None = Query(None, description="适用品牌ID，可多选"),
    model_ids: list[str] | None = Query(None, description="适用机型ID，可多选"),
    keyword: str | None = Query(
        None,
        description="主标题、副标题、正文、关联标准项、场景标签或分类名称关键词搜索",
    ),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页条数"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
    current_user: User = Depends(get_current_user),
):
    q = _filtered_knowledge_query(
        db,
        current_user,
        status=status,
        category_id=category_id,
        applicable_category_ids=applicable_category_ids,
        brand_ids=brand_ids,
        model_ids=model_ids,
        keyword=keyword,
    )
    response.headers["X-Total-Count"] = str(q.count())
    items = q.order_by(Knowledge.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return [_to_response(i) for i in items]


@router.get(
    "/review:selection",
    response_model=KnowledgeReviewSelectionResponse,
    summary="获取全部待审核知识ID",
)
def list_review_selection(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:approve")),
):
    knowledge_ids = [
        item_id
        for (item_id,) in (
            db.query(Knowledge.id)
            .filter(Knowledge.status == KnowledgeStatus.REVIEW)
            .order_by(Knowledge.created_at.desc())
            .all()
        )
    ]
    return KnowledgeReviewSelectionResponse(
        total=len(knowledge_ids),
        knowledge_ids=knowledge_ids,
    )


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
        "category_id": item.category_id,
        "status": item.status.value,
        "applicable_scenes": deepcopy(item.applicable_scenes or []),
        "applicable_categories": deepcopy(item.applicable_categories or []),
        "applicable_brands": deepcopy(item.applicable_brands or []),
        "applicable_models": deepcopy(item.applicable_models or []),
        "related_standard_items": deepcopy(item.related_standard_items or []),
        "source_topic_key": item.source_topic_key,
        "source_record_id": item.source_record_id,
        "source_knowledge_key": item.source_knowledge_key,
        "source_fields": deepcopy(getattr(item, "source_fields", None) or {}),
    }


def _approval_change_log(
    item: Knowledge,
    *,
    reviewed_by: str,
    reviewed_at: datetime,
) -> KnowledgeChangeLog:
    """记录待审核知识成功发布的审核人、审核时间和状态变化。"""
    return KnowledgeChangeLog(
        id=f"kcl-{uuid.uuid4().hex[:12]}",
        knowledge_id=item.id,
        changed_by=reviewed_by,
        changed_fields=["status"],
        before_data={"status": KnowledgeStatus.REVIEW.value},
        after_data={"status": KnowledgeStatus.PUBLISHED.value},
        created_at=reviewed_at,
    )


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
    _require_manual_applicable_category(
        source=item.source,
        category_id=updates.get("category_id", item.category_id),
        applicable_categories=updates.get(
            "applicable_categories",
            item.applicable_categories,
        ),
    )
    try:
        for field, val in updates.items():
            if field == "content":
                normalized = _normalize_content(val)
                setattr(item, field, normalized)
                _persist_temp_media(
                    db,
                    item,
                    normalized,
                    current_user.username,
                )
                # 同步 content.blocks 里的 alt/caption 回 media 表
                _sync_media_meta(db, item.id, normalized)
                referenced_media = _referenced_media_filenames(normalized)
                for media in list(item.media):
                    if media.filename not in referenced_media:
                        enqueue_media_deletion(
                            db,
                            media.file_path,
                            media.filename,
                            storage_backend=media_storage.backend,
                        )
                        db.delete(media)
            elif field == "status":
                setattr(item, field, KnowledgeStatus(val))
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
    except Exception:
        db.rollback()
        raise
    db.refresh(item)
    return _to_response(item)


@router.delete("/{knowledge_id}", status_code=204, summary="删除知识条目")
def delete_knowledge(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:deprecate"))):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    try:
        for media in item.media:
            enqueue_media_deletion(
                db,
                media.file_path,
                media.filename,
                storage_backend=media_storage.backend,
            )
        db.delete(item)
        db.commit()
    except Exception:
        db.rollback()
        raise


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
    item.deduplication_metadata = _deduplication_metadata(
        decision,
        confirmed_by=current_user.username if confirm_dedup_review else None,
    )
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
def approve_knowledge(
    knowledge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:approve")),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if item.status != KnowledgeStatus.REVIEW:
        raise HTTPException(400, "只有审核中状态才能审批通过")
    pending_matches = _pending_deduplication_matches(item)
    if pending_matches:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DUPLICATE_CONFIRMATION_REQUIRED",
                "message": _deduplication_confirmation_message(pending_matches),
            },
        )
    ensure_embedding(db, item)
    ensure_search_embeddings(db, item)
    reviewed_at = datetime.utcnow()
    item.status = KnowledgeStatus.PUBLISHED
    item.updated_by = current_user.username
    item.updated_at = reviewed_at
    db.add(
        _approval_change_log(
            item,
            reviewed_by=current_user.username,
            reviewed_at=reviewed_at,
        )
    )
    db.commit()
    db.refresh(item)
    return _to_response(item)


@router.post(
    "/review:batch-approve",
    response_model=KnowledgeBatchApproveResponse,
    summary="批量通过待审核知识",
)
def batch_approve_knowledge(
    body: KnowledgeBatchApprove,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:approve")),
):
    approved = failed = reused = 0
    results: list[KnowledgeBatchApproveResult] = []
    seen_ids: set[str] = set()

    for knowledge_id in body.knowledge_ids:
        if knowledge_id in seen_ids:
            reused += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="reused",
                    error_code="DUPLICATE_SELECTION",
                    error_message="该知识已在本次批量审核中处理。",
                )
            )
            continue
        seen_ids.add(knowledge_id)

        item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
        if not item:
            failed += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="failed",
                    error_code="KNOWLEDGE_NOT_FOUND",
                    error_message="知识条目不存在。",
                )
            )
            continue
        if item.status == KnowledgeStatus.PUBLISHED:
            reused += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="reused",
                    error_code="ALREADY_PUBLISHED",
                    error_message="该知识已发布。",
                )
            )
            continue
        if item.status != KnowledgeStatus.REVIEW:
            failed += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="failed",
                    error_code="STATUS_NOT_REVIEW",
                    error_message="只有待审核状态的知识可以批量通过。",
                )
            )
            continue
        pending_matches = _pending_deduplication_matches(item)
        if pending_matches:
            failed += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="failed",
                    error_code="DUPLICATE_CONFIRMATION_REQUIRED",
                    error_message=_deduplication_confirmation_message(pending_matches),
                )
            )
            continue
        try:
            ensure_embedding(db, item)
            ensure_search_embeddings(db, item)
            reviewed_at = datetime.utcnow()
            item.status = KnowledgeStatus.PUBLISHED
            item.updated_by = current_user.username
            item.updated_at = reviewed_at
            db.add(
                _approval_change_log(
                    item,
                    reviewed_by=current_user.username,
                    reviewed_at=reviewed_at,
                )
            )
            db.commit()
            approved += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="approved",
                )
            )
        except EmbeddingServiceUnavailable as exc:
            db.rollback()
            failed += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="failed",
                    error_code="EMBEDDING_UNAVAILABLE",
                    error_message=f"向量服务不可用，未发布：{exc}",
                )
            )
        except Exception:
            db.rollback()
            logger.exception("Batch approval failed for knowledge %s", knowledge_id)
            failed += 1
            results.append(
                KnowledgeBatchApproveResult(
                    knowledge_id=knowledge_id,
                    status="failed",
                    error_code="APPROVE_FAILED",
                    error_message="批量审核失败，请稍后重试。",
                )
            )

    return KnowledgeBatchApproveResponse(
        approved=approved,
        failed=failed,
        reused=reused,
        results=results,
    )


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
    filename = f"{uuid.uuid4().hex}{ext}"
    try:
        storage_key = await run_in_threadpool(
            media_storage.put,
            filename,
            content,
            file.content_type
            or ("video/mp4" if media_type == "video" else "image/png"),
        )
    except MediaStorageError as exc:
        raise HTTPException(502, "媒体存储服务不可用") from exc

    media = KnowledgeMedia(
        id=f"media-{uuid.uuid4().hex[:8]}",
        knowledge_id=knowledge_id,
        media_type=media_type,
        filename=filename,
        original_name=file.filename or filename,
        file_path=storage_key,
        file_size=len(content),
        mime_type=file.content_type or ("video/mp4" if media_type == "video" else "image/png"),
        alt=alt or filename,
        caption=caption or '',
    )
    try:
        db.add(media)
        db.commit()
    except Exception:
        db.rollback()
        await run_in_threadpool(
            delete_media_immediately_or_enqueue,
            storage_key,
            filename,
            storage=media_storage,
        )
        raise
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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    content, extension = await _read_validated_upload(file, media_type)
    object_id = uuid.uuid4().hex
    temp_id = f"temp-{object_id}"
    filename = f"{object_id}{extension}"
    mime_type = file.content_type or (
        "video/mp4" if media_type == "video" else "image/png"
    )
    ready_ttl = max(1, settings.MEDIA_UPLOAD_STAGING_TTL_SECONDS)
    active_ttl = max(
        ready_ttl * 2,
        settings.MEDIA_UPLOAD_ACTIVE_TTL_SECONDS,
    )
    staging = MediaUploadStaging(
        id=temp_id,
        username=current_user.username,
        storage_backend=media_storage.backend,
        storage_key="",
        filename=filename,
        status="uploading",
        media_type=media_type,
        original_name=file.filename or filename,
        file_size=len(content),
        mime_type=mime_type,
        alt=alt,
        caption=caption,
        expires_at=datetime.utcnow() + timedelta(seconds=active_ttl),
    )
    try:
        db.add(staging)
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Hold the staging row lock across the object upload. Expiry workers use
    # SKIP LOCKED, so they cannot retire this lease while put() is in flight.
    db.refresh(staging, with_for_update=True)
    try:
        storage_key = await run_in_threadpool(
            media_storage.put,
            filename,
            content,
            mime_type,
        )
    except MediaStorageError as exc:
        try:
            staging.expires_at = datetime.utcnow()
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "Failed to expire media staging after upload failure: %s",
                temp_id,
            )
        raise HTTPException(502, "媒体存储服务不可用") from exc

    staging.storage_key = storage_key
    staging.status = "ready"
    staging.expires_at = datetime.utcnow() + timedelta(seconds=ready_ttl)
    try:
        db.commit()
    except Exception:
        db.rollback()
        try:
            failed_staging = (
                db.query(MediaUploadStaging)
                .filter(MediaUploadStaging.id == temp_id)
                .with_for_update()
                .first()
            )
            if failed_staging:
                failed_staging.expires_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "Failed to expire media staging after finalize failure: %s",
                temp_id,
            )
        await run_in_threadpool(
            delete_media_immediately_or_enqueue,
            storage_key,
            filename,
            storage=media_storage,
        )
        raise
    return {
        "id": temp_id,
        "filename": temp_id,
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
    storage_key = media.file_path
    filename = media.filename
    normalized = _normalize_content(deepcopy(item.content))
    original_block_count = len(normalized.get("blocks", []))
    normalized["blocks"] = [
        block
        for block in normalized.get("blocks", [])
        if not (
            isinstance(block.get("media_id"), str)
            and block["media_id"].replace("/uploads/", "", 1) == filename
        )
    ]
    content_changed = len(normalized["blocks"]) != original_block_count
    if content_changed:
        item.content = normalized
        item.updated_at = datetime.utcnow()
    try:
        enqueue_media_deletion(
            db,
            storage_key,
            filename,
            storage_backend=media_storage.backend,
        )
        db.delete(media)
        if content_changed:
            ensure_embedding(db, item)
            ensure_search_embeddings(db, item)
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---- 候选池 ----

@router.post("/candidates", response_model=KnowledgeResponse, status_code=201, summary="提交候选知识")
def submit_candidate(
    body: CandidateSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:create")),
):
    decision = _check_manual_deduplication(
        db,
        title=body.title,
        subtitles=[],
        content=_normalize_content(body.content),
        scene_tags=body.applicable_scenes,
        confirm_dedup_review=body.confirm_dedup_review,
    )

    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        content=_normalize_content(body.content),
        category_id=body.category_id,
        status=KnowledgeStatus.REVIEW,
        applicable_scenes=body.applicable_scenes,
        source=body.source,
        source_session_id=body.source_session_id,
        related_standard_items=body.related_standard_items,
        created_by=current_user.username,
        updated_by=current_user.username,
        deduplication_metadata=_deduplication_metadata(
            decision,
            confirmed_by=(
                current_user.username if body.confirm_dedup_review else None
            ),
        ),
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
            score=round(score, 6),
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

