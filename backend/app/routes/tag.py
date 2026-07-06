import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.knowledge import TagDimension, TagValue
from app.schemas.knowledge import (
    TagDimensionCreate, TagDimensionResponse, TagValueCreate, TagValueResponse,
)

router = APIRouter(prefix="/tags", tags=["标签管理"])


@router.post("/dimensions", response_model=TagDimensionResponse, status_code=201, summary="创建标签维度", description="新增标签维度，如：场景、情绪、难度、时效、渠道")
def create_dimension(body: TagDimensionCreate, db: Session = Depends(get_db)):
    dim = TagDimension(id=f"td-{uuid.uuid4().hex[:8]}", name=body.name)
    db.add(dim)
    db.commit()
    db.refresh(dim)
    return TagDimensionResponse(id=dim.id, name=dim.name, values=[])


@router.get("/dimensions", response_model=list[TagDimensionResponse], summary="查询所有标签维度", description="返回所有标签维度及其包含的标签值")
def list_dimensions(db: Session = Depends(get_db)):
    dims = db.query(TagDimension).all()
    result = []
    for d in dims:
        vals = [TagValueResponse(id=v.id, dimension_id=v.dimension_id, value=v.value) for v in d.values]
        result.append(TagDimensionResponse(id=d.id, name=d.name, values=vals))
    return result


@router.delete("/dimensions/{dim_id}", status_code=204, summary="删除标签维度", description="删除标签维度及其下所有标签值")
def delete_dimension(dim_id: str, db: Session = Depends(get_db)):
    dim = db.query(TagDimension).filter(TagDimension.id == dim_id).first()
    if not dim:
        raise HTTPException(404, "标签维度不存在")
    db.delete(dim)
    db.commit()


@router.post("/dimensions/{dim_id}/values", response_model=TagValueResponse, status_code=201, summary="创建标签值", description="在指定维度下新增标签值，如在场景维度下新增退货、登录失败等")
def create_tag_value(dim_id: str, body: TagValueCreate, db: Session = Depends(get_db)):
    dim = db.query(TagDimension).filter(TagDimension.id == dim_id).first()
    if not dim:
        raise HTTPException(404, "标签维度不存在")
    tv = TagValue(id=f"tv-{uuid.uuid4().hex[:8]}", dimension_id=dim_id, value=body.value)
    db.add(tv)
    db.commit()
    db.refresh(tv)
    return tv


@router.delete("/values/{value_id}", status_code=204, summary="删除标签值", description="根据ID删除指定标签值")
def delete_tag_value(value_id: str, db: Session = Depends(get_db)):
    tv = db.query(TagValue).filter(TagValue.id == value_id).first()
    if not tv:
        raise HTTPException(404, "标签值不存在")
    db.delete(tv)
    db.commit()
