from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.knowledge import KnowledgeMedia
from app.services.media_storage import MediaStorageError, get_media_storage


router = APIRouter(tags=["知识媒体"])
media_storage = get_media_storage()


@router.get("/uploads/{filename}", include_in_schema=False)
def serve_media(filename: str, db: Session = Depends(get_db)):
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
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "媒体文件不存在") from exc
    except MediaStorageError as exc:
        raise HTTPException(502, "媒体存储服务不可用") from exc
