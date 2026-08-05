import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill


MAX_IMPORT_ROWS = 500
MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
IMPORT_SHEET_NAME = "知识导入"
EXPORT_SHEET_NAME = "知识库主表"

EXPORT_HEADERS = [
    "知识ID",
    "主题键",
    "记录ID",
    "知识键",
    "主标题",
    "副标题",
    "知识内容",
    "知识来源",
    "业务类型",
    "知识分类",
    "录入方式",
    "关联标准项",
    "适用范围",
    "生效状态",
    "来源版本",
    "变更类型",
    "创建类型",
    "失效类型",
    "失效原因",
    "来源追溯",
    "校验备注",
]

EXPORT_STATUS_LABELS = {
    "draft": "草稿",
    "review": "待审核",
    "published": "生效中",
    "deprecated": "已失效",
}

EXPORT_SOURCE_LABELS = {
    "manual": "手工录入",
    "excel": "Excel 批量导入",
    "integration": "接口导入",
    "automation": "接口导入",
    "candidate": "候选知识",
}

HEADER_ALIASES = {
    "title": {"标题", "知识标题", "主标题"},
    "knowledge_origin": {"知识来源", "业务来源"},
    "business_type": {"业务类型", "所属业务类型"},
    "category": {"知识分类", "所属分类", "分类", "知识分类ID", "分类ID"},
    "content": {"正文", "知识正文", "知识内容", "内容"},
    "subtitles": {"副标题", "副标题列表"},
    "scenes": {"场景标签", "适用场景"},
    "scope": {"适用范围"},
    "source_status": {"生效状态"},
    "applicable_categories": {"适用类目"},
    "brands": {"适用品牌", "品牌"},
    "models": {"适用机型", "机型"},
    "related_standard_items": {"关联标准项", "关联标准", "标准项"},
    "source_topic_key": {"主题键"},
    "source_record_id": {"记录ID"},
    "source_knowledge_key": {"知识键"},
}

EXPORT_HEADER_IMPORT_FIELDS = {
    "主题键": "source_topic_key",
    "记录ID": "source_record_id",
    "知识键": "source_knowledge_key",
    "主标题": "title",
    "副标题": "subtitles",
    "知识内容": "content",
    "业务类型": "business_type",
    "知识分类": "category",
    "知识来源": "knowledge_origin",
    "业务来源": "knowledge_origin",
    "关联标准项": "related_standard_items",
    "适用范围": "scope",
    "生效状态": "source_status",
}

CATEGORY_VALUE_ALIASES = {
    "场景判定": "质检标准",
    "标准定义": "质检标准",
    "检测方法": "操作流程",
}

BUSINESS_TYPE_LABELS = {
    "self_operated": "自营回收",
    "aggregated": "聚合回收",
}
BUSINESS_TYPE_VALUE_ALIASES = {
    "自营回收": "self_operated",
    "聚合回收": "aggregated",
    "self_operated": "self_operated",
    "aggregated": "aggregated",
}

KNOWLEDGE_ORIGIN_LABELS = {
    "headquarters_standard": "总部标准",
    "business_accumulation": "业务沉淀",
}
KNOWLEDGE_ORIGIN_VALUE_ALIASES = {
    "总部标准": "headquarters_standard",
    "业务沉淀": "business_accumulation",
    "headquarters_standard": "headquarters_standard",
    "business_accumulation": "business_accumulation",
}

VALID_SOURCE_STATUSES = {"生效中", "待审核", "已禁用"}
IMPORTABLE_SOURCE_STATUS = "生效中"
REVIEW_SOURCE_STATUS = "待审核"
DEPRECATED_SOURCE_STATUS = "已禁用"
UNRESTRICTED_SCOPES = {"通用"}
EXTERNAL_MEDIA_TOKEN_PATTERN = re.compile(
    r"\[(?P<kind>img|video):[ \t]*"
    r"(?P<url>https://[^\s\[\]<>\"']+)\]",
    re.IGNORECASE,
)


class KnowledgeExcelError(ValueError):
    """工作簿级错误，整个文件无法继续解析。"""


class KnowledgeExcelRowError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ExcelKnowledgeRow:
    row_number: int
    title: str
    knowledge_origin: str = ""
    business_type: str = ""
    category_id: str = ""
    content: Any = ""
    subtitles: list[str] | None = None
    applicable_scenes: list[str] | None = None
    applicable_categories: list[str] | None = None
    applicable_brands: list[str] | None = None
    applicable_models: list[str] | None = None
    related_standard_items: list[str] | None = None
    source_topic_key: str = ""
    source_record_id: str = ""
    source_knowledge_key: str = ""
    source_fields: dict[str, str] = field(default_factory=dict)
    source_status: str = ""
    source_scope: str = ""
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.error_code is None


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_header(value) -> str:
    text = _cell_text(value)
    text = re.sub(r"[（(].*?[）)]", "", text)
    return text.replace("*", "").replace(" ", "").strip()


def _split_values(value) -> list[str]:
    text = _cell_text(value)
    if not text:
        return []
    return [item.strip() for item in re.split(r"[；;|\n]+", text) if item.strip()]


def _merge_values(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value not in seen:
                seen.add(value)
                merged.append(value)
    return merged


def _is_safe_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _content_with_external_media(value) -> str | dict[str, Any]:
    text = _cell_text(value)
    blocks: list[dict[str, str]] = []
    found_media = False
    cursor = 0

    def append_text(segment: str) -> None:
        segment = segment.strip("\n")
        if segment.strip():
            blocks.append({"type": "text", "value": segment})

    for match in EXTERNAL_MEDIA_TOKEN_PATTERN.finditer(text):
        external_url = match.group("url").strip()
        if not _is_safe_external_url(external_url):
            continue
        media_type = "image" if match.group("kind").lower() == "img" else "video"
        append_text(text[cursor : match.start()])
        blocks.append(
            {
                "type": media_type,
                "external_url": external_url,
                "alt": "",
                "caption": "",
            }
        )
        cursor = match.end()
        found_media = True
    append_text(text[cursor:])

    return {"blocks": blocks} if found_media else text


def _category_records(categories) -> tuple[dict[str, object], dict[str, list[str]], dict[str, str]]:
    by_id = {str(category.id): category for category in categories}
    by_name: dict[str, list[str]] = {}
    path_by_id: dict[str, str] = {}

    def build_path(category_id: str, visited: set[str] | None = None) -> str:
        if category_id in path_by_id:
            return path_by_id[category_id]
        category = by_id[category_id]
        visited = set(visited or ())
        if category_id in visited:
            return str(category.name)
        visited.add(category_id)
        parent_id = str(category.parent_id) if category.parent_id else ""
        if parent_id and parent_id in by_id:
            path = f"{build_path(parent_id, visited)}/{category.name}"
        else:
            path = str(category.name)
        path_by_id[category_id] = path
        return path

    for category_id, category in by_id.items():
        name = str(category.name).strip()
        by_name.setdefault(name, []).append(category_id)
        build_path(category_id)

    return by_id, by_name, path_by_id


def _resolve_category(
    value,
    category_records: tuple[dict[str, object], dict[str, list[str]], dict[str, str]],
) -> str:
    text = _cell_text(value)
    if not text:
        raise KnowledgeExcelRowError("CATEGORY_REQUIRED", "知识分类不能为空。")

    by_id, by_name, path_by_id = category_records
    if text in by_id:
        return text

    path_matches = [
        category_id
        for category_id, path in path_by_id.items()
        if path == text
    ]
    if len(path_matches) == 1:
        return path_matches[0]

    name_matches = by_name.get(text, [])
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        raise KnowledgeExcelRowError(
            "CATEGORY_AMBIGUOUS",
            f"分类名称“{text}”存在重名，请填写分类ID或完整分类路径。",
        )

    mapped_name = CATEGORY_VALUE_ALIASES.get(text)
    if mapped_name:
        mapped_matches = by_name.get(mapped_name, [])
        if len(mapped_matches) == 1:
            return mapped_matches[0]
        if len(mapped_matches) > 1:
            raise KnowledgeExcelRowError(
                "CATEGORY_AMBIGUOUS",
                f"兼容分类“{text}”映射到“{mapped_name}”后存在重名，"
                "请填写分类ID或完整分类路径。",
            )
    raise KnowledgeExcelRowError(
        "CATEGORY_NOT_FOUND",
        f"分类“{text}”不存在，请从模板的“分类字典”工作表中选择。",
    )


def _resolve_business_type(value) -> str:
    text = _cell_text(value)
    if not text:
        raise KnowledgeExcelRowError(
            "BUSINESS_TYPE_REQUIRED",
            "业务类型不能为空。",
        )
    normalized = text.lower()
    business_type = BUSINESS_TYPE_VALUE_ALIASES.get(normalized)
    if business_type:
        return business_type
    raise KnowledgeExcelRowError(
        "BUSINESS_TYPE_INVALID",
        f"业务类型“{text}”不受支持，仅允许自营回收、聚合回收、"
        "self_operated 或 aggregated。",
    )


def _resolve_knowledge_origin(value) -> str:
    text = _cell_text(value)
    if not text:
        raise KnowledgeExcelRowError(
            "KNOWLEDGE_ORIGIN_REQUIRED",
            "知识来源不能为空。",
        )
    normalized = text.lower()
    knowledge_origin = KNOWLEDGE_ORIGIN_VALUE_ALIASES.get(normalized)
    if knowledge_origin:
        return knowledge_origin
    raise KnowledgeExcelRowError(
        "KNOWLEDGE_ORIGIN_INVALID",
        f"知识来源“{text}”不受支持，仅允许总部标准、业务沉淀、"
        "headquarters_standard 或 business_accumulation。",
    )


def _header_indexes(header_row) -> dict[str, int]:
    normalized = {
        _normalize_header(value): index
        for index, value in enumerate(header_row)
        if _normalize_header(value)
    }
    indexes: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                indexes[field] = normalized[alias]
                break

    is_source_deprecation_sheet = (
        "source_status" in indexes
        and any(
            field in indexes
            for field in (
                "source_knowledge_key",
                "source_topic_key",
                "source_record_id",
            )
        )
    )
    missing = []
    if "knowledge_origin" not in indexes:
        missing.append("知识来源")
    if "business_type" not in indexes:
        missing.append("业务类型")
    if not is_source_deprecation_sheet:
        missing.extend(
            label
            for field, label in (
                ("title", "标题"),
                ("category", "知识分类"),
                ("content", "正文"),
            )
            if field not in indexes
        )
    if missing:
        raise KnowledgeExcelError(
            f"缺少必填列：{'、'.join(missing)}。请使用系统下载的最新模板。"
        )
    return indexes


def _source_fields(header_row, values) -> dict[str, str]:
    """保留上传表中所有非空表头对应的原始单元格值，供后续导出还原。"""
    fields: dict[str, str] = {}
    for index, header in enumerate(header_row):
        normalized_header = _normalize_header(header)
        if not normalized_header:
            continue
        value = values[index] if index < len(values) else None
        fields[normalized_header] = _cell_text(value)
    return fields


def _validate_xlsx_container(data: bytes) -> None:
    if not data.startswith(b"PK"):
        raise KnowledgeExcelError("文件不是有效的 .xlsx 工作簿。")
    try:
        with ZipFile(BytesIO(data)) as archive:
            total_size = sum(entry.file_size for entry in archive.infolist())
    except BadZipFile as exc:
        raise KnowledgeExcelError("文件不是有效的 .xlsx 工作簿。") from exc
    if total_size > MAX_UNCOMPRESSED_BYTES:
        raise KnowledgeExcelError("Excel 解压后体积过大，请拆分后导入。")


def parse_knowledge_workbook(data: bytes, categories) -> list[ExcelKnowledgeRow]:
    if not data:
        raise KnowledgeExcelError("Excel 文件为空。")
    if len(data) > MAX_IMPORT_FILE_BYTES:
        raise KnowledgeExcelError("Excel 文件不能超过 5MB。")
    _validate_xlsx_container(data)

    try:
        workbook = load_workbook(
            BytesIO(data),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    except Exception as exc:
        raise KnowledgeExcelError("Excel 文件损坏或无法读取。") from exc

    sheet = (
        workbook[IMPORT_SHEET_NAME]
        if IMPORT_SHEET_NAME in workbook.sheetnames
        else workbook.active
    )
    rows = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration as exc:
        raise KnowledgeExcelError("Excel 中没有可读取的表头。") from exc
    indexes = _header_indexes(header_row)
    category_records = _category_records(categories)

    def value_at(values, field: str):
        index = indexes.get(field)
        return values[index] if index is not None and index < len(values) else None

    parsed_rows: list[ExcelKnowledgeRow] = []
    for row_number, values in enumerate(rows, start=2):
        if not any(_cell_text(value) for value in values):
            continue
        if len(parsed_rows) >= MAX_IMPORT_ROWS:
            raise KnowledgeExcelError(
                f"单次最多导入 {MAX_IMPORT_ROWS} 条知识，请拆分文件后重试。"
            )

        title = _cell_text(value_at(values, "title"))
        knowledge_origin = _cell_text(value_at(values, "knowledge_origin"))
        business_type = _cell_text(value_at(values, "business_type"))
        source_status = _cell_text(value_at(values, "source_status"))
        source_scope = _cell_text(value_at(values, "scope"))
        source_topic_key = _cell_text(value_at(values, "source_topic_key"))
        source_record_id = _cell_text(value_at(values, "source_record_id"))
        source_knowledge_key = _cell_text(value_at(values, "source_knowledge_key"))
        result = ExcelKnowledgeRow(
            row_number=row_number,
            title=title,
            source_fields=_source_fields(header_row, values),
            source_status=source_status,
            source_scope=source_scope,
            source_topic_key=source_topic_key,
            source_record_id=source_record_id,
            source_knowledge_key=source_knowledge_key,
        )
        try:
            result.knowledge_origin = _resolve_knowledge_origin(knowledge_origin)
            result.business_type = _resolve_business_type(business_type)
            if "source_status" in indexes:
                if not source_status:
                    raise KnowledgeExcelRowError(
                        "SOURCE_STATUS_REQUIRED",
                        "生效状态不能为空。",
                    )
                if source_status not in VALID_SOURCE_STATUSES:
                    raise KnowledgeExcelRowError(
                        "SOURCE_STATUS_INVALID",
                        f"生效状态“{source_status}”不受支持，"
                        "仅允许生效中、待审核或已禁用。",
                    )
                if source_status == DEPRECATED_SOURCE_STATUS:
                    if not any(
                        (
                            source_knowledge_key,
                            source_topic_key,
                            source_record_id,
                        )
                    ):
                        raise KnowledgeExcelRowError(
                            "SOURCE_IDENTIFIER_REQUIRED",
                            "“已禁用”记录至少需要填写知识键、主题键或记录ID，"
                            "以定位需要废弃的原知识。",
                        )
                    parsed_rows.append(result)
                    continue

                if source_status not in {
                    IMPORTABLE_SOURCE_STATUS,
                    REVIEW_SOURCE_STATUS,
                }:
                    raise KnowledgeExcelRowError(
                        "SOURCE_STATUS_NOT_IMPORTABLE",
                        f"该记录为“{source_status}”，无法处理。",
                    )

            if not title:
                raise KnowledgeExcelRowError("TITLE_REQUIRED", "标题不能为空。")
            if len(title) > 256:
                raise KnowledgeExcelRowError("TITLE_TOO_LONG", "标题不能超过 256 个字符。")

            content = _cell_text(value_at(values, "content"))
            if not content:
                raise KnowledgeExcelRowError("CONTENT_REQUIRED", "正文不能为空。")
            if len(content) > 100_000:
                raise KnowledgeExcelRowError(
                    "CONTENT_TOO_LONG",
                    "单条正文不能超过 100000 个字符。",
                )

            result.category_id = _resolve_category(
                value_at(values, "category"),
                category_records,
            )
            result.content = _content_with_external_media(content)
            result.subtitles = _split_values(value_at(values, "subtitles"))
            scope_tags = [
                f"适用范围：{scope}"
                for scope in _split_values(source_scope)
                if scope not in UNRESTRICTED_SCOPES
            ]
            result.applicable_scenes = _merge_values(
                _split_values(value_at(values, "scenes")),
                scope_tags,
            )
            result.applicable_categories = _split_values(
                value_at(values, "applicable_categories")
            )
            result.applicable_brands = _split_values(value_at(values, "brands"))
            result.applicable_models = _split_values(value_at(values, "models"))
            result.related_standard_items = _split_values(
                value_at(values, "related_standard_items")
            )
        except KnowledgeExcelRowError as exc:
            result.error_code = exc.code
            result.error_message = str(exc)
        parsed_rows.append(result)

    if not parsed_rows:
        raise KnowledgeExcelError("Excel 中没有可导入的数据行。")
    return parsed_rows


def _export_cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("name", "label", "value", "title", "id"):
            item = value.get(key)
            if item not in (None, ""):
                return _export_cell_text(item)
        return ""
    return str(value).strip()


def _export_join(values: Any, separator: str = "；") -> str:
    if not isinstance(values, (list, tuple, set)):
        return _export_cell_text(values)
    result: list[str] = []
    for value in values:
        text = _export_cell_text(value)
        if text and text not in result:
            result.append(text)
    return separator.join(result)


def _export_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return _export_cell_text(value)

    blocks = value.get("blocks")
    if not isinstance(blocks, list):
        return _export_cell_text(value)

    pieces: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            text = _export_cell_text(block)
            if text:
                pieces.append(text)
            continue
        block_type = _export_cell_text(block.get("type")).lower()
        if block_type == "text":
            text = _export_cell_text(block.get("value") or block.get("text"))
        elif block_type in {"image", "video"}:
            external_url = _export_cell_text(block.get("external_url"))
            if external_url:
                token = "img" if block_type == "image" else "video"
                text = f"[{token}:{external_url}]"
            else:
                label = _export_cell_text(block.get("caption") or block.get("alt"))
                media_label = "图片" if block_type == "image" else "视频"
                text = f"[{media_label}{'：' + label if label else ''}]"
        else:
            text = _export_cell_text(block.get("value") or block.get("text"))
        if text:
            pieces.append(text)
    return "\n".join(pieces)


def _export_scope(item: Any) -> str:
    groups = (
        ("场景", getattr(item, "applicable_scenes", None)),
        ("适用类目", getattr(item, "applicable_categories", None)),
        ("适用品牌", getattr(item, "applicable_brands", None)),
        ("适用机型", getattr(item, "applicable_models", None)),
    )
    parts = [
        f"{label}：{values}"
        for label, raw_values in groups
        if (values := _export_join(raw_values))
    ]
    return "；".join(parts) if parts else "通用"


def _export_status(value: Any) -> str:
    raw = _export_cell_text(getattr(value, "value", value)).lower()
    return EXPORT_STATUS_LABELS.get(raw, _export_cell_text(value))


def _export_source(value: Any) -> str:
    raw = _export_cell_text(value)
    return EXPORT_SOURCE_LABELS.get(raw, raw)


def _export_business_type(value: Any) -> str:
    raw = _export_cell_text(getattr(value, "value", value))
    normalized = raw.lower()
    canonical = BUSINESS_TYPE_VALUE_ALIASES.get(normalized, normalized)
    return BUSINESS_TYPE_LABELS.get(canonical, raw)


def _export_knowledge_origin(value: Any) -> str:
    raw = _export_cell_text(getattr(value, "value", value))
    normalized = raw.lower()
    canonical = KNOWLEDGE_ORIGIN_VALUE_ALIASES.get(normalized, normalized)
    return KNOWLEDGE_ORIGIN_LABELS.get(canonical, raw)


def _export_source_field(source_fields: Any, header: str, fallback: str) -> str:
    """导出时优先使用上传时保留的原始字段，旧数据则使用系统字段兜底。"""
    if not isinstance(source_fields, dict):
        return fallback
    candidates = [_normalize_header(header)]
    import_field = EXPORT_HEADER_IMPORT_FIELDS.get(header)
    if import_field:
        candidates.extend(
            _normalize_header(alias)
            for alias in HEADER_ALIASES.get(import_field, set())
        )
    for candidate in candidates:
        if candidate in source_fields:
            return _export_cell_text(source_fields[candidate])
    return fallback


def build_knowledge_export_workbook(items) -> bytes:
    """生成与历史知识库主表字段一致的只读导出工作簿。"""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = EXPORT_SHEET_NAME
    sheet.append(EXPORT_HEADERS)

    for item in items:
        category = getattr(item, "category", None)
        category_name = _export_cell_text(getattr(category, "name", None))
        if not category_name:
            category_name = _export_cell_text(getattr(item, "category_id", None))
        source_fields = getattr(item, "source_fields", None)
        business_type = getattr(item, "business_type", None)
        if not _export_cell_text(business_type):
            business_type = _export_source_field(
                source_fields,
                "业务类型",
                "",
            )
        knowledge_origin = getattr(item, "knowledge_origin", None)
        if not _export_cell_text(knowledge_origin):
            knowledge_origin = _export_source_field(
                source_fields,
                "知识来源",
                "",
            )
        sheet.append(
            [
                _export_cell_text(getattr(item, "id", None)),
                _export_source_field(source_fields, "主题键", _export_cell_text(getattr(item, "source_topic_key", None))),
                _export_source_field(source_fields, "记录ID", _export_cell_text(getattr(item, "source_record_id", None))),
                _export_source_field(source_fields, "知识键", _export_cell_text(getattr(item, "source_knowledge_key", None))),
                _export_source_field(source_fields, "主标题", _export_cell_text(getattr(item, "title", None))),
                _export_source_field(source_fields, "副标题", _export_join(getattr(item, "subtitles", None), separator="\n")),
                _export_source_field(source_fields, "知识内容", _export_content(getattr(item, "content", None))),
                _export_knowledge_origin(knowledge_origin),
                _export_business_type(business_type),
                _export_source_field(source_fields, "知识分类", category_name),
                _export_source(getattr(item, "source", None)),
                _export_source_field(source_fields, "关联标准项", _export_join(getattr(item, "related_standard_items", None)),),
                _export_source_field(source_fields, "适用范围", _export_scope(item)),
                _export_source_field(source_fields, "生效状态", _export_status(getattr(item, "status", None))),
                _export_source_field(source_fields, "来源版本", ""),
                _export_source_field(source_fields, "变更类型", ""),
                _export_source_field(source_fields, "创建类型", ""),
                _export_source_field(source_fields, "失效类型", ""),
                _export_source_field(source_fields, "失效原因", ""),
                _export_source_field(source_fields, "来源追溯", ""),
                _export_source_field(source_fields, "校验备注", ""),
            ]
        )

    header_fill = PatternFill("solid", fgColor="D9E8FB")
    header_font = Font(name="宋体", size=11, bold=True)
    body_font = Font(name="宋体", size=11)
    header_alignment = Alignment(
        horizontal="center",
        vertical="center",
        wrap_text=True,
    )
    body_alignment = Alignment(vertical="top", wrap_text=True)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:U{max(sheet.max_row, 1)}"
    sheet.row_dimensions[1].height = 30
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row):
        for cell in row:
            cell.font = body_font
            cell.alignment = body_alignment

    column_widths = {
        "A": 14,
        "B": 18,
        "C": 16,
        "D": 16,
        "E": 32,
        "F": 36,
        "G": 72,
        "H": 16,
        "I": 16,
        "J": 18,
        "K": 18,
        "L": 28,
        "M": 42,
        "N": 14,
        "O": 16,
        "P": 16,
        "Q": 16,
        "R": 16,
        "S": 22,
        "T": 28,
        "U": 28,
    }
    for column, width in column_widths.items():
        sheet.column_dimensions[column].width = width

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def build_knowledge_import_template(categories) -> bytes:
    workbook = Workbook()
    import_sheet = workbook.active
    import_sheet.title = IMPORT_SHEET_NAME
    knowledge_origin_sheet = workbook.create_sheet("知识来源字典")
    business_type_sheet = workbook.create_sheet("业务类型字典")
    dictionary_sheet = workbook.create_sheet("分类字典")
    instructions_sheet = workbook.create_sheet("填写说明")

    headers = [
        "标题（必填）",
        "知识来源（必填）",
        "业务类型（必填）",
        "知识分类（必填）",
        "正文（必填）",
        "副标题（选填）",
        "场景标签（选填）",
        "关联标准项（选填）",
        "适用类目（选填）",
        "适用品牌（选填）",
        "适用机型（选填）",
    ]
    import_sheet.append(headers)
    import_sheet.freeze_panes = "A2"
    import_sheet.auto_filter.ref = "A1:K1"
    import_sheet.row_dimensions[1].height = 28
    import_sheet.column_dimensions["A"].width = 32
    import_sheet.column_dimensions["B"].width = 22
    import_sheet.column_dimensions["C"].width = 22
    import_sheet.column_dimensions["D"].width = 28
    import_sheet.column_dimensions["E"].width = 70
    for column in ("F", "G", "H", "I", "J", "K"):
        import_sheet.column_dimensions[column].width = 24

    required_fill = PatternFill("solid", fgColor="0F766E")
    optional_fill = PatternFill("solid", fgColor="475569")
    for index, cell in enumerate(import_sheet[1], start=1):
        cell.fill = required_fill if index <= 5 else optional_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    import_sheet["A1"].comment = Comment("必填，最多 256 个字符。", "知识库")
    import_sheet["B1"].comment = Comment(
        "必填。可填写总部标准、业务沉淀，或对应代码 "
        "headquarters_standard、business_accumulation。",
        "知识库",
    )
    import_sheet["C1"].comment = Comment(
        "必填。可填写自营回收、聚合回收，或对应代码 "
        "self_operated、aggregated。",
        "知识库",
    )
    import_sheet["D1"].comment = Comment(
        "必填。优先填写分类ID，也支持唯一分类名称或完整分类路径。",
        "知识库",
    )
    import_sheet["E1"].comment = Comment(
        "必填。仅处理插件自动回填的 [img:https://...] 或 "
        "[video:https://...] 标记；其他 URL 保持原文。",
        "知识库",
    )
    import_sheet["F1"].comment = Comment("多项请使用中文分号“；”分隔。", "知识库")
    for cell_ref in ("G1", "H1", "I1", "J1", "K1"):
        import_sheet[cell_ref].comment = Comment(
            "多项请使用中文分号“；”分隔。",
            "知识库",
        )

    knowledge_origin_sheet.append(["知识来源代码", "知识来源名称"])
    for code, label in KNOWLEDGE_ORIGIN_LABELS.items():
        knowledge_origin_sheet.append([code, label])
    knowledge_origin_sheet.freeze_panes = "A2"
    knowledge_origin_sheet.auto_filter.ref = (
        f"A1:B{max(knowledge_origin_sheet.max_row, 1)}"
    )
    knowledge_origin_sheet.column_dimensions["A"].width = 28
    knowledge_origin_sheet.column_dimensions["B"].width = 24
    for cell in knowledge_origin_sheet[1]:
        cell.fill = required_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")

    business_type_sheet.append(["业务类型代码", "业务类型名称"])
    for code, label in BUSINESS_TYPE_LABELS.items():
        business_type_sheet.append([code, label])
    business_type_sheet.freeze_panes = "A2"
    business_type_sheet.auto_filter.ref = (
        f"A1:B{max(business_type_sheet.max_row, 1)}"
    )
    business_type_sheet.column_dimensions["A"].width = 24
    business_type_sheet.column_dimensions["B"].width = 24
    for cell in business_type_sheet[1]:
        cell.fill = required_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")

    dictionary_sheet.append(["分类ID", "分类名称", "完整分类路径"])
    by_id, _, path_by_id = _category_records(categories)
    sorted_categories = sorted(
        by_id.values(),
        key=lambda item: (
            int(getattr(item, "level", 1) or 1),
            int(getattr(item, "sort_order", 0) or 0),
            str(item.name),
        ),
    )
    for category in sorted_categories:
        category_id = str(category.id)
        dictionary_sheet.append(
            [category_id, str(category.name), path_by_id[category_id]]
        )
    dictionary_sheet.freeze_panes = "A2"
    dictionary_sheet.auto_filter.ref = (
        f"A1:C{max(dictionary_sheet.max_row, 1)}"
    )
    dictionary_sheet.column_dimensions["A"].width = 24
    dictionary_sheet.column_dimensions["B"].width = 28
    dictionary_sheet.column_dimensions["C"].width = 42
    for cell in dictionary_sheet[1]:
        cell.fill = required_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")

    instructions = [
        ("必填列", "标题、知识来源、业务类型、知识分类、正文。"),
        (
            "知识来源",
            "从“知识来源字典”复制中文名称或代码；"
            "仅允许总部标准、业务沉淀、headquarters_standard、"
            "business_accumulation。",
        ),
        (
            "业务类型",
            "从“业务类型字典”复制中文名称或代码；"
            "仅允许自营回收、聚合回收、self_operated、aggregated。",
        ),
        ("知识分类", "推荐从“分类字典”复制分类ID；也可填写唯一分类名称或完整分类路径。"),
        ("多值字段", "副标题、场景标签、关联标准项等多项内容使用中文分号“；”分隔。"),
        (
            "兼容格式",
            "支持“知识库主表”的主标题、知识内容、适用范围和生效状态列；"
            "主表也必须提供知识来源（兼容列名“业务来源”）；"
            "存在生效状态列时：生效中直接发布，待审核进入审核，"
            "已禁用按知识键、主题键或记录ID同步废弃原知识。",
        ),
        (
            "正文媒体",
            "仅识别插件标记 [img:https://...] 和 [video:https://...]；"
            "导入后会在原位置显示缩略图或视频卡片，官网、文档及正文原有 URL 不转换。",
        ),
        (
            "疑似重复确认",
            "疑似重复行会自动进入系统“候选价值复核”的“疑似重复确认”筛选，"
            "由审核人核对命中知识后决定是否送审；无需修改 Excel 或重复上传。",
        ),
        (
            "导入结果",
            "来源表的“生效中”行直接发布，“待审核”行进入待审核，"
            "“已禁用”行同步废弃原知识；普通模板成功行进入待审核；"
            "疑似重复、格式错误、分类不存在或完全重复的行会逐行返回原因。",
        ),
        ("单次上限", f"每个文件最多 {MAX_IMPORT_ROWS} 条、文件最大 5MB，仅支持 .xlsx。"),
        (
            "示例",
            "标题：设备无法开机；知识来源：总部标准；业务类型：自营回收；"
            "知识分类：cat-qc-process；正文：先检查电量，再长按电源键。",
        ),
    ]
    instructions_sheet.append(["项目", "说明"])
    for item in instructions:
        instructions_sheet.append(item)
    instructions_sheet.column_dimensions["A"].width = 18
    instructions_sheet.column_dimensions["B"].width = 90
    instructions_sheet.freeze_panes = "A2"
    for cell in instructions_sheet[1]:
        cell.fill = required_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")
    for row in instructions_sheet.iter_rows(min_row=2, max_col=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
