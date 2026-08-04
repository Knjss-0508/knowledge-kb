from fastapi import APIRouter

from app.schemas.knowledge import BusinessTypeOption


router = APIRouter(prefix="/business-types", tags=["业务类型"])


@router.get(
    "",
    response_model=list[BusinessTypeOption],
    summary="查询业务类型",
    description="返回知识库支持的只读业务类型字典。",
)
def list_business_types() -> list[BusinessTypeOption]:
    return [
        BusinessTypeOption(value="self_operated", label="自营回收"),
        BusinessTypeOption(value="aggregated", label="聚合回收"),
    ]
