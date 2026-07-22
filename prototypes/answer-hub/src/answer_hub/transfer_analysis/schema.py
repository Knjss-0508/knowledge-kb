from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


TRANSFER_ANALYSIS_PROMPT_VERSION = "baixiaosheng-transfer-analysis-v1"

TRANSFER_REASON_OPTIONS = (
    "该问题没有相关知识",
    "问题复杂不确定怎么提问",
    "回答内容无法理解",
    "答非所问",
    "更信任人工",
    "其他",
)

SOLUTION_METHOD_OPTIONS = (
    "直接回答",
    "追问后回答",
    "调用工具",
    "不可解决",
)

YES_NO_UNKNOWN_OPTIONS = ("是", "否", "不确定")

BASE_ANALYSIS_COLUMNS = [
    "会话id",
    "工单ID",
    "问题",
    "大模型回答",
    "意图是否明确",
    "真实意图",
    "回答",
    "转人工原因",
    "转人工原因(校正)",
    "备注",
    "大模型是否可以解决",
    "是否有效问",
    "类目",
    "机型",
    "订单所处状态",
    "意图识别结果",
]

AUDIT_COLUMNS = [
    "曼哈顿转人工ID",
    "关联置信度",
    "关联依据",
    "百晓生完整会话",
    "转人工后完整会话",
    "召回知识",
    "Top相似度",
    "生产阈值",
    "是否建议补充知识",
    "建议优化责任方",
    "解决方式",
    "所需工具",
    "工具是否调用",
    "工具调用结果",
    "工具归因标签",
    "字段置信度",
    "证据引用",
    "是否需要人工复核",
    "模型名称",
    "Prompt版本",
    "模型状态",
    "审核状态",
    "审核人",
    "审核时间",
]

ANALYSIS_COLUMNS = BASE_ANALYSIS_COLUMNS + AUDIT_COLUMNS

REVIEW_EDIT_COLUMNS = [
    "意图是否明确",
    "真实意图",
    "转人工原因(校正)",
    "备注",
    "大模型是否可以解决",
    "是否有效问",
    "是否建议补充知识",
    "建议优化责任方",
    "解决方式",
    "所需工具",
    "工具是否调用",
    "工具调用结果",
    "工具归因标签",
]

BAD_CASE_COLUMNS = [
    "会话id",
    "工单ID",
    "问题",
    "回答",
    "转人工原因",
    "转人工原因(校正)",
    "备注",
    "大模型是否可以解决",
    "建议优化责任方",
    "召回知识",
    "Top相似度",
    "是否需要人工复核",
]

KNOWLEDGE_GAP_COLUMNS = [
    "会话id",
    "工单ID",
    "真实意图",
    "类目",
    "机型",
    "备注",
    "召回知识",
    "是否建议补充知识",
]

RETRIEVAL_COLUMNS = [
    "会话id",
    "工单ID",
    "真实意图",
    "召回知识",
    "Top相似度",
    "生产阈值",
    "回答",
    "转人工原因(校正)",
    "备注",
]

TOOL_ISSUE_COLUMNS = [
    "会话id",
    "工单ID",
    "问题",
    "真实意图",
    "所需工具",
    "工具是否调用",
    "工具调用结果",
    "工具归因标签",
    "备注",
]

OWNER_COLUMNS = [
    "建议优化责任方",
    "问题类型",
    "数量",
    "示例工单ID",
    "建议动作",
]


@dataclass(frozen=True)
class ToolCapability:
    name: str
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    confirmed_scope: bool = False
    requires_attachment: bool = True
    description: str = ""

    def matches_name(self, value: str) -> bool:
        normalized = str(value or "").strip().lower()
        candidates = (self.name, *self.aliases)
        return any(candidate.lower() in normalized for candidate in candidates if candidate)

    def matches_question(self, value: str) -> bool:
        normalized = str(value or "").lower()
        return any(keyword.lower() in normalized for keyword in self.keywords if keyword)


@dataclass(frozen=True)
class CapabilityRegistry:
    version: str
    supports_general_multimodal: bool = False
    tools: tuple[ToolCapability, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def tool_for_name(self, value: str) -> ToolCapability | None:
        return next((tool for tool in self.tools if tool.matches_name(value)), None)

    def tools_for_question(self, value: str) -> list[ToolCapability]:
        return [tool for tool in self.tools if tool.matches_question(value)]


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry(
    version="baixiaosheng-capabilities-v1",
    supports_general_multimodal=False,
    tools=(
        ToolCapability(
            name="内存硬盘品牌识别工具",
            aliases=("内存品牌识别", "硬盘品牌识别", "内存硬盘识别"),
            keywords=("内存品牌", "硬盘品牌", "内存条品牌", "固态硬盘品牌", "SSD品牌", "HDD品牌"),
            confirmed_scope=True,
            requires_attachment=True,
            description="识别内存或硬盘是否属于指定品牌；不代表通用图片理解能力。",
        ),
        ToolCapability(
            name="笔记本识别工具",
            aliases=("识别笔记本", "笔记本工具"),
            keywords=("识别笔记本", "笔记本识别", "笔记本品牌", "笔记本型号"),
            confirmed_scope=False,
            requires_attachment=True,
            description="精确输入输出范围待通过工具说明或成功调用记录确认。",
        ),
    ),
)
