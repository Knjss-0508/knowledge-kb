from __future__ import annotations

"""Small OpenAI-compatible client for the MiMo phone MVP.

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


PROMPT_VERSION = "phone-topic-transcription-v1"
TOPIC_REVIEW_PROMPT_VERSION = "phone-topic-initial-review-v1"


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
    return f"""你是答疑中台的手机上门回收质检知识标注员。请把一条第二部分会话记录改写成一条可供人工复核的知识候选。

必须遵守：
1. 只能依据输入会话、图片证据和下方【本次检索到的手机质检标准】；不可捏造标准。
2. standard_refs 只能填写本次检索结果中的 standard_ref，且必须是 JSON 字符串数组；不能匹配时填 [] 并将 needs_human_review 设为 true。
3. 图片不能单独构成发布结论；图片模糊、不可判断或与标准不充分对应时，在 image_evidence_summary 中说明，并设 needs_human_review=true。
4. reasoning_summary 是给审核人的简短依据摘要，不要输出思维过程，不超过 240 字。
5. title 必须是可复用的知识标题，不得复述“回收师遇到/咨询/希望获得”等会话叙述。content 是可审核的知识正文，按“判定规则、场景结论、处理建议、限制条件”组织；不得整段复制输入的核心问题、判定依据或参考话术。
6. 若会话没有“具体对象 + 明确现象/图片证据 + 可对应标准”中的关键证据，或包含“疑似、不确定、证据不足、未识别、未发现”等表达，不得给出确定性质检结论。此时 knowledge_form 必须为“流程方法”，内容应给出检查步骤、需补充证据、标准对照和转人工条件；本次个案结论不可外推为通用规则。
7. 只输出一个 JSON 对象，不要 Markdown，不要额外解释。字段必须完整：
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
  "confidence": 0.0,
  "reasoning_summary": "string",
  "needs_human_review": true,
  "image_evidence_summary": "string"
}}

【第二部分记录】
{json.dumps(_source_payload(source_row), ensure_ascii=False, indent=2)}

【本次检索到的手机质检标准】
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
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是答疑中台的手机质检知识标注员。输入是已经聚类的多个方向二案例特征，不是单条工单。

请沉淀一条可复用、供人工审核的主题级知识草稿。只能依据主题特征、证据摘要和本次检索标准；不能把任何单条工单结论直接外推为通用事实。

必须遵守：
1. 主标题必须是自然、清楚、可直接使用的知识标题，不得堆砌关键词、使用斜杠串词或写成“异常核验/屏幕/显示异常”这类标签组合。副标题不是必填项；主标题已经表达清楚时输出空数组，最多输出 2 个自然问法。
2. 只有同时满足“明确边界问题 + 可引用的本次标准 + 足够支持的主题证据”时，knowledge_form 才可为“具体判定”。其余情况必须为“流程方法”。
3. 外观、显示、拆修、胶状物、功能异常等需要现场图片判断的问题，默认沉淀核验过程：确认部位、补充近景/全景/多角度证据、对照有效标准、证据不足转人工。
4. 机型/型号等信息查询问题，输出查询与核对流程，而不是某一案例的具体机型结论。
5. standard_refs 只能填写本次检索结果的 standard_ref；无可信标准时填 []，并将 needs_human_review=true。
6. reasoning_summary 只写简短审核依据，不输出思维过程；不要编造聊天细节、图片细节或标准条款。
7. 文字已经能完整表达规则时，requires_images=false，不能把图片当装饰；只有必须通过外观、部位、颜色、裂纹、坏点或拆修痕迹等视觉差异才能解释时，requires_images=true，并给出 image_usage_instruction。
8. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
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
  "confidence": 0.0,
  "reasoning_summary": "string",
  "needs_human_review": true,
  "image_evidence_summary": "string",
  "requires_images": false,
  "image_usage_instruction": "string"
}}

【主题输入】
{json.dumps(topic, ensure_ascii=False, indent=2)}

【本次检索到的生效手机标准】
{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}
{retry_instruction}
"""


def _build_topic_review_prompt(
    topic: dict[str, Any],
    draft: dict[str, Any],
    matches: list[tuple[StandardCatalogItem, float]],
    transcription_matches: list[tuple[StandardCatalogItem, float]] | None = None,
    retry_reason: str = "",
) -> str:
    retry_instruction = f"\n【上次输出不合格原因】\n{retry_reason}" if retry_reason else ""
    return f"""你是答疑中台的手机质检知识初审员。现在需要审核一条已经转写完成的主题级知识草稿。

你的职责是“审核标注”，不是改写知识：不得修改标题、正文、分类或标准引用；只判断草稿能否进入人工复标。

审核规则：
1. 只能依据主题证据摘要、转写阶段使用的标准和本次独立检索到的标准审核，不得补充未提供的事实。
2. 草稿为“具体判定”时，必须有本次检索标准引用和足够主题证据；否则结论为“需修改”或“证据不足待补充”。
3. 外观、显示、拆修、胶状物、功能等依赖图片的问题，草稿沉淀为核验流程是合理的；不能因为没有个案最终判定而驳回流程型知识。
4. 无可信标准的草稿不得标“通过”；应标“证据不足待补充”，并标记重点复核。
5. 必须审核知识内容是否准确覆盖规则、处理步骤和限制条件，不能只检查格式和字段完整性。
6. 必须审核主标题是否自然清楚、副标题是否只是关键词堆砌；主标题清楚时不应强行要求副标题。
7. 必须审核图片必要性：文字能说清时不应要求图片；依赖视觉差异时没有保留图片，应标记需修改或证据不足。
8. 转写引用标准与独立复核标准不一致时，应标记“标准项映射错”或“标准召回不足”。
9. 只输出一个 JSON 对象，不要 Markdown。字段必须完整：
{{
  "decision": "通过 / 需修改 / 驳回 / 证据不足待补充",
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

【本次检索到的生效手机标准】
{json.dumps(_standard_payload(matches), ensure_ascii=False, indent=2)}

【转写阶段使用的标准】
{json.dumps(_standard_payload(transcription_matches or []), ensure_ascii=False, indent=2)}
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
    return {
        "title": title,
        "subtitles": [
            _text(item, 120)
            for item in subtitles[:2]
            if _text(item, 120) and _text(item, 120) != title
        ],
        "content": _text(value["content"], 4000),
        "category_l1": _text(value["category_l1"], 80),
        "category_l2": _text(value["category_l2"], 80),
        "layer": _text(value["layer"], 32) or "L2",
        "knowledge_form": knowledge_form,
        "standard_refs": list(dict.fromkeys(refs)),
        "applicable_scope": _text(value["applicable_scope"], 800),
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
                    {"role": "system", "content": "你是严谨的手机回收质检知识标注助手。"},
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

    def label_topic(
        self,
        topic: dict[str, Any],
        matches: list[tuple[StandardCatalogItem, float]],
    ) -> MimoLabelResult:
        """Generate a reusable draft after clustering, never from a single case."""
        allowed_refs = {_standard_ref(item) for item, _score in matches if _standard_ref(item)}
        validation_error = ""
        last_response: dict[str, Any] = {}
        request_audit: dict[str, Any] = {}
        for attempt in range(2):
            prompt = _build_topic_prompt(topic, matches, validation_error)
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的手机主题知识标注助手。"},
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
            )
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的手机主题知识初审员。"},
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
