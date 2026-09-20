import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.knowledge import KnowledgeMedia, MediaUploadStaging
from app.services.media_storage import (
    MIN_INTERNAL_MEDIA_KEY_LENGTH,
    MediaStorageError,
    _normalize_remote_media_path_prefix,
    get_media_storage,
)


router = APIRouter(tags=["知识媒体"])
media_storage = get_media_storage()
_INTERNAL_MEDIA_PATH_PREFIX = _normalize_remote_media_path_prefix(
    settings.REMOTE_MEDIA_PATH_PREFIX
)


@router.get("/uploads/{filename}", include_in_schema=False)
def serve_media(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
):
    media = (
        db.query(KnowledgeMedia)
        .filter(KnowledgeMedia.filename == filename)
        .first()
    )
    if not media:
        raise HTTPException(404, "媒体文件不存在")
    try:
        return media_storage.build_response(
            media.file_path,
            media.filename,
            media.mime_type,
            request_headers=request.headers,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "媒体文件不存在") from exc
    except MediaStorageError as exc:
        raise HTTPException(502, "媒体存储服务不可用") from exc


def _require_internal_media_key(request: Request) -> None:
    expected = settings.REMOTE_MEDIA_API_KEY.strip()
    provided = request.headers.get("x-internal-media-key", "").strip()
    if (
        len(expected) < MIN_INTERNAL_MEDIA_KEY_LENGTH
        or not provided
        or not hmac.compare_digest(provided, expected)
    ):
        raise HTTPException(status_code=404, detail="媒体文件不存在")


def _validate_internal_filename(filename: str) -> str:
    value = str(filename or "").strip()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise HTTPException(status_code=400, detail="媒体文件名无效")
    return value


def _media_storage_metadata(
    db: Session,
    filename: str,
    storage_key: str,
    mime_type: str,
) -> tuple[str, str]:
    media = (
        db.query(KnowledgeMedia)
        .filter(KnowledgeMedia.filename == filename)
        .first()
    )
    if media:
        return storage_key or media.file_path, mime_type or media.mime_type
    staging = (
        db.query(MediaUploadStaging)
        .filter(MediaUploadStaging.filename == filename)
        .first()
    )
    if staging:
        return storage_key or staging.storage_key, mime_type or staging.mime_type
    return storage_key or filename, mime_type or "application/octet-stream"


async def _read_internal_media_body(request: Request) -> bytes:
    """Read an internal upload without buffering an unbounded request body."""

    limit = max(0, int(settings.UPLOAD_MAX_BYTES))
    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(status_code=413, detail="媒体文件超过大小限制")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="媒体长度无效") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="媒体文件超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get(f"{_INTERNAL_MEDIA_PATH_PREFIX}/{{filename}}", include_in_schema=False)
def get_internal_media(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Private media gateway used by a backend running on another machine."""

    _require_internal_media_key(request)
    if media_storage.backend != "local":
        raise HTTPException(status_code=503, detail="远程媒体网关配置错误")
    safe_filename = _validate_internal_filename(filename)
    storage_key, mime_type = _media_storage_metadata(
        db,
        safe_filename,
        request.query_params.get("storage_key", "").strip(),
        request.query_params.get("mime_type", "").strip(),
    )
    try:
        return media_storage.build_response(
            storage_key,
            safe_filename,
            mime_type,
            request_headers=request.headers,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="媒体文件不存在") from exc
    except MediaStorageError as exc:
        raise HTTPException(status_code=502, detail="媒体存储服务不可用") from exc


@router.put(f"{_INTERNAL_MEDIA_PATH_PREFIX}/{{filename}}", include_in_schema=False)
async def put_internal_media(filename: str, request: Request):
    _require_internal_media_key(request)
    if media_storage.backend != "local":
        raise HTTPException(status_code=503, detail="远程媒体网关配置错误")
    safe_filename = _validate_internal_filename(filename)
    content = await _read_internal_media_body(request)
    mime_type = request.headers.get("content-type", "application/octet-stream")
    try:
        storage_key = media_storage.put(safe_filename, content, mime_type)
    except MediaStorageError as exc:
        raise HTTPException(status_code=502, detail="媒体存储服务不可用") from exc
    return JSONResponse(
        {"filename": safe_filename, "storage_key": str(storage_key)},
        status_code=201,
    )


@router.delete(f"{_INTERNAL_MEDIA_PATH_PREFIX}/{{filename}}", include_in_schema=False)
def delete_internal_media(
    filename: str,
    request: Request,
):
    _require_internal_media_key(request)
    if media_storage.backend != "local":
        raise HTTPException(status_code=503, detail="远程媒体网关配置错误")
    safe_filename = _validate_internal_filename(filename)
    try:
        media_storage.delete(
            request.query_params.get("storage_key", "").strip(),
            safe_filename,
        )
    except FileNotFoundError:
        pass
    except MediaStorageError as exc:
        raise HTTPException(status_code=502, detail="媒体存储服务不可用") from exc
    return Response(status_code=204)
