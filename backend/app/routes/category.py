import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.knowledge import Category
from app.schemas.knowledge import CategoryCreate, CategoryResponse

router = APIRouter(prefix="/categories", tags=["分类管理"])


@router.post("", response_model=CategoryResponse, status_code=201, summary="创建分类", description="创建一级或二级分类，二级分类需指定parent_id")
def create_category(body: CategoryCreate, db: Session = Depends(get_db)):
    cat = Category(
        id=f"cat-{uuid.uuid4().hex[:8]}",
        name=body.name,
        parent_id=body.parent_id,
        level=body.level,
        sort_order=body.sort_order,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.get("", response_model=list[CategoryResponse], summary="查询所有分类", description="返回所有分类列表，按层级和排序号排列")
def list_categories(db: Session = Depends(get_db)):
    return db.query(Category).order_by(Category.level, Category.sort_order).all()


@router.delete("/{category_id}", status_code=204, summary="删除分类", description="根据ID删除指定分类")
def delete_category(category_id: str, db: Session = Depends(get_db)):
    cat = db.query(Category).filter(Category.id == category_id).first()
    if not cat:
        raise HTTPException(404, "分类不存在")
    db.delete(cat)
    db.commit()
