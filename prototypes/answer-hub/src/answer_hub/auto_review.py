from __future__ import annotations

"""Model-review validation metrics and controlled production release policy."""

from dataclasses import dataclass
from typing import Any
import os

from .product_taxonomy import infer_product_category, resolve_product_category

from .mimo import load_dotenv


POLICY_VERSION = "model-auto-review-v1"
PASS_DECISIONS = {"通过", "修改后通过"}
USABLE_VALUES = {"是", "可用", "通过", "yes", "true", "1"}
UNUSABLE_VALUES = {"否", "不可用", "驳回", "no", "false", "0"}
WORTHY_VALUES = {"是", "值得沉淀", "值得", "yes", "true", "1"}
UNWORTHY_VALUES = {"否", "不值得沉淀", "不值得", "no", "false", "0"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "是", "启用"}


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, min(float(os.getenv(name, str(default))), 1.0))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class AutoReviewPolicy:
    enabled: bool = False
    min_confidence: float = 0.85
    min_validation_samples: int = 40
    min_validation_accuracy: float = 0.90
    min_pass_precision: float = 0.95
    validated_model: str = ""
    validated_prompt_version: str = ""

    @classmethod
    def from_env(cls) -> "AutoReviewPolicy":
        load_dotenv()
        return cls(
            enabled=_env_bool("AUTO_REVIEW_ENABLED", False),
            min_confidence=_env_float("AUTO_REVIEW_MIN_CONFIDENCE", 0.85),
            min_validation_samples=_env_int("AUTO_REVIEW_MIN_VALIDATION_SAMPLES", 40),
            min_validation_accuracy=_env_float(
                "AUTO_REVIEW_MIN_VALIDATION_ACCURACY",
                0.90,
            ),
            min_pass_precision=_env_float(
                "AUTO_REVIEW_MIN_PASS_PRECISION",
                0.95,
            ),
            validated_model=os.getenv("AUTO_REVIEW_VALIDATED_MODEL", "").strip(),
            validated_prompt_version=os.getenv(
                "AUTO_REVIEW_VALIDATED_PROMPT_VERSION",
                "",
            ).strip(),
        )

    @property
    def deployment_ready(self) -> bool:
        return bool(self.validated_model and self.validated_prompt_version)


def candidate_product_type(candidate: dict[str, Any]) -> str:
    direct = candidate.get("产品类型编码") or candidate.get("产品类型")
    category = resolve_product_category(direct)
    if category:
        return category.name
    inferred = infer_product_category(
        (
            candidate.get("适用范围"),
            candidate.get("主题聚类键"),
            candidate.get("知识分类"),
        )
    )
    return inferred.name if inferred else "未识别"


def assess_auto_review_candidate(
    candidate: dict[str, Any],
    policy: AutoReviewPolicy,
    *,
    enforce_deployment_version: bool = False,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if _text(candidate.get("模型初标状态")) != "topic_initial_reviewed_model":
        reasons.append("模型初标未由正式模型成功完成")
    if _text(candidate.get("模型初标提供方")).lower() != "mimo":
        reasons.append("模型初标不是 MiMo 正式结果")
    if _text(candidate.get("模型初标结论")) != "通过":
        reasons.append("模型初标结论不是通过")
    if _text(candidate.get("模型初标是否值得沉淀")) != "值得沉淀":
        reasons.append("模型未确认该知识点值得沉淀")
    if _safe_float(candidate.get("模型初标置信度")) < policy.min_confidence:
        reasons.append(f"模型初标置信度低于 {policy.min_confidence:.2f}")
    if _text(candidate.get("模型初标重点复核")) == "是":
        reasons.append("模型标记为重点复核")
    if _text(candidate.get("是否重点复核")) == "是":
        reasons.append("转写阶段标记为重点复核")
    if _text(candidate.get("模型初标标准一致性")) not in {"一致", "无可信标准"}:
        reasons.append("标准一致性未通过")
    if _text(candidate.get("模型初标证据充分性")) == "不足":
        reasons.append("证据不足")
    if _text(candidate.get("模型初标内容一致性")) != "一致":
        reasons.append("知识内容一致性未通过")
    if _text(candidate.get("模型初标标题质量")) != "清晰":
        reasons.append("标题质量未通过")
    if _text(candidate.get("模型初标图片必要性")) == "图片不足":
        reasons.append("必要图片不足")
    model_name = _text(candidate.get("模型初标模型名称"))
    prompt_version = _text(candidate.get("模型初标Prompt版本"))
    if policy.validated_model and model_name != policy.validated_model:
        reasons.append("模型版本未通过当前验证")
    if (
        policy.validated_prompt_version
        and prompt_version != policy.validated_prompt_version
    ):
        reasons.append("Prompt版本未通过当前验证")
    if enforce_deployment_version and not policy.deployment_ready:
        reasons.append("生产自动放行尚未绑定已验证模型和Prompt版本")
    return not reasons, reasons


def apply_auto_review_annotation(
    candidate: dict[str, Any],
    policy: AutoReviewPolicy,
) -> dict[str, Any]:
    approved, reasons = assess_auto_review_candidate(
        candidate,
        policy,
        enforce_deployment_version=policy.enabled,
    )
    if policy.enabled:
        status = "auto_approved" if approved else "manual_exception"
    else:
        status = (
            "validation_auto_approve"
            if approved
            else "validation_manual_exception"
        )
    candidate["自动审核状态"] = status
    candidate["自动审核原因"] = "；".join(reasons) if reasons else "满足模型自动放行条件"
    candidate["自动审核策略版本"] = POLICY_VERSION
    return candidate


def partition_auto_review_candidates(
    candidates: list[dict[str, Any]],
    policy: AutoReviewPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    approved_rows: list[dict[str, Any]] = []
    exception_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        approved, reasons = assess_auto_review_candidate(
            candidate,
            policy,
            enforce_deployment_version=policy.enabled,
        )
        row = {
            **candidate,
            "自动审核状态": "auto_approved" if approved else "manual_exception",
            "自动审核原因": "；".join(reasons) if reasons else "满足模型自动放行条件",
            "自动审核策略版本": POLICY_VERSION,
        }
        if approved:
            approved_rows.append(row)
        else:
            exception_rows.append(row)
    return approved_rows, exception_rows


def _teammate_gold(candidate: dict[str, Any]) -> bool | None:
    decision = teammate_validation_decision(candidate)
    if decision in PASS_DECISIONS:
        return not bool(_text(candidate.get("如何修改")))
    if decision in {"驳回", "标记Bad Case"}:
        return False
    return None


def teammate_validation_decision(candidate: dict[str, Any]) -> str:
    knowledge_value = _text(candidate.get("是否值得沉淀")).lower()
    if knowledge_value in UNWORTHY_VALUES:
        return "驳回"
    if knowledge_value not in WORTHY_VALUES:
        explicit_decision = _text(candidate.get("审核结论"))
        return (
            explicit_decision
            if explicit_decision in {"驳回", "标记Bad Case"}
            else ""
        )
    usable = _text(candidate.get("是否可用")).lower()
    if usable in USABLE_VALUES:
        return "修改后通过" if _text(candidate.get("如何修改")) else "通过"
    if usable in UNUSABLE_VALUES:
        return "驳回"
    return _text(candidate.get("审核结论"))


def evaluate_auto_review_validation(
    candidates: list[dict[str, Any]],
    policy: AutoReviewPolicy,
) -> dict[str, Any]:
    validated = correct = true_pass = false_pass = false_reject = true_reject = 0
    by_product: dict[str, dict[str, int]] = {}
    versions: set[tuple[str, str]] = set()

    for candidate in candidates:
        gold = _teammate_gold(candidate)
        if gold is None:
            continue
        predicted, _reasons = assess_auto_review_candidate(
            candidate,
            policy,
            enforce_deployment_version=False,
        )
        validated += 1
        product_type = candidate_product_type(candidate)
        product = by_product.setdefault(
            product_type,
            {"validated": 0, "correct": 0, "false_pass": 0, "false_reject": 0},
        )
        product["validated"] += 1
        versions.add(
            (
                _text(candidate.get("模型初标模型名称")),
                _text(candidate.get("模型初标Prompt版本")),
            )
        )
        if predicted == gold:
            correct += 1
            product["correct"] += 1
        if predicted and gold:
            true_pass += 1
        elif predicted and not gold:
            false_pass += 1
            product["false_pass"] += 1
        elif not predicted and gold:
            false_reject += 1
            product["false_reject"] += 1
        else:
            true_reject += 1

    accuracy = correct / validated if validated else None
    pass_precision = (
        true_pass / (true_pass + false_pass)
        if true_pass + false_pass
        else None
    )
    pass_recall = (
        true_pass / (true_pass + false_reject)
        if true_pass + false_reject
        else None
    )
    product_rows = [
        {
            "产品类型": product_type,
            **values,
            "accuracy": (
                values["correct"] / values["validated"]
                if values["validated"]
                else None
            ),
        }
        for product_type, values in sorted(by_product.items())
    ]
    version_rows = [
        {"model": model, "prompt_version": prompt}
        for model, prompt in sorted(versions)
    ]
    gate_reasons: list[str] = []
    if validated < policy.min_validation_samples:
        gate_reasons.append(
            f"验证样本少于 {policy.min_validation_samples} 条"
        )
    if accuracy is None or accuracy < policy.min_validation_accuracy:
        gate_reasons.append(
            f"准确率未达到 {policy.min_validation_accuracy:.0%}"
        )
    if pass_precision is None or pass_precision < policy.min_pass_precision:
        gate_reasons.append(
            f"自动放行精确率未达到 {policy.min_pass_precision:.0%}"
        )
    if len(versions) != 1:
        gate_reasons.append("验证数据必须对应单一模型和Prompt版本")

    return {
        "total_rows": len(candidates),
        "validated_rows": validated,
        "validation_coverage": validated / len(candidates) if candidates else 0.0,
        "correct_rows": correct,
        "accuracy": accuracy,
        "pass_precision": pass_precision,
        "pass_recall": pass_recall,
        "true_pass": true_pass,
        "false_pass": false_pass,
        "false_reject": false_reject,
        "true_reject": true_reject,
        "gate_ready": not gate_reasons,
        "gate_reasons": gate_reasons,
        "by_product": product_rows,
        "versions": version_rows,
    }


def select_candidates_for_submission(
    candidates: list[dict[str, Any]],
    policy: AutoReviewPolicy,
) -> list[dict[str, Any]]:
    if policy.enabled:
        approved, _exceptions = partition_auto_review_candidates(candidates, policy)
        return [
            {
                **candidate,
                "审核结论": "通过",
                "审核备注": "模型自动标注通过，替代第三部分人工复标。",
            }
            for candidate in approved
        ]

    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = teammate_validation_decision(candidate)
        if decision in PASS_DECISIONS:
            selected.append({**candidate, "审核结论": decision})
    return selected
