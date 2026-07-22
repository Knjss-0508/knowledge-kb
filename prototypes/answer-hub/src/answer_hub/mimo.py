from __future__ import annotations

"""Small OpenAI-compatible client for the multi-category knowledge workflow.

The client intentionally uses only the Python standard library: the project can
run with the dependencies that are already declared in ``pyproject.toml`` and
the API key never needs to be sent to the browser.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import os
import re

from .catalog import StandardCatalogItem
from .images import ImageEvidence
from .product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    canonical_product_name,
    configured_product_names,
    product_category_prompt,
)


PROMPT_VERSION = "multi-category-topic-transcription-v2"
TOPIC_REVIEW_PROMPT_VERSION = "multi-category-topic-initial-review-v3"
CLUSTER_PAIR_REVIEW_PROMPT_VERSION = "knowledge-cluster-membership-review-v3"
TOPIC_SIGNAL_PROMPT_VERSION = "multi-category-conversation-topic-signal-v4"
CLUSTER_UNIT_PROMPT_VERSION = "multi-category-conversation-cluster-units-v3"
ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION = "atomic-knowledge-topic-clustering-v2"


class MimoError(RuntimeError):
    """The configured MiMo endpoint could not produce a valid candidate."""


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overwriting actual environment vars."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class MimoConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "MimoConfig | None":
        load_dotenv()
        api_key = os.getenv("MIMO_API_KEY", "").strip()
        base_url = os.getenv("MIMO_BASE_URL", "").strip()
        model = os.getenv("MIMO_MODEL", "").strip()
        if not (api_key and base_url and model):
            return None
        try:
            timeout = max(10, min(int(os.getenv("MIMO_TIMEOUT_SECONDS", "60")), 180))
        except ValueError:
            timeout = 60
        return cls(api_key=api_key, base_url=base_url, model=model, timeout_seconds=timeout)

    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


@dataclass(frozen=True)
class MimoLabelResult:
    candidate: dict[str, Any]
    request_audit: dict[str, Any]
    response_audit: dict[str, Any]


def _text(value: Any, limit: int = 7000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _standard_ref(item: StandardCatalogItem) -> str:
    return item.standard_id or item.standard_path or item.title


def _standard_payload(matches: list[tuple[StandardCatalogItem, float]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item, score in matches:
        payload.append(
            {
                "standard_ref": _standard_ref(item),
                "standard_id": item.standard_id,
                "title": item.title,
                "category_l1": item.category_l1,
                "category_l2": item.category_l2,
                "knowledge_type": item.knowledge_type,
                "standard_path": item.standard_path,
                "keywords": item.keywords,
                "scope": item.scope,
                "response_snippet": item.response_snippet,
                "status": item.status,
                "version": item.version,
                "retrieval_score": round(score, 3),
            }
        )
    return payload


def _source_payload(source_row: dict[str, Any]) -> dict[str, str]:
    fields = [
        "工单ID", "回收单号", "产品类型", "一级分类", "二级分类",
        "核心问题", "聊天内容", "判定结论", "判定依据", "参考话术",
    ]
    return {field: _text(source_row.get(field)) for field in fields if _text(source_row.get(field))}


def _image_metadata(images: list[ImageEvidence]) -> list[dict[str, Any]]:
    return [image.metadata() for image in images]


def _topic_signal_source_payload(source_row: dict[str, Any]) -> dict[str, Any]:
    """Separate primary conversation evidence from legacy classifier metadata."""
    return {
        "primary_evidence": {
            "work_order_id": _text(source_row.get("工单ID")),
            "product_type": _text(source_row.get("产品类型")),
            "conversation": _text(source_row.get("聊天内容"), 9000),
            "historical_actual_reply": _text(
                source_row.get("历史实际回复") or source_row.get("参考话术"),
                4000,
            ),
            "video_links": _text(source_row.get("视频链接"), 3000),
            "video_processing_status": _text(source_row.get("视频处理状态")),
        },
        "legacy_reference_only": {
            "quality_question_description": _text(source_row.get("核心问题")),
            "legacy_judgment": _text(source_row.get("判定结论")),
            "legacy_basis": _text(source_row.get("判定依据")),
            "legacy_category_l1": _text(source_row.get("一级分类")),
            "legacy_category_l2": _text(source_row.get("二级分类")),
        },
    }


def _build_topic_signal_prompt(
    source_row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    images: list[ImageEvidence],
    retry_reason: str = "",
    use_standard_references: bool = True,
) -> str:
    has_ready_images = any(image.status == "ready" and image.data_url for image in images)
    image_note = (
        "没有可用图片。"
        if not has_ready_images
        else (
            "已附上现场图片；图片是会话证据的一部分，不能脱离有效标准单独下结论。"
            if use_standard_references
            else "已附上现场图片；图片是会话证据的一部分，不能脱离完整会话和已知事实单独下结论。"
        )
    )
    video_note = (
        _text(source_row.get("视频处理状态"))
        or ("存在视频链接，但当前接口未上传或解析视频内容。" if _text(source_row.get("视频链接")) else "没有视频链接。")
    )
    evidence_instruction = (
        "必须以【primary_evidence】中的完整聊天内容和可用图片为主，结合本次检索到的有效标准理解用户真实意图。"
        if use_standard_references
        else "必须只以【primary_evidence】中的完整聊天内容、历史实际回复和可用图片为主；本模式不检索、不引用质检标准。"
    )
    standard_rule = (
        "standard_refs 只能从本次检索结果的 standard_ref 中选择；无可靠匹配则输出 []，并将 needs_human_review 设为 true。"
        if use_standard_references
        else "standard_refs 必须输出 []。不得补写、猜测或引用任何质检标准；是否需要人工复核只由案例证据、图片质量和标注置信度决定。"
    )
    standard_section = (
        "【本次检索到的当前品类生效标准与已有知识】\n"
        f"{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}"
        if use_standard_references
        else "【标准引用模式】\n本次关闭标准检索与标准引用，只处理第二部分案例证据。"
    )
    resolution_examples = (
        "“对照标准判定”“补拍图片核验”“信息查询与实物核对”“补充证据后再判定”"
        if use_standard_references
        else "“结合案例证据判断”“补拍图片核验”“信息查询与实物核对”“补充信息后再处理”"
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是答疑中台的会话语义标注员。请从一条已脱敏的多品类质检会话中提取用于主题聚类的规范化标签。当前生效品类为：{product_category_prompt()}。

本任务不是知识改写，也不是复述百晓生的质检问题。{evidence_instruction} legacy_reference_only 中的质检问题描述、历史判定和历史一二级分类来自旧系统，只能在与聊天证据一致时作弱参考；出现冲突时必须以完整会话为准。

必须遵守：
1. intent 只能为："标准判定"、"检测核验"、"信息查询"、"流程操作"、"其他待确认" 之一。
2. subject 表示用户实际询问的对象或部位；phenomenon 表示异常现象或查询目标。无法确认时填“待确认”，不得从旧分类猜测。
3. resolution_mode 表示最终应沉淀的处理方式，例如{resolution_examples}。
4. topic_tags 必须是 3 到 6 个可复用的短标签，使用“维度:值”格式，且至少覆盖 intent、subject、phenomenon 或 query_target、resolution_mode。不要复制旧分类名称凑标签。
5. {standard_rule}
6. category_l1 和 category_l2 是模型推断的工作分类，不得直接照抄 legacy 分类；无充分证据时填“待确认”。
7. 需要通过外观、部位、颜色、裂纹、坏点、拆修痕迹等视觉差异才能判断时，requires_images=true；图片不可用或不足时在 image_evidence_summary 说明，并标记人工复核。
8. 当前接口没有上传视频内容。若存在视频链接，只能把它视为“尚待人工查看的视频证据”，不得猜测视频画面、动作或声音；结论依赖视频时应标记人工复核。
9. reasoning_summary 仅写给审核人的结论依据，不要输出思维过程，不超过 240 字。
10. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "intent": "标准判定 / 检测核验 / 信息查询 / 流程操作 / 其他待确认",
  "subject": "string",
  "phenomenon": "string",
  "resolution_mode": "string",
  "category_l1": "string",
  "category_l2": "string",
  "topic_tags": ["维度:值"],
  "standard_refs": ["standard_ref"],
  "requires_images": false,
  "image_evidence_summary": "string",
  "reasoning_summary": "string",
  "confidence": 0.0,
  "needs_human_review": true
}}

【会话输入】
{json.dumps(_topic_signal_source_payload(source_row), ensure_ascii=False, indent=2)}

{standard_section}

【图片情况】
{image_note}
图片下载元数据：{json.dumps(_image_metadata(images), ensure_ascii=False)}

【视频情况】
{video_note}
{retry_instruction}
"""


def _build_prompt(
    source_row: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    images: list[ImageEvidence],
    retry_reason: str = "",
) -> str:
    standard_payload = _standard_payload(matches)
    has_ready_images = any(image.status == "ready" and image.data_url for image in images)
    image_note = "本条没有可用图片。" if not has_ready_images else "已附上可用现场图片；图片只能作为证据，不能脱离标准自行下结论。"
    chat_note = (
        "本条未提供原始聊天内容；只能依据结构化字段、图片证据和检索标准，"
        "不得补写会话中未出现的事实，并将 needs_human_review 设为 true。"
        if not _text(source_row.get("聊天内容"))
        else ""
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是答疑中台的多品类回收质检知识标注员。请把一条第二部分会话记录改写成一条可供人工复核的知识候选。当前生效品类为：{product_category_prompt()}；无法确认时填写“{UNKNOWN_PRODUCT_NAME}”并进入人工复核。

必须遵守：
1. 只能依据输入会话、图片证据和下方【本次检索到的当前品类标准与已有知识】；不可捏造标准。knowledge_type 为“已有知识”时优先复用其口径，只有现有知识未覆盖时才新增。
2. standard_refs 只能填写本次检索结果中的 standard_ref，且必须是 JSON 字符串数组；不能匹配时填 [] 并将 needs_human_review 设为 true。
3. 图片不能单独构成发布结论；图片模糊、不可判断或与标准不充分对应时，在 image_evidence_summary 中说明，并设 needs_human_review=true。
4. reasoning_summary 是给审核人的简短依据摘要，不要输出思维过程，不超过 240 字。
5. title 必须是可复用的知识标题，不得复述“回收师遇到/咨询/希望获得”等会话叙述。content 只保留 3～5 个必要要点，总长度不超过 500 字；不得整段复制输入的核心问题、判定依据或参考话术。
6. 若会话没有“具体对象 + 明确现象/图片证据 + 可对应标准”中的关键证据，或包含“疑似、不确定、证据不足、未识别、未发现”等表达，不得给出确定性质检结论。此时 knowledge_form 必须为“流程方法”，内容应给出检查步骤、需补充证据和标准对照，并以“补充证据后再判定”收口；本次个案结论不可外推为通用规则。
7. applicable_scope 必须使用“品类-适用范围”格式，例如“手机-通用”“笔记本-Windows”“相机镜头-通用”。没有明确平台、品牌或机型差异时使用“品类-通用”。
8. recommended_reply 是人工答疑可直接发送的推荐回复，80～180 字，语气简洁，不写内部审核术语，不承诺证据不足时的确定结论。
9. 只输出一个 JSON 对象，不要 Markdown，不要额外解释。字段必须完整：
{{
  "title": "string",
  "subtitles": ["string"],
  "content": "string",
  "category_l1": "string",
  "category_l2": "string",
  "layer": "L2",
  "knowledge_form": "具体判定 或 流程方法",
  "standard_refs": ["standard_ref"],
  "applicable_scope": "string",
  "recommended_reply": "string",
  "confidence": 0.0,
  "reasoning_summary": "string",
  "needs_human_review": true,
  "image_evidence_summary": "string"
}}

【第二部分记录】
{json.dumps(_source_payload(source_row), ensure_ascii=False, indent=2)}

【本次检索到的当前品类标准与已有知识】
{json.dumps(standard_payload, ensure_ascii=False, indent=2)}

【图片情况】
{image_note}
图片下载元数据：{json.dumps(_image_metadata(images), ensure_ascii=False)}
{chat_note}
{retry_instruction}
"""


def _build_topic_prompt(
    topic: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    retry_reason: str = "",
    use_standard_references: bool = True,
) -> str:
    evidence_instruction = (
        "只能依据主题特征、证据摘要和本次检索标准"
        if use_standard_references
        else "只能依据主题特征、证据摘要、历史实际回复和案例图"
    )
    concrete_rule = (
        "只有同时满足“明确边界问题 + 可引用的本次标准 + 足够支持的主题证据”时，knowledge_form 才可为“具体判定”。"
        if use_standard_references
        else "只有主题内案例证据清楚、一致且足以支持可复用结论时，knowledge_form 才可为“具体判定”；否则必须为“流程方法”。"
    )
    standard_rule = (
        "standard_refs 只能填写本次检索结果的 standard_ref；knowledge_type 为“已有知识”时优先复用，未覆盖才新增；无可信标准时填 []，并将 needs_human_review=true。"
        if use_standard_references
        else "standard_refs 必须输出 []。不得根据记忆补写标准，也不得把质检标准问题改写成案例知识。"
    )
    standard_section = (
        "【本次检索到的当前品类生效标准与已有知识】\n"
        f"{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}"
        if use_standard_references
        else "【标准引用模式】\n本次关闭标准检索与标准引用，草稿必须完全来源于主题案例证据。"
    )
    image_flow_rule = (
        "确认部位、补充近景/全景/多角度证据、对照有效标准、证据不足时补充证据后再判定"
        if use_standard_references
        else "确认部位、补充近景/全景/多角度证据、结合完整会话与案例证据核验、信息不足时补充后再处理"
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是答疑中台的多品类质检知识标注员。输入是已经聚类的 1～N 个原子问题或案例特征，不是固定两两配对。当前生效品类为：{product_category_prompt()}。

请沉淀一条可复用、供人工审核的主题级知识草稿。{evidence_instruction}；不能把任何单条工单结论直接外推为通用事实。

必须遵守：
1. 主标题必须是自然、清楚、可直接使用的知识标题，不得堆砌关键词、使用斜杠串词或写成“异常核验/屏幕/显示异常”这类标签组合。副标题不是必填项；主标题已经表达清楚时输出空数组，最多输出 2 个自然问法。
2. {concrete_rule} 其余情况必须为“流程方法”。
3. 外观、显示、拆修、胶状物、功能异常等需要现场图片判断的问题，默认沉淀核验过程：{image_flow_rule}。
4. 机型/型号等信息查询问题，输出查询与核对流程，而不是某一案例的具体机型结论。
5. {standard_rule}
6. reasoning_summary 只写简短审核依据，不输出思维过程；不要编造聊天细节、图片细节或标准条款。
7. 文字已经能完整表达规则时，requires_images=false，不能把图片当装饰；只有必须通过外观、部位、颜色、裂纹、坏点或拆修痕迹等视觉差异才能解释时，requires_images=true，并给出 image_usage_instruction。
8. content 只保留 3～5 个必要要点，总长度不超过 500 字。applicable_scope 必须使用“品类-适用范围”格式，没有明确差异时使用“品类-通用”。recommended_reply 为 80～180 字、可直接发送的人工答疑回复。
9. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "title": "string",
  "subtitles": ["string"],
  "content": "string",
  "category_l1": "string",
  "category_l2": "string",
  "layer": "L2",
  "knowledge_form": "具体判定 或 流程方法",
  "standard_refs": ["standard_ref"],
  "applicable_scope": "string",
  "recommended_reply": "string",
  "confidence": 0.0,
  "reasoning_summary": "string",
  "needs_human_review": true,
  "image_evidence_summary": "string",
  "requires_images": false,
  "image_usage_instruction": "string"
}}

【主题输入】
{json.dumps(topic, ensure_ascii=False, indent=2)}

{standard_section}
{retry_instruction}
"""


def _build_topic_review_prompt(
    topic: dict[str, Any],
    draft: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    transcription_matches: list[tuple[StandardCatalogItem, float]] | None = None,
    retry_reason: str = "",
    use_standard_references: bool = True,
) -> str:
    responsibility = (
        "不得修改标题、正文、分类或标准引用"
        if use_standard_references
        else "不得修改标题、正文或分类，也不得要求补写标准引用"
    )
    evidence_rule = (
        "只能依据主题证据摘要、转写阶段使用的标准和本次独立检索到的标准审核"
        if use_standard_references
        else "只能依据主题证据摘要、来源案例、历史实际回复和案例图审核"
    )
    concrete_rule = (
        "草稿为“具体判定”时，必须有本次检索标准引用和足够主题证据；否则结论为“需修改”或“证据不足待补充”。"
        if use_standard_references
        else "草稿为“具体判定”时，必须有清楚、一致且可复用的案例证据；证据不足时结论为“需修改”或“证据不足待补充”。"
    )
    standard_rules = (
        "4. 无可信标准的草稿不得标“通过”；应标“证据不足待补充”，并标记重点复核。\n"
        "8. 转写引用标准与独立复核标准不一致时，应标记“标准项映射错”或“标准召回不足”。"
        if use_standard_references
        else "4. 本模式不使用标准引用；standard_consistency 固定填写“无可信标准”，但不能仅因此判定不通过。\n"
        "8. 草稿、回复或图片说明中出现编造的标准编号、标准条款或标准问题时，应标记需修改。"
    )
    standard_sections = (
        "【本次检索到的当前品类生效标准与已有知识】\n"
        f"{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}\n\n"
        "【转写阶段使用的标准】\n"
        f"{json.dumps(_standard_payload(transcription_matches or []), ensure_ascii=False, indent=2)}"
        if use_standard_references
        else "【标准引用模式】\n本次关闭标准检索与标准引用，不得因缺少标准而否决草稿。"
    )
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是答疑中台的多品类质检知识初审员。现在需要审核一条已经转写完成的主题级知识草稿。当前生效品类为：{product_category_prompt()}。

你的职责是“审核标注”，不是改写知识：{responsibility}；只判断草稿能否进入人工复标。

审核规则：
1. {evidence_rule}，不得补充未提供的事实。
2. {concrete_rule}
3. 外观、显示、拆修、胶状物、功能等依赖图片的问题，草稿沉淀为核验流程是合理的；不能因为没有个案最终判定而驳回流程型知识。
{standard_rules}
5. 必须审核知识内容是否准确覆盖规则、处理步骤和限制条件，不能只检查格式和字段完整性。
6. 必须审核主标题是否自然清楚、副标题是否只是关键词堆砌；主标题清楚时不应强行要求副标题。
7. 必须审核图片必要性：文字能说清时不应要求图片；依赖视觉差异时没有保留图片，应标记需修改或证据不足。
9. 必须标注知识点是否值得沉淀：只有问题清楚、处理方式可复用、不是仅对单个工单有效，且后续答疑存在复用价值时，才标记“值得沉淀”；纯个案结论、无有效处理信息或明显无复用价值时标记“不值得沉淀”；证据尚不足时标记“待确认”。
10. 标记“不值得沉淀”时 decision 必须为“驳回”；标记“待确认”时 decision 不能为“通过”。
11. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "decision": "通过 / 需修改 / 驳回 / 证据不足待补充",
  "knowledge_value": "值得沉淀 / 不值得沉淀 / 待确认",
  "error_type": "string",
  "reason": "string",
  "standard_consistency": "一致 / 不一致 / 无可信标准",
  "evidence_sufficiency": "充分 / 部分充分 / 不足",
  "content_consistency": "一致 / 部分一致 / 不一致",
  "image_necessity": "需要保留 / 不需要 / 图片不足",
  "title_quality": "清晰 / 需修改",
  "confidence": 0.0,
  "priority_review": true
}}

【主题信息】
{json.dumps(topic, ensure_ascii=False, indent=2)}

【待审核的转写草稿】
{json.dumps(draft, ensure_ascii=False, indent=2)}

{standard_sections}
{retry_instruction}
"""


def _build_cluster_pair_review_prompt(
    left: dict[str, Any],
    right: dict[str, Any],
    similarity: float,
    threshold: float,
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是人工答疑知识库的主题聚类审核员。请判断候选原子知识 A 是否能够加入主题簇 B。

主题簇 B 可以包含 1 到 N 个知识点。不得只挑选其中最相似的一条进行判断；只有候选 A 能够与簇内所有成员共用同一条标准答疑知识时，才允许加入。

唯一标准：
合并后是否可以直接共用同一条知识标题、适用范围、判定标准和处理结论。

只有同时满足以下条件才能判断为“同一主题”：
1. 适用品类、平台、品牌和机型范围一致，或者明确属于同一通用标准。
2. 核心对象或部位一致。
3. 判定目标一致。
4. 标准处理路径一致。
5. 阈值、例外条件不会产生不同答疑结论。

以下情况应判断为“不同主题”：
- 一级品类不同，且不是明确的通用标准。
- 苹果、安卓、鸿蒙或其他平台适用标准不同。
- 功能问题与外观问题不同。
- 判定阈值、例外条件或标准路径不同。
- 合并后需要写多个互不相关的处理结论。
- 只是出现“屏幕、摄像头、拆修、异常”等相同宽泛词语。

完整聊天、已经提取出的图片/视频事实摘要和结构化问题单元是主要证据。不得猜测未解析的图片或视频内容，不得补充输入中没有的业务标准。

不要根据相似度直接下结论。相似度只用于候选召回，不是合并证据。

只输出一个 JSON 对象，不要 Markdown：
{{
  "decision": "同一主题 / 不同主题 / 不确定",
  "topic_label": "两条记录可能共享的简短主题；不同主题时概括主要差异",
  "reason": "给人工审核人的简短判断依据，不超过 300 字",
  "key_difference": "决定是否拆分的关键差异；没有则为空字符串",
  "confidence": 0.0
}}

【语义聚类信息】
余弦相似度：{similarity:.4f}
当前阈值：{threshold:.4f}

【记录 A】
{json.dumps(left, ensure_ascii=False, indent=2)}

【记录 B】
{json.dumps(right, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _build_cluster_unit_prompt(
    source_row: dict[str, Any],
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    payload = {
        "work_order_id": _text(source_row.get("工单ID")),
        "product_type": _text(source_row.get("产品类型")),
        "device_model": _text(source_row.get("机型")),
        "legacy_category_l1": _text(source_row.get("一级分类")),
        "legacy_category_l2": _text(source_row.get("二级分类")),
        "conversation": _text(source_row.get("聊天内容"), 9000),
        "upstream_core_problem": _text(source_row.get("核心问题"), 1200),
        "upstream_judgment": _text(source_row.get("判定结论"), 1200),
        "upstream_media_fact_summary": _text(source_row.get("上游媒体分析摘要"), 2400),
        "has_image_links": bool(_text(source_row.get("图片链接"))),
        "has_video_links": bool(_text(source_row.get("视频链接"))),
    }
    return f"""你是人工答疑知识库新版聚类流程的原子知识提取器。请判断一条会话包含一个还是多个可以独立沉淀的知识主题，并输出用于聚类的原子知识点。

证据使用规则：
1. 完整聊天是主证据。
2. upstream_core_problem、upstream_judgment 和 upstream_media_fact_summary 是第二部分已经结合图片或视频生成的分析结果，可作为重要媒体语义证据。
3. product_type 和 device_model 用于判断适用品类、平台、品牌和机型范围。
4. legacy_category_l1、legacy_category_l2 只能作为弱参考，不得覆盖实际聊天内容。
4. 不重新猜测图片或视频内容；只能使用输入中已经提取出的媒体事实。

主题拆分规则：
1. 同一对象、同一异常的追问、澄清、补充图片或处理过程属于一个主题。
2. 同一对象、同一现象在两个质检选项中进行选择，通常属于一个主题。
3. 需要不同知识正文、不同判断对象或不同处理标准的问题必须拆开。
3. 即使最终客服只回答了其中一个问题，也不能丢弃会话中清晰存在的另一个独立问题。
4. 能明确识别多个独立问题时标记 multi_topic；不是 uncertain。
5. 只有聊天或媒体证据不足、无法判断真实问题时才标记 uncertain。
6. 最多提取 3 个主题。寒暄、催促、致谢和系统提示不作为主题。

适用范围规则：
1. 默认不同一级品类不能共用一条知识。
2. 苹果手机、安卓手机、鸿蒙设备或其他平台标准不一致时，必须保留平台范围。
3. device_model 只是当前案例设备，不代表知识天然为机型专用。没有明确的品牌/机型特殊标准时，默认标记为“品类专用”或“平台专用”。
4. 只有输入明确说明某品牌或某机型存在特殊阈值、例外或操作路径时，才能标记为“品牌专用”或“机型专用”。
4. 只有输入证据明确说明各品类处理标准完全一致时，才能标记为通用。
5. 无法确认品类、平台或标准路径时填写“待确认”，并将 requires_review 设为 true。
6. 已知品牌应填写正确平台：Apple/iPhone/iPad 对应 iOS；小米、红米、OPPO、vivo、三星、一加、realme、努比亚等手机对应 Android；华为设备有明确鸿蒙证据时填 HarmonyOS，否则填待确认。

知识分类规则：
1. category_l1 只能为：基本情况、成色与回收标准、外观问题、显示问题、功能问题、拆修问题、信息查询、流程操作、其他待确认。
2. 屏幕颜色异常、色斑、闪屏、亮线、坏点、漏液等属于“显示问题”，除非会话明确询问非原装、更换或维修。
3. 划痕、磕碰、掉漆、凹陷、胶条、脱胶等物理外观属于“外观问题”。
4. 摄像头、扬声器、充电、按键等功能是否正常属于“功能问题”。
5. 非原装部件、更换、维修痕迹、拆机痕迹等属于“拆修问题”。
6. 不得因为上游分类或标准名称中出现“拆修”就覆盖实际聊天中的显示、功能或外观问题。

标准和阈值规则：
1. standard_path 只能概括输入已经明确提供的统一处理路径；证据不足时填“待确认”。
2. threshold_or_exception 只能提取输入中明确出现的数字阈值、条件或例外。
3. 不得根据常识补写“0次”“大于N个”等输入未明确提供的阈值。
4. 输入没有明确阈值时填“无明确阈值”，无法判断时填“待确认”。
5. standard_path、threshold_or_exception、品类或范围任一字段为“待确认”时，requires_review 必须为 true。

每个原子知识点的 normalized_issue 应尽量遵循：
“适用范围｜对象/部位｜异常现象或查询目标｜判定目标/处理动作”。

只返回一个 JSON 对象，不要 Markdown：
{{
  "conversation_type": "single_topic / multi_topic / uncertain",
  "reason": "简短说明为什么单主题、拆分或不确定，不超过240字",
  "topics": [
    {{
      "normalized_issue": "可独立聚类的核心问题，不超过80字",
      "product_category": "一级产品品类，只能为{product_category_prompt()}或{UNKNOWN_PRODUCT_NAME}",
      "scope_type": "通用 / 品类专用 / 平台专用 / 品牌专用 / 机型专用 / 待确认",
      "platform": "iOS / Android / HarmonyOS / Windows / macOS / 通用 / 待确认",
      "brand": "品牌名称；不限制品牌填通用；无法确认填待确认",
      "model_scope": "具体机型、机型系列、通用或待确认",
      "category_l1": "知识一级分类",
      "category_l2": "知识二级分类",
      "intent": "标准判定 / 检测核验 / 信息查询 / 流程操作 / 其他待确认",
      "subject": "实际对象或部位",
      "phenomenon": "异常现象或查询目标",
      "judgment_target": "希望得到的统一判定目标",
      "resolution_mode": "应沉淀的处理方式",
      "standard_path": "适用的标准处理路径；无法确认填待确认",
      "threshold_or_exception": "输入中明确出现的阈值或例外；没有填无明确阈值；无法确认填待确认",
      "evidence_summary": "支持该问题单元的聊天及上游媒体事实摘要，不超过300字",
      "confidence": 0.0,
      "requires_review": true
    }}
  ]
}}

【输入】
{json.dumps(payload, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _atomic_unit_payload(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "atomic_id": _text(unit.get("unit_id") or unit.get("atomic_id"), 120),
        "normalized_issue": _text(unit.get("normalized_issue"), 160),
        "product_category": _text(unit.get("product_category"), 80),
        "scope_type": _text(unit.get("scope_type"), 32),
        "platform": _text(unit.get("platform"), 80),
        "brand": _text(unit.get("brand"), 80),
        "model_scope": _text(unit.get("model_scope"), 120),
        "category_l1": _text(unit.get("category_l1"), 80),
        "category_l2": _text(unit.get("category_l2"), 80),
        "intent": _text(unit.get("intent"), 32),
        "subject": _text(unit.get("subject"), 120),
        "phenomenon": _text(unit.get("phenomenon"), 160),
        "judgment_target": _text(unit.get("judgment_target"), 160),
        "resolution_mode": _text(unit.get("resolution_mode"), 160),
        "standard_path": _text(unit.get("standard_path"), 240),
        "threshold_or_exception": _text(unit.get("threshold_or_exception"), 240),
        "requires_review": _as_bool(unit.get("requires_review")),
    }


def _build_atomic_topic_cluster_prompt(
    atomic_units: list[dict[str, Any]],
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    payload = [_atomic_unit_payload(unit) for unit in atomic_units]
    return f"""你是人工答疑知识库的主题聚类专家。

任务：
将输入的原子知识点直接归并为若干主题簇。一个主题簇可以包含 1 到 N 个知识点。
这不是两两配对任务，不得限制每个聚类的成员数量，不得为了减少簇数而强行合并。

唯一聚类标准：
簇内所有知识点是否能够共用同一条标准答疑知识。

只有同时满足以下条件才能合并：
1. 适用范围一致；
2. 核心对象一致；
3. 判定目标一致；
4. 标准处理路径一致；
5. 阈值、例外条件不会导致不同答疑结论。

以下情况必须拆分：
1. 苹果、安卓、鸿蒙或通用标准不同；
2. 功能问题与外观问题、显示问题、拆修问题不同；
3. 判定阈值不同；
4. 标准处理路径不同；
5. 合并后需要在一条知识中写多个互不相关的处理结论。

执行规则：
1. 允许单知识点独立成簇。
2. 不得按关键词或字面相似直接合并，优先依据对象、判定目标、标准处理路径和阈值例外。
3. standard_path、resolution_mode 或 threshold_or_exception 的文字不同不代表一定不同；必须判断语义和最终答疑结论是否一致。
4. 不得发明输入中没有的阈值、例外、适用范围或业务规则。
5. 多成员簇的五个一致性字段必须全部为 true；任一项不一致时不得合并，应拆为单成员簇。
6. 如果一个原子知识点本身仍包含多个独立主题，放入 split_requests，不得直接聚类。
7. 每个 atomic_id 必须且只能出现在 clusters、split_requests、review_requests 三者之一。
8. 不得遗漏、重复或改写 atomic_id。
9. theme_name 应概括可直接沉淀的一条标准答疑知识，不要使用宽泛对象名。
10. “无法与其他知识点合并”不等于“不确定”。只要该原子知识点自身主题清楚，就必须建立单成员簇。
11. review_requests 仅用于输入字段自身矛盾、缺失或无法判断适用范围/路径/阈值的情况；不得用于存放清晰但独立的知识点。
12. 只输出一个 JSON 对象，不要 Markdown。

输出结构：
{{
  "clusters": [
    {{
      "cluster_id": "C001",
      "theme_name": "简短、唯一、可沉淀的知识主题",
      "member_atomic_ids": ["原子知识ID"],
      "scope_consistent": true,
      "object_consistent": true,
      "judgment_target_consistent": true,
      "standard_path_consistent": true,
      "threshold_exception_consistent": true,
      "shared_knowledge_definition": "该簇可共用的一条标准答疑知识定义",
      "merge_basis": "说明为什么所有成员能够共用同一条知识，不超过300字"
    }}
  ],
  "split_requests": [
    {{
      "atomic_id": "原子知识ID",
      "reason": "为什么仍需拆成多个原子知识点",
      "suggested_splits": ["建议拆分方向"]
    }}
  ],
  "review_requests": [
    {{
      "atomic_id": "原子知识ID",
      "review_type": "适用范围 / 对象 / 判定目标 / 标准路径 / 阈值例外 / 其他",
      "reason": "为什么无法自动聚类"
    }}
  ]
}}

【待聚类原子知识点】
{json.dumps(payload, ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end >= start else text


def _content_from_response(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MimoError("MiMo 响应中没有 choices[0].message.content") from exc
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "是"}
    return bool(value)


def _fallback_recommended_reply(title: str, content: str) -> str:
    lines = [
        re.sub(r"^\s*(?:[-•]|\d+[.、])\s*", "", line).strip()
        for line in str(content or "").splitlines()
    ]
    points = [line for line in lines if line and not line.endswith("：")][:3]
    body = "；".join(points)
    if not body:
        body = "请先确认具体对象和现象，补充必要的图片、截图或检测结果，再对照当前有效标准判定。"
    return _text(f"您好，关于“{title}”，建议{body}。若现有证据不能对应标准，请补充证据后再判定。", 240)


def _validate_candidate(value: Any, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("MiMo 输出不是 JSON 对象")
    required_text = [
        "title", "content", "category_l1", "category_l2", "layer",
        "knowledge_form", "applicable_scope", "reasoning_summary", "image_evidence_summary",
    ]
    for key in required_text:
        if not _text(value.get(key)):
            raise MimoError(f"MiMo 输出缺少或为空：{key}")
    subtitles = value.get("subtitles")
    refs = value.get("standard_refs")
    if not isinstance(subtitles, list) or not all(isinstance(item, str) for item in subtitles):
        raise MimoError("MiMo 输出的 subtitles 必须为字符串数组")
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        raise MimoError("MiMo 输出的 standard_refs 必须为字符串数组")
    invalid_refs = set(refs) - allowed_refs
    if invalid_refs:
        raise MimoError(f"MiMo 引用了本次未检索到的标准：{', '.join(sorted(invalid_refs))}")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("MiMo 输出的 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("MiMo 输出的 confidence 必须在 0~1")
    knowledge_form = _text(value["knowledge_form"], 32)
    if knowledge_form not in {"具体判定", "流程方法"}:
        raise MimoError("MiMo 输出的 knowledge_form 必须为 具体判定 或 流程方法")
    title = _text(value["title"], 120)
    if "/" in title or "／" in title or title.count("、") >= 2:
        raise MimoError("主标题不能使用斜杠或关键词列表堆砌")
    content = _text(value["content"], 1200)
    return {
        "title": title,
        "subtitles": [
            _text(item, 120)
            for item in subtitles[:2]
            if _text(item, 120) and _text(item, 120) != title
        ],
        "content": content,
        "category_l1": _text(value["category_l1"], 80),
        "category_l2": _text(value["category_l2"], 80),
        "layer": _text(value["layer"], 32) or "L2",
        "knowledge_form": knowledge_form,
        "standard_refs": list(dict.fromkeys(refs)),
        "applicable_scope": _text(value["applicable_scope"], 800),
        "recommended_reply": _text(value.get("recommended_reply"), 240)
        or _fallback_recommended_reply(title, content),
        "confidence": round(confidence, 3),
        "reasoning_summary": _text(value["reasoning_summary"], 240),
        "needs_human_review": _as_bool(value.get("needs_human_review")),
        "image_evidence_summary": _text(value["image_evidence_summary"], 800),
        "requires_images": _as_bool(value.get("requires_images")),
        "image_usage_instruction": _text(value.get("image_usage_instruction"), 400),
    }


def _validate_topic_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("MiMo 初标输出不是 JSON 对象")
    decision = _text(value.get("decision"), 32)
    if decision not in {"通过", "需修改", "驳回", "证据不足待补充"}:
        raise MimoError("MiMo 初标 decision 不合法")
    knowledge_value = _text(value.get("knowledge_value"), 32)
    if knowledge_value not in {"值得沉淀", "不值得沉淀", "待确认"}:
        raise MimoError("MiMo 初标 knowledge_value 不合法")
    if knowledge_value == "不值得沉淀" and decision != "驳回":
        raise MimoError("MiMo 初标标记不值得沉淀时 decision 必须为驳回")
    if knowledge_value == "待确认" and decision == "通过":
        raise MimoError("MiMo 初标标记待确认时 decision 不能为通过")
    standard_consistency = _text(value.get("standard_consistency"), 32)
    if standard_consistency not in {"一致", "不一致", "无可信标准"}:
        raise MimoError("MiMo 初标 standard_consistency 不合法")
    evidence_sufficiency = _text(value.get("evidence_sufficiency"), 32)
    if evidence_sufficiency not in {"充分", "部分充分", "不足"}:
        raise MimoError("MiMo 初标 evidence_sufficiency 不合法")
    content_consistency = _text(value.get("content_consistency"), 32)
    if content_consistency not in {"一致", "部分一致", "不一致"}:
        raise MimoError("MiMo 初标 content_consistency 不合法")
    image_necessity = _text(value.get("image_necessity"), 32)
    if image_necessity not in {"需要保留", "不需要", "图片不足"}:
        raise MimoError("MiMo 初标 image_necessity 不合法")
    title_quality = _text(value.get("title_quality"), 32)
    if title_quality not in {"清晰", "需修改"}:
        raise MimoError("MiMo 初标 title_quality 不合法")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("MiMo 初标 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("MiMo 初标 confidence 必须在 0~1")
    return {
        "decision": decision,
        "knowledge_value": knowledge_value,
        "error_type": _text(value.get("error_type"), 120),
        "reason": _text(value.get("reason"), 500),
        "standard_consistency": standard_consistency,
        "evidence_sufficiency": evidence_sufficiency,
        "content_consistency": content_consistency,
        "image_necessity": image_necessity,
        "title_quality": title_quality,
        "confidence": round(confidence, 3),
        "priority_review": _as_bool(value.get("priority_review")),
    }


def _validate_cluster_pair_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("聚类标注输出不是 JSON 对象")
    decision = _text(value.get("decision"), 32)
    if decision not in {"同一主题", "不同主题", "不确定"}:
        raise MimoError("聚类标注 decision 不合法")
    topic_label = _text(value.get("topic_label"), 160)
    reason = _text(value.get("reason"), 300)
    if not topic_label or not reason:
        raise MimoError("聚类标注缺少 topic_label 或 reason")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("聚类标注 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("聚类标注 confidence 必须在 0~1")
    return {
        "decision": decision,
        "topic_label": topic_label,
        "reason": reason,
        "key_difference": _text(value.get("key_difference"), 240),
        "confidence": round(confidence, 3),
    }


def _validate_cluster_units(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("聚类问题单元输出不是 JSON 对象")
    conversation_type = _text(value.get("conversation_type"), 32)
    if conversation_type not in {"single_topic", "multi_topic", "uncertain"}:
        raise MimoError("聚类问题单元 conversation_type 不合法")
    reason = _text(value.get("reason"), 240)
    if not reason:
        raise MimoError("聚类问题单元缺少 reason")
    topics = value.get("topics")
    if not isinstance(topics, list):
        raise MimoError("聚类问题单元 topics 必须为数组")
    if conversation_type == "single_topic" and len(topics) != 1:
        raise MimoError("single_topic 必须恰好包含 1 个问题单元")
    if conversation_type == "multi_topic" and not 2 <= len(topics) <= 3:
        raise MimoError("multi_topic 必须包含 2 到 3 个问题单元")
    if conversation_type == "uncertain" and len(topics) > 1:
        raise MimoError("uncertain 最多保留 1 个暂定问题单元")

    allowed_intents = {"标准判定", "检测核验", "信息查询", "流程操作", "其他待确认"}
    allowed_scope_types = {
        "通用",
        "品类专用",
        "平台专用",
        "品牌专用",
        "机型专用",
        "待确认",
    }
    allowed_product_categories = {*configured_product_names(), UNKNOWN_PRODUCT_NAME}
    allowed_category_l1 = {
        "基本情况",
        "成色与回收标准",
        "外观问题",
        "显示问题",
        "功能问题",
        "拆修问题",
        "信息查询",
        "流程操作",
        "其他待确认",
    }
    normalized_topics: list[dict[str, Any]] = []
    for topic in topics:
        if not isinstance(topic, dict):
            raise MimoError("聚类问题单元 topic 必须为 JSON 对象")
        normalized_issue = _text(topic.get("normalized_issue"), 80)
        raw_product_category = _text(topic.get("product_category"), 80)
        product_category = canonical_product_name(
            raw_product_category,
            unknown=UNKNOWN_PRODUCT_NAME if raw_product_category == UNKNOWN_PRODUCT_NAME else "",
        )
        scope_type = _text(topic.get("scope_type"), 32)
        platform = _text(topic.get("platform"), 80)
        brand = _text(topic.get("brand"), 80)
        model_scope = _text(topic.get("model_scope"), 120)
        category_l1 = _text(topic.get("category_l1"), 80)
        category_l2 = _text(topic.get("category_l2"), 80)
        intent = _text(topic.get("intent"), 32)
        subject = _text(topic.get("subject"), 120)
        phenomenon = _text(topic.get("phenomenon"), 160)
        judgment_target = _text(topic.get("judgment_target"), 160)
        resolution_mode = _text(topic.get("resolution_mode"), 160)
        standard_path = _text(topic.get("standard_path"), 200)
        threshold_or_exception = _text(topic.get("threshold_or_exception"), 200)
        evidence_summary = _text(topic.get("evidence_summary"), 300)
        if not all(
            (
                normalized_issue,
                product_category,
                scope_type,
                platform,
                brand,
                model_scope,
                category_l1,
                category_l2,
                subject,
                phenomenon,
                judgment_target,
                resolution_mode,
                standard_path,
                threshold_or_exception,
                evidence_summary,
            )
        ):
            raise MimoError("聚类问题单元缺少必要文本字段")
        if intent not in allowed_intents:
            raise MimoError("聚类问题单元 intent 不合法")
        if product_category not in allowed_product_categories:
            raise MimoError("聚类问题单元 product_category 不合法")
        if scope_type not in allowed_scope_types:
            raise MimoError("聚类问题单元 scope_type 不合法")
        if category_l1 not in allowed_category_l1:
            raise MimoError("聚类问题单元 category_l1 不合法")
        semantic_text = " ".join(
            (
                normalized_issue,
                phenomenon,
                judgment_target,
                resolution_mode,
                evidence_summary,
            )
        )
        display_markers = (
            "色斑",
            "颜色异常",
            "偏绿",
            "偏蓝",
            "闪屏",
            "闪烁",
            "亮线",
            "绿线",
            "坏点",
            "漏液",
            "显示异常",
        )
        repair_markers = ("拆修", "维修", "更换", "非原装", "第三方", "弹窗")
        if (
            any(marker in semantic_text for marker in display_markers)
            and not any(marker in semantic_text for marker in repair_markers)
            and category_l1 != "显示问题"
        ):
            raise MimoError("屏幕显示现象必须归入显示问题，不得误归外观或拆修问题")
        if (
            any(marker in semantic_text for marker in ("全新机", "二手", "成色定级"))
            and category_l1 == "基本情况"
        ):
            category_l1 = "成色与回收标准"
        scope_level = {
            "通用": 0,
            "品类专用": 1,
            "平台专用": 2,
            "品牌专用": 3,
            "机型专用": 4,
            "待确认": -1,
        }[scope_type]
        if scope_level < 2 and scope_level >= 0:
            platform = "通用"
        if scope_level < 3 and scope_level >= 0:
            brand = "通用"
        if scope_level < 4 and scope_level >= 0:
            model_scope = "通用"
        try:
            confidence = float(topic.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise MimoError("聚类问题单元 confidence 必须是 0~1 数字") from exc
        if not 0 <= confidence <= 1:
            raise MimoError("聚类问题单元 confidence 必须在 0~1")
        requires_review = _as_bool(topic.get("requires_review")) or any(
            "待确认" in item
            for item in (
                product_category,
                scope_type,
                platform,
                brand,
                model_scope,
                category_l1,
                category_l2,
                judgment_target,
                standard_path,
                threshold_or_exception,
            )
        )
        normalized_topics.append(
            {
                "normalized_issue": normalized_issue,
                "product_category": product_category,
                "scope_type": scope_type,
                "platform": platform,
                "brand": brand,
                "model_scope": model_scope,
                "category_l1": category_l1,
                "category_l2": category_l2,
                "intent": intent,
                "subject": subject,
                "phenomenon": phenomenon,
                "judgment_target": judgment_target,
                "resolution_mode": resolution_mode,
                "standard_path": standard_path,
                "threshold_or_exception": threshold_or_exception,
                "evidence_summary": evidence_summary,
                "confidence": round(confidence, 3),
                "requires_review": requires_review,
            }
        )
    return {
        "conversation_type": conversation_type,
        "reason": reason,
        "topics": normalized_topics,
    }


def _validate_atomic_topic_clusters(
    value: Any,
    allowed_atomic_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("原子知识主题聚类输出不是 JSON 对象")
    if not allowed_atomic_ids:
        raise MimoError("原子知识主题聚类输入不能为空")

    clusters = value.get("clusters")
    split_requests = value.get("split_requests")
    review_requests = value.get("review_requests")
    if not isinstance(clusters, list):
        raise MimoError("原子知识主题聚类 clusters 必须为数组")
    if not isinstance(split_requests, list):
        raise MimoError("原子知识主题聚类 split_requests 必须为数组")
    if not isinstance(review_requests, list):
        raise MimoError("原子知识主题聚类 review_requests 必须为数组")

    seen_atomic_ids: set[str] = set()
    seen_cluster_ids: set[str] = set()
    consistency_fields = (
        "scope_consistent",
        "object_consistent",
        "judgment_target_consistent",
        "standard_path_consistent",
        "threshold_exception_consistent",
    )
    normalized_clusters: list[dict[str, Any]] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            raise MimoError("原子知识主题簇必须为 JSON 对象")
        cluster_id = _text(cluster.get("cluster_id"), 80)
        theme_name = _text(cluster.get("theme_name"), 160)
        shared_definition = _text(cluster.get("shared_knowledge_definition"), 500)
        merge_basis = _text(cluster.get("merge_basis"), 300)
        if not all((cluster_id, theme_name, shared_definition, merge_basis)):
            raise MimoError("原子知识主题簇缺少名称、知识定义或合并依据")
        if cluster_id in seen_cluster_ids:
            raise MimoError(f"原子知识主题簇 ID 重复：{cluster_id}")
        seen_cluster_ids.add(cluster_id)

        member_ids = cluster.get("member_atomic_ids")
        if (
            not isinstance(member_ids, list)
            or not member_ids
            or not all(isinstance(item, str) and _text(item, 120) for item in member_ids)
        ):
            raise MimoError("member_atomic_ids 必须为非空字符串数组")
        normalized_member_ids = [_text(item, 120) for item in member_ids]
        if len(normalized_member_ids) != len(set(normalized_member_ids)):
            raise MimoError(f"主题簇 {cluster_id} 内存在重复 atomic_id")

        flags: dict[str, bool] = {}
        for field in consistency_fields:
            field_value = cluster.get(field)
            if not isinstance(field_value, bool):
                raise MimoError(f"主题簇 {cluster_id} 的 {field} 必须为布尔值")
            flags[field] = field_value
        if len(normalized_member_ids) > 1 and not all(flags.values()):
            raise MimoError(f"多成员主题簇 {cluster_id} 未通过全部五项一致性检查")

        for atomic_id in normalized_member_ids:
            if atomic_id not in allowed_atomic_ids:
                raise MimoError(f"输出包含输入中不存在的 atomic_id：{atomic_id}")
            if atomic_id in seen_atomic_ids:
                raise MimoError(f"atomic_id 重复分配：{atomic_id}")
            seen_atomic_ids.add(atomic_id)
        normalized_clusters.append(
            {
                "cluster_id": cluster_id,
                "theme_name": theme_name,
                "member_atomic_ids": normalized_member_ids,
                **flags,
                "shared_knowledge_definition": shared_definition,
                "merge_basis": merge_basis,
            }
        )

    normalized_splits: list[dict[str, Any]] = []
    for request in split_requests:
        if not isinstance(request, dict):
            raise MimoError("split_requests 成员必须为 JSON 对象")
        atomic_id = _text(request.get("atomic_id"), 120)
        reason = _text(request.get("reason"), 300)
        suggested_splits = request.get("suggested_splits")
        if (
            not atomic_id
            or not reason
            or not isinstance(suggested_splits, list)
            or not all(isinstance(item, str) for item in suggested_splits)
        ):
            raise MimoError("split_requests 缺少 atomic_id、reason 或 suggested_splits")
        if atomic_id not in allowed_atomic_ids:
            raise MimoError(f"split_requests 包含输入中不存在的 atomic_id：{atomic_id}")
        if atomic_id in seen_atomic_ids:
            raise MimoError(f"atomic_id 重复分配：{atomic_id}")
        seen_atomic_ids.add(atomic_id)
        normalized_splits.append(
            {
                "atomic_id": atomic_id,
                "reason": reason,
                "suggested_splits": [
                    _text(item, 160)
                    for item in suggested_splits[:5]
                    if _text(item, 160)
                ],
            }
        )

    normalized_reviews: list[dict[str, Any]] = []
    for request in review_requests:
        if not isinstance(request, dict):
            raise MimoError("review_requests 成员必须为 JSON 对象")
        atomic_id = _text(request.get("atomic_id"), 120)
        review_type = _text(request.get("review_type"), 80)
        reason = _text(request.get("reason"), 300)
        if not all((atomic_id, review_type, reason)):
            raise MimoError("review_requests 缺少 atomic_id、review_type 或 reason")
        if atomic_id not in allowed_atomic_ids:
            raise MimoError(f"review_requests 包含输入中不存在的 atomic_id：{atomic_id}")
        if atomic_id in seen_atomic_ids:
            raise MimoError(f"atomic_id 重复分配：{atomic_id}")
        seen_atomic_ids.add(atomic_id)
        normalized_reviews.append(
            {
                "atomic_id": atomic_id,
                "review_type": review_type,
                "reason": reason,
            }
        )

    missing_ids = allowed_atomic_ids - seen_atomic_ids
    if missing_ids:
        raise MimoError(f"原子知识主题聚类遗漏 atomic_id：{', '.join(sorted(missing_ids))}")
    return {
        "clusters": normalized_clusters,
        "split_requests": normalized_splits,
        "review_requests": normalized_reviews,
    }


def _validate_topic_signal(value: Any, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MimoError("会话语义标注输出不是 JSON 对象")
    intent = _text(value.get("intent"), 32)
    allowed_intents = {"标准判定", "检测核验", "信息查询", "流程操作", "其他待确认"}
    if intent not in allowed_intents:
        raise MimoError("会话语义标注 intent 不合法")
    required_text = [
        "subject",
        "phenomenon",
        "resolution_mode",
        "category_l1",
        "category_l2",
        "image_evidence_summary",
        "reasoning_summary",
    ]
    for key in required_text:
        if not _text(value.get(key)):
            raise MimoError(f"会话语义标注缺少或为空：{key}")
    tags = value.get("topic_tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise MimoError("会话语义标注 topic_tags 必须为字符串数组")
    normalized_tags = list(dict.fromkeys(_text(tag, 80) for tag in tags if _text(tag, 80)))
    if not 3 <= len(normalized_tags) <= 6 or any(":" not in tag for tag in normalized_tags):
        raise MimoError("会话语义标注 topic_tags 必须为 3 到 6 个“维度:值”标签")
    refs = value.get("standard_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise MimoError("会话语义标注 standard_refs 必须为字符串数组")
    normalized_refs = list(dict.fromkeys(_text(ref, 160) for ref in refs if _text(ref, 160)))
    invalid_refs = set(normalized_refs) - allowed_refs
    if invalid_refs:
        raise MimoError(f"会话语义标注引用了本次未检索到的标准：{', '.join(sorted(invalid_refs))}")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MimoError("会话语义标注 confidence 必须是 0~1 数字") from exc
    if not 0 <= confidence <= 1:
        raise MimoError("会话语义标注 confidence 必须在 0~1")
    return {
        "intent": intent,
        "subject": _text(value.get("subject"), 120),
        "phenomenon": _text(value.get("phenomenon"), 160),
        "resolution_mode": _text(value.get("resolution_mode"), 160),
        "category_l1": _text(value.get("category_l1"), 80),
        "category_l2": _text(value.get("category_l2"), 80),
        "topic_tags": normalized_tags,
        "standard_refs": normalized_refs,
        "requires_images": _as_bool(value.get("requires_images")),
        "image_evidence_summary": _text(value.get("image_evidence_summary"), 800),
        "reasoning_summary": _text(value.get("reasoning_summary"), 240),
        "confidence": round(confidence, 3),
        "needs_human_review": _as_bool(value.get("needs_human_review")),
    }


class MimoClient:
    def __init__(self, config: MimoConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> "MimoClient | None":
        config = MimoConfig.from_env()
        return cls(config) if config else None

    def label(
        self,
        source_row: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        images: list[ImageEvidence],
    ) -> MimoLabelResult:
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_prompt(source_row, matches, images, validation_error)
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images[:4]:
                if image.status == "ready" and image.data_url:
                    content.append({"type": "image_url", "image_url": {"url": image.data_url}})
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类回收质检知识标注助手。"},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": PROMPT_VERSION,
                "source": _source_payload(source_row),
                "retrieved_standards": _standard_payload(matches),
                "images": _image_metadata(images),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                candidate = _validate_candidate(json.loads(_strip_json_fence(raw_content)), allowed_refs)
                return MimoLabelResult(candidate=candidate, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 调用失败：{exc}") from exc
        raise MimoError("MiMo 调用未产生有效结果")

    def analyze_topic_signal(
        self,
        source_row: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        images: list[ImageEvidence],
        use_standard_references: bool = True,
    ) -> MimoLabelResult:
        """Normalize one conversation into auditable clustering signals."""
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_signal_prompt(
                source_row,
                matches,
                images,
                validation_error,
                use_standard_references=use_standard_references,
            )
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images[:4]:
                if image.status == "ready" and image.data_url:
                    content.append({"type": "image_url", "image_url": {"url": image.data_url}})
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类质检会话语义标注助手。"},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": TOPIC_SIGNAL_PROMPT_VERSION,
                "source": _topic_signal_source_payload(source_row),
                "retrieved_standards": _standard_payload(matches),
                "images": _image_metadata(images),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                signal = _validate_topic_signal(json.loads(_strip_json_fence(raw_content)), allowed_refs)
                return MimoLabelResult(candidate=signal, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 会话语义标注 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 会话语义标注调用失败：{exc}") from exc
        raise MimoError(f"MiMo 会话语义标注未产生有效结果：{last_response}")

    def analyze_cluster_units(
        self,
        source_row: dict[str, Any],
    ) -> MimoLabelResult:
        """Split one conversation into one to three auditable clustering units."""
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_cluster_unit_prompt(source_row, retry_reason=validation_error)
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的知识主题聚类问题单元提取器。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
                "source": {
                    "工单ID": _text(source_row.get("工单ID")),
                    "产品类型": _text(source_row.get("产品类型")),
                    "聊天内容": _text(source_row.get("聊天内容"), 9000),
                    "核心问题": _text(source_row.get("核心问题"), 1200),
                    "判定结论": _text(source_row.get("判定结论"), 1200),
                    "上游媒体分析摘要": _text(source_row.get("上游媒体分析摘要"), 2400),
                    "有图片链接": bool(_text(source_row.get("图片链接"))),
                    "有视频链接": bool(_text(source_row.get("视频链接"))),
                },
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                cluster_units = _validate_cluster_units(
                    json.loads(_strip_json_fence(raw_content))
                )
                return MimoLabelResult(
                    candidate=cluster_units,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        f"MiMo 聚类问题单元 JSON 校验失败（已重试一次）：{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo 聚类问题单元调用失败：{exc}") from exc
        raise MimoError(f"MiMo 聚类问题单元未产生有效结果：{last_response}")

    def cluster_atomic_units(
        self,
        atomic_units: list[dict[str, Any]],
    ) -> MimoLabelResult:
        """Directly partition one compatible bucket into one-to-N knowledge topics."""
        allowed_atomic_ids = {
            _text(unit.get("unit_id") or unit.get("atomic_id"), 120)
            for unit in atomic_units
            if _text(unit.get("unit_id") or unit.get("atomic_id"), 120)
        }
        if len(allowed_atomic_ids) != len(atomic_units):
            raise MimoError("原子知识点 ID 为空或重复")

        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_atomic_topic_cluster_prompt(
                atomic_units,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严格执行硬业务规则的知识主题聚类专家。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
                "atomic_units": [_atomic_unit_payload(unit) for unit in atomic_units],
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                cluster_result = _validate_atomic_topic_clusters(
                    json.loads(_strip_json_fence(raw_content)),
                    allowed_atomic_ids,
                )
                return MimoLabelResult(
                    candidate=cluster_result,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(
                        f"MiMo 原子知识主题聚类 JSON 校验失败（已重试一次）：{validation_error}"
                    ) from exc
            except Exception as exc:
                raise MimoError(f"MiMo 原子知识主题聚类调用失败：{exc}") from exc
        raise MimoError(f"MiMo 原子知识主题聚类未产生有效结果：{last_response}")

    def label_topic(
        self,
        topic: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        use_standard_references: bool = True,
    ) -> MimoLabelResult:
        """Generate a reusable draft after clustering, never from a single case."""
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_prompt(
                topic,
                matches,
                validation_error,
                use_standard_references=use_standard_references,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类主题知识标注助手。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": PROMPT_VERSION,
                "topic": topic,
                "retrieved_standards": _standard_payload(matches),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                candidate = _validate_candidate(json.loads(_strip_json_fence(raw_content)), allowed_refs)
                return MimoLabelResult(candidate=candidate, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 主题 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 主题调用失败：{exc}") from exc
        raise MimoError(f"MiMo 主题调用未产生有效结果：{last_response}")

    def review_topic(
        self,
        topic: dict[str, Any],
        draft: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
        transcription_matches: list[tuple[StandardCatalogItem, float]] | None = None,
        use_standard_references: bool = True,
    ) -> MimoLabelResult:
        """Audit a transcribed topic draft without changing its 13-column content."""
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_review_prompt(
                topic,
                draft,
                matches,
                transcription_matches=transcription_matches,
                retry_reason=validation_error,
                use_standard_references=use_standard_references,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的多品类主题知识初审员。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": TOPIC_REVIEW_PROMPT_VERSION,
                "topic": topic,
                "draft": draft,
                "retrieved_standards": _standard_payload(matches),
                "transcription_retrieved_standards": _standard_payload(transcription_matches or []),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                review = _validate_topic_review(json.loads(_strip_json_fence(raw_content)))
                return MimoLabelResult(candidate=review, request_audit=request_audit, response_audit=raw_response)
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 主题初标 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 主题初标调用失败：{exc}") from exc
        raise MimoError(f"MiMo 主题初标未产生有效结果：{last_response}")

    def review_cluster_pair(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        similarity: float,
        threshold: float,
    ) -> MimoLabelResult:
        """Judge whether two conversation records belong to the same knowledge topic."""
        return self._review_cluster_candidate(left, right, similarity, threshold)

    def review_cluster_membership(
        self,
        candidate: dict[str, Any],
        cluster_members: list[dict[str, Any]],
        similarity: float,
        threshold: float,
    ) -> MimoLabelResult:
        """Judge whether one atomic knowledge point can join every member of a topic cluster."""
        cluster_payload = {
            "cluster_member_count": len(cluster_members),
            "cluster_members": cluster_members,
        }
        return self._review_cluster_candidate(
            candidate,
            cluster_payload,
            similarity,
            threshold,
        )

    def _review_cluster_candidate(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        similarity: float,
        threshold: float,
    ) -> MimoLabelResult:
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_cluster_pair_review_prompt(
                left,
                right,
                similarity,
                threshold,
                retry_reason=validation_error,
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的知识主题聚类标注员。"},
                    {"role": "user", "content": [{"type": "text", "text": prompt}]},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            request_audit = {
                "endpoint": self.config.chat_completions_url(),
                "model": self.config.model,
                "prompt_version": CLUSTER_PAIR_REVIEW_PROMPT_VERSION,
                "left": left,
                "right": right,
                "similarity": round(similarity, 4),
                "threshold": round(threshold, 4),
                "attempt": attempt + 1,
            }
            try:
                raw_response = self._post(payload)
                last_response = raw_response
                raw_content = _content_from_response(raw_response)
                review = _validate_cluster_pair_review(json.loads(_strip_json_fence(raw_content)))
                return MimoLabelResult(
                    candidate=review,
                    request_audit=request_audit,
                    response_audit=raw_response,
                )
            except (json.JSONDecodeError, MimoError) as exc:
                validation_error = str(exc)
                if attempt == 1:
                    raise MimoError(f"MiMo 聚类标注 JSON 校验失败（已重试一次）：{validation_error}") from exc
            except Exception as exc:
                raise MimoError(f"MiMo 聚类标注调用失败：{exc}") from exc
        raise MimoError(f"MiMo 聚类标注未产生有效结果：{last_response}")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.config.chat_completions_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise MimoError(f"MiMo HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise MimoError(f"MiMo 网络错误：{exc.reason}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MimoError(f"MiMo 返回非 JSON 响应：{raw[:300]}") from exc
        if not isinstance(parsed, dict):
            raise MimoError("MiMo 返回的根节点不是对象")
        return parsed
