from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any
import json
import re
import threading
import unicodedata

from .audit import AuditStore
from .business_taxonomy import business_line_from_record
from .clustering_rules import build_clustering_fingerprint
from .product_taxonomy import canonical_product_name, product_from_scope


AUTO_MERGE_THRESHOLD = 0.90
MANUAL_REVIEW_THRESHOLD = 0.75
AMBIGUOUS_MATCH_MARGIN = 0.05
SCOPE_AGNOSTIC_TARGETS = {
    "model_query",
    "memory_storage_brand",
    "new_device_eligibility",
    "camera_lens_surface_condition",
    "screen_color_spot",
    "screen_color_aging",
}
THRESHOLD_AGNOSTIC_TARGETS = {
    "model_query",
    "memory_storage_brand",
}
OBJECT_AGNOSTIC_TARGETS = {
    "model_query",
    "memory_storage_brand",
}
_TOPIC_REGISTRY_WRITE_LOCK = threading.RLock()


@dataclass(frozen=True)
class TopicResolution:
    topic_id: str
    topic_key: tuple[str, ...]
    rows: list[dict[str, Any]]
    matched_existing: bool
    historical_topic_id: str
    requires_review: bool
    decision: str
    confidence: float
    reason: str
    evidence_version: int
    added_member_count: int
    duplicate_member_count: int


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value).casefold())
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _first_value(rows: list[dict[str, Any]], *fields: str) -> str:
    for row in rows:
        for field in fields:
            value = _text(row.get(field))
            if value:
                return value
    return ""


def _business_line(rows: list[dict[str, Any]]) -> str:
    names = {
        line.name
        for row in rows
        if (line := business_line_from_record(row)) is not None
    }
    if len(names) != 1:
        raise ValueError("增量主题只能包含同一回收业务层级")
    return next(iter(names))


def _product_category(rows: list[dict[str, Any]]) -> str:
    resolved_names = [
        canonical_product_name(
            row.get("产品类型") or row.get("product_category"),
            unknown="",
        )
        for row in rows
    ]
    if any(not name for name in resolved_names):
        raise ValueError("增量主题包含未配置产品品类")
    names = set(resolved_names)
    if len(names) != 1:
        raise ValueError("增量主题只能包含一个已配置产品品类")
    return next(iter(names))


def _row_fingerprint(row: dict[str, Any]) -> dict[str, str]:
    fingerprint = build_clustering_fingerprint(
        product_category=_text(
            row.get("产品类型") or row.get("product_category")
        ),
        category_l1=_text(
            row.get("模型主题一级分类")
            or row.get("一级分类")
            or row.get("category_l1")
        ),
        intent=_text(row.get("问题意图") or row.get("intent")),
        subject=_text(row.get("对象/部位") or row.get("subject")),
        phenomenon=_text(row.get("异常现象") or row.get("phenomenon")),
        normalized_issue=_text(
            row.get("核心问题") or row.get("normalized_issue")
        ),
        judgment_target=_text(
            row.get("判定目标") or row.get("judgment_target")
        ),
        resolution_mode=_text(
            row.get("解题方式") or row.get("resolution_mode")
        ),
        standard_path=_text(
            row.get("主标准路径") or row.get("standard_path")
        ),
        conversation=_text(
            row.get("语义标注依据")
            or row.get("聊天内容")
            or row.get("evidence_summary")
        ),
    )
    return {
        "standard_family": fingerprint.standard_family,
        "merge_policy": fingerprint.merge_policy,
        "object_key": fingerprint.object_key,
        "phenomenon_value": fingerprint.phenomenon_value,
        "query_target": fingerprint.query_target,
        "detection_target": fingerprint.detection_target,
        "platform": _normalized(
            row.get("_原子平台") or row.get("platform")
        ),
        "brand": _normalized(
            row.get("_原子品牌") or row.get("brand")
        ),
        "model_scope": _normalized(
            row.get("_原子机型范围") or row.get("model_scope")
        ),
        "threshold_values": "|".join(
            sorted(
                set(
                    re.findall(
                        r"\d+(?:\.\d+)?",
                        _text(
                            row.get("_原子阈值例外")
                            or row.get("阈值/例外")
                            or row.get("threshold_or_exception")
                        )
                        + _text(
                            row.get("主标准路径")
                            or row.get("standard_path")
                        ),
                    )
                )
            )
        ),
    }


def _topic_text(rows: list[dict[str, Any]]) -> str:
    fields = (
        "核心问题",
        "模型主题一级分类",
        "模型主题二级分类",
        "问题意图",
        "对象/部位",
        "异常现象",
        "判定目标",
        "解题方式",
        "_聚类主题标题",
        "_聚类知识定义",
    )
    return "|".join(
        value
        for row in rows
        for field in fields
        if (value := _normalized(row.get(field)))
    )


def _topic_signature(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = [_row_fingerprint(row) for row in rows]

    def values(field: str) -> list[str]:
        return sorted(
            {
                fingerprint[field]
                for fingerprint in fingerprints
                if fingerprint[field]
            }
        )

    return {
        "standard_families": values("standard_family"),
        "merge_policies": values("merge_policy"),
        "object_keys": values("object_key"),
        "phenomenon_values": values("phenomenon_value"),
        "query_targets": values("query_target"),
        "detection_targets": values("detection_target"),
        "platforms": values("platform"),
        "brands": values("brand"),
        "model_scopes": values("model_scope"),
        "threshold_values": values("threshold_values"),
        "topic_text": _topic_text(rows),
    }


def _sets(value: dict[str, Any], field: str) -> set[str]:
    return {
        _text(item)
        for item in value.get(field) or []
        if _text(item)
    }


def _validate_internal_signature(signature: dict[str, Any]) -> None:
    standard_families = _sets(signature, "standard_families")
    merge_policies = _sets(signature, "merge_policies")
    phenomenon_values = _sets(signature, "phenomenon_values")
    if len(standard_families) > 1:
        raise ValueError("增量主题内部包含不同质检聚类标准族")
    if len(merge_policies) > 1:
        raise ValueError("增量主题内部包含不同质检聚类合并策略")
    if (
        merge_policies == {"separatebyphenomenon"}
        and len(phenomenon_values) > 1
    ):
        raise ValueError("同一标准族的不同现象值必须拆分")


def _merge_signatures(
    historical: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    list_fields = (
        "standard_families",
        "merge_policies",
        "object_keys",
        "phenomenon_values",
        "query_targets",
        "detection_targets",
        "platforms",
        "brands",
        "model_scopes",
        "threshold_values",
    )
    for field in list_fields:
        merged[field] = sorted(
            _sets(historical, field) | _sets(current, field)
        )
    texts = list(
        dict.fromkeys(
            text
            for text in (
                _text(historical.get("topic_text")),
                _text(current.get("topic_text")),
            )
            if text
        )
    )
    merged["topic_text"] = "|".join(texts)[:4000]
    return merged


def _has_target_conflict(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    for field in ("query_targets", "detection_targets"):
        left_values = _sets(left, field)
        right_values = _sets(right, field)
        if left_values and right_values and left_values != right_values:
            return True
    return False


def _shared_target(
    left: dict[str, Any],
    right: dict[str, Any],
) -> str:
    for field in ("query_targets", "detection_targets"):
        left_values = _sets(left, field)
        right_values = _sets(right, field)
        if (
            len(left_values) == 1
            and left_values == right_values
        ):
            return next(iter(left_values))
    return ""


def _has_scope_or_threshold_conflict(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[bool, str]:
    target = _shared_target(left, right)
    if target not in SCOPE_AGNOSTIC_TARGETS:
        for field, label in (
            ("platforms", "平台范围"),
            ("brands", "品牌范围"),
            ("model_scopes", "机型范围"),
        ):
            left_values = _sets(left, field)
            right_values = _sets(right, field)
            if (
                left_values
                and right_values
                and left_values != right_values
            ):
                return True, f"{label}不同"
    if target not in THRESHOLD_AGNOSTIC_TARGETS:
        left_thresholds = _sets(left, "threshold_values")
        right_thresholds = _sets(right, "threshold_values")
        if (
            left_thresholds
            and right_thresholds
            and left_thresholds != right_thresholds
        ):
            return True, "阈值或例外边界不同"
    return False, ""


def _has_object_conflict(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    if _shared_target(left, right) in OBJECT_AGNOSTIC_TARGETS:
        return False
    left_objects = _sets(left, "object_keys")
    right_objects = _sets(right, "object_keys")
    return bool(
        left_objects
        and right_objects
        and left_objects != right_objects
    )


def _character_similarity(left: str, right: str) -> float:
    def grams(value: str) -> Counter[str]:
        normalized = _normalized(value)
        return Counter(
            normalized[index : index + size]
            for size in (2, 3)
            for index in range(max(0, len(normalized) - size + 1))
        )

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 0.0
    shared = left_grams.keys() & right_grams.keys()
    dot_product = sum(
        left_grams[gram] * right_grams[gram]
        for gram in shared
    )
    left_norm = sum(value * value for value in left_grams.values()) ** 0.5
    right_norm = sum(value * value for value in right_grams.values()) ** 0.5
    return (
        dot_product / (left_norm * right_norm)
        if left_norm and right_norm
        else 0.0
    )


def _signature_similarity(
    current: dict[str, Any],
    historical: dict[str, Any],
) -> tuple[float, str]:
    if _has_target_conflict(current, historical):
        return 0.0, "查询或检测目标不同"
    scope_conflict, scope_reason = _has_scope_or_threshold_conflict(
        current,
        historical,
    )
    if scope_conflict:
        return 0.0, scope_reason
    if _has_object_conflict(current, historical):
        return 0.0, "具体判定对象不同，不能归入同一历史主题"

    current_families = _sets(current, "standard_families")
    historical_families = _sets(historical, "standard_families")
    if current_families and historical_families:
        if current_families != historical_families:
            return 0.0, "质检聚类标准族不同"
        current_policies = _sets(current, "merge_policies")
        historical_policies = _sets(historical, "merge_policies")
        if current_policies != historical_policies:
            return 0.0, "质检聚类合并策略不同"
        if current_policies == {"separatebyphenomenon"}:
            if (
                _sets(current, "phenomenon_values")
                != _sets(historical, "phenomenon_values")
            ):
                return 0.0, "同一标准族的现象值不同，必须拆分"
        return 0.98, "命中同一质检聚类标准族且不存在目标冲突"

    current_targets = (
        _sets(current, "query_targets")
        | _sets(current, "detection_targets")
    )
    historical_targets = (
        _sets(historical, "query_targets")
        | _sets(historical, "detection_targets")
    )
    if current_targets and current_targets == historical_targets:
        return 0.94, "术语归一后属于同一查询或检测目标"

    current_text = _text(current.get("topic_text"))
    historical_text = _text(historical.get("topic_text"))
    if not current_text or not historical_text:
        return 0.0, "缺少可比较的主题文本"
    similarity = max(
        SequenceMatcher(None, current_text, historical_text).ratio(),
        _character_similarity(current_text, historical_text),
    )
    return similarity, "主题结构化文本相似度匹配"


def _membership(row: dict[str, Any]) -> dict[str, Any]:
    source_record_id = _first_value(
        [row],
        "来源记录ID",
        "数据ID",
        "工单ID",
    )
    original_work_order_id = _first_value(
        [row],
        "原始工单ID",
        "工单ID",
        "回收单号",
    )
    current_work_order_id = _text(row.get("工单ID"))
    explicit_original_work_order_id = _text(
        row.get("原始工单ID")
    )
    if (
        current_work_order_id
        and explicit_original_work_order_id
        and current_work_order_id != explicit_original_work_order_id
    ):
        raise ValueError("工单ID与原始工单ID不一致，禁止写入主题库")
    atomic_id = _first_value(
        [row],
        "_原子知识ID",
        "原子知识ID",
    )
    atomic_identity = (
        atomic_id
        or _normalized(
            _first_value(
                [row],
                "核心问题",
                "聊天内容",
            )
        )
    )
    identity = "|".join(
        (
            source_record_id,
            original_work_order_id,
            atomic_identity,
        )
    )
    return {
        "membership_key": sha256(identity.encode("utf-8")).hexdigest(),
        "source_record_id": source_record_id,
        "original_work_order_id": original_work_order_id,
        "atomic_id": atomic_id,
        "evidence": dict(row),
    }


def _collision_safe_topic_id(
    proposed_topic_id: str,
    historical_topics: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> str:
    existing_ids = {
        _text(topic.get("topic_id"))
        for topic in historical_topics
        if _text(topic.get("topic_id"))
    }
    if proposed_topic_id not in existing_ids:
        return proposed_topic_id
    member_keys = sorted(
        _membership(row)["membership_key"]
        for row in rows
    )
    seed = "|".join((proposed_topic_id, *member_keys))
    digest = sha256(seed.encode("utf-8")).hexdigest().upper()
    for length in range(10, len(digest) + 1, 2):
        candidate = f"TOP-{digest[:length]}"
        if candidate not in existing_ids:
            return candidate
    raise ValueError("无法为不匹配的主题生成无碰撞稳定主题ID")


def _same_work_order_atomic_conflict(
    new_members: list[dict[str, Any]],
    historical_members: list[dict[str, str]],
) -> bool:
    for new_member in new_members:
        new_work_order = _text(new_member["original_work_order_id"])
        new_atomic_id = _text(new_member["atomic_id"])
        if not new_work_order or not new_atomic_id:
            continue
        for historical_member in historical_members:
            historical_work_order = _text(
                historical_member.get("original_work_order_id")
            )
            historical_atomic_id = _text(
                historical_member.get("atomic_id")
            )
            if (
                historical_work_order != new_work_order
                or not historical_atomic_id
                or "-LEGACY-" in historical_atomic_id
            ):
                continue
            if historical_atomic_id != new_atomic_id:
                return True
    return False


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not _text(value):
        return {}
    try:
        parsed = json.loads(_text(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _legacy_candidate_rows(
    topic_id: str,
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    business_line = _text(candidate.get("回收业务层级"))
    product_category = canonical_product_name(
        product_from_scope(
            candidate.get("适用范围") or candidate.get("产品类型")
        ),
        unknown="",
    )
    if not business_line or not product_category:
        return []

    evidence_package = _json_object(candidate.get("主题事实证据包"))
    facts = evidence_package.get("facts")
    if not isinstance(facts, list):
        facts = []
    rows: list[dict[str, Any]] = []
    for index, fact in enumerate(facts, start=1):
        if not isinstance(fact, dict):
            continue
        source_record_id = _text(fact.get("source_record_id"))
        work_order_id = _text(fact.get("work_order_id"))
        image_urls = [
            _text(url)
            for url in fact.get("image_urls") or []
            if _text(url)
        ]
        rows.append(
            {
                "数据ID": source_record_id or f"{topic_id}-LEGACY-{index}",
                "来源记录ID": source_record_id,
                "工单ID": work_order_id,
                "原始工单ID": work_order_id,
                "回收业务层级": business_line,
                "产品类型": product_category,
                "聊天内容": _text(fact.get("conversation_excerpt")),
                "核心问题": (
                    _text(fact.get("atomic_question"))
                    or _text(fact.get("human_core_problem"))
                ),
                "原始核心问题": _text(fact.get("human_core_problem")),
                "判定结论": _text(
                    fact.get("human_judgment_conclusion")
                ),
                "原始判定结论": _text(
                    fact.get("human_judgment_conclusion")
                ),
                "判定依据": _text(fact.get("judgment_basis")),
                "历史实际回复": _text(
                    fact.get("historical_actual_reply")
                ),
                "图片链接": "\n".join(image_urls),
                "图片处理状态": (
                    f"可用:{len(image_urls)}"
                    if image_urls
                    else _text(fact.get("image_processing_status"))
                ),
                "模型主题一级分类": _text(
                    candidate.get("主题问题分类")
                ),
                "模型主题二级分类": _text(
                    candidate.get("知识分类")
                ),
                "问题意图": _text(candidate.get("主题问题意图")),
                "对象/部位": _text(candidate.get("主题对象/部位")),
                "异常现象": _text(candidate.get("主题异常现象")),
                "解题方式": _text(candidate.get("主题解题方式")),
                "_原子知识ID": f"{topic_id}-LEGACY-{index}",
            }
        )
    return rows


def _legacy_candidate_merge_eligible(
    legacy: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    if _text(legacy.get("review_status")) == "published":
        return True
    admission_status = _text(candidate.get("聚类准入状态"))
    if admission_status not in {
        "已自动放行",
        "admitted",
        "auto_admitted",
    }:
        return False
    try:
        confidence = float(candidate.get("聚类准入置信度"))
    except (TypeError, ValueError):
        return False
    return confidence >= MANUAL_REVIEW_THRESHOLD


class TopicRegistry:
    def __init__(self, audit_store: AuditStore) -> None:
        self.audit_store = audit_store
        self._topic_cache: dict[
            tuple[str, str],
            list[dict[str, Any]],
        ] = {}
        self._bootstrap_existing_candidates()

    def _topics_for_scope(
        self,
        business_line: str,
        product_category: str,
    ) -> list[dict[str, Any]]:
        scope = (business_line, product_category)
        if scope not in self._topic_cache:
            self._topic_cache[scope] = (
                self.audit_store.list_registered_topics(
                    business_line,
                    product_category,
                )
            )
        return self._topic_cache[scope]

    def _bootstrap_existing_candidates(self) -> None:
        for legacy in self.audit_store.list_unregistered_topic_candidates():
            topic_id = _text(legacy.get("topic_id"))
            candidate = legacy.get("candidate")
            if not topic_id or not isinstance(candidate, dict):
                continue
            if not _legacy_candidate_merge_eligible(legacy, candidate):
                continue
            rows = _legacy_candidate_rows(topic_id, candidate)
            if not rows:
                continue
            try:
                business_line = _business_line(rows)
                product_category = _product_category(rows)
            except ValueError:
                continue
            raw_key = _text(candidate.get("主题聚类键"))
            topic_key = tuple(
                part.strip()
                for part in raw_key.split("|")
                if part.strip()
            ) or (
                "legacy",
                business_line,
                product_category,
                topic_id,
            )
            self.audit_store.integrate_registered_topic(
                topic_id=topic_id,
                proposed_topic_id=topic_id,
                business_line=business_line,
                product_category=product_category,
                topic_key=topic_key,
                signature=_topic_signature(rows),
                representative=dict(rows[0]),
                members=[_membership(row) for row in rows],
                run_id=_text(legacy.get("run_id")) or "legacy-bootstrap",
                decision="bootstrapped_existing_candidate",
                confidence=1.0,
                reason="从升级前审计候选的来源事实证据包建立历史主题索引",
            )

    def integrate(
        self,
        *,
        proposed_topic_id: str,
        topic_key: tuple[str, ...],
        rows: list[dict[str, Any]],
        run_id: str,
    ) -> TopicResolution:
        with _TOPIC_REGISTRY_WRITE_LOCK:
            return self._integrate(
                proposed_topic_id=proposed_topic_id,
                topic_key=topic_key,
                rows=rows,
                run_id=run_id,
            )

    def _integrate(
        self,
        *,
        proposed_topic_id: str,
        topic_key: tuple[str, ...],
        rows: list[dict[str, Any]],
        run_id: str,
    ) -> TopicResolution:
        if not rows:
            raise ValueError("增量主题不能没有来源证据")
        business_line = _business_line(rows)
        product_category = _product_category(rows)
        signature = _topic_signature(rows)
        _validate_internal_signature(signature)
        members = [_membership(row) for row in rows]
        historical_topics = self._topics_for_scope(
            business_line,
            product_category,
        )

        scored = sorted(
            (
                (
                    *_signature_similarity(
                        signature,
                        historical["signature"],
                    ),
                    historical,
                )
                for historical in historical_topics
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        best = scored[0] if scored else None
        second_best = scored[1] if len(scored) > 1 else None
        hard_conflict_reason = ""
        if best and float(best[0]) >= MANUAL_REVIEW_THRESHOLD:
            best_topic_id = _text(best[2].get("topic_id"))
            if _same_work_order_atomic_conflict(
                members,
                self.audit_store.list_registered_topic_member_identities(
                    best_topic_id
                ),
            ):
                hard_conflict_reason = (
                    "同一原始工单拆出的不同原子问题不能重新合并"
                )
        ambiguous_high_match = bool(
            best
            and second_best
            and not hard_conflict_reason
            and float(best[0]) >= AUTO_MERGE_THRESHOLD
            and float(second_best[0]) >= MANUAL_REVIEW_THRESHOLD
            and float(best[0]) - float(second_best[0])
            < AMBIGUOUS_MATCH_MARGIN
        )
        matched_existing = bool(
            best
            and float(best[0]) >= AUTO_MERGE_THRESHOLD
            and not hard_conflict_reason
            and not ambiguous_high_match
        )
        if matched_existing and best is not None:
            confidence, reason, historical = best
            topic_id = _text(historical["topic_id"])
            resolved_topic_key = tuple(
                _text(item)
                for item in historical.get("topic_key") or topic_key
            )
            decision = "merged_existing_topic"
            requires_review = False
        elif best and not hard_conflict_reason and (
            float(best[0]) >= MANUAL_REVIEW_THRESHOLD
            or ambiguous_high_match
        ):
            confidence, reason, historical = best
            topic_id = proposed_topic_id
            resolved_topic_key = topic_key
            decision = "historical_topic_review_required"
            requires_review = True
            if ambiguous_high_match:
                reason = (
                    "命中多个接近的历史主题，不能自动选择；"
                    + reason
                )
        else:
            confidence = (
                0.0
                if hard_conflict_reason
                else float(best[0])
                if best
                else 1.0
            )
            reason = (
                hard_conflict_reason
                or _text(best[1])
                if best
                else "当前业务层级和产品品类下没有历史主题"
            )
            historical = None
            topic_id = _collision_safe_topic_id(
                proposed_topic_id,
                historical_topics,
                rows,
            )
            resolved_topic_key = topic_key
            decision = "created_new_topic"
            requires_review = False

        if requires_review:
            self.audit_store.record_topic_merge_event(
                run_id=run_id,
                proposed_topic_id=proposed_topic_id,
                resolved_topic_id=_text(
                    historical["topic_id"]
                    if historical is not None
                    else ""
                ),
                decision=decision,
                confidence=float(confidence),
                reason=reason,
            )
            return TopicResolution(
                topic_id=topic_id,
                topic_key=resolved_topic_key,
                rows=[dict(row) for row in rows],
                matched_existing=False,
                historical_topic_id=_text(
                    historical["topic_id"]
                    if historical is not None
                    else ""
                ),
                requires_review=True,
                decision=decision,
                confidence=round(float(confidence), 4),
                reason=reason,
                evidence_version=0,
                added_member_count=0,
                duplicate_member_count=0,
            )

        stored_signature = (
            _merge_signatures(
                historical["signature"],
                signature,
            )
            if matched_existing and historical is not None
            else signature
        )
        stored_representative = (
            historical["representative"]
            if matched_existing and historical is not None
            else dict(rows[0])
        )
        stored = self.audit_store.integrate_registered_topic(
            topic_id=topic_id,
            proposed_topic_id=proposed_topic_id,
            business_line=business_line,
            product_category=product_category,
            topic_key=topic_key,
            signature=stored_signature,
            representative=stored_representative,
            members=members,
            run_id=run_id,
            decision=decision,
            confidence=float(confidence),
            reason=reason,
        )
        cached_topic = {
            "topic_id": stored["topic_id"],
            "business_line": business_line,
            "product_category": product_category,
            "topic_key": list(resolved_topic_key),
            "signature": stored_signature,
            "representative": stored_representative,
            "status": "active",
            "evidence_version": int(stored["evidence_version"]),
            "member_count": int(stored["member_count"]),
        }
        cached_index = next(
            (
                index
                for index, topic in enumerate(historical_topics)
                if _text(topic.get("topic_id")) == stored["topic_id"]
            ),
            None,
        )
        if cached_index is None:
            historical_topics.append(cached_topic)
        else:
            historical_topics[cached_index] = cached_topic
        return TopicResolution(
            topic_id=stored["topic_id"],
            topic_key=resolved_topic_key,
            rows=stored["rows"],
            matched_existing=matched_existing,
            historical_topic_id=(
                stored["topic_id"] if matched_existing else ""
            ),
            requires_review=False,
            decision=decision,
            confidence=round(float(confidence), 4),
            reason=reason,
            evidence_version=int(stored["evidence_version"]),
            added_member_count=int(stored["added_member_count"]),
            duplicate_member_count=int(stored["duplicate_member_count"]),
        )
