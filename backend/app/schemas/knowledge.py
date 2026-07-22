from datetime import datetime
from typing import Literal, Optional, Any

from pydantic import BaseModel, Field, field_validator


# ---- 富文本内容块 ----

class ContentBlock(BaseModel):
    type: str = Field(description="内容块类型: text=文本 image=图片 video=视频")
    value: Optional[str] = Field(None, description="文本内容，type=text时使用")
    media_id: Optional[str] = Field(None, description="媒体文件ID，type=image/video时使用")
    alt: Optional[str] = Field(None, description="媒体标题/替代文本")
    caption: Optional[str] = Field(None, description="媒体描述/说明文字，会显示在内容下方")
    duration: Optional[str] = Field(None, description="视频时长，如 03:20，仅type=video时使用")


class RichContent(BaseModel):
    blocks: list[ContentBlock] = Field(default=[], description="内容块列表")


# ---- 媒体文件 ----

class MediaResponse(BaseModel):
    id: str = Field(description="媒体文件ID")
    media_type: str = Field(description="类型: image/video")
    filename: str = Field(description="存储文件名")
    original_name: str = Field(description="原始文件名")
    file_path: str = Field(description="访问路径")
    file_size: int = Field(description="文件大小(字节)")
    mime_type: str = Field(description="MIME类型")
    alt: str = Field(description="标题/替代文本")
    caption: str = Field(description="描述/说明文字")
    duration: str = Field(description="视频时长")
    sort_order: int = Field(description="排序号")

    class Config:
        from_attributes = True


# ---- 知识条目 ----

class KnowledgeCreate(BaseModel):
    title: str = Field(..., max_length=256, description="知识标题")
    subtitles: list[str] = Field(default=[], description="副标题列表，可多条")
    content: Any = Field(..., description="知识内容，支持富文本: 纯字符串 或 {blocks:[...]} 结构")
    category_id: str = Field(..., description="所属分类ID")
    applicable_scenes: list[str] = Field(default=[], description="场景标签列表")
    applicable_categories: list[Any] = Field(default=[], description="适用类目")
    applicable_brands: list[Any] = Field(default=[], description="适用品牌")
    applicable_models: list[Any] = Field(default=[], description="适用机型")
    confirm_dedup_review: bool = Field(
        False,
        description="语义重复对比后，创建人确认仍需提交审核",
    )
    created_by: str = Field("system", description="创建人")

    @field_validator("category_id")
    @classmethod
    def category_id_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("所属分类不能为空")
        return value


class KnowledgeUpdate(BaseModel):
    title: Optional[str] = Field(None, description="知识标题")
    subtitles: Optional[list[str]] = Field(None, description="副标题列表")
    content: Optional[Any] = Field(None, description="知识内容")
    category_id: Optional[str] = Field(None, description="所属分类ID")
    status: Optional[str] = Field(None, description="状态: draft/review/published/deprecated")
    applicable_scenes: Optional[list[str]] = Field(None, description="场景标签列表")
    applicable_categories: Optional[list[Any]] = Field(None, description="适用类目")
    applicable_brands: Optional[list[Any]] = Field(None, description="适用品牌")
    applicable_models: Optional[list[Any]] = Field(None, description="适用机型")

    @field_validator("category_id")
    @classmethod
    def category_id_must_not_be_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            raise ValueError("所属分类不能为空")
        value = value.strip()
        if not value:
            raise ValueError("所属分类不能为空")
        return value


class KnowledgeResponse(BaseModel):
    id: str = Field(description="知识条目ID")
    title: str = Field(description="知识标题")
    subtitles: list[str] = Field(default=[], description="副标题列表")
    content: Any = Field(description="知识内容")
    category_id: Optional[str] = Field(None, description="所属分类ID")
    status: str = Field(description="当前状态")
    source: str = Field(description="来源")
    quality_score: float = Field(description="质量评分")
    applicable_scenes: list[str] = Field(default=[], description="场景标签")
    applicable_categories: list[Any] = Field(default=[], description="适用类目")
    applicable_brands: list[Any] = Field(default=[], description="适用品牌")
    applicable_models: list[Any] = Field(default=[], description="适用机型")
    deduplication_metadata: dict[str, Any] = Field(default={}, description="提交审核时的查重结果")
    created_by: str = Field(description="创建人")
    updated_by: Optional[str] = Field(None, description="最近变更人")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    tags: list["TagBrief"] = Field(default=[], description="关联标签列表")
    media: list["MediaResponse"] = Field(default=[], description="关联媒体文件列表")

    class Config:
        from_attributes = True


# ---- 分类 ----

class CategoryCreate(BaseModel):
    name: str = Field(..., max_length=128, description="分类名称")
    parent_id: Optional[str] = Field(None, description="父分类ID")
    level: int = Field(1, description="分类层级: 1=一级 2=二级")
    sort_order: int = Field(0, description="排序号")


class CategoryResponse(BaseModel):
    id: str = Field(description="分类ID")
    name: str = Field(description="分类名称")
    parent_id: Optional[str] = Field(None, description="父分类ID")
    level: int = Field(description="分类层级")
    sort_order: int = Field(description="排序号")

    class Config:
        from_attributes = True


# ---- 标签 ----

class TagDimensionCreate(BaseModel):
    name: str = Field(..., max_length=64, description="维度名称")


class TagValueCreate(BaseModel):
    value: str = Field(..., max_length=128, description="标签值")


class TagValueResponse(BaseModel):
    id: str = Field(description="标签值ID")
    dimension_id: str = Field(description="所属维度ID")
    value: str = Field(description="标签值")

    class Config:
        from_attributes = True


class TagBrief(BaseModel):
    id: str = Field(description="标签值ID")
    dimension_id: str = Field(description="所属维度ID")
    value: str = Field(description="标签值")

    class Config:
        from_attributes = True


class TagDimensionResponse(BaseModel):
    id: str = Field(description="维度ID")
    name: str = Field(description="维度名称")
    values: list[TagValueResponse] = Field(default=[], description="标签值列表")

    class Config:
        from_attributes = True


# ---- 检索 ----

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="搜索关键词")
    category_id: Optional[str] = Field(None, description="限定分类ID")
    tags: Optional[list[str]] = Field(None, description="限定标签")
    top_k: int = Field(default=10, ge=1, le=50, description="返回条数上限")


class SearchResult(BaseModel):
    id: str = Field(description="知识条目ID")
    title: str = Field(description="知识标题")
    content: Any = Field(description="知识内容")
    score: float = Field(description="匹配得分")
    status: str = Field(description="状态")
    category_id: Optional[str] = Field(None, description="所属分类ID")


class SearchResponse(BaseModel):
    query: str = Field(description="搜索关键词")
    total: int = Field(description="匹配总数")
    results: list[SearchResult] = Field(description="搜索结果列表")


# ---- 候选池 ----

class CandidateSubmit(BaseModel):
    title: str = Field(..., description="标题")
    content: Any = Field(..., description="内容")
    category_id: str = Field(..., description="所属分类ID")
    applicable_scenes: list[str] = Field(default=[], description="场景标签")
    source: str = Field("manual", description="来源")
    source_session_id: Optional[str] = Field(None, description="关联会话ID")
    submitted_by: str = Field("system", description="提交人")


# ---- 反馈 ----

class FeedbackSubmit(BaseModel):
    knowledge_id: str = Field(..., description="知识条目ID")
    action: str = Field(..., pattern=r"^(useful|useless)$", description="反馈动作")
    session_id: Optional[str] = Field(None, description="关联会话ID")


class DeduplicationFeedbackSubmit(BaseModel):
    matched_knowledge_id: str = Field(..., min_length=1, max_length=64, description="命中的已有知识ID")
    verdict: Literal["different"] = Field("different", description="人工复核结论")
    reason: str = Field(..., min_length=1, max_length=1000, description="判定不同的原因")


class ExcelImportRowResult(BaseModel):
    row: int = Field(description="Excel 行号")
    title: str = Field(description="知识标题")
    status: Literal["imported", "failed"] = Field(description="导入结果")
    knowledge_id: Optional[str] = Field(None, description="成功导入后的知识ID")
    error_code: Optional[str] = Field(None, description="失败错误码")
    error_message: Optional[str] = Field(None, description="失败原因")


class ExcelImportResponse(BaseModel):
    total: int = Field(description="有效数据总行数")
    imported: int = Field(description="成功导入行数")
    failed: int = Field(description="失败行数")
    results: list[ExcelImportRowResult] = Field(description="逐行导入结果")


KnowledgeResponse.model_rebuild()
