from datetime import datetime
from typing import Literal, Optional, Any

from pydantic import BaseModel, Field, field_validator


BusinessType = Literal["self_operated", "aggregated"]
KnowledgeImportType = Literal["knowledge", "model_configuration"]
KnowledgeOrigin = Literal[
    "headquarters_standard",
    "business_accumulation",
    "model_configuration",
]
WritableKnowledgeOrigin = Literal[
    "headquarters_standard",
    "business_accumulation",
]


# ---- 富文本内容块 ----

class ContentBlock(BaseModel):
    type: str = Field(description="内容块类型: text=文本 image=图片 video=视频")
    value: Optional[str] = Field(None, description="文本内容，type=text时使用")
    media_id: Optional[str] = Field(None, description="媒体文件ID，type=image/video时使用")
    external_url: Optional[str] = Field(
        None,
        description="HTTPS 外链媒体地址，type=image/video时可代替media_id使用",
    )
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

class BusinessTypeOption(BaseModel):
    value: BusinessType = Field(description="业务类型编码")
    label: str = Field(description="业务类型名称")


class KnowledgeOriginOption(BaseModel):
    value: KnowledgeOrigin = Field(description="知识来源编码")
    label: str = Field(description="知识来源名称")


class KnowledgeCreate(BaseModel):
    knowledge_origin: WritableKnowledgeOrigin = Field(..., description="知识来源")
    business_type: BusinessType = Field(..., description="业务类型")
    title: str = Field(..., max_length=256, description="知识标题")
    subtitles: list[str] = Field(default=[], description="副标题列表，可多条")
    content: Any = Field(..., description="知识内容，支持富文本: 纯字符串 或 {blocks:[...]} 结构")
    category_id: str = Field(..., description="所属分类ID")
    applicable_scenes: list[str] = Field(default=[], description="场景标签列表")
    applicable_categories: list[Any] = Field(default=[], description="适用类目")
    applicable_brands: list[Any] = Field(default=[], description="适用品牌")
    applicable_models: list[Any] = Field(default=[], description="适用机型")
    related_standard_items: list[str] = Field(default=[], description="关联标准项")
    source_topic_key: Optional[str] = Field(None, max_length=512, description="来源主题键，可选")
    source_record_id: Optional[str] = Field(None, max_length=256, description="来源记录ID，可选")
    source_knowledge_key: Optional[str] = Field(None, max_length=512, description="来源知识键，可选")
    source_fields: dict[str, str] = Field(default_factory=dict, description="Excel 源表原始字段，仅用于追溯和导出")
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
    knowledge_origin: Optional[WritableKnowledgeOrigin] = Field(
        None,
        description="可人工维护的知识来源",
    )
    business_type: Optional[BusinessType] = Field(None, description="业务类型")
    title: Optional[str] = Field(None, description="知识标题")
    subtitles: Optional[list[str]] = Field(None, description="副标题列表")
    content: Optional[Any] = Field(None, description="知识内容")
    category_id: Optional[str] = Field(None, description="所属分类ID")
    status: Optional[str] = Field(None, description="状态: draft/review/published/deprecated")
    applicable_scenes: Optional[list[str]] = Field(None, description="场景标签列表")
    applicable_categories: Optional[list[Any]] = Field(None, description="适用类目")
    applicable_brands: Optional[list[Any]] = Field(None, description="适用品牌")
    applicable_models: Optional[list[Any]] = Field(None, description="适用机型")
    related_standard_items: Optional[list[str]] = Field(None, description="关联标准项")
    source_topic_key: Optional[str] = Field(None, max_length=512, description="来源主题键，可选")
    source_record_id: Optional[str] = Field(None, max_length=256, description="来源记录ID，可选")
    source_knowledge_key: Optional[str] = Field(None, max_length=512, description="来源知识键，可选")

    @field_validator("business_type")
    @classmethod
    def business_type_must_not_be_null(
        cls,
        value: Optional[BusinessType],
    ) -> Optional[BusinessType]:
        if value is None:
            raise ValueError("业务类型不能为空")
        return value

    @field_validator("knowledge_origin")
    @classmethod
    def knowledge_origin_must_not_be_null(
        cls,
        value: Optional[WritableKnowledgeOrigin],
    ) -> Optional[WritableKnowledgeOrigin]:
        if value is None:
            raise ValueError("知识来源不能为空")
        return value

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
    knowledge_origin: KnowledgeOrigin = Field(description="知识来源")
    business_type: BusinessType = Field(description="业务类型")
    title: str = Field(description="知识标题")
    subtitles: list[str] = Field(default=[], description="副标题列表")
    content: Any = Field(description="知识内容")
    category_id: Optional[str] = Field(None, description="所属分类ID")
    status: str = Field(description="当前状态")
    source: str = Field(description="录入方式")
    quality_score: float = Field(description="质量评分")
    applicable_scenes: list[str] = Field(default=[], description="场景标签")
    applicable_categories: list[Any] = Field(default=[], description="适用类目")
    applicable_brands: list[Any] = Field(default=[], description="适用品牌")
    applicable_models: list[Any] = Field(default=[], description="适用机型")
    related_standard_items: list[str] = Field(default=[], description="关联标准项")
    source_topic_key: Optional[str] = Field(None, description="来源主题键")
    source_record_id: Optional[str] = Field(None, description="来源记录ID")
    source_knowledge_key: Optional[str] = Field(None, description="来源知识键")
    import_review_metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Excel 待审核知识的校验备注与来源追溯，仅用于人工审核",
    )
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
    knowledge_origin: WritableKnowledgeOrigin = Field(
        ...,
        description="限定语义知识来源",
    )
    business_type: BusinessType = Field(..., description="限定业务类型")
    category_id: Optional[str] = Field(None, description="限定分类ID")
    tags: Optional[list[str]] = Field(None, description="限定标签")
    top_k: int = Field(default=10, ge=1, le=50, description="返回条数上限")


class SearchResult(BaseModel):
    id: str = Field(description="知识条目ID")
    knowledge_origin: KnowledgeOrigin = Field(description="知识来源")
    business_type: BusinessType = Field(description="业务类型")
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
    knowledge_origin: WritableKnowledgeOrigin = Field(
        ...,
        description="可人工维护的知识来源",
    )
    business_type: BusinessType = Field(..., description="业务类型")
    title: str = Field(..., description="标题")
    content: Any = Field(..., description="内容")
    category_id: str = Field(..., description="所属分类ID")
    applicable_scenes: list[str] = Field(default=[], description="场景标签")
    related_standard_items: list[str] = Field(default=[], description="关联标准项")
    confirm_dedup_review: bool = Field(
        False,
        description="疑似重复对比后，人工确认内容确实不同再提交",
    )
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
    status: Literal["imported", "review_pending", "review_required", "deprecated", "failed"] = Field(description="导入结果")
    knowledge_id: Optional[str] = Field(None, description="成功导入后的知识ID")
    error_code: Optional[str] = Field(None, description="失败错误码")
    error_message: Optional[str] = Field(None, description="失败原因")
    deduplication: dict[str, Any] | None = Field(
        None,
        description="疑似重复或重复时的命中详情",
    )
    review_task_id: Optional[str] = Field(
        None,
        description="疑似重复进入审核队列后的任务ID",
    )
    operation: Optional[Literal["created", "updated", "unchanged"]] = Field(
        None,
        description="机型配置整批同步中的逐行幂等操作结果",
    )


class ExcelImportResponse(BaseModel):
    total: int = Field(description="有效数据总行数")
    imported: int = Field(description="成功导入行数")
    review_required: int = Field(description="需人工确认疑似重复的行数")
    pending_review: int = Field(0, description="已进入待审核状态的行数")
    deprecated: int = Field(0, description="已同步为废弃状态的行数")
    failed: int = Field(description="失败行数")
    results: list[ExcelImportRowResult] = Field(description="逐行导入结果")


class KnowledgeImportTaskResponse(BaseModel):
    id: str = Field(description="导入任务ID")
    import_type: KnowledgeImportType = Field(description="导入类型")
    original_filename: str = Field(description="原始 Excel 文件名")
    file_size: int = Field(description="上传文件大小（字节）")
    status: Literal[
        "queued",
        "running",
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
    ] = Field(description="任务状态")
    total_rows: int = Field(description="解析后的总行数")
    processed_rows: int = Field(description="已完成处理的行数")
    imported: int = Field(description="成功处理数")
    review_required: int = Field(description="需人工确认疑似重复的知识数")
    pending_review: int = Field(description="进入待发布审核的知识数")
    deprecated: int = Field(description="已同步为废弃的知识数")
    failed: int = Field(description="失败行数")
    created: int = Field(description="机型配置新增数")
    updated: int = Field(description="机型配置更新数")
    unchanged: int = Field(description="机型配置无变化数")
    retry_rows: list[int] = Field(
        default_factory=list,
        description="等待安全重试的 Excel 原始行号",
    )
    attempt_count: int = Field(description="当前处理周期已尝试次数")
    next_attempt_at: datetime = Field(description="下次允许后台领取时间")
    error_message: str = Field("", description="任务级错误信息")
    created_at: datetime = Field(description="任务创建时间")
    started_at: datetime | None = Field(None, description="开始处理时间")
    completed_at: datetime | None = Field(None, description="完成时间")
    results: list[ExcelImportRowResult] = Field(default_factory=list, description="已处理行结果")


class KnowledgeImportTaskListResponse(BaseModel):
    tasks: list[KnowledgeImportTaskResponse]


class KnowledgeBatchApprove(BaseModel):
    knowledge_ids: list[str] = Field(..., min_length=1, max_length=500)


class KnowledgeBatchApproveResult(BaseModel):
    knowledge_id: str
    status: Literal["approved", "failed", "reused"]
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class KnowledgeBatchApproveResponse(BaseModel):
    approved: int
    failed: int
    reused: int
    results: list[KnowledgeBatchApproveResult]


class KnowledgeReviewSelectionResponse(BaseModel):
    total: int
    knowledge_ids: list[str]


KnowledgeResponse.model_rebuild()
