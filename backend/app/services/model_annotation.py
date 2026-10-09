from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.services.model_connection import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    cached_model_config,
    mask_api_key,
    merge_model_config,
)


@dataclass
class ModelAnnotationError(RuntimeError):
    """Raised when the internal DeepSeek-flash correction call cannot run."""

    message: str
    error_code: str = "MODEL_ANNOTATION_ERROR"
    retryable: bool = False
    http_status: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


def _json_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelAnnotationError("DeepSeek-flash 返回的结果不是合法 JSON。", "INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise ModelAnnotationError("DeepSeek-flash 返回结果必须是 JSON 对象。", "INVALID_JSON")
    return payload


def _correction_prompt(candidate: dict[str, Any]) -> str:
    return (
        "你是知识候选标注纠错器。只能根据人工复核结果重新给出模型建议，"
        "不能修改人工真值，也不能补造来源证据。请只返回 JSON 对象，字段包括："
        "knowledge_value、decision、draft_quality、topic_purity、reusability、risk_flags、suggested_action、confidence、reason、error_type。"
        "knowledge_value 只能是 worthy、unworthy、pending；confidence 是 0 到 1 的数。\n\n"
        + json.dumps(candidate, ensure_ascii=False, indent=2)
    )


def _annotation_prompt(candidate: dict[str, Any]) -> str:
    return (
        "你是知识转写草稿审核标注器。必须先阅读主题、来源证据、知识草稿和推荐回复，"
        "再判断是否值得沉淀；不能只凭标题或聚类标签判断。"
        "只返回 JSON 对象，字段包括：knowledge_value、decision、draft_quality、"
        "topic_purity、reusability、risk_flags、suggested_action、confidence、reason、error_type。"
        "knowledge_value 只能是 worthy、unworthy、pending；"
        "draft_quality 只能是 approved、revision_required、hold_for_evidence、rejected；"
        "suggested_action 只能是 submit_for_human_review、return_for_revision、"
        "hold_for_evidence、discard；risk_flags 是字符串数组；confidence 是 0 到 1 的数。\n\n"
        + json.dumps(candidate, ensure_ascii=False, indent=2)
    )


def _draft_revision_prompt(candidate: dict[str, Any]) -> str:
    return (
        "你是知识转写草稿修订器。只能依据输入中的来源证据和已有草稿修改知识正文与推荐回复，"
        "不得补造事实、总部标准、阈值、品牌、机型或适用范围。证据不足时必须保留不确定性，"
        "不能为了写完整而给出确定结论。请只返回 JSON 对象，字段必须包括："
        "revised_content、revised_recommended_reply、change_summary、evidence_used、limitations。"
        "revised_content 必须是可保存的知识正文对象或字符串；revised_recommended_reply 必须是可直接发送的简洁回复；"
        "change_summary、evidence_used、limitations 必须是字符串数组。不得返回人工审核结论。\n\n"
        + json.dumps(candidate, ensure_ascii=False, indent=2)
    )


def _draft_prompt_optimization_prompt(training_snapshot: dict[str, Any]) -> str:
    return (
        "你是知识聚类转写提示词优化教练。请根据冻结样本中的模型原始草稿与人工最终稿差异，"
        "总结人工对知识正文和推荐回复的真实要求，并生成一份完整的候选知识转写 Prompt。"
        "不得修改人工最终稿，不得补造来源证据，不得建议自动发布或自动覆盖当前 Prompt。"
        "只返回 JSON 对象，字段必须包括：analysis_summary、root_causes、prompt_changes、"
        "candidate_prompt、expected_risks、recommended_next_action。"
        "root_causes、prompt_changes、expected_risks 必须是数组；"
        "recommended_next_action 只能是 human_review、collect_more_edits、offline_replay。\n\n"
        + json.dumps(training_snapshot, ensure_ascii=False, indent=2)
    )


def _prompt_optimization_prompt(training_snapshot: dict[str, Any]) -> str:
    return (
        "你是知识转写后审核提示词的优化教练。你必须只根据冻结的影子评测数据，"
        "分析模型初标与人工真值的差异，提出可审计的候选提示词修改方案。"
        "不能把影子数据当作正式训练真值，不能建议自动发布、自动切换生产路由或自动覆盖当前提示词。"
        "只返回 JSON 对象，字段必须包括：analysis_summary、root_causes、prompt_changes、"
        "candidate_prompt、expected_risks、recommended_next_action。"
        "root_causes、prompt_changes、expected_risks 必须是数组；candidate_prompt 是用于"
        "“转写后沉淀价值和草稿处理判断”的完整候选提示词；"
        "recommended_next_action 只能是 shadow_rerun、collect_more_truth、human_review。\n\n"
        + json.dumps(training_snapshot, ensure_ascii=False, indent=2)
    )


def _prompt_revision_prompt(revision_snapshot: dict[str, Any]) -> str:
    return (
        "你是知识转写后审核提示词的修订教练。请基于已有候选 Prompt 和人工已归因的退化用例，"
        "生成下一版候选 Prompt。人工真值是唯一依据，不能修改、推翻或补造人工真值。"
        "本任务仅用于 shadow_only 影子评测；不得自动启用、不得切换生产路由、不得提交模型权重训练。"
        "只返回 JSON 对象，字段必须包括：analysis_summary、prompt_changes、candidate_prompt、"
        "expected_risks、recommended_next_action。prompt_changes、expected_risks 必须是数组；"
        "recommended_next_action 只能是 validation_shadow_rerun、collect_more_truth、human_review。\n\n"
        + json.dumps(revision_snapshot, ensure_ascii=False, indent=2)
    )


def _shadow_rerun_prompt(
    *,
    review_prompt: str,
    candidate: dict[str, Any],
) -> str:
    return (
        "你正在进行仅用于影子评测的知识转写草稿审核。不得自动发布、"
        "不得切换生产路由、不得把输入作为正式训练真值。严格遵守以下审核提示词：\n\n"
        f"{review_prompt}\n\n"
        "待审核样本如下。请仅输出上述审核提示词要求的 JSON 对象：\n"
        + json.dumps(candidate, ensure_ascii=False, indent=2)
    )


def _environment_model_config() -> dict[str, Any]:
    """The route defined by this deployment's DEEPSEEK_*/GROUP_LLM_* variables."""
    base_url = (
        settings.DEEPSEEK_BASE_URL.strip()
        or settings.GROUP_LLM_BASE_URL.strip()
    ).rstrip("/")
    api_key = settings.DEEPSEEK_API_KEY.strip() or settings.GROUP_LLM_API_KEY.strip()
    model = settings.DEEPSEEK_MODEL.strip() or "deepseek-flash"
    try:
        timeout = max(1.0, float(settings.DEEPSEEK_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_MODEL_TIMEOUT_SECONDS
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": timeout,
    }


def resolve_call_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The effective route for one model call.

    An explicit ``config`` (used by the connection test with unsaved form
    values) wins; otherwise the override saved from the confidence-training
    page is layered on top of the environment variables.
    """
    if config:
        return merge_model_config(config, _environment_model_config())
    return merge_model_config(cached_model_config(), _environment_model_config())


def _call_deepseek_flash(
    prompt: str,
    *,
    purpose: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = resolve_call_config(config)
    base_url = str(resolved.get("base_url") or "").strip().rstrip("/")
    api_key = str(resolved.get("api_key") or "").strip()
    model = str(resolved.get("model") or "").strip() or "deepseek-flash"
    try:
        timeout = max(
            1.0,
            float(resolved.get("timeout_seconds") or DEFAULT_MODEL_TIMEOUT_SECONDS),
        )
    except (TypeError, ValueError):
        timeout = DEFAULT_MODEL_TIMEOUT_SECONDS
    if not base_url or not api_key:
        raise ModelAnnotationError(
            "内部 DeepSeek-flash API 尚未配置；请在“模型连接配置”里填写模型地址和 API Key，"
            "或配置 DEEPSEEK_*（也可复用 GROUP_LLM_API_KEY）。",
            "CONFIG_MISSING",
        )
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "你必须严格返回 JSON，不要输出 Markdown。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        response = httpx.post(
            url,
            headers=headers,
            json=body,
            timeout=timeout,
        )
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise ModelAnnotationError(
                f"内部 DeepSeek-flash 返回 HTTP {response.status_code}。",
                f"HTTP_{response.status_code}",
                retryable,
                response.status_code,
            )
        payload = response.json()
    except ModelAnnotationError:
        raise
    except httpx.TimeoutException as exc:
        raise ModelAnnotationError(
            f"调用内部 DeepSeek-flash {purpose}接口超时。",
            "TIMEOUT",
            True,
        ) from exc
    except httpx.HTTPError as exc:
        raise ModelAnnotationError(
            f"调用内部 DeepSeek-flash {purpose}接口网络失败。",
            "NETWORK_ERROR",
            True,
        ) from exc
    except ValueError as exc:
        raise ModelAnnotationError(
            f"内部 DeepSeek-flash {purpose}接口返回不是合法 JSON。",
            "INVALID_RESPONSE_JSON",
        ) from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelAnnotationError(
            "DeepSeek-flash 返回缺少 choices.message.content。",
            "INVALID_RESPONSE_SHAPE",
        ) from exc
    result = _json_content(content)
    result["requested_model"] = model
    result["model_name"] = model
    result["resolved_model_version"] = str(payload.get("model") or model)
    result["prompt_version"] = settings.DEEPSEEK_PROMPT_VERSION
    return result


def correct_with_deepseek_flash(candidate: dict[str, Any]) -> dict[str, Any]:
    return _call_deepseek_flash(_correction_prompt(candidate), purpose="纠错")


def annotate_transcribed_candidate(
    candidate: dict[str, Any],
    *,
    review_prompt: str | None = None,
    prompt_version: str | None = None,
) -> dict[str, Any]:
    result = _call_deepseek_flash(
        (review_prompt or _annotation_prompt(candidate)),
        purpose="草稿标注",
    ) if review_prompt else _call_deepseek_flash(_annotation_prompt(candidate), purpose="草稿标注")
    if prompt_version:
        result["prompt_version"] = prompt_version
    return result


def revise_transcribed_candidate_draft(candidate: dict[str, Any]) -> dict[str, Any]:
    return _call_deepseek_flash(_draft_revision_prompt(candidate), purpose="草稿修订")


def analyze_confidence_training_prompt(training_snapshot: dict[str, Any]) -> dict[str, Any]:
    return _call_deepseek_flash(
        _prompt_optimization_prompt(training_snapshot),
        purpose="提示词优化分析",
    )


def analyze_draft_generation_prompt(training_snapshot: dict[str, Any]) -> dict[str, Any]:
    return _call_deepseek_flash(
        _draft_prompt_optimization_prompt(training_snapshot),
        purpose="知识草稿提示词优化分析",
    )


def revise_confidence_training_prompt(revision_snapshot: dict[str, Any]) -> dict[str, Any]:
    return _call_deepseek_flash(
        _prompt_revision_prompt(revision_snapshot),
        purpose="退化样本提示词修订",
    )


def shadow_rerun_transcribed_candidate(
    *,
    review_prompt: str,
    candidate: dict[str, Any],
    prompt_version: str,
) -> dict[str, Any]:
    result = _call_deepseek_flash(
        _shadow_rerun_prompt(review_prompt=review_prompt, candidate=candidate),
        purpose="候选提示词影子复跑",
    )
    result["prompt_version"] = prompt_version
    return result


def test_deepseek_flash_connection(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe the DeepSeek-flash route and report what was actually used.

    ``config`` carries unsaved form values from the model-connection dialog so
    an operator can test an address or key before storing it.
    """
    resolved = resolve_call_config(config)
    started = time.perf_counter()
    if config:
        result = _call_deepseek_flash(
            "请只返回 JSON：{\"status\":\"ok\"}",
            purpose="连接测试",
            config=config,
        )
    else:
        result = _call_deepseek_flash(
            "请只返回 JSON：{\"status\":\"ok\"}",
            purpose="连接测试",
        )
    if str(result.get("status") or "").lower() != "ok":
        raise ModelAnnotationError("DeepSeek-flash 连接测试返回了非预期结果。")
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    result["base_url"] = str(resolved.get("base_url") or "")
    result["model"] = str(resolved.get("model") or "")
    result["timeout_seconds"] = resolved.get("timeout_seconds")
    result["api_key_masked"] = mask_api_key(resolved.get("api_key"))
    return result
