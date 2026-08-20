import json
import re
import uuid
import logging
import string
import hashlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
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
from app.routes.manhattan import (
    cached_applicable_category_keys,
    cached_manhattan_options_snapshot,
)
from app.models.user import User
from app.models.knowledge import (
    Category, Knowledge, KnowledgeStatus,
    KnowledgeTag, KnowledgeMedia, MediaUploadStaging,
    KnowledgeDeduplicationFeedback, KnowledgeChangeLog, KnowledgeImportTask,
)
from app.core.config import settings
from app.services.embedding import EmbeddingServiceUnavailable, embed_texts
from app.services.applicability import build_applicability_canonicalizer
from app.services.knowledge_dedup import (
    DedupDecision,
    build_dedup_documents,
    build_search_documents_for_fields,
    check_duplicate,
    content_hash_for_text,
    ensure_embedding,
    ensure_search_embeddings,
    save_embedding,
    search_embeddings,
)
from app.services.knowledge_excel import (
    DEPRECATED_SOURCE_STATUS,
    HEADER_ALIASES,
    MAX_IMPORT_FILE_BYTES,
    KnowledgeExcelError,
    IMPORTABLE_SOURCE_STATUS,
    build_knowledge_update_template,
    build_model_configuration_import_template,
    build_knowledge_export_workbook,
    build_knowledge_import_template,
    parse_model_configuration_workbook,
    parse_knowledge_workbook,
    parse_knowledge_update_workbook,
)
from app.services.model_configuration import (
    MODEL_CONFIGURATION_ATTRIBUTE_FIELDS,
    ModelConfigurationSyncError,
    acquire_model_configuration_write_lock,
    parse_model_configuration_payload,
    sync_model_configurations,
)
from app.services.media_deletion import (
    delete_media_immediately_or_enqueue,
    enqueue_media_deletion,
)
from app.services.media_storage import MediaStorageError, get_media_storage
from app.schemas.knowledge import (
    BusinessType,
    KnowledgeImportType,
    KnowledgeOrigin,
    KnowledgeCreate, KnowledgeUpdate, KnowledgeResponse,
    ModelConfigurationUpdate,
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


@dataclass
class _ImportEmbeddingBundle:
    """Vectors prepared before a row is processed, keyed by exact source text."""

    dedup_vectors: tuple[list[float], list[float], list[float]] | None = None
    search_vectors: dict[tuple[str, int, str], list[float]] | None = None
    error: Exception | None = None


@dataclass
class _ImportEmbeddingPlan:
    row_number: int
    dedup_texts: tuple[str, str, str]
    search_documents: list[tuple[str, int, str]]

    @property
    def texts(self) -> list[str]:
        return [
            *self.dedup_texts,
            *(text for _, _, text in self.search_documents),
        ]

    @property
    def character_count(self) -> int:
        return sum(len(text) for text in self.texts)


class _ImportTaskLeaseLost(RuntimeError):
    """Raised when a persisted import task can no longer be renewed safely."""


class _ImportTaskRetryableError(RuntimeError):
    """Raised when a transient dependency failure should retry the whole task."""


_RETRYABLE_IMPORT_RESULT_CODES = {
    "EMBEDDING_UNAVAILABLE",
    "DEDUP_UNAVAILABLE",
}


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
BUSINESS_TYPE_LABELS = {
    "self_operated": "自营回收",
    "aggregated": "聚合回收",
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


def _applicable_option_id(value) -> str:
    if isinstance(value, dict):
        for key in ("categoryId", "category_id", "id", "code", "value"):
            raw = value.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        return ""
    return str(value or "").strip()


def _validate_business_applicable_categories(
    *,
    business_type: str,
    applicable_categories,
) -> None:
    selected_values = {
        option_id.strip()
        for option_id in (
            _applicable_option_id(value)
            for value in (applicable_categories or [])
        )
        if option_id.strip()
    }
    if not selected_values:
        return
    allowed_keys = cached_applicable_category_keys(business_type)
    business_label = BUSINESS_TYPE_LABELS.get(business_type, business_type)
    if not allowed_keys:
        raise HTTPException(
            status_code=422,
            detail=f"{business_label}适用类目缓存尚未准备，请先登录 Manhattan 并刷新类目缓存。",
        )
    invalid_values = sorted(
        value
        for value in selected_values
        if value.casefold() not in allowed_keys
    )
    if invalid_values:
        raise HTTPException(
            status_code=422,
            detail=(
                f"所选适用类目不属于{business_label}："
                + "、".join(invalid_values[:5])
            ),
        )


def _manhattan_option_text(option, *keys: str) -> str:
    if not isinstance(option, dict):
        return ""
    for key in keys:
        value = option.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _resolve_model_configuration_scope(
    *,
    category_id: str,
    brand_id: str,
    model_id: str,
) -> dict[str, str]:
    """按自营 Manhattan 缓存解析机型配置的可信名称和父子关系。"""
    snapshot = cached_manhattan_options_snapshot()
    business_options = (
        snapshot.get("options_by_business_type", {}).get("self_operated")
        if isinstance(snapshot, dict)
        else None
    )
    categories = (
        business_options.get("applicable_categories", [])
        if isinstance(business_options, dict)
        else []
    )
    if not categories:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MANHATTAN_CACHE_UNAVAILABLE",
                "message": "自营类目缓存尚未准备，暂不能保存机型配置信息。",
            },
        )

    category_matches = [
        option
        for option in categories
        if _manhattan_option_text(
            option,
            "categoryId",
            "id",
            "code",
            "value",
        )
        == category_id
    ]
    if len(category_matches) != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_CONFIGURATION_CATEGORY_INVALID",
                "message": f"类目ID {category_id} 不在当前自营类目缓存中。",
            },
        )
    category_name = _manhattan_option_text(
        category_matches[0],
        "categoryName",
        "name",
        "label",
        "title",
        "text",
    )
    if not category_name:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MANHATTAN_CACHE_INCOMPLETE",
                "message": f"类目ID {category_id} 缺少名称，暂不能保存。",
            },
        )

    brands_by_category = business_options.get("brands_by_category", {})
    category_brands = (
        brands_by_category.get(category_id, [])
        if isinstance(brands_by_category, dict)
        else []
    )
    if not category_brands:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MANHATTAN_BRAND_CACHE_UNAVAILABLE",
                "message": f"类目ID {category_id} 的品牌缓存尚未准备。",
            },
        )
    brand_matches = [
        option
        for option in category_brands
        if _manhattan_option_text(
            option,
            "brandId",
            "id",
            "code",
            "value",
        )
        == brand_id
    ]
    if len(brand_matches) != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_CONFIGURATION_BRAND_SCOPE_INVALID",
                "message": (
                    f"品牌ID {brand_id} 不属于当前类目ID {category_id}。"
                ),
            },
        )
    brand_name = _manhattan_option_text(
        brand_matches[0],
        "brandName",
        "name",
        "label",
        "title",
        "text",
    )
    if not brand_name:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MANHATTAN_CACHE_INCOMPLETE",
                "message": f"品牌ID {brand_id} 缺少名称，暂不能保存。",
            },
        )

    models = business_options.get("models", [])
    if not models:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MANHATTAN_MODEL_CACHE_UNAVAILABLE",
                "message": "自营机型缓存尚未准备，暂不能保存。",
            },
        )
    model_matches = [
        option
        for option in models
        if (
            _manhattan_option_text(
                option,
                "modelId",
                "id",
                "code",
                "value",
            )
            == model_id
            and _manhattan_option_text(
                option,
                "categoryId",
                "category_id",
            )
            == category_id
            and _manhattan_option_text(
                option,
                "brandId",
                "brand_id",
            )
            == brand_id
        )
    ]
    if len(model_matches) != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_CONFIGURATION_MODEL_SCOPE_INVALID",
                "message": (
                    f"机型ID {model_id} 不属于当前类目ID {category_id} "
                    f"和品牌ID {brand_id}。"
                ),
            },
        )
    model_name = _manhattan_option_text(
        model_matches[0],
        "modelName",
        "name",
        "label",
        "title",
        "text",
    )
    if not model_name:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MANHATTAN_CACHE_INCOMPLETE",
                "message": f"机型ID {model_id} 缺少名称，暂不能保存。",
            },
        )
    return {
        "category_id": category_id,
        "category_name": category_name,
        "brand_id": brand_id,
        "brand_name": brand_name,
        "model_id": model_id,
        "model_name": model_name,
    }


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
        query = db.query(Knowledge).filter(identifier_columns[label] == value)
        knowledge_origin = str(getattr(row, "knowledge_origin", "") or "").strip()
        if knowledge_origin:
            query = query.filter(Knowledge.knowledge_origin == knowledge_origin)
        business_type = str(getattr(row, "business_type", "") or "").strip()
        if business_type:
            query = query.filter(Knowledge.business_type == business_type)
        matches = query.all()
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
        "block_threshold": decision.block_threshold,
        "review_threshold": decision.review_threshold,
        "matches": [
            {
                "knowledge_id": match.knowledge_id,
                "title": match.title,
                "status": match.status,
                "knowledge_origin": match.knowledge_origin,
                "business_type": match.business_type,
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
    knowledge_origin: str,
    business_type: str,
    exclude_knowledge_id: str | None = None,
    confirm_dedup_review: bool = False,
    allow_duplicate_review: bool = False,
    embedding_vectors: tuple[list[float], list[float], list[float]] | None = None,
) -> DedupDecision:
    try:
        decision = check_duplicate(
            db,
            title=title,
            subtitles=subtitles,
            content=content,
            scene_tags=scene_tags,
            knowledge_origin=knowledge_origin,
            business_type=business_type,
            exclude_knowledge_id=exclude_knowledge_id,
            embedding_vectors=embedding_vectors,
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


def _model_configuration_detail(item: Knowledge) -> dict | None:
    if getattr(item, "knowledge_origin", "") != "model_configuration":
        return None
    source_fields = (
        item.source_fields
        if isinstance(getattr(item, "source_fields", None), dict)
        else {}
    )
    content = str(source_fields.get("综合内容") or "").strip()
    if not content:
        normalized_content = _normalize_content(item.content)
        content = "\n".join(
            str(block.get("value") or "")
            for block in normalized_content.get("blocks", [])
            if block.get("type") == "text"
        ).strip()
    return {
        "title": item.title,
        "content": content,
        "category_id": str(source_fields.get("品类ID") or "").strip(),
        "category_name": str(source_fields.get("品类") or "").strip(),
        "brand_id": str(source_fields.get("品牌ID") or "").strip(),
        "brand_name": str(source_fields.get("品牌") or "").strip(),
        "model_id": str(source_fields.get("型号ID") or "").strip(),
        "model_name": str(source_fields.get("型号") or "").strip(),
        "attributes": {
            field: str(source_fields.get(field) or "").strip()
            for field in MODEL_CONFIGURATION_ATTRIBUTE_FIELDS
        },
    }


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
        "knowledge_origin": getattr(item, "knowledge_origin", "business_accumulation"),
        "business_type": item.business_type,
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
        "model_configuration": _model_configuration_detail(item),
    }


# ---- CRUD ----

def _create_knowledge_item(
    body: KnowledgeCreate,
    db: Session,
    current_user: User,
    *,
    source: str = "manual",
    allow_duplicate_review: bool = False,
    embedding_vectors: tuple[list[float], list[float], list[float]] | None = None,
    search_embedding_vectors: dict[tuple[str, int, str], list[float]] | None = None,
    ensure_search_index: bool = True,
) -> Knowledge:
    if body.knowledge_origin == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工创建或普通导入。",
        )
    _require_manual_applicable_category(
        source=source,
        category_id=body.category_id,
        applicable_categories=body.applicable_categories,
    )
    _validate_business_applicable_categories(
        business_type=body.business_type,
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
        knowledge_origin=body.knowledge_origin,
        scene_tags=body.applicable_scenes or [],
        business_type=body.business_type,
        confirm_dedup_review=body.confirm_dedup_review,
        allow_duplicate_review=allow_duplicate_review,
        embedding_vectors=embedding_vectors,
    )
    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        subtitles=body.subtitles or [],
        content=normalized_content,
        knowledge_origin=body.knowledge_origin,
        business_type=body.business_type,
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
    if ensure_search_index:
        ensure_search_embeddings(
            db,
            item,
            precomputed_vectors=search_embedding_vectors,
        )
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


@router.get("/import/template", summary="下载 Excel 批量处理模板")
def download_knowledge_import_template(
    import_type: KnowledgeImportType = Query(
        "knowledge",
        description=(
            "导入类型：knowledge=普通知识，"
            "knowledge_update=按知识ID批量修改，"
            "model_configuration=机型配置信息"
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_import_type_permission(import_type, current_user)
    if import_type == "model_configuration":
        payload = build_model_configuration_import_template()
        filename = "model-configuration-import-template.xlsx"
    elif import_type == "knowledge_update":
        categories = db.query(Category).order_by(
            Category.level,
            Category.sort_order,
        ).all()
        payload = build_knowledge_update_template(categories)
        filename = "knowledge-update-template.xlsx"
    else:
        categories = db.query(Category).order_by(
            Category.level,
            Category.sort_order,
        ).all()
        payload = build_knowledge_import_template(categories)
        filename = "knowledge-import-template.xlsx"
    return StreamingResponse(
        BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@router.post(
    "/import/excel",
    response_model=KnowledgeImportTaskResponse,
    status_code=202,
    summary="上传 Excel 并创建后台处理任务",
)
async def import_knowledge_excel(
    file: UploadFile = File(...),
    import_type: KnowledgeImportType = Form("knowledge"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_import_type_permission(import_type, current_user)
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
        import_type=import_type,
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
        import_type=(
            getattr(task, "import_type", None)
            or "knowledge"
        ),
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
        created=int(getattr(task, "created", 0) or 0),
        updated=int(getattr(task, "updated", 0) or 0),
        unchanged=int(getattr(task, "unchanged", 0) or 0),
        retry_rows=[
            int(row_number)
            for row_number in (task.retry_rows or [])
        ],
        attempt_count=task.attempt_count,
        next_attempt_at=task.next_attempt_at,
        error_message=task.error_message or "",
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        results=[
            ExcelImportRowResult.model_validate(result)
            for result in (
                task.results or []
            )[:max(1, min(result_limit, 5000))]
        ]
        if include_results
        else [],
    )


def _can_view_import_task(task: KnowledgeImportTask, current_user: User) -> bool:
    return (
        current_user.role == "super_admin"
        or task.created_by == current_user.username
    )


def _can_access_import_tasks(current_user: User) -> bool:
    return (
        has_permission(current_user, "knowledge:create")
        or has_permission(current_user, "knowledge:edit_published")
    )


def _require_import_task_access(
    current_user: User = Depends(get_current_user),
) -> User:
    if not _can_access_import_tasks(current_user):
        raise HTTPException(status_code=403, detail="无权访问 Excel 后台任务。")
    return current_user


def _require_import_type_permission(
    import_type: KnowledgeImportType,
    current_user: User,
) -> None:
    permission = (
        "knowledge:edit_published"
        if import_type == "knowledge_update"
        else "knowledge:create"
    )
    if not has_permission(current_user, permission):
        raise HTTPException(status_code=403, detail="Permission denied.")


@router.get(
    "/import/tasks",
    response_model=KnowledgeImportTaskListResponse,
    summary="查看 Excel 后台任务",
)
def list_knowledge_import_tasks(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_import_task_access),
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
    summary="查看 Excel 后台任务详情",
)
def get_knowledge_import_task(
    task_id: str,
    include_results: bool = Query(False, description="是否返回逐行处理结果"),
    result_limit: int = Query(
        100,
        ge=1,
        le=5000,
        description="最多返回的逐行结果数",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_import_task_access),
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


def _retryable_import_result(result: dict) -> bool:
    error_code = str(result.get("error_code") or "").strip()
    if error_code in _RETRYABLE_IMPORT_RESULT_CODES:
        return True
    error_message = str(result.get("error_message") or "")
    return (
        error_code == "IMPORT_REJECTED"
        and (
            "Embedding 服务不可用" in error_message
            or "Embedding service is unavailable" in error_message
        )
    )


@router.post(
    "/import/tasks/{task_id}/cancel",
    response_model=KnowledgeImportTaskResponse,
    summary="取消 Excel 后台任务",
)
def cancel_knowledge_import_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_import_task_access),
):
    task = (
        db.query(KnowledgeImportTask)
        .filter(KnowledgeImportTask.id == task_id)
        .with_for_update()
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="导入任务不存在。")
    if not _can_view_import_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权取消该导入任务。")
    if task.status == "cancelled":
        return _to_import_task_response(task)
    if task.status not in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail="只有排队中或处理中的导入任务可以取消。",
        )

    now = datetime.utcnow()
    task.status = "cancelled"
    task.attempt_count = int(task.attempt_count or 0) + 1
    task.lease_expires_at = None
    task.completed_at = now
    task.error_message = ""
    task.updated_at = now
    db.commit()
    db.refresh(task)
    return _to_import_task_response(task)


@router.post(
    "/import/tasks/{task_id}/retry-failed",
    response_model=KnowledgeImportTaskResponse,
    summary="重试 Excel 任务中的基础设施失败行",
)
def retry_failed_knowledge_import_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_import_task_access),
):
    task = (
        db.query(KnowledgeImportTask)
        .filter(KnowledgeImportTask.id == task_id)
        .with_for_update()
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="导入任务不存在。")
    if not _can_view_import_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权重试该导入任务。")
    if task.status != "completed_with_errors":
        raise HTTPException(
            status_code=409,
            detail="只有“完成有失败”的导入任务可以重试失败行。",
        )

    file_content = bytes(task.file_content or b"")
    if (
        len(file_content) != int(task.file_size or 0)
        or hashlib.sha256(file_content).hexdigest() != task.file_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="原始 Excel 文件完整性校验失败，无法安全重试。",
        )

    results = list(task.results or [])
    try:
        validated_results = [
            ExcelImportRowResult.model_validate(result)
            for result in results
        ]
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="任务逐行结果格式异常，无法安全重试。",
        ) from exc

    if int(task.processed_rows or 0) != len(validated_results):
        raise HTTPException(
            status_code=409,
            detail="任务进度与逐行结果数量不一致，无法安全重试。",
        )
    result_row_numbers = [result.row for result in validated_results]
    if (
        any(row_number < 2 for row_number in result_row_numbers)
        or len(set(result_row_numbers)) != len(result_row_numbers)
    ):
        raise HTTPException(
            status_code=409,
            detail="任务逐行结果缺少唯一 Excel 行号，无法安全重试。",
        )

    failed_pairs = [
        (raw_result, validated_result)
        for raw_result, validated_result in zip(
            results,
            validated_results,
            strict=True,
        )
        if validated_result.status == "failed"
    ]
    failed_results = [raw_result for raw_result, _ in failed_pairs]
    if not failed_results:
        raise HTTPException(
            status_code=409,
            detail="该任务没有可重试的失败行。",
        )
    if int(task.failed or 0) != len(failed_results):
        raise HTTPException(
            status_code=409,
            detail="任务失败计数与逐行结果不一致，无法安全重试。",
        )
    non_retryable = [
        result for result in failed_results
        if not _retryable_import_result(result)
    ]
    if non_retryable:
        rows = "、".join(
            str(result.get("row") or "?")
            for result in non_retryable[:10]
        )
        raise HTTPException(
            status_code=409,
            detail=f"失败行包含数据或业务校验错误，不能自动重试：{rows}",
        )

    retry_rows = sorted(
        result.row
        for _, result in failed_pairs
    )
    categories = db.query(Category).order_by(
        Category.level,
        Category.sort_order,
    ).all()
    try:
        parser = (
            parse_knowledge_update_workbook
            if task.import_type == "knowledge_update"
            else parse_knowledge_workbook
        )
        workbook_rows = parser(file_content, categories)
    except KnowledgeExcelError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"原始 Excel 已无法重新解析，不能安全重试：{exc}",
        ) from exc
    if len(workbook_rows) != int(task.total_rows or 0):
        raise HTTPException(
            status_code=409,
            detail="原始 Excel 行数与任务记录不一致，无法安全重试。",
        )
    available_row_numbers = {
        int(row.row_number)
        for row in workbook_rows
    }
    missing_retry_rows = sorted(
        set(retry_rows) - available_row_numbers
    )
    if missing_retry_rows:
        raise HTTPException(
            status_code=409,
            detail=(
                "原始 Excel 缺少待重试行，无法安全重试："
                + "、".join(str(row) for row in missing_retry_rows[:20])
            ),
        )

    now = datetime.utcnow()
    remaining_pairs = [
        (raw_result, validated_result)
        for raw_result, validated_result in zip(
            results,
            validated_results,
            strict=True,
        )
        if validated_result.status != "failed"
    ]
    task.results = [
        raw_result
        for raw_result, _ in remaining_pairs
    ]
    task.retry_rows = retry_rows
    task.processed_rows = len(remaining_pairs)
    task.imported = sum(
        result.status in {
            "imported",
            "review_pending",
            "review_required",
        }
        for _, result in remaining_pairs
    )
    task.review_required = sum(
        result.status == "review_required"
        for _, result in remaining_pairs
    )
    task.pending_review = sum(
        result.status == "review_pending"
        for _, result in remaining_pairs
    )
    task.deprecated = sum(
        result.status == "deprecated"
        for _, result in remaining_pairs
    )
    task.created = sum(
        result.operation == "created"
        for _, result in remaining_pairs
    )
    task.updated = sum(
        result.operation == "updated"
        for _, result in remaining_pairs
    )
    task.unchanged = sum(
        result.operation == "unchanged"
        for _, result in remaining_pairs
    )
    task.failed = 0
    task.status = "queued"
    task.attempt_count = 0
    task.next_attempt_at = now
    task.lease_expires_at = None
    task.started_at = None
    task.completed_at = None
    task.error_message = ""
    task.updated_at = now
    db.commit()
    db.refresh(task)
    return _to_import_task_response(task)


def _task_lease_expiry(now: datetime) -> datetime:
    # One CPU embedding request may legitimately occupy the worker until the
    # import-specific timeout. Keep the lease alive beyond that request even
    # when an older deployment still configures the previous 120-second lease.
    # The auto provider may try both protocol variants before returning.
    provider_attempts = (
        2
        if settings.EMBEDDING_PROVIDER.strip().lower() == "auto"
        else 1
    )
    lease_seconds = max(
        30.0,
        float(settings.KNOWLEDGE_IMPORT_LEASE_SECONDS),
        (
            float(settings.KNOWLEDGE_IMPORT_EMBEDDING_TIMEOUT_SECONDS)
            * provider_attempts
            + 60.0
        ),
    )
    return now + timedelta(
        seconds=lease_seconds
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
    task.retry_rows = [
        int(row_number)
        for row_number in (task.retry_rows or [])
        if int(row_number) != result.row
    ]
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
    if result.operation == "created":
        task.created = (task.created or 0) + 1
    if result.operation == "updated":
        task.updated = (task.updated or 0) + 1
    if result.operation == "unchanged":
        task.unchanged = (task.unchanged or 0) + 1
    task.lease_expires_at = _task_lease_expiry(now)
    task.updated_at = now


def _excel_row_failure_result(row, exc: Exception) -> ExcelImportRowResult:
    if isinstance(exc, EmbeddingServiceUnavailable):
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=getattr(row, "knowledge_id", None) or None,
            error_code=(
                "EMBEDDING_UNAVAILABLE"
                if exc.retryable
                else "EMBEDDING_REJECTED"
            ),
            error_message=str(exc),
        )
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
            knowledge_id=getattr(row, "knowledge_id", None) or None,
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
            knowledge_id=getattr(row, "knowledge_id", None) or None,
            error_code="INVALID_ROW",
            error_message="数据校验失败，请检查分类和字段格式。",
        )
    if isinstance(exc, SourceKnowledgeMatchError):
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=getattr(row, "knowledge_id", None) or None,
            error_code=exc.code,
            error_message=str(exc),
        )
    if isinstance(exc, ValueError):
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=getattr(row, "knowledge_id", None) or None,
            error_code="INVALID_ROW",
            error_message=str(exc),
        )
    logger.exception("Excel knowledge import failed at row %s", row.row_number)
    return ExcelImportRowResult(
        row=row.row_number,
        title=row.title,
        status="failed",
        knowledge_id=getattr(row, "knowledge_id", None) or None,
        error_code="IMPORT_FAILED",
        error_message="导入失败，请检查服务日志。",
    )


def _excel_row_body(row) -> KnowledgeCreate:
    return KnowledgeCreate(
        title=row.title,
        subtitles=row.subtitles or [],
        content=row.content,
        knowledge_origin=row.knowledge_origin,
        business_type=row.business_type,
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


def _canonicalize_excel_rows_applicability(rows: list, cache: dict) -> None:
    """用同一缓存快照规范化一次任务内的全部有效行。"""
    canonicalizers = {}
    for row in rows:
        if not row.is_valid or row.source_status == DEPRECATED_SOURCE_STATUS:
            continue
        canonicalizer = canonicalizers.get(row.business_type)
        if canonicalizer is None:
            canonicalizer = build_applicability_canonicalizer(
                cache,
                row.business_type,
            )
            canonicalizers[row.business_type] = canonicalizer
        applicability = canonicalizer.canonicalize(
            category_values=row.applicable_categories,
            brand_values=row.applicable_brands,
            model_values=row.applicable_models,
        )
        row.applicable_categories = applicability["categories"]
        row.applicable_brands = applicability["brands"]
        row.applicable_models = applicability["models"]


def _build_import_embedding_plan(row) -> _ImportEmbeddingPlan:
    body = _excel_row_body(row)
    normalized_content = _normalize_content(body.content)
    dedup_texts = build_dedup_documents(body.title, normalized_content)
    search_documents = (
        build_search_documents_for_fields(
            body.title,
            body.subtitles or [],
            normalized_content,
        )
        if row.source_status == IMPORTABLE_SOURCE_STATUS
        else []
    )
    return _ImportEmbeddingPlan(
        row_number=row.row_number,
        dedup_texts=dedup_texts,
        search_documents=search_documents,
    )


def _store_import_embedding_vectors(
    plan: _ImportEmbeddingPlan,
    vectors: list[list[float]],
) -> _ImportEmbeddingBundle:
    expected_count = len(plan.texts)
    if len(vectors) != expected_count:
        raise ValueError(
            "Embedding result count does not match the import row plan."
        )
    search_vectors: dict[tuple[str, int, str], list[float]] = {}
    for (kind, index, text), vector in zip(
        plan.search_documents,
        vectors[3:],
    ):
        search_vectors[(kind, index, content_hash_for_text(text))] = vector
    return _ImportEmbeddingBundle(
        dedup_vectors=(
            vectors[0],
            vectors[1],
            vectors[2],
        ),
        search_vectors=search_vectors,
    )


def _import_embedding_batches(
    rows: list,
) -> list[list[tuple[object, _ImportEmbeddingPlan | None]]]:
    max_rows = max(1, settings.KNOWLEDGE_IMPORT_EMBEDDING_BATCH_ROWS)
    max_chars = max(settings.EMBEDDING_MAX_BATCH_CHARS, 1)
    batches: list[list[tuple[object, _ImportEmbeddingPlan | None]]] = []
    current: list[tuple[object, _ImportEmbeddingPlan | None]] = []
    current_chars = 0
    for row in rows:
        if not row.is_valid or row.source_status == DEPRECATED_SOURCE_STATUS:
            if current:
                batches.append(current)
                current = []
                current_chars = 0
            batches.append([(row, None)])
            continue
        try:
            plan = _build_import_embedding_plan(row)
        except Exception:
            if current:
                batches.append(current)
                current = []
                current_chars = 0
            batches.append([(row, None)])
            continue
        plan_chars = plan.character_count
        if current and (
            len(current) >= max_rows
            or current_chars + plan_chars > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append((row, plan))
        current_chars += plan_chars
    if current:
        batches.append(current)
    return batches


def _precompute_import_embeddings(
    rows: list,
    *,
    on_batch_complete: Callable[[int, int], None] | None = None,
) -> dict[int, _ImportEmbeddingBundle]:
    def embed_with_progress(texts: list[str]) -> list[list[float]]:
        if on_batch_complete is None:
            return embed_texts(
                texts,
                timeout_seconds=(
                    settings.KNOWLEDGE_IMPORT_EMBEDDING_TIMEOUT_SECONDS
                ),
            )
        return embed_texts(
            texts,
            on_batch_complete=on_batch_complete,
            timeout_seconds=(
                settings.KNOWLEDGE_IMPORT_EMBEDDING_TIMEOUT_SECONDS
            ),
        )

    bundles: dict[int, _ImportEmbeddingBundle] = {}
    for batch in _import_embedding_batches(rows):
        valid_plans = [
            (row, plan)
            for row, plan in batch
            if plan is not None and plan.dedup_texts[0]
        ]
        if not valid_plans:
            continue

        flat_texts: list[str] = []
        for _, plan in valid_plans:
            flat_texts.extend(plan.texts)
        try:
            flat_vectors = embed_with_progress(flat_texts)
            offset = 0
            for row, plan in valid_plans:
                count = len(plan.texts)
                bundles[row.row_number] = _store_import_embedding_vectors(
                    plan,
                    flat_vectors[offset : offset + count],
                )
                offset += count
        except _ImportTaskLeaseLost:
            raise
        except Exception as batch_exc:
            if (
                isinstance(batch_exc, EmbeddingServiceUnavailable)
                and batch_exc.retryable
            ):
                raise
            # A long document can make a combined request fail. Retry rows
            # individually so one bad row does not fail the whole task.
            for row, plan in valid_plans:
                try:
                    bundles[row.row_number] = _store_import_embedding_vectors(
                        plan,
                        embed_with_progress(plan.texts),
                    )
                except Exception as exc:
                    if isinstance(exc, _ImportTaskLeaseLost):
                        raise
                    if (
                        isinstance(exc, EmbeddingServiceUnavailable)
                        and exc.retryable
                    ):
                        raise
                    bundles[row.row_number] = _ImportEmbeddingBundle(error=exc)
    return bundles


def _process_excel_import_row(
    db: Session,
    row,
    current_user: User,
    *,
    embedding_bundle: _ImportEmbeddingBundle | None = None,
) -> ExcelImportRowResult:
    embedding_bundle = embedding_bundle or _ImportEmbeddingBundle()
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

    if embedding_bundle.error is not None:
        if isinstance(embedding_bundle.error, EmbeddingServiceUnavailable):
            raise embedding_bundle.error
        raise embedding_bundle.error

    body = _excel_row_body(row)
    item = _create_knowledge_item(
        body,
        db,
        current_user,
        source="excel",
        allow_duplicate_review=True,
        embedding_vectors=embedding_bundle.dedup_vectors,
        search_embedding_vectors=embedding_bundle.search_vectors,
        ensure_search_index=False,
    )
    _auto_publish_approved_source_excel(
        item,
        source_status=row.source_status,
        current_user=current_user,
    )
    if item.status == KnowledgeStatus.PUBLISHED:
        ensure_search_embeddings(
            db,
            item,
            precomputed_vectors=embedding_bundle.search_vectors,
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


_UPDATE_SNAPSHOT_TO_EXCEL_FIELD = {
    "title": "title",
    "subtitles": "subtitles",
    "content": "content",
    "knowledge_origin": "knowledge_origin",
    "business_type": "business_type",
    "category_id": "category",
    "applicable_scenes": "scenes",
    "applicable_categories": "applicable_categories",
    "applicable_brands": "brands",
    "applicable_models": "models",
    "related_standard_items": "related_standard_items",
}


def _source_fields_after_excel_update(
    item: Knowledge,
    row,
    changed_fields: list[str],
) -> dict:
    """同步已有追溯字段的对应值，不新增或覆盖系统字段。"""

    source_fields = deepcopy(
        item.source_fields
        if isinstance(item.source_fields, dict)
        else {}
    )
    for snapshot_field in changed_fields:
        excel_field = _UPDATE_SNAPSHOT_TO_EXCEL_FIELD.get(snapshot_field)
        if not excel_field:
            continue
        aliases = HEADER_ALIASES.get(excel_field, set())
        raw_value = next(
            (
                row.source_fields[alias]
                for alias in aliases
                if alias in row.source_fields
            ),
            None,
        )
        if raw_value is None:
            continue
        for alias in aliases:
            if alias in source_fields:
                source_fields[alias] = raw_value

    applicability_fields = {
        "applicable_scenes",
        "applicable_categories",
        "applicable_brands",
        "applicable_models",
    }
    if (
        applicability_fields.intersection(changed_fields)
        and "scope" not in row.provided_fields
    ):
        for alias in HEADER_ALIASES.get("scope", set()):
            source_fields.pop(alias, None)
    return source_fields


def _process_excel_update_row(
    db: Session,
    row,
    current_user: User,
) -> ExcelImportRowResult:
    if not row.is_valid:
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=row.knowledge_id or None,
            error_code=row.error_code,
            error_message=row.error_message,
        )

    item = (
        db.query(Knowledge)
        .filter(Knowledge.id == row.knowledge_id)
        .with_for_update()
        .first()
    )
    if not item:
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=row.knowledge_id,
            error_code="KNOWLEDGE_ID_NOT_FOUND",
            error_message=(
                f"未找到知识ID“{row.knowledge_id}”，该行不会新增知识。"
            ),
        )
    if item.knowledge_origin == "model_configuration":
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=item.id,
            error_code="KNOWLEDGE_ORIGIN_MANAGED",
            error_message=(
                "机型配置信息由专用 Excel 同步维护，"
                "不能通过知识批量修改覆盖。"
            ),
        )
    if not _can_edit_knowledge(item, current_user):
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=item.id,
            error_code="KNOWLEDGE_UPDATE_FORBIDDEN",
            error_message="当前账号无权修改该状态下的知识。",
        )
    if item.media:
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="failed",
            knowledge_id=item.id,
            error_code="LOCAL_MEDIA_BATCH_UPDATE_UNSUPPORTED",
            error_message=(
                "该知识包含本地上传的图片或视频，"
                "Excel 无法安全回填媒体，请在页面中单条编辑。"
            ),
        )

    provided_fields = set(row.provided_fields or set())
    next_values = {
        "title": row.title,
        "knowledge_origin": row.knowledge_origin,
        "business_type": row.business_type,
        "category_id": row.category_id,
        "content": _normalize_content(row.content),
    }
    optional_values = {
        "subtitles": row.subtitles or [],
        "applicable_scenes": row.applicable_scenes or [],
        "applicable_categories": row.applicable_categories or [],
        "applicable_brands": row.applicable_brands or [],
        "applicable_models": row.applicable_models or [],
        "related_standard_items": row.related_standard_items or [],
    }
    optional_columns = {
        "subtitles": {"subtitles"},
        "applicable_scenes": {"scenes", "scope"},
        "applicable_categories": {"applicable_categories"},
        "applicable_brands": {"brands"},
        "applicable_models": {"models"},
        "related_standard_items": {"related_standard_items"},
    }
    for field, columns in optional_columns.items():
        if columns.intersection(provided_fields):
            next_values[field] = optional_values[field]

    _require_manual_applicable_category(
        source=item.source,
        category_id=next_values["category_id"],
        applicable_categories=next_values.get(
            "applicable_categories",
            item.applicable_categories,
        ),
    )
    _validate_business_applicable_categories(
        business_type=next_values["business_type"],
        applicable_categories=next_values.get(
            "applicable_categories",
            item.applicable_categories,
        ),
    )

    before_data = _knowledge_snapshot(item)
    for field, value in next_values.items():
        setattr(item, field, value)

    core_after_data = _knowledge_snapshot(item)
    core_changed_fields = [
        field
        for field, before_value in before_data.items()
        if field != "source_fields"
        and before_value != core_after_data.get(field)
    ]
    if not core_changed_fields:
        return ExcelImportRowResult(
            row=row.row_number,
            title=row.title,
            status="imported",
            knowledge_id=item.id,
            operation="unchanged",
        )

    item.source_fields = _source_fields_after_excel_update(
        item,
        row,
        core_changed_fields,
    )
    after_data = _knowledge_snapshot(item)
    changed_at = datetime.utcnow()
    item.updated_by = current_user.username
    item.updated_at = changed_at
    change_log = _change_log_from_snapshots(
        item,
        changed_by=current_user.username,
        before_data=before_data,
        after_data=after_data,
        created_at=changed_at,
    )
    if change_log is not None:
        db.add(change_log)

    if {"title", "subtitles", "content"}.intersection(core_changed_fields):
        ensure_embedding(db, item)
        ensure_search_embeddings(db, item)

    return ExcelImportRowResult(
        row=row.row_number,
        title=row.title,
        status="imported",
        knowledge_id=item.id,
        operation="updated",
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


def _import_retry_delay_seconds(attempt_count: int) -> int:
    base_seconds = max(
        1,
        int(settings.KNOWLEDGE_IMPORT_RETRY_BASE_SECONDS),
    )
    max_seconds = max(
        base_seconds,
        int(settings.KNOWLEDGE_IMPORT_RETRY_MAX_SECONDS),
    )
    exponent = max(0, int(attempt_count) - 1)
    return min(max_seconds, base_seconds * (2 ** min(exponent, 20)))


def _mark_import_task_retry(
    task: KnowledgeImportTask,
    message: str,
    *,
    now: datetime,
) -> bool:
    max_attempts = max(1, int(settings.KNOWLEDGE_IMPORT_MAX_ATTEMPTS))
    attempt_count = int(task.attempt_count or 0)
    if attempt_count >= max_attempts:
        _mark_import_task_failed(
            task,
            (
                f"后台处理达到最大尝试次数（{attempt_count}/{max_attempts}）："
                f"{message}"
            ),
            now=now,
        )
        return False

    retry_delay = _import_retry_delay_seconds(attempt_count)
    task.status = "queued"
    task.error_message = (
        f"后台处理暂时失败，将在 {retry_delay} 秒后自动重试"
        f"（{attempt_count}/{max_attempts}）：{message}"
    )[:2000]
    task.lease_expires_at = None
    task.next_attempt_at = now + timedelta(seconds=retry_delay)
    task.completed_at = None
    task.updated_at = now
    return True


def _is_retryable_import_exception(exc: Exception) -> bool:
    if isinstance(exc, EmbeddingServiceUnavailable):
        return exc.retryable
    return (
        isinstance(exc, _ImportTaskRetryableError)
        or (
            isinstance(exc, HTTPException)
            and exc.status_code == 503
        )
    )


def _retryable_import_exception(exc: Exception) -> _ImportTaskRetryableError:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail)
        else:
            message = str(detail)
    else:
        message = str(exc)
    return _ImportTaskRetryableError(
        message or "Embedding 服务暂时不可用。"
    )


def _lock_import_task_attempt(
    db: Session,
    task_id: str,
    claimed_attempt: int,
) -> KnowledgeImportTask | None:
    """Reload and lock the claimed task before mutating its lifecycle state."""

    return (
        db.query(KnowledgeImportTask)
        .filter(
            KnowledgeImportTask.id == task_id,
            KnowledgeImportTask.attempt_count == claimed_attempt,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
        .first()
    )


def _owns_import_task_attempt(
    task: KnowledgeImportTask | None,
    claimed_attempt: int,
) -> bool:
    return bool(
        task
        and task.status == "running"
        and int(task.attempt_count or 0) == claimed_attempt
    )


def _process_model_configuration_import_task(
    db: Session,
    task_id: str,
    claimed_attempt: int,
) -> None:
    task = _lock_import_task_attempt(
        db,
        task_id,
        claimed_attempt,
    )
    if not _owns_import_task_attempt(task, claimed_attempt):
        return

    try:
        workbook = parse_model_configuration_workbook(
            bytes(task.file_content or b"")
        )
    except KnowledgeExcelError as exc:
        _mark_import_task_failed(
            task,
            str(exc),
            now=datetime.utcnow(),
        )
        db.commit()
        return

    if task.total_rows and task.total_rows != len(workbook.records):
        _mark_import_task_failed(
            task,
            "机型配置文件解析结果发生变化，任务已停止以避免重复写入。",
            now=datetime.utcnow(),
        )
        db.commit()
        return
    if int(task.processed_rows or 0) or list(task.results or []):
        _mark_import_task_failed(
            task,
            "机型配置整批任务存在非原子历史进度，已停止以避免部分覆盖。",
            now=datetime.utcnow(),
        )
        db.commit()
        return

    task.total_rows = len(workbook.records)
    task.error_message = ""
    task.lease_expires_at = _task_lease_expiry(datetime.utcnow())
    task.updated_at = datetime.utcnow()
    db.commit()

    task = _lock_import_task_attempt(
        db,
        task_id,
        claimed_attempt,
    )
    if not _owns_import_task_attempt(task, claimed_attempt):
        return
    # 机型配置必须整批原子提交，因此从同步开始到完成始终持有任务行锁。
    # 此阶段取消请求会等待事务结束；调用端只应向 queued 任务提供取消入口。
    try:
        sync_result = sync_model_configurations(
            db,
            workbook.records,
            actor=task.created_by,
        )
        if len(sync_result.items) != len(workbook.records):
            raise RuntimeError(
                "机型配置同步结果数量与工作簿记录数不一致。"
            )
        if (
            sync_result.total != len(workbook.records)
            or (
                sync_result.created
                + sync_result.updated
                + sync_result.unchanged
            )
            != sync_result.total
        ):
            raise RuntimeError("机型配置同步汇总数量不一致。")

        results: list[dict] = []
        for row_number, record, item_result in zip(
            workbook.row_numbers,
            workbook.records,
            sync_result.items,
            strict=True,
        ):
            if item_result.source_record_id != record.source_record_id:
                raise RuntimeError(
                    "机型配置同步结果顺序与工作簿记录不一致。"
                )
            results.append(
                ExcelImportRowResult(
                    row=row_number,
                    title=record.title,
                    status="imported",
                    knowledge_id=item_result.knowledge_id,
                    operation=item_result.operation,
                ).model_dump(mode="json")
            )

        completed_at = datetime.utcnow()
        task.results = results
        task.retry_rows = []
        task.processed_rows = sync_result.total
        task.imported = sync_result.total
        task.review_required = 0
        task.pending_review = 0
        task.deprecated = 0
        task.failed = 0
        task.created = sync_result.created
        task.updated = sync_result.updated
        task.unchanged = sync_result.unchanged
        task.status = "completed"
        task.error_message = ""
        task.lease_expires_at = None
        task.completed_at = completed_at
        task.updated_at = completed_at
        db.commit()
    except Exception as exc:
        db.rollback()
        task = _lock_import_task_attempt(
            db,
            task_id,
            claimed_attempt,
        )
        if not _owns_import_task_attempt(task, claimed_attempt):
            return
        task.results = []
        task.retry_rows = []
        task.processed_rows = int(task.total_rows or 0)
        task.imported = 0
        task.review_required = 0
        task.pending_review = 0
        task.deprecated = 0
        task.failed = int(task.total_rows or 0)
        task.created = 0
        task.updated = 0
        task.unchanged = 0
        if isinstance(exc, ModelConfigurationSyncError):
            row_by_source_record_id = {
                record.source_record_id: row_number
                for row_number, record in zip(
                    workbook.row_numbers,
                    workbook.records,
                    strict=True,
                )
            }
            excel_row = row_by_source_record_id.get(
                exc.source_record_id or ""
            )
            row_prefix = f"Excel 第 {excel_row} 行，" if excel_row else ""
            message = f"{row_prefix}{exc.code}：{exc}"
        elif isinstance(exc, IntegrityError):
            message = "数据库唯一键冲突，请检查来源知识ID和机型ID组合。"
        else:
            logger.exception(
                "Model configuration import task %s failed.",
                task_id,
            )
            message = "机型配置信息整批同步失败，请检查服务日志。"
        _mark_import_task_failed(
            task,
            f"机型配置信息整批导入失败，全部数据已回滚：{message}",
            now=datetime.utcnow(),
        )
        db.commit()


def process_knowledge_import_task(
    task_id: str,
    *,
    session_factory=SessionLocal,
) -> None:
    """Process a persisted task from the first uncommitted Excel row onward."""

    db = session_factory()
    claimed_attempt: int | None = None

    def owns_import_task(candidate: KnowledgeImportTask | None) -> bool:
        return bool(
            candidate
            and claimed_attempt is not None
            and candidate.status == "running"
            and int(candidate.attempt_count or 0) == claimed_attempt
        )

    try:
        task = db.query(KnowledgeImportTask).filter(
            KnowledgeImportTask.id == task_id
        ).first()
        if not task or task.status != "running":
            return
        claimed_attempt = int(task.attempt_count or 0)

        if task.import_type == "model_configuration":
            _process_model_configuration_import_task(
                db,
                task_id,
                claimed_attempt,
            )
            return

        is_knowledge_update = task.import_type == "knowledge_update"
        categories = db.query(Category).order_by(
            Category.level,
            Category.sort_order,
        ).all()
        try:
            parser = (
                parse_knowledge_update_workbook
                if is_knowledge_update
                else parse_knowledge_workbook
            )
            rows = parser(task.file_content, categories)
            _canonicalize_excel_rows_applicability(
                rows,
                cached_manhattan_options_snapshot(),
            )
        except KnowledgeExcelError as exc:
            current_task = _lock_import_task_attempt(
                db,
                task_id,
                claimed_attempt,
            )
            if not owns_import_task(current_task):
                return
            _mark_import_task_failed(current_task, str(exc), now=datetime.utcnow())
            db.commit()
            return

        current_task = _lock_import_task_attempt(
            db,
            task_id,
            claimed_attempt,
        )
        if not owns_import_task(current_task):
            return
        task = current_task
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

        if is_knowledge_update:
            background_user = (
                db.query(User)
                .filter(
                    User.username == task.created_by,
                    User.is_active.is_(True),
                )
                .first()
            )
            if (
                background_user is None
                or not has_permission(
                    background_user,
                    "knowledge:edit_published",
                )
            ):
                task = _lock_import_task_attempt(
                    db,
                    task_id,
                    claimed_attempt,
                )
                if not owns_import_task(task):
                    return
                _mark_import_task_failed(
                    task,
                    "任务创建人不存在、已停用或已失去批量修改权限，任务未继续执行。",
                    now=datetime.utcnow(),
                )
                db.commit()
                return
        else:
            background_user = SimpleNamespace(username=task.created_by)

        def renew_import_task_lease(_processed: int, _total: int) -> None:
            current_task = _lock_import_task_attempt(
                db,
                task_id,
                claimed_attempt,
            )
            if not owns_import_task(current_task):
                raise _ImportTaskLeaseLost(
                    f"Import task {task_id} is no longer running."
                )
            now = datetime.utcnow()
            current_task.lease_expires_at = _task_lease_expiry(now)
            current_task.updated_at = now
            db.commit()

        retry_row_numbers = {
            int(row_number)
            for row_number in (task.retry_rows or [])
        }
        if retry_row_numbers:
            available_row_numbers = {
                int(row.row_number)
                for row in rows
            }
            missing_retry_rows = sorted(
                retry_row_numbers - available_row_numbers
            )
            if missing_retry_rows:
                task = _lock_import_task_attempt(
                    db,
                    task_id,
                    claimed_attempt,
                )
                if not owns_import_task(task):
                    return
                _mark_import_task_failed(
                    task,
                    (
                        "原始 Excel 中缺少待重试行，任务已停止："
                        + "、".join(str(row) for row in missing_retry_rows[:20])
                    ),
                    now=datetime.utcnow(),
                )
                db.commit()
                return
            remaining_rows = [
                row
                for row in rows
                if int(row.row_number) in retry_row_numbers
            ]
        else:
            remaining_rows = rows[task.processed_rows:]
        row_batches = (
            [[(row, None)] for row in remaining_rows]
            if is_knowledge_update
            else _import_embedding_batches(remaining_rows)
        )
        for row_batch in row_batches:
            task = _lock_import_task_attempt(
                db,
                task_id,
                claimed_attempt,
            )
            if not owns_import_task(task):
                return
            task.lease_expires_at = _task_lease_expiry(datetime.utcnow())
            task.updated_at = datetime.utcnow()
            db.commit()

            batch_rows = [row for row, _ in row_batch]
            bundles = (
                {}
                if is_knowledge_update
                else _precompute_import_embeddings(
                    batch_rows,
                    on_batch_complete=renew_import_task_lease,
                )
            )

            task = _lock_import_task_attempt(
                db,
                task_id,
                claimed_attempt,
            )
            if not owns_import_task(task):
                return
            task.lease_expires_at = _task_lease_expiry(datetime.utcnow())
            task.updated_at = datetime.utcnow()
            db.commit()
            for row in batch_rows:
                task = _lock_import_task_attempt(
                    db,
                    task_id,
                    claimed_attempt,
                )
                if not owns_import_task(task):
                    return
                try:
                    if is_knowledge_update:
                        result = _process_excel_update_row(
                            db,
                            row,
                            background_user,
                        )
                    else:
                        bundle = bundles.get(row.row_number)
                        if (
                            bundle is None
                            or (
                                bundle.error is not None
                                and not isinstance(
                                    bundle.error,
                                    EmbeddingServiceUnavailable,
                                )
                            )
                        ):
                            result = _process_excel_import_row(
                                db,
                                row,
                                background_user,
                            )
                        else:
                            result = _process_excel_import_row(
                                db,
                                row,
                                background_user,
                                embedding_bundle=bundle,
                            )
                except Exception as exc:
                    db.rollback()
                    if _is_retryable_import_exception(exc):
                        raise _retryable_import_exception(exc) from exc
                    task = _lock_import_task_attempt(
                        db,
                        task_id,
                        claimed_attempt,
                    )
                    if not owns_import_task(task):
                        return
                    result = _excel_row_failure_result(row, exc)

                _append_import_task_result(task, result, now=datetime.utcnow())
                db.commit()

        task = _lock_import_task_attempt(
            db,
            task_id,
            claimed_attempt,
        )
        if not owns_import_task(task):
            return
        task.status = "completed_with_errors" if task.failed else "completed"
        task.error_message = ""
        task.lease_expires_at = None
        task.completed_at = datetime.utcnow()
        task.updated_at = task.completed_at
        db.commit()
    except Exception as exc:
        db.rollback()
        task = (
            _lock_import_task_attempt(
                db,
                task_id,
                claimed_attempt,
            )
            if claimed_attempt is not None
            else None
        )
        can_retry = (
            task is not None
            and task.status == "running"
        )
        retry_scheduled = False
        if can_retry:
            retry_scheduled = _mark_import_task_retry(
                task,
                str(exc) or type(exc).__name__,
                now=datetime.utcnow(),
            )
            db.commit()
        if isinstance(exc, _ImportTaskLeaseLost):
            logger.info(
                "Knowledge import task %s stopped because its lease was lost.",
                task_id,
            )
        elif _is_retryable_import_exception(exc) and retry_scheduled:
            logger.warning(
                "Knowledge import task %s will retry after a transient failure: %s",
                task_id,
                exc,
            )
        elif (
            _is_retryable_import_exception(exc)
            and task is not None
            and task.status == "failed"
        ):
            logger.error(
                "Knowledge import task %s stopped after a transient failure: %s",
                task_id,
                exc,
            )
        elif _is_retryable_import_exception(exc):
            logger.info(
                "Knowledge import task %s stopped because its ownership changed.",
                task_id,
            )
        else:
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
        max_attempts = max(
            1,
            int(settings.KNOWLEDGE_IMPORT_MAX_ATTEMPTS),
        )
        if int(task.attempt_count or 0) >= max_attempts:
            _mark_import_task_failed(
                task,
                (
                    "后台任务租约已到期，且达到最大尝试次数"
                    f"（{int(task.attempt_count or 0)}/{max_attempts}）。"
                ),
                now=now,
            )
            db.commit()
            return True
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
    knowledge_origin: str | None = None,
    business_type: str | None = None,
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
    if knowledge_origin:
        q = q.filter(Knowledge.knowledge_origin == knowledge_origin)
    if business_type:
        q = q.filter(Knowledge.business_type == business_type)
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
    knowledge_origin: str | None = None,
    business_type: str | None = None,
    category_id: str | None,
    applicable_category_ids: list[str] | None,
    brand_ids: list[str] | None,
    model_ids: list[str] | None,
    keyword: str | None,
) -> bool:
    """Avoid accidentally exporting the full knowledge base without a filter."""
    return bool(
        status
        or knowledge_origin
        or business_type
        or category_id
        or any(applicable_category_ids or [])
        or any(brand_ids or [])
        or any(model_ids or [])
        or (keyword or "").strip()
    )


@router.get("/export/excel", summary="导出知识库 Excel")
def export_knowledge_excel(
    status: str | None = Query(None, description="状态筛选"),
    knowledge_origin: KnowledgeOrigin | None = Query(None, description="知识来源"),
    business_type: BusinessType | None = Query(None, description="业务类型"),
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
        knowledge_origin=knowledge_origin,
        business_type=business_type,
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
        knowledge_origin=knowledge_origin,
        business_type=business_type,
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
    description="支持按状态、知识来源、业务类型、知识分类、适用类目、品牌和机型筛选，分页查询",
)
def list_knowledge(
    response: Response,
    status: str | None = Query(None, description="状态筛选"),
    knowledge_origin: KnowledgeOrigin | None = Query(None, description="知识来源"),
    business_type: BusinessType | None = Query(None, description="业务类型"),
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
        knowledge_origin=knowledge_origin,
        business_type=business_type,
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
    knowledge_origin: KnowledgeOrigin | None = Query(None, description="知识来源"),
    business_type: BusinessType | None = Query(None, description="业务类型"),
    category_id: str | None = Query(None, description="分类ID"),
    applicable_category_ids: list[str] | None = Query(None, description="适用类目ID，可多选"),
    brand_ids: list[str] | None = Query(None, description="适用品牌ID，可多选"),
    model_ids: list[str] | None = Query(None, description="适用机型ID，可多选"),
    keyword: str | None = Query(None, description="知识关键词"),
):
    query = db.query(Knowledge.id).filter(
        Knowledge.status == KnowledgeStatus.REVIEW
    )
    if knowledge_origin:
        query = query.filter(Knowledge.knowledge_origin == knowledge_origin)
    if business_type:
        query = query.filter(Knowledge.business_type == business_type)
    if category_id:
        query = query.filter(Knowledge.category_id == category_id)
    for column, values in (
        (Knowledge.applicable_categories, applicable_category_ids),
        (Knowledge.applicable_brands, brand_ids),
        (Knowledge.applicable_models, model_ids),
    ):
        normalized_values = [
            value.strip()
            for value in (values or [])
            if value and value.strip()
        ]
        if normalized_values:
            query = query.filter(
                or_(
                    *[
                        cast(column, JSONB).contains([value])
                        for value in dict.fromkeys(normalized_values)
                    ]
                )
            )
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        keyword_pattern = f"%{normalized_keyword}%"
        query = query.filter(
            or_(
                Knowledge.title.ilike(keyword_pattern),
                _jsonb_text_match(
                    Knowledge.subtitles,
                    "$[*]",
                    normalized_keyword,
                ),
                _jsonb_text_match(
                    Knowledge.content,
                    "$.blocks[*].value",
                    normalized_keyword,
                ),
                _jsonb_text_match(
                    Knowledge.related_standard_items,
                    "$[*]",
                    normalized_keyword,
                ),
                _jsonb_text_match(
                    Knowledge.applicable_scenes,
                    "$[*]",
                    normalized_keyword,
                ),
                Knowledge.category.has(Category.name.ilike(keyword_pattern)),
            )
        )
    knowledge_ids = [
        item_id
        for (item_id,) in (
            query.order_by(Knowledge.created_at.desc())
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
                "knowledge_origin": getattr(item, "knowledge_origin", "business_accumulation"),
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
                "knowledge_origin": getattr(item, "knowledge_origin", "business_accumulation"),
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
        "knowledge_origin": getattr(item, "knowledge_origin", "business_accumulation"),
        "business_type": item.business_type,
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


def _change_log_from_snapshots(
    item: Knowledge,
    *,
    changed_by: str,
    before_data: dict,
    after_data: dict,
    created_at: datetime | None = None,
) -> KnowledgeChangeLog | None:
    """根据前后快照生成仅包含实际变化字段的审计记录。"""
    ordered_fields = list(before_data)
    ordered_fields.extend(
        field for field in after_data
        if field not in before_data
    )
    changed_fields = [
        field
        for field in ordered_fields
        if before_data.get(field) != after_data.get(field)
    ]
    if not changed_fields:
        return None
    return KnowledgeChangeLog(
        id=f"kcl-{uuid.uuid4().hex[:12]}",
        knowledge_id=item.id,
        changed_by=changed_by,
        changed_fields=changed_fields,
        before_data={
            field: deepcopy(before_data.get(field))
            for field in changed_fields
        },
        after_data={
            field: deepcopy(after_data.get(field))
            for field in changed_fields
        },
        created_at=created_at or datetime.utcnow(),
    )


def _approval_change_log(
    item: Knowledge,
    *,
    reviewed_by: str,
    reviewed_at: datetime,
) -> KnowledgeChangeLog:
    """记录待审核知识成功发布的审核人、审核时间和状态变化。"""
    change_log = _change_log_from_snapshots(
        item,
        changed_by=reviewed_by,
        before_data={"status": KnowledgeStatus.REVIEW.value},
        after_data={"status": KnowledgeStatus.PUBLISHED.value},
        created_at=reviewed_at,
    )
    if change_log is None:
        raise RuntimeError("发布审核状态未发生变化，无法生成变更日志。")
    return change_log


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


@router.put(
    "/{knowledge_id}/model-configuration",
    response_model=KnowledgeResponse,
    summary="更新机型配置信息",
)
def update_model_configuration(
    knowledge_id: str,
    body: ModelConfigurationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permission("knowledge:edit_published")
    ),
):
    try:
        acquire_model_configuration_write_lock(db)
    except ModelConfigurationSyncError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail={
                "code": exc.code,
                "message": str(exc),
            },
        ) from exc

    item = (
        db.query(Knowledge)
        .populate_existing()
        .filter(Knowledge.id == knowledge_id)
        .with_for_update()
        .first()
    )
    if not item:
        db.rollback()
        raise HTTPException(404, "知识条目不存在")
    if item.knowledge_origin != "model_configuration":
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_CONFIGURATION_ONLY",
                "message": "只有机型配置信息可以使用此编辑接口。",
            },
        )
    source_record_id = str(item.source_record_id or "").strip()
    if not source_record_id:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_CONFIGURATION_SOURCE_RECORD_ID_MISSING",
                "message": "当前机型配置信息缺少来源知识ID，不能执行原地更新。",
            },
        )
    try:
        resolved_scope = _resolve_model_configuration_scope(
            category_id=body.category_id,
            brand_id=body.brand_id,
            model_id=body.model_id,
        )
    except HTTPException:
        db.rollback()
        raise

    source_fields = deepcopy(
        item.source_fields
        if isinstance(item.source_fields, dict)
        else {}
    )
    attributes = body.attributes.model_dump(by_alias=True)
    for field in MODEL_CONFIGURATION_ATTRIBUTE_FIELDS:
        value = str(attributes[field] or "").strip()
        if value:
            source_fields[field] = value
        else:
            source_fields.pop(field, None)

    payload = {
        "records": [
            {
                "source_record_id": source_record_id,
                "title": body.title,
                "content": body.content,
                **resolved_scope,
                "source_fields": source_fields,
            }
        ]
    }

    try:
        records = parse_model_configuration_payload(payload)
        sync_result = sync_model_configurations(
            db,
            records,
            actor=current_user.username,
            allow_source_key_change_for=item.id,
        )
        if (
            sync_result.total != 1
            or len(sync_result.items) != 1
            or sync_result.items[0].knowledge_id != item.id
        ):
            raise ModelConfigurationSyncError(
                "MODEL_CONFIGURATION_TARGET_MISMATCH",
                "机型配置同步结果与当前知识不一致，已取消更新。",
                source_record_id=source_record_id,
            )
        db.commit()
    except ModelConfigurationSyncError as exc:
        db.rollback()
        conflict_codes = {
            "SOURCE_IDENTIFIER_AMBIGUOUS",
            "SOURCE_IDENTIFIER_CONFLICT",
            "SOURCE_RECORD_ID_REUSED",
            "MODEL_CONFIGURATION_TARGET_MISMATCH",
        }
        raise HTTPException(
            status_code=409 if exc.code in conflict_codes else 422,
            detail={
                "code": exc.code,
                "message": str(exc),
            },
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_IDENTIFIER_CONFLICT",
                "message": "新的品类、品牌和型号组合已绑定其他机型配置信息。",
            },
        ) from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(item)
    return _to_response(item)


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
    body_fields_set = getattr(body, "model_fields_set", set())
    requested_origin = (
        getattr(body, "knowledge_origin", None)
        if "knowledge_origin" in body_fields_set
        else getattr(item, "knowledge_origin", "business_accumulation")
    )
    if (
        getattr(item, "knowledge_origin", "") == "model_configuration"
        or requested_origin == "model_configuration"
    ):
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工修改知识来源或内容。",
        )
    was_published = item.status == KnowledgeStatus.PUBLISHED
    updates = body.model_dump(exclude_unset=True)
    updated_fields = set(updates)
    origin_changed = (
        "knowledge_origin" in updates
        and updates["knowledge_origin"]
        != getattr(item, "knowledge_origin", "business_accumulation")
    )
    business_type_changed = (
        "business_type" in updates
        and updates["business_type"] != item.business_type
    )
    if (
        "business_type" in updates
        and updates["business_type"] != item.business_type
        and any(
            (
                item.applicable_categories,
                item.applicable_brands,
                item.applicable_models,
            )
        )
        and not {
            "applicable_categories",
            "applicable_brands",
            "applicable_models",
        }.issubset(updated_fields)
    ):
        raise HTTPException(
            status_code=422,
            detail="更改业务类型时必须重新提交适用类目、品牌和机型，避免沿用另一业务的数据。",
        )
    _require_manual_applicable_category(
        source=item.source,
        category_id=updates.get("category_id", item.category_id),
        applicable_categories=updates.get(
            "applicable_categories",
            item.applicable_categories,
        ),
    )
    _validate_business_applicable_categories(
        business_type=updates.get("business_type", item.business_type),
        applicable_categories=updates.get(
            "applicable_categories",
            item.applicable_categories,
        ),
    )
    before_data = _knowledge_snapshot(item)
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
        if origin_changed or business_type_changed:
            refreshed_decision = _check_manual_deduplication(
                db,
                title=item.title,
                subtitles=item.subtitles or [],
                content=item.content,
                scene_tags=item.applicable_scenes or [],
                knowledge_origin=getattr(
                    item,
                    "knowledge_origin",
                    "business_accumulation",
                ),
                business_type=item.business_type,
                exclude_knowledge_id=item.id,
                allow_duplicate_review=True,
            )
            item.deduplication_metadata = _deduplication_metadata(
                refreshed_decision
            )
            if (
                was_published
                and refreshed_decision.action == "review_duplicate"
            ):
                item.status = KnowledgeStatus.REVIEW
            if refreshed_decision.embedding:
                save_embedding(
                    db,
                    knowledge=item,
                    content_hash=refreshed_decision.content_hash,
                    embedding=refreshed_decision.embedding,
                    title_embedding=refreshed_decision.title_embedding,
                    content_embedding=refreshed_decision.content_embedding,
                )
        after_data = _knowledge_snapshot(item)
        changed_fields = [
            field for field, before_value in (before_data or {}).items()
            if before_value != after_data.get(field)
        ]
        changed_at = datetime.utcnow()
        if changed_fields:
            item.updated_by = current_user.username
            change_log = _change_log_from_snapshots(
                item,
                changed_by=current_user.username,
                before_data=before_data,
                after_data=after_data,
                created_at=changed_at,
            )
            if change_log is not None:
                db.add(change_log)
        if {"title", "subtitles", "content"} & updated_fields:
            ensure_embedding(db, item)
            ensure_search_embeddings(db, item)
        item.updated_at = changed_at
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
    if getattr(item, "knowledge_origin", "") == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工删除。",
        )
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
    if (
        item.knowledge_origin == "model_configuration"
        or matched_item.knowledge_origin == "model_configuration"
    ):
        raise HTTPException(
            status_code=422,
            detail="机型配置信息不参与查重反馈或向量训练。",
        )

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
    before_data = _knowledge_snapshot(item)
    decision = _check_manual_deduplication(
        db,
        title=item.title,
        subtitles=item.subtitles or [],
        content=item.content,
        scene_tags=item.applicable_scenes or [],
        knowledge_origin=item.knowledge_origin,
        business_type=item.business_type,
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
    changed_at = datetime.utcnow()
    item.updated_by = current_user.username
    item.updated_at = changed_at
    after_data = _knowledge_snapshot(item)
    change_log = _change_log_from_snapshots(
        item,
        changed_by=current_user.username,
        before_data=before_data,
        after_data=after_data,
        created_at=changed_at,
    )
    if change_log is not None:
        db.add(change_log)
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
def deprecate_knowledge(
    knowledge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:deprecate")),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if getattr(item, "knowledge_origin", "") == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工废弃。",
        )
    before_data = _knowledge_snapshot(item)
    changed_at = datetime.utcnow()
    item.status = KnowledgeStatus.DEPRECATED
    item.updated_by = current_user.username
    item.updated_at = changed_at
    after_data = _knowledge_snapshot(item)
    change_log = _change_log_from_snapshots(
        item,
        changed_by=current_user.username,
        before_data=before_data,
        after_data=after_data,
        created_at=changed_at,
    )
    if change_log is not None:
        db.add(change_log)
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
    if getattr(item, "knowledge_origin", "") == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工恢复。",
        )
    if item.status != KnowledgeStatus.DEPRECATED:
        raise HTTPException(400, "Only deprecated knowledge items can be restored.")
    before_data = _knowledge_snapshot(item)
    item.status = KnowledgeStatus.PUBLISHED
    item.updated_by = current_user.username
    changed_at = datetime.utcnow()
    item.updated_at = changed_at
    after_data = _knowledge_snapshot(item)
    change_log = _change_log_from_snapshots(
        item,
        changed_by=current_user.username,
        before_data=before_data,
        after_data=after_data,
        created_at=changed_at,
    )
    if change_log is not None:
        db.add(change_log)
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
    if getattr(item, "knowledge_origin", "") == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工上传媒体。",
        )
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
    if getattr(item, "knowledge_origin", "") == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工修改媒体。",
        )
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
    if getattr(item, "knowledge_origin", "") == "model_configuration":
        raise HTTPException(
            status_code=422,
            detail="机型配置信息由飞书专用同步维护，不能人工删除媒体。",
        )
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
        knowledge_origin=body.knowledge_origin,
        business_type=body.business_type,
        confirm_dedup_review=body.confirm_dedup_review,
    )

    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        content=_normalize_content(body.content),
        knowledge_origin=body.knowledge_origin,
        business_type=body.business_type,
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
            knowledge_origin=body.knowledge_origin,
            business_type=body.business_type,
            category_id=body.category_id,
            tags=body.tags,
            top_k=body.top_k,
        )
    except EmbeddingServiceUnavailable as exc:
        raise HTTPException(503, "Embedding 服务不可用，无法完成语义检索") from exc

    # Existing installations can still serve title matches while search vectors
    # are being rebuilt in the background.
    if not ranked:
        q = db.query(Knowledge).filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            Knowledge.knowledge_origin == body.knowledge_origin,
            Knowledge.business_type == body.business_type,
        )
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
            status=i.status.value,
            knowledge_origin=i.knowledge_origin,
            business_type=i.business_type,
            category_id=i.category_id,
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

