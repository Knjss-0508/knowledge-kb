import uuid
import os
import string
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.routes.auth import get_current_user, require_permission
from app.models.user import User
from app.models.knowledge import (
    Knowledge, KnowledgeStatus, KnowledgeLayer,
    KnowledgeTag, KnowledgeMedia,
)
from app.schemas.knowledge import (
    KnowledgeCreate, KnowledgeUpdate, KnowledgeResponse,
    CandidateSubmit, FeedbackSubmit,
    SearchRequest, SearchResponse, SearchResult,
)

router = APIRouter(prefix="/knowledge", tags=["知识库管理"])

UPLOAD_DIR = r"C:\Users\a1873\Documents\答疑中台知识库项目\backend\uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_IMAGE = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_VIDEO = {"video/mp4", "video/webm", "video/quicktime"}

ALPHA = string.ascii_uppercase  # A-Z


def _generate_knowledge_id(db: Session) -> str:
    """生成 A-00001 格式的知识ID，按字母+数字递增，最高 Z-99999"""
    all_ids = [row[0] for row in db.query(Knowledge.id).all()]
    valid_ids = []
    for kid in all_ids:
        if len(kid) == 7 and kid[1] == '-' and kid[0] in ALPHA and kid[2:].isdigit():
            valid_ids.append(kid)
    if not valid_ids:
        return "A-00001"
    valid_ids.sort(key=lambda x: (x[0], int(x[2:])))
    last = valid_ids[-1]
    letter_idx = ALPHA.index(last[0])
    num = int(last[2:]) + 1
    if num > 99999:
        letter_idx += 1
        num = 1
    if letter_idx >= len(ALPHA):
        raise ValueError("知识ID已达上限 Z-99999")
    return f"{ALPHA[letter_idx]}-{num:05d}"



def _normalize_content(raw):
    if raw is None:
        return {"blocks": []}
    if isinstance(raw, str):
        return {"blocks": [{"type": "text", "value": raw}]}
    if isinstance(raw, dict) and "blocks" in raw:
        return raw
    return {"blocks": [{"type": "text", "value": str(raw)}]}


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
        "is_model_personal": item.is_model_personal == "true",
        "created_by": item.created_by,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "tags": tags,
        "media": media_list,
    }


# ---- CRUD ----

@router.post("", response_model=KnowledgeResponse, status_code=201, summary="创建知识条目", description="新建一条知识条目，初始状态为草稿(draft)")
def create_knowledge(body: KnowledgeCreate, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:create"))):
    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        subtitles=body.subtitles or [],
        content=_normalize_content(body.content),
        layer=KnowledgeLayer(body.layer),
        category_id=body.category_id,
        applicable_scenes=body.applicable_scenes,
        applicable_business_types=body.applicable_business_types,
        applicable_categories=body.applicable_categories,
        applicable_brands=body.applicable_brands,
        applicable_models=body.applicable_models,
        is_model_personal="true" if body.is_model_personal else "false",
        created_by=body.created_by,
    )
    db.add(item)
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


@router.get("/{knowledge_id}", response_model=KnowledgeResponse, summary="获取知识条目详情")
def get_knowledge(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:view")), current_user: User = Depends(get_current_user)):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if current_user.role == "visitor" and item.status != KnowledgeStatus.PUBLISHED:
        raise HTTPException(403, "Permission denied.")
    return _to_response(item)


@router.patch("/{knowledge_id}", response_model=KnowledgeResponse, summary="更新知识条目")
def update_knowledge(knowledge_id: str, body: KnowledgeUpdate, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:create"))):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    for field, val in body.model_dump(exclude_unset=True).items():
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


# ---- 审核流程 ----

@router.post("/{knowledge_id}/submit-review", response_model=KnowledgeResponse, summary="提交审核")
def submit_review(knowledge_id: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:submit"))):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")
    if item.status != KnowledgeStatus.DRAFT:
        raise HTTPException(400, "只有草稿状态才能提交审核")
    item.status = KnowledgeStatus.REVIEW
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


# ---- 媒体上传(图片+视频) ----

@router.post("/{knowledge_id}/media", summary="上传媒体文件", description="上传图片或视频到指定知识条目")
async def upload_media(
    knowledge_id: str,
    file: UploadFile = File(...),
    media_type: str = Form("image"),
    alt: str = Form(""),
    caption: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_permission("knowledge:create")),
):
    item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
    if not item:
        raise HTTPException(404, "知识条目不存在")

    if media_type == "image" and file.content_type not in ALLOWED_IMAGE:
        raise HTTPException(400, f"不支持的图片格式: {file.content_type}，支持: png/jpg/gif/webp")
    if media_type == "video" and file.content_type not in ALLOWED_VIDEO:
        raise HTTPException(400, f"不支持的视频格式: {file.content_type}，支持: mp4/webm/mov")

    ext = os.path.splitext(file.filename)[1] or (".mp4" if media_type == "video" else ".png")
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    content = await file.read()
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
async def upload_temp(file: UploadFile = File(...), media_type: str = Form("image"), alt: str = Form(""), caption: str = Form(""), _=Depends(require_permission("knowledge:create"))):
    ext = os.path.splitext(file.filename)[1] or ".png"
    filename = f"temp-{uuid.uuid4().hex[:12]}{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)
    return {
        "id": filename,
        "filename": filename,
        "original_name": file.filename,
        "file_path": f"/uploads/{filename}",
        "file_size": len(content),
        "mime_type": file.content_type or "image/png",
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
def update_media(knowledge_id: str, media_file: str, alt: str = Form(""), caption: str = Form(""), db: Session = Depends(get_db), _=Depends(require_permission("knowledge:create"))):
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
def delete_media(knowledge_id: str, media_file: str, db: Session = Depends(get_db), _=Depends(require_permission("knowledge:create"))):
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
def submit_candidate(body: CandidateSubmit, db: Session = Depends(get_db)):
    item = Knowledge(
        id=_generate_knowledge_id(db),
        title=body.title,
        content=_normalize_content(body.content),
        layer=KnowledgeLayer.L2,
        source=body.source,
        source_session_id=body.source_session_id,
        created_by=body.submitted_by,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _to_response(item)


# ---- 检索 ----

@router.post("/search", response_model=SearchResponse, summary="检索知识库")
def search_knowledge(body: SearchRequest, db: Session = Depends(get_db)):
    q = db.query(Knowledge).filter(Knowledge.status == KnowledgeStatus.PUBLISHED)
    if body.category_id:
        q = q.filter(Knowledge.category_id == body.category_id)
    if body.layer:
        q = q.filter(Knowledge.layer == KnowledgeLayer(body.layer))
    if body.query:
        q = q.filter(Knowledge.title.ilike(f"%{body.query}%"))
    items = q.order_by(Knowledge.quality_score.desc()).limit(body.top_k).all()
    results = [
        SearchResult(
            id=i.id, title=i.title, content=i.content,
            score=i.quality_score, layer=i.layer.value,
            status=i.status.value, category_id=i.category_id,
        )
        for i in items
    ]
    return SearchResponse(query=body.query, total=len(results), results=results)


# ---- 反馈 ----

@router.post("/feedback", summary="提交使用反馈")
def submit_feedback(body: FeedbackSubmit, db: Session = Depends(get_db)):
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

