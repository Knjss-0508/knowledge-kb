from fastapi import APIRouter

from app.schemas.knowledge import KnowledgeOriginOption


router = APIRouter(prefix="/knowledge-origins", tags=["知识来源"])


@router.get(
    "",
    response_model=list[KnowledgeOriginOption],
    summary="查询知识来源",
    description="返回知识库支持的只读知识来源字典。",
)
def list_knowledge_origins() -> list[KnowledgeOriginOption]:
    return [
        KnowledgeOriginOption(
            value="headquarters_standard",
            label="总部标准",
        ),
        KnowledgeOriginOption(
            value="business_accumulation",
            label="业务沉淀",
        ),
        KnowledgeOriginOption(
            value="model_configuration",
            label="机型配置信息",
        ),
    ]
