import re
from dataclasses import dataclass
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

HEADER_ALIASES = {
    "title": {"标题", "知识标题", "主标题"},
    "category": {"知识分类", "所属分类", "分类", "知识分类ID", "分类ID"},
    "content": {"正文", "知识正文", "知识内容", "内容"},
    "subtitles": {"副标题", "副标题列表"},
    "scenes": {"场景标签", "适用场景"},
    "scope": {"适用范围"},
    "source_status": {"生效状态"},
    "applicable_categories": {"适用类目"},
    "brands": {"适用品牌", "品牌"},
    "models": {"适用机型", "机型"},
}

CATEGORY_VALUE_ALIASES = {
    "场景判定": "质检标准",
    "标准定义": "质检标准",
    "检测方法": "操作流程",
}

VALID_SOURCE_STATUSES = {"生效中", "待审核", "已禁用"}
IMPORTABLE_SOURCE_STATUS = "生效中"
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
    category_id: str = ""
    content: Any = ""
    subtitles: list[str] | None = None
    applicable_scenes: list[str] | None = None
    applicable_categories: list[str] | None = None
    applicable_brands: list[str] | None = None
    applicable_models: list[str] | None = None
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

    missing = [
        label
        for field, label in (
            ("title", "标题"),
            ("category", "知识分类"),
            ("content", "正文"),
        )
        if field not in indexes
    ]
    if missing:
        raise KnowledgeExcelError(
            f"缺少必填列：{'、'.join(missing)}。请使用系统下载的最新模板。"
        )
    return indexes


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
        source_status = _cell_text(value_at(values, "source_status"))
        source_scope = _cell_text(value_at(values, "scope"))
        result = ExcelKnowledgeRow(
            row_number=row_number,
            title=title,
            source_status=source_status,
            source_scope=source_scope,
        )
        try:
            if "source_status" in indexes:
                if not source_status:
                    raise KnowledgeExcelRowError(
                        "SOURCE_STATUS_REQUIRED",
                        "生效状态不能为空；仅“生效中”记录允许上传。",
                    )
                if source_status not in VALID_SOURCE_STATUSES:
                    raise KnowledgeExcelRowError(
                        "SOURCE_STATUS_INVALID",
                        f"生效状态“{source_status}”不受支持，"
                        "仅允许生效中、待审核或已禁用。",
                    )
                if source_status != IMPORTABLE_SOURCE_STATUS:
                    raise KnowledgeExcelRowError(
                        "SOURCE_STATUS_NOT_IMPORTABLE",
                        f"该记录为“{source_status}”，不会上传；"
                        "审核通过并改为“生效中”后再导入。",
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
        except KnowledgeExcelRowError as exc:
            result.error_code = exc.code
            result.error_message = str(exc)
        parsed_rows.append(result)

    if not parsed_rows:
        raise KnowledgeExcelError("Excel 中没有可导入的数据行。")
    return parsed_rows


def build_knowledge_import_template(categories) -> bytes:
    workbook = Workbook()
    import_sheet = workbook.active
    import_sheet.title = IMPORT_SHEET_NAME
    dictionary_sheet = workbook.create_sheet("分类字典")
    instructions_sheet = workbook.create_sheet("填写说明")

    headers = [
        "标题（必填）",
        "知识分类（必填）",
        "正文（必填）",
        "副标题（选填）",
        "场景标签（选填）",
        "适用类目（选填）",
        "适用品牌（选填）",
        "适用机型（选填）",
    ]
    import_sheet.append(headers)
    import_sheet.freeze_panes = "A2"
    import_sheet.auto_filter.ref = "A1:H1"
    import_sheet.row_dimensions[1].height = 28
    import_sheet.column_dimensions["A"].width = 32
    import_sheet.column_dimensions["B"].width = 28
    import_sheet.column_dimensions["C"].width = 70
    for column in ("D", "E", "F", "G", "H"):
        import_sheet.column_dimensions[column].width = 24

    required_fill = PatternFill("solid", fgColor="0F766E")
    optional_fill = PatternFill("solid", fgColor="475569")
    for index, cell in enumerate(import_sheet[1], start=1):
        cell.fill = required_fill if index <= 3 else optional_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    import_sheet["A1"].comment = Comment("必填，最多 256 个字符。", "知识库")
    import_sheet["B1"].comment = Comment(
        "必填。优先填写分类ID，也支持唯一分类名称或完整分类路径。",
        "知识库",
    )
    import_sheet["C1"].comment = Comment(
        "必填。仅处理插件自动回填的 [img:https://...] 或 "
        "[video:https://...] 标记；其他 URL 保持原文。",
        "知识库",
    )
    import_sheet["D1"].comment = Comment("多项请使用中文分号“；”分隔。", "知识库")
    for cell_ref in ("E1", "F1", "G1", "H1"):
        import_sheet[cell_ref].comment = Comment(
            "多项请使用中文分号“；”分隔。",
            "知识库",
        )

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
        ("必填列", "标题、知识分类、正文。"),
        ("知识分类", "推荐从“分类字典”复制分类ID；也可填写唯一分类名称或完整分类路径。"),
        ("多值字段", "副标题、场景标签等多项内容使用中文分号“；”分隔。"),
        (
            "兼容格式",
            "支持“知识库主表”的主标题、知识内容、适用范围和生效状态列；"
            "存在生效状态列时仅导入“生效中”记录。",
        ),
        (
            "正文媒体",
            "仅识别插件标记 [img:https://...] 和 [video:https://...]；"
            "导入后会在原位置显示缩略图或视频卡片，官网、文档及正文原有 URL 不转换。",
        ),
        ("导入结果", "成功行进入待审核状态；格式错误、分类不存在或查重未通过的行单独返回失败原因。"),
        ("单次上限", f"每个文件最多 {MAX_IMPORT_ROWS} 条、文件最大 5MB，仅支持 .xlsx。"),
        ("示例", "标题：设备无法开机；知识分类：cat-qc-process；正文：先检查电量，再长按电源键。"),
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
