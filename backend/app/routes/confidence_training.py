import csv
import asyncio
import hashlib
import io
import json
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import (
    Boolean,
    Float,
    Numeric,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, load_only

from app.core.database import SessionLocal, get_db
from app.models.integration import ConfidenceTrainingJob, IntegrationIngestion
from app.models.user import User
from app.routes.auth import require_permission
from app.schemas.confidence_training import (
    ConfidenceRegressionReviewUpdate,
    ConfidenceTrainingItem,
    ConfidenceTrainingJob as ConfidenceTrainingJobSchema,
    ConfidenceTrainingOverview,
    ConfidencePromptDecision,
    ConfidenceTrainingSettings,
    ConfidenceTrainingSettingsUpdate,
    ConfidenceTrainingUpdate,
    ModelConnectionConfig,
    ModelConnectionTest,
    ModelConnectionUpdate,
)
from app.services.confidence_training import (
    EVALUATION_SCOPE,
    MODEL_NAME,
    aggregate,
    item_payload,
    settings_snapshot,
    update_item,
    update_settings,
)
from app.services.model_annotation import (
    ModelAnnotationError,
    analyze_draft_generation_prompt,
    analyze_confidence_training_prompt,
    correct_with_deepseek_flash,
    revise_confidence_training_prompt,
    shadow_rerun_transcribed_candidate,
    test_deepseek_flash_connection,
)
from app.services.model_connection import (
    ModelConnectionError,
    model_config_for_test,
    model_config_snapshot,
    update_model_config,
)
from app.services.active_prompt import get_active_confidence_prompt as resolve_active_confidence_prompt


router = APIRouter(prefix="/confidence-training", tags=["置信度训练"])

CONFIDENCE_PROMPT_SAMPLE_LIMIT = 200
CONFIDENCE_PROMPT_DISCOVERY_COUNT = 100
CONFIDENCE_PROMPT_VALIDATION_COUNT = 50
CONFIDENCE_PROMPT_TEST_COUNT = 50
CONFIDENCE_PROMPT_REPRESENTATIVE_CASE_LIMIT = 12


# 置信度训练只会读取下面这几列：selection_metadata / processing_metadata /
# source_message_ids 不参与任何 payload 构造，用 load_only 排除后可以省掉大量
# 无用 JSON 列的传输与 ORM 实体化开销。
_ROW_COLUMNS = (
    IntegrationIngestion.id,
    IntegrationIngestion.event_id,
    IntegrationIngestion.review_status,
    IntegrationIngestion.candidate_payload,
    IntegrationIngestion.review_metadata,
)

_BASE_FILTERS = (
    IntegrationIngestion.review_status.isnot(None),
    IntegrationIngestion.source_system != "excel",
)

# Python float() 能接受的数字字面量；jsonb 里的数值先转成文本再用它做守卫，
# 避免把 "abc" / 对象 / 数组直接 CAST 成 float 时报错。
_NUMERIC_TEXT = r"^[[:space:]]*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?[[:space:]]*$"


def _rows(db: Session) -> list[IntegrationIngestion]:
    return (
        db.query(IntegrationIngestion)
        .options(load_only(*_ROW_COLUMNS))
        .filter(*_BASE_FILTERS)
        .order_by(IntegrationIngestion.created_at.desc())
        .all()
    )


def _jsonb_text(meta, *path):
    """按路径取 review_metadata 里的文本值（键缺失或 JSON null 都得到 SQL NULL）。

    路径中间层不是对象时（Python 里 _model_review / _human_review /
    _review_metadata 会把它折成空 dict）同样得到 NULL；这里用到的都是非数字键，
    不会误命中 JSON 数组下标，所以与 Python 的取值结果一致。
    """
    return func.jsonb_extract_path_text(meta, *path)


class _ItemFacts:
    """把 item_payload() 用到的派生字段翻译成 SQL 表达式。

    aggregate() 只读取 _FACT_KEYS 这 8 个键，所以 /overview 可以只在 SQL 里按这
    些字段分组计数，不必把 19341 行（含 8KB/行的 candidate_payload）搬进 Python。

    每个 _build_* 都逐条对齐 app.services.confidence_training 的 Python 语义。
    这里只读 review_metadata：其中 model_review / human_review 与 candidate_payload
    里的同名子对象逐行完全一致（已对全部 19341 行做过 jsonb 文本比对），
    而 confidence_training 本来就只来自 review_metadata。
    """

    _FACT_KEYS = (
        "confidence_band",
        "truth_status",
        "strict_correct",
        "acceptable_correct",
        "error_severity",
        "dataset_split",
        "human_label",
        "training_candidate_status",
    )

    def __init__(self, meta):
        self._meta = meta
        self._cache: dict[str, Any] = {}

    def fact(self, key: str):
        if key not in self._cache:
            self._cache[key] = getattr(self, "_build_%s" % key)()
        return self._cache[key]

    def facts(self, keys) -> list:
        return [self.fact(key) for key in keys]

    def _derived_not_evaluable(self):
        """_derived_evaluation() 里 not_evaluable 的短路条件。"""
        label = self.fact("human_label")
        return or_(
            label.is_(None),
            and_(label == "worthy", self.fact("human_draft_disposition").is_(None)),
            self.fact("model_label").is_(None),
        )

    def _derived_same_value(self):
        return cast(self.fact("human_label") == self.fact("model_label"), Boolean)

    def _derived_same_draft(self):
        return case(
            (self.fact("human_label") == "unworthy", literal(True)),
            (self.fact("model_draft_disposition").is_(None), literal(True)),
            else_=cast(
                self.fact("model_draft_disposition") == self.fact("human_draft_disposition"),
                Boolean,
            ),
        )

    def _bool_override(self, key: str, derived):
        """item_payload 里 `evaluation.get(key, derived)` 的语义：键存在就用原值。"""
        evaluation = func.jsonb_extract_path(self._meta, "confidence_training")
        exists = func.jsonb_exists(evaluation, key)
        raw = cast(func.jsonb_extract_path(self._meta, "confidence_training", key), Boolean)
        return case((exists, raw), else_=derived)

    def _text(self, *path):
        return _jsonb_text(self._meta, *path)

    def _build_model_label(self):
        # model.get("knowledge_value") or model.get("decision")，再 str().strip() or None；
        # 两者都取不到时 Python 得到 str(None) == "None"，所以兜底值就是字面量 "None"。
        value = func.coalesce(
            func.nullif(self._text("model_review", "knowledge_value"), literal("")),
            func.nullif(self._text("model_review", "decision"), literal("")),
            literal("None"),
        )
        return func.nullif(func.btrim(value), literal(""))

    def _build_human_label(self):
        value = func.btrim(self._text("human_review", "knowledge_value"))
        return case(
            (value == "worthy", value),
            (value == "unworthy", value),
            else_=None,
        )

    def _build_human_draft_disposition(self):
        value = func.btrim(
            func.coalesce(self._text("human_review", "draft_disposition"), literal(""))
        )
        return case(
            (self.fact("human_label") == "unworthy", literal("not_applicable")),
            (value == "approved", literal("approved")),
            (value == "revision_required", literal("revision_required")),
            (value == "hold_for_evidence", literal("hold_for_evidence")),
            else_=None,
        )

    def _build_model_draft_disposition(self):
        action = func.btrim(
            func.coalesce(self._text("model_review", "suggested_action"), literal(""))
        )
        quality = func.btrim(
            func.coalesce(self._text("model_review", "draft_quality"), literal(""))
        )
        return case(
            (action == "submit_for_human_review", literal("approved")),
            (action == "return_for_revision", literal("revision_required")),
            (action == "hold_for_evidence", literal("hold_for_evidence")),
            (quality == "合格", literal("approved")),
            (quality == "需修改", literal("revision_required")),
            (quality == "待补证据", literal("hold_for_evidence")),
            else_=None,
        )

    def _build_confidence_band(self):
        text_value = self._text("model_review", "confidence")
        # 先用 numeric 判断是否落在 [0,1]（numeric 不会像 float8 那样在 1e400 上溢出），
        # 确认落在范围内才转 float8 —— 这样与 Python 的 double 比较结果完全一致。
        numeric = case(
            (text_value.op("~")(_NUMERIC_TEXT), cast(text_value, Numeric)),
            else_=None,
        )
        in_range = case((numeric.between(0, 1), cast(text_value, Float)), else_=None)
        derived = case(
            (in_range < 0.70, literal("B0")),
            (in_range < 0.85, literal("B1")),
            (in_range < 0.95, literal("B2")),
            (in_range.isnot(None), literal("B3")),
            else_=None,
        )
        override = func.nullif(self._text("confidence_training", "confidence_band"), literal(""))
        return func.coalesce(override, derived)

    def _build_truth_status(self):
        label = self.fact("human_label")
        confirmed = and_(
            label.isnot(None),
            or_(label == "unworthy", self.fact("human_draft_disposition").isnot(None)),
        )
        return case((confirmed, literal("confirmed")), else_=literal("pending"))

    def _build_training_candidate_status(self):
        eligible = func.btrim(
            func.coalesce(self._text("human_review", "training_eligible"), literal(""))
        )
        recommended = case(
            (
                and_(eligible == "是", self.fact("truth_status") == "confirmed"),
                literal("recommended"),
            ),
            else_=literal("not_recommended"),
        )
        override = func.nullif(
            self._text("confidence_training", "training_candidate_status"), literal("")
        )
        return func.coalesce(override, recommended)

    def _build_error_severity(self):
        # item_payload 直接透传 evaluation.get("error_severity")，没有 or 兜底，
        # 所以这里也不能用 nullif 把空字符串折成 NULL。
        return self._text("confidence_training", "error_severity")

    def _build_dataset_split(self):
        return self._text("confidence_training", "dataset_split")

    def _build_strict_correct(self):
        derived = case(
            (self._derived_not_evaluable(), None),
            else_=cast(and_(self._derived_same_value(), self._derived_same_draft()), Boolean),
        )
        return self._bool_override("strict_correct", derived)

    def _build_acceptable_correct(self):
        derived = case(
            (self._derived_not_evaluable(), None),
            else_=self._derived_same_value(),
        )
        return self._bool_override("acceptable_correct", derived)


def _item_source(*, with_event_id: bool = False):
    """派生字段需要的最小列集合：id / created_at / review_metadata（转成 jsonb）。

    review_metadata 是 json 类型，`->` / `->>` 每次都会重新解析整个文档，
    所以在这一层先 CAST 成 jsonb，让上层所有 jsonb 表达式只解析一次。
    末尾的 LIMIT 只是为了不让 PostgreSQL 把这个子查询内联进外层（一旦内联，
    CAST 会被复制进每个 jsonb 表达式，19341 行上会从 0.5s 涨到 6s）。
    """
    columns = [
        IntegrationIngestion.id.label("id"),
        IntegrationIngestion.created_at.label("created_at"),
    ]
    if with_event_id:
        columns.append(IntegrationIngestion.event_id.label("event_id"))
    columns.append(cast(IntegrationIngestion.review_metadata, JSONB).label("meta"))
    return (
        select(*columns)
        .where(*_BASE_FILTERS)
        .limit(2**31 - 1)
        .subquery("confidence_meta")
    )


def _fact_source(*, with_event_id: bool = False, fact_keys: tuple[str, ...] = ()):
    """在 _item_source 之上再套一层，把派生字段算成普通列。

    上层只要引用这些列，SQLAlchemy 就不会把一个派生字段的整套 CASE 表达式
    重复展开（置信度分档的表达式里同一个 jsonb 取值和正则各出现 4 次，
    重复展开后 19341 行上要多花约 0.1s）。这一层的 LIMIT 同样是防止
    PostgreSQL 把子查询拉平。
    """
    base = _item_source(with_event_id=with_event_id)
    facts = _ItemFacts(base.c.meta)
    columns = [base.c.id, base.c.created_at]
    if with_event_id:
        columns.append(base.c.event_id)
    columns.extend(facts.fact(key).label(key) for key in fact_keys)
    return select(*columns).limit(2**31 - 1).subquery("confidence_facts")


def _aggregate_overview(db: Session) -> tuple[dict[str, Any], list]:
    """在 SQL 里按派生字段分组计数，再交给 aggregate() 汇总。

    改造前是把 19341 行全部实体化（含 8KB/行的 candidate_payload）再逐行调用
    item_payload()，占了接口 3.7s 里的绝大部分；aggregate() 只依赖这 8 个字段的
    取值组合，所以按它们分组计数后的结果与逐行计算完全等价。
    """
    keys = _ItemFacts._FACT_KEYS
    source = _fact_source(fact_keys=keys)
    columns = [source.c[key] for key in keys]
    query = select(*columns, func.count().label("row_count")).group_by(*columns)
    items: list[dict[str, Any]] = []
    for row in db.execute(query).all():
        template = {key: row[index] for index, key in enumerate(keys)}
        # aggregate() 只读这些键，同一个 dict 复用不会互相污染。
        items.extend([template] * row[-1])
    return aggregate(items)


def _fetch_recent_items(db: Session, batch_id: str, limit: int) -> list[dict[str, Any]]:
    """按 created_at 倒序取前 limit 条；不做任何 JSON 派生字段过滤。

    LIMIT 下推到 SQL 与改造前的 `items[:limit]` 等价：created_at 全表无重复、
    无 NULL，ORDER BY created_at DESC 是全序，所以截断位置唯一。
    """
    query = db.query(*_ROW_COLUMNS).filter(*_BASE_FILTERS)
    if batch_id:
        query = query.filter(func.strpos(IntegrationIngestion.event_id, batch_id) > 0)
    rows = query.order_by(IntegrationIngestion.created_at.desc()).limit(limit).all()
    return [item_payload(row) for row in rows]


def _fetch_items_by_id(db: Session, item_ids: list[str]) -> list[dict[str, Any]]:
    """按给定 id 取回整行并构造 payload，返回顺序与 item_ids 一致。"""
    if not item_ids:
        return []
    rows = {
        row.id: row
        for row in db.query(*_ROW_COLUMNS).filter(IntegrationIngestion.id.in_(item_ids)).all()
    }
    return [item_payload(rows[item_id]) for item_id in item_ids if item_id in rows]


def _filtered_item_ids(
    db: Session,
    *,
    batch_id: str,
    confidence_band: str,
    annotation_status: str,
    training_status: str,
    limit: int,
) -> list[str]:
    """把派生字段过滤下推到 SQL，只取需要的前 limit 个 id。"""
    needed: list[str] = []
    if confidence_band:
        needed.append("confidence_band")
    if annotation_status:
        needed.append("human_label")
    if training_status:
        needed.append("training_candidate_status")
    source = _fact_source(with_event_id=True, fact_keys=tuple(needed))
    conditions = []
    if batch_id:
        # 与 Python 的 `batch_id in item["event_id"]` 等价
        conditions.append(func.strpos(source.c.event_id, batch_id) > 0)
    if confidence_band:
        conditions.append(source.c.confidence_band == confidence_band)
    if annotation_status == "annotated":
        conditions.append(source.c.human_label.isnot(None))
    elif annotation_status == "pending":
        conditions.append(source.c.human_label.is_(None))
    if training_status:
        conditions.append(source.c.training_candidate_status == training_status)
    query = select(source.c.id)
    if conditions:
        query = query.where(*conditions)
    query = query.order_by(source.c.created_at.desc()).limit(limit)
    return list(db.execute(query).scalars().all())


def _training_job_payload(job: ConfidenceTrainingJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "model_name": job.model_name,
        "evaluation_scope": job.evaluation_scope,
        "dataset_hash": job.dataset_hash,
        "sample_count": job.sample_count,
        "train_count": job.train_count,
        "validation_count": job.validation_count,
        "test_count": job.test_count,
        "requested_by": job.requested_by,
        "error_message": job.error_message,
        "analysis_result": dict(job.analysis_result or {}),
        "candidate_prompt": job.candidate_prompt,
        "resolved_model_version": job.resolved_model_version,
    "shadow_evaluation": dict(job.shadow_evaluation or {}),
        "regression_reviews": dict(job.regression_reviews or {}),
        "prompt_versions": list(job.prompt_versions or []),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@router.get("/active-prompt")
def get_active_confidence_prompt_endpoint(
    _: User = Depends(require_permission("knowledge:submit")),
):
    prompt, version = resolve_active_confidence_prompt()
    return {"prompt_version": version, "prompt": prompt, "source": "manual_activation" if version != CURRENT_REVIEW_PROMPT_VERSION else "default"}


@router.post("/jobs/{job_id}/decision", response_model=ConfidenceTrainingJobSchema)
def decide_confidence_prompt(
    job_id: str,
    body: ConfidencePromptDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    decision = body.decision
    if decision not in {"accept", "reject", "defer"}:
        raise HTTPException(422, "人工决定必须是 accept、reject 或 defer。")
    comparison = dict((job.shadow_evaluation or {}).get("comparison") or {})
    if decision == "accept":
        ok, reason = _adoption_preconditions(job)
        if not ok:
            raise HTTPException(409, reason)
    versions = list(job.prompt_versions or [])
    if decision == "accept":
        for version in versions:
            if version.get("status") == "active":
                version["status"] = "superseded"
        active_version = "v" + str(len(versions) + 1) + "-active"
        versions.append({
            "version": active_version,
            "prompt": job.candidate_prompt,
            "status": "active",
            "source": "human_decision",
            "decision": decision,
            "decided_by": current_user.username,
            "decided_at": datetime.utcnow().isoformat(),
            "evaluation_summary": comparison,
        })
        job.status = "prompt_accepted"
        job.stage = f"{active_version} 已由人工采纳，后续模型标注将使用该 Prompt"
        job.error_message = "人工采纳完成；旧 Prompt 保留为 superseded 历史版本，可在后续增加回滚操作。"
    else:
        for version in versions:
            if version.get("status") in {"candidate", "validation_pending", "diagnostic_pending"}:
                version["status"] = "rejected" if decision == "reject" else "deferred"
        job.status = "prompt_rejected" if decision == "reject" else "prompt_decision_deferred"
        job.stage = "人工决定：" + ("不采纳候选 Prompt" if decision == "reject" else "暂缓采纳候选 Prompt")
        job.error_message = f"人工决定={decision}；候选 Prompt 未切换为 active。"
    job.prompt_versions = versions
    job.analysis_result = {**dict(job.analysis_result or {}), "human_decision": {"decision": decision, "decided_by": current_user.username, "decided_at": datetime.utcnow().isoformat()}}
    db.add(job)
    db.commit()
    db.refresh(job)
    return _training_job_payload(job)


def _stable_sample_rank(row_id: str) -> str:
    return hashlib.sha256(f"confidence-prompt-sample-v1:{row_id}".encode("utf-8")).hexdigest()


def _stratified_training_rows(
    rows_with_items: list[tuple[IntegrationIngestion, dict[str, Any]]],
) -> list[tuple[IntegrationIngestion, dict[str, Any]]]:
    strata: dict[tuple[bool, str, str], list[tuple[IntegrationIngestion, dict[str, Any]]]] = {}
    for row, item in rows_with_items:
        mismatch = (
            item.get("model_label") != item.get("human_label")
            or item.get("model_draft_disposition") != item.get("human_draft_disposition")
        )
        key = (
            mismatch,
            str(item.get("human_label") or "unknown"),
            str(item.get("confidence_band") or "unscored"),
        )
        strata.setdefault(key, []).append((row, item))
    for group in strata.values():
        group.sort(key=lambda pair: _stable_sample_rank(str(pair[0].id)))

    selected: list[tuple[IntegrationIngestion, dict[str, Any]]] = []
    ordered_keys = sorted(strata, key=lambda key: (not key[0], key[1], key[2]))
    while len(selected) < CONFIDENCE_PROMPT_SAMPLE_LIMIT:
        added = False
        for key in ordered_keys:
            group = strata[key]
            if group:
                selected.append(group.pop(0))
                added = True
                if len(selected) >= CONFIDENCE_PROMPT_SAMPLE_LIMIT:
                    break
        if not added:
            break
    return selected


def _dataset_split_counts(total: int) -> dict[str, int]:
    if total >= CONFIDENCE_PROMPT_SAMPLE_LIMIT:
        return {
            "train": CONFIDENCE_PROMPT_DISCOVERY_COUNT,
            "validation": CONFIDENCE_PROMPT_VALIDATION_COUNT,
            "test": CONFIDENCE_PROMPT_TEST_COUNT,
        }
    if total < 3:
        return {"train": total, "validation": 0, "test": 0}
    validation_count = max(1, total // 4)
    test_count = max(1, total // 4)
    return {
        "train": total - validation_count - test_count,
        "validation": validation_count,
        "test": test_count,
    }


def _manual_training_dataset(rows: list[IntegrationIngestion]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    confirmed_rows = []
    for row in rows:
        item = item_payload(row)
        if item["truth_status"] == "confirmed":
            confirmed_rows.append((row, item))
    selected_rows = _stratified_training_rows(confirmed_rows)
    dataset: list[dict[str, Any]] = []
    counts = _dataset_split_counts(len(selected_rows))
    train_count = counts["train"]
    validation_count = counts["validation"]
    for index, (row, item) in enumerate(selected_rows):
        knowledge = dict((row.candidate_payload or {}).get("knowledge") or {})
        split = (
            "train" if index < train_count
            else ("validation" if index < train_count + validation_count else "test")
        )
        dataset.append({
            "id": row.id,
            "split": split,
            "title": str(knowledge.get("title") or item["title"]),
            "content": knowledge.get("content"),
            "recommended_reply": knowledge.get("recommended_reply"),
            "model_label": item["model_label"],
            "human_label": item["human_label"],
            "model_draft_disposition": item["model_draft_disposition"],
            "human_draft_disposition": item["human_draft_disposition"],
            **_shadow_sample_completeness({
                "content": knowledge.get("content"),
                "recommended_reply": knowledge.get("recommended_reply"),
                "evidence_excerpt": knowledge.get("evidence_excerpt"),
            }),
            "evaluation_scope": EVALUATION_SCOPE,
            "not_for_weight_training": True,
        })
    return dataset, counts


def _prompt_optimization_snapshot(dataset: list[dict[str, Any]]) -> dict[str, Any]:
    def text_excerpt(value: Any, limit: int) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return str(text).strip()[:limit]

    mismatches = [
        item for item in dataset
        if item.get("model_label") != item.get("human_label")
        or item.get("model_draft_disposition") != item.get("human_draft_disposition")
    ]
    # Keep the first real analysis bounded: error-first samples are more useful
    # than sending an oversized snapshot that can time out at the internal API.
    selected = (
        mismatches + [item for item in dataset if item not in mismatches]
    )[:CONFIDENCE_PROMPT_REPRESENTATIVE_CASE_LIMIT]
    return {
        "evaluation_scope": EVALUATION_SCOPE,
        "current_prompt_version": "post-transcription-value-and-content-review-v10",
        "current_policy": {
            "knowledge_value": ["worthy", "unworthy", "pending"],
            "draft_disposition": ["approved", "revision_required", "hold_for_evidence"],
            "rule": "沉淀价值和草稿处理必须分别判断。",
        },
        "population": {
            "total_confirmed_truth": len(dataset),
            "model_human_value_mismatches": sum(item.get("model_label") != item.get("human_label") for item in dataset),
            "model_human_draft_mismatches": sum(item.get("model_draft_disposition") != item.get("human_draft_disposition") for item in dataset),
        },
        "representative_cases": [
            {
                "id": item["id"],
                "title": item["title"],
                "content_excerpt": text_excerpt(item.get("content"), 360),
                "recommended_reply_excerpt": text_excerpt(item.get("recommended_reply"), 180),
                "model_label": item.get("model_label"),
                "human_label": item.get("human_label"),
                "model_draft_disposition": item.get("model_draft_disposition"),
                "human_draft_disposition": item.get("human_draft_disposition"),
            }
            for item in selected
        ],
    }


CURRENT_REVIEW_PROMPT_VERSION = "post-transcription-value-and-content-review-v10"
CURRENT_REVIEW_PROMPT = """你是知识转写草稿审核标注器。必须先阅读主题、来源证据、知识草稿和推荐回复，再判断是否值得沉淀；不能只凭标题或聚类标签判断。

只返回 JSON 对象，字段包括：knowledge_value、draft_disposition、reason、evidence_used、limitations、confidence。
knowledge_value 只能是 worthy、unworthy、pending。
draft_disposition 只能是 approved、revision_required、hold_for_evidence、not_applicable。
若 knowledge_value=unworthy，draft_disposition 必须为 not_applicable；若 knowledge_value=pending，draft_disposition 必须为 hold_for_evidence；approved 或 revision_required 只能对应 worthy。"""


def _has_meaningful_shadow_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_meaningful_shadow_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_meaningful_shadow_value(item) for item in value)
    return True


def _shadow_sample_completeness(sample: dict[str, Any]) -> dict[str, Any]:
    fields = ("content", "recommended_reply", "evidence_excerpt")
    missing_fields = [
        field for field in fields
        if not _has_meaningful_shadow_value(sample.get(field))
    ]
    evaluable = len(missing_fields) < len(fields)
    return {
        "evaluation_eligible": evaluable,
        "input_completeness": (
            "complete" if not missing_fields
            else ("partial" if evaluable else "not_evaluable")
        ),
        "missing_fields": missing_fields,
        "not_evaluable_reason": (
            "缺少知识正文、推荐回复和来源证据，无法进行新旧 Prompt 对比。"
            if not evaluable else ""
        ),
    }


def _shadow_candidate(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": sample.get("title"),
        "content": sample.get("content"),
        "recommended_reply": sample.get("recommended_reply"),
        "evidence_excerpt": sample.get("evidence_excerpt"),
        "input_completeness": sample.get("input_completeness"),
        "missing_fields": list(sample.get("missing_fields") or []),
    }


def _shadow_value(result: dict[str, Any]) -> str | None:
    value = str(result.get("knowledge_value") or result.get("decision") or "").strip()
    return value if value in {"worthy", "unworthy", "pending"} else None


def _shadow_draft(result: dict[str, Any]) -> str | None:
    value = str(result.get("draft_disposition") or "").strip()
    if value in {"approved", "revision_required", "hold_for_evidence", "not_applicable"}:
        return value
    action = str(result.get("suggested_action") or "").strip()
    return {
        "submit_for_human_review": "approved",
        "return_for_revision": "revision_required",
        "hold_for_evidence": "hold_for_evidence",
        "discard": "not_applicable",
    }.get(action)


def _wilson_lower_bound(correct: int, total: int) -> float | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = correct / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5
    return round(max(0.0, (centre - spread) / denominator), 4)


def _shadow_metrics(rows: list[dict[str, Any]], result_key: str) -> dict[str, Any]:
    total = len(rows)
    value_correct = sum(row[result_key]["value_correct"] for row in rows)
    draft_correct = sum(row[result_key]["draft_correct"] for row in rows)
    strict_correct = sum(row[result_key]["strict_correct"] for row in rows)
    return {
        "n": total,
        "value_accuracy": round(value_correct / total, 4) if total else None,
        "draft_accuracy": round(draft_correct / total, 4) if total else None,
        "strict_accuracy": round(strict_correct / total, 4) if total else None,
        "strict_accuracy_ci95_lower": _wilson_lower_bound(strict_correct, total),
        "value_correct_count": value_correct,
        "draft_correct_count": draft_correct,
        "strict_correct_count": strict_correct,
    }


def _shadow_comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = _shadow_metrics(rows, "baseline")
    candidate = _shadow_metrics(rows, "candidate")
    improved = sum(
        not row["baseline"]["strict_correct"] and row["candidate"]["strict_correct"]
        for row in rows
    )
    regressed = sum(
        row["baseline"]["strict_correct"] and not row["candidate"]["strict_correct"]
        for row in rows
    )
    delta = round((candidate["strict_accuracy"] or 0) - (baseline["strict_accuracy"] or 0), 4)
    gate_passed = (
        delta > 0
        and candidate["strict_accuracy_ci95_lower"] is not None
        and candidate["strict_accuracy_ci95_lower"] >= (baseline["strict_accuracy_ci95_lower"] or 0)
    )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "strict_accuracy_delta": delta,
        "improved_count": improved,
        "regressed_count": regressed,
        "gate_passed": gate_passed,
        "regression_review_required": regressed > 0,
        "recommended_action": (
            (
                f"总体指标改善；有 {regressed} 条退化，需人工分析后决定是否采用"
                if regressed
                else "总体指标改善，可由人工决定是否进入下一阶段"
            ) if gate_passed
            else "保持当前 Prompt；人工审核错例后继续修改候选 Prompt"
        ),
    }


def _draft_prompt_training_dataset(rows: list[IntegrationIngestion]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    samples: list[dict[str, Any]] = []
    for row in rows:
        metadata = dict(row.review_metadata or {})
        history = list(metadata.get("draft_revision_history") or [])
        payload = dict(row.candidate_payload or {})
        human_review = dict(payload.get("human_review") or metadata.get("human_review") or {})
        if not history or human_review.get("knowledge_value") != "worthy":
            continue
        first_revision = dict(history[0] or {})
        before = dict(first_revision.get("before") or {})
        knowledge = dict(payload.get("knowledge") or {})
        after = {
            "title": str(knowledge.get("title") or ""),
            "content": knowledge.get("content") or {"blocks": []},
            "recommended_reply": str(knowledge.get("recommended_reply") or ""),
        }
        if not before or before == after:
            continue
        samples.append({
            "id": f"{row.id}:human-final-draft",
            "ingestion_id": row.id,
            "source": "human_final_draft",
            "before": before,
            "after": after,
            "evidence_excerpt": str(knowledge.get("evidence_excerpt") or "")[:1200],
            "human_review": human_review,
            "revision_count": len(history),
            "evaluation_scope": EVALUATION_SCOPE,
            "not_for_weight_training": True,
        })
    samples.sort(key=lambda item: _stable_sample_rank(str(item["id"])))
    samples = samples[:CONFIDENCE_PROMPT_SAMPLE_LIMIT]
    counts = _dataset_split_counts(len(samples))
    for index, sample in enumerate(samples):
        sample["split"] = (
            "train" if index < counts["train"]
            else ("validation" if index < counts["train"] + counts["validation"] else "test")
        )
    return samples, counts


def _draft_prompt_optimization_snapshot(dataset: list[dict[str, Any]]) -> dict[str, Any]:
    discovery = [item for item in dataset if item.get("split") == "train"]
    return {
        "optimization_target": "draft_generation",
        "evaluation_scope": EVALUATION_SCOPE,
        "population": {"edited_draft_samples": len(dataset)},
        "representative_edits": discovery[:CONFIDENCE_PROMPT_REPRESENTATIVE_CASE_LIMIT],
        "requirements": [
            "只能依据来源证据生成知识正文与推荐回复。",
            "学习人工真实修改，不得把模型自评当作人工终稿。",
            "候选 Prompt 不自动启用，必须经过离线回放和人工审核。",
        ],
    }


def _shadow_rerun_samples(
    dataset: list[dict[str, Any]], target_split: str,
) -> list[dict[str, Any]]:
    return [sample for sample in dataset if sample.get("split") == target_split]


def _regressed_rows(job: ConfidenceTrainingJob) -> list[dict[str, Any]]:
    return [
        row for row in list((job.shadow_evaluation or {}).get("rows") or [])
        if bool((row.get("baseline") or {}).get("strict_correct"))
        and not bool((row.get("candidate") or {}).get("strict_correct"))
    ]


def _revision_preconditions(job: ConfidenceTrainingJob) -> tuple[bool, str]:
    evaluation = dict(job.shadow_evaluation or {})
    if evaluation.get("status") != "completed":
        return False, "请先完成本轮候选 Prompt 影子复跑。"
    regressed = _regressed_rows(job)
    if not regressed:
        return False, "当前没有可用于修订的已确认退化样本。"
    reviews = dict(job.regression_reviews or {})
    missing = [str(row.get("id")) for row in regressed if not reviews.get(str(row.get("id")))]
    if missing:
        return False, f"还有 {len(missing)} 条退化样本未完成人工归因。"
    truth_correction = [
        item for item in reviews.values()
        if item.get("classification") == "human_truth_correction_required"
    ]
    if truth_correction:
        return False, "存在“人工真值需修正”的退化样本，请先回候选价值复核修正并重新冻结评测。"
    if not any(bool(item.get("keep_as_regression_case")) for item in reviews.values()):
        return False, "至少保留一条退化样本作为回归测试用例。"
    return True, ""


def _adoption_preconditions(job: ConfidenceTrainingJob) -> tuple[bool, str]:
    """采用门禁：只要求本轮影子复跑的验收指标通过。

    退化样本的人工归因是「生成修订候选 Prompt」的前置条件（见
    ``_revision_preconditions``），只有在确实存在退化样本时才作为采用门禁的
    一部分；否则零退化的任务会因为「没有可用于修订的退化样本」被拦住。
    """
    if not job.candidate_prompt:
        return False, "当前任务没有可采纳的候选 Prompt。"
    comparison = dict((job.shadow_evaluation or {}).get("comparison") or {})
    if not comparison.get("gate_passed"):
        return False, "当前任务未通过采用门禁：验收指标未改善或置信下界未达标。"
    if comparison.get("regressed_count"):
        ok, reason = _revision_preconditions(job)
        if not ok:
            return False, "当前任务未通过采用门禁：" + reason
    return True, ""


def _prompt_revision_snapshot(job: ConfidenceTrainingJob) -> dict[str, Any]:
    versions = list(job.prompt_versions or [])
    base = versions[-1] if versions else {
        "version": "v1-candidate",
        "prompt": job.candidate_prompt,
    }
    reviews = dict(job.regression_reviews or {})
    cases = []
    for row in _regressed_rows(job):
        review = dict(reviews.get(str(row.get("id"))) or {})
        if not review.get("keep_as_regression_case"):
            continue
        cases.append({
            "id": row.get("id"),
            "title": row.get("title"),
            "human_truth": {
                "knowledge_value": row.get("human_label"),
                "draft_disposition": row.get("human_draft_disposition"),
            },
            "baseline_output": row.get("baseline"),
            "candidate_output": row.get("candidate"),
            "human_classification": review.get("classification"),
            "human_note": review.get("note"),
        })
    return {
        "evaluation_scope": EVALUATION_SCOPE,
        "base_prompt_version": base.get("version", "v1-candidate"),
        "base_prompt": base.get("prompt", job.candidate_prompt),
        "regression_cases": cases,
        "constraints": [
            "不得修改人工真值，也不得要求系统自动修改人工真值。",
            "不得自动启用候选 Prompt、不得切换生产路由、不得提交模型权重训练。",
            "修订后必须保留 unworthy 与 pending 的安全边界，且不得使回归用例退化。",
        ],
    }


def _run_shadow_rerun(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if not job or not job.candidate_prompt:
            return
        stored_evaluation = dict(job.shadow_evaluation or {})
        phase_results = dict(stored_evaluation.get("phase_results") or {})
        rows: list[dict[str, Any]] = list(stored_evaluation.get("rows") or [])
        skipped_samples: list[dict[str, Any]] = list(stored_evaluation.get("skipped_samples") or [])
        completed_ids = {
            str(row.get("id"))
            for row in [*rows, *skipped_samples]
            if row.get("id")
        }
        target_split = str(stored_evaluation.get("target_split") or "validation")
        samples = _shadow_rerun_samples(list(job.dataset_payload or []), target_split)
        for index, sample in enumerate(samples, start=1):
            if str(sample.get("id")) in completed_ids:
                continue
            completeness = _shadow_sample_completeness(sample)
            if not completeness["evaluation_eligible"]:
                skipped_samples.append({
                    "id": sample.get("id"),
                    "title": sample.get("title"),
                    "split": sample.get("split"),
                    "reason": completeness["not_evaluable_reason"],
                    "missing_fields": completeness["missing_fields"],
                })
                job.shadow_evaluation = {
                    "status": "running",
                    "processed_samples": len(rows) + len(skipped_samples),
                    "evaluated_samples": len(rows),
                    "skipped_sample_count": len(skipped_samples),
                    "total_samples": len(samples),
                    "evaluation_scope": EVALUATION_SCOPE,
                    "target_split": target_split,
                    "phase_results": phase_results,
                    "rows": rows,
                    "skipped_samples": skipped_samples,
                }
                job.stage = (
                    f"影子复跑中：已处理 {len(rows) + len(skipped_samples)}/{len(samples)} 条，"
                    f"其中跳过 {len(skipped_samples)} 条不完整数据"
                )
                db.add(job)
                db.commit()
                continue
            candidate = _shadow_candidate(sample)
            def call_with_retry(*, review_prompt: str, prompt_version: str, label: str) -> dict[str, Any]:
                last_error: ModelAnnotationError | None = None
                for attempt in range(1, 4):
                    try:
                        result = shadow_rerun_transcribed_candidate(
                            review_prompt=review_prompt,
                            candidate=candidate,
                            prompt_version=prompt_version,
                        )
                        # The existing group-model configuration is limited to
                        # one request per second. Keep paired baseline/candidate
                        # calls from immediately tripping the gateway limiter.
                        time.sleep(1.05)
                        return result
                    except ModelAnnotationError as exc:
                        last_error = exc
                        if not exc.retryable or attempt >= 3:
                            raise ModelAnnotationError(
                                f"第 {index} 条 {label} 调用失败（重试 {attempt}/3）：{exc}",
                                f"SHADOW_{label.upper()}_{exc.error_code}",
                                False,
                            ) from exc
                        time.sleep(2 if attempt == 1 else 5)
                raise last_error or ModelAnnotationError("未知模型调用错误。", "UNKNOWN")

            try:
                baseline_result = call_with_retry(
                    review_prompt=CURRENT_REVIEW_PROMPT,
                    prompt_version=CURRENT_REVIEW_PROMPT_VERSION,
                    label="baseline",
                )
                candidate_result = call_with_retry(
                    review_prompt=job.candidate_prompt,
                    prompt_version="candidate-from-" + job.id,
                    label="candidate",
                )
            except ModelAnnotationError as exc:
                job.status = "shadow_rerun_failed"
                job.stage = "候选 Prompt 影子复跑失败"
                job.error_message = f"{exc.error_code}：{exc}"
                job.shadow_evaluation = {
                    **dict(job.shadow_evaluation or {}),
                    "status": "failed",
                    "failed_sample_index": index,
                    "failed_sample_id": sample.get("id"),
                    "error_code": exc.error_code,
                    "error_message": str(exc),
                    "rows": rows,
                    "skipped_samples": skipped_samples,
                }
                db.add(job)
                db.commit()
                return
            truth_value = sample.get("human_label")
            truth_draft = sample.get("human_draft_disposition")
            def evaluation(result: dict[str, Any]) -> dict[str, Any]:
                value = _shadow_value(result)
                draft = _shadow_draft(result)
                value_correct = value == truth_value
                draft_correct = draft == truth_draft
                return {
                    "knowledge_value": value,
                    "draft_disposition": draft,
                    "confidence": result.get("confidence"),
                    "reason": str(result.get("reason") or "")[:1200],
                    "value_correct": value_correct,
                    "draft_correct": draft_correct,
                    "strict_correct": value_correct and draft_correct,
                    "resolved_model_version": result.get("resolved_model_version"),
                }
            rows.append({
                "id": sample.get("id"),
                "title": sample.get("title"),
                "split": sample.get("split"),
                "human_label": truth_value,
                "human_draft_disposition": truth_draft,
                "baseline": evaluation(baseline_result),
                "candidate": evaluation(candidate_result),
            })
            job.shadow_evaluation = {
                "status": "running",
                "processed_samples": len(rows) + len(skipped_samples),
                "evaluated_samples": len(rows),
                "skipped_sample_count": len(skipped_samples),
                "total_samples": len(samples),
                "evaluation_scope": EVALUATION_SCOPE,
                "target_split": target_split,
                "phase_results": phase_results,
                "rows": rows,
                "skipped_samples": skipped_samples,
            }
            job.stage = (
                f"影子复跑中：已处理 {len(rows) + len(skipped_samples)}/{len(samples)} 条，"
                f"其中跳过 {len(skipped_samples)} 条不完整数据"
            )
            db.add(job)
            db.commit()
        comparison = _shadow_comparison(rows)
        job.status = (
            "shadow_validation_review_pending"
            if target_split == "validation"
            else "shadow_test_review_pending"
        )
        job.stage = (
            "验证集新旧 Prompt 复跑完成，等待人工判断是否进入测试集"
            if target_split == "validation"
            else "测试集新旧 Prompt 复跑完成，等待人工决定是否采用"
        )
        job.shadow_evaluation = {
            "status": "completed",
            "evaluation_scope": EVALUATION_SCOPE,
            "target_split": target_split,
            "phase_results": phase_results,
            "baseline_prompt_version": CURRENT_REVIEW_PROMPT_VERSION,
            "candidate_prompt_version": "candidate-from-" + job.id,
            "rows": rows,
            "processed_samples": len(rows) + len(skipped_samples),
            "evaluated_samples": len(rows),
            "skipped_sample_count": len(skipped_samples),
            "skipped_samples": skipped_samples,
            "comparison": comparison,
        }
        job.error_message = (
            "shadow_only：复跑结果仅用于人工验收，不自动启用候选 Prompt，"
            "不改变生产路由或模型权重。"
        )
        db.add(job)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if job:
            job.status = "shadow_rerun_failed"
            job.stage = "候选 Prompt 影子复跑异常中断"
            job.error_message = f"影子复跑异常：{type(exc).__name__}"
            db.add(job)
            db.commit()
    finally:
        db.close()


def recover_confidence_shadow_reruns_after_startup() -> None:
    """Return unfinished local work to the durable queue after a restart."""
    db = SessionLocal()
    try:
        jobs = (
            db.query(ConfidenceTrainingJob)
            .filter(ConfidenceTrainingJob.status == "shadow_rerun_running")
            .all()
        )
        for job in jobs:
            job.status = "shadow_rerun_queued"
            job.stage = "本地服务重启后已恢复排队，将从已完成样本继续"
            evaluation = dict(job.shadow_evaluation or {})
            evaluation["status"] = "queued"
            job.shadow_evaluation = evaluation
            job.error_message = ""
            db.add(job)
        if jobs:
            db.commit()
    finally:
        db.close()


async def run_confidence_shadow_rerun_worker(stop_event: asyncio.Event) -> None:
    """Process one durable shadow rerun at a time without tying it to HTTP."""
    recover_confidence_shadow_reruns_after_startup()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            job = (
                db.query(ConfidenceTrainingJob)
                .filter(ConfidenceTrainingJob.status == "shadow_rerun_queued")
                .order_by(ConfidenceTrainingJob.created_at)
                .first()
            )
            if job:
                job_id = job.id
                job.status = "shadow_rerun_running"
                job.stage = "候选 Prompt 影子复跑启动中"
                evaluation = dict(job.shadow_evaluation or {})
                evaluation["status"] = "running"
                job.shadow_evaluation = evaluation
                db.add(job)
                db.commit()
                await asyncio.to_thread(_run_shadow_rerun, job_id)
                continue
        finally:
            db.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


def _run_prompt_optimization_analysis(job_id: str) -> None:
    """Run the slow internal-model call after the create response is returned."""
    db = SessionLocal()
    try:
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if not job:
            return
        try:
            existing_analysis = dict(job.analysis_result or {})
            optimization_target = str(existing_analysis.get("optimization_target") or "review")
            if optimization_target == "draft_generation":
                analysis = analyze_draft_generation_prompt(
                    _draft_prompt_optimization_snapshot(list(job.dataset_payload or []))
                )
            else:
                analysis = analyze_confidence_training_prompt(
                    _prompt_optimization_snapshot(list(job.dataset_payload or []))
                )
            analysis["optimization_target"] = optimization_target
            job.status = "prompt_review_pending"
            job.stage = (
                "知识草稿 Prompt 分析完成，等待人工审核候选提示词"
                if optimization_target == "draft_generation"
                else "DeepSeek-flash 已完成错误分析，等待人工审核候选提示词"
            )
            job.analysis_result = analysis
            job.candidate_prompt = str(analysis.get("candidate_prompt") or "")
            job.resolved_model_version = str(
                analysis.get("resolved_model_version") or analysis.get("model_name") or ""
            )
            job.error_message = (
                "shadow_only：候选提示词仅用于后续影子复跑，未修改当前生产提示词或模型权重。"
            )
            job.prompt_versions = [{
                "version": "v1-candidate",
                "prompt": job.candidate_prompt,
                "status": "diagnostic_pending",
                "source": "initial_error_analysis",
                "created_at": datetime.utcnow().isoformat(),
                "diagnostic_job_id": job.id,
            }]
        except ModelAnnotationError as exc:
            job.status = "analysis_failed"
            job.stage = "DeepSeek-flash 提示词分析失败"
            job.error_message = str(exc)
        db.add(job)
        db.commit()
    finally:
        db.close()


@router.get("/overview", response_model=ConfidenceTrainingOverview)
def confidence_training_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    overview, bands = _aggregate_overview(db)
    overview["bands"] = bands
    overview["settings"] = settings_snapshot(db)
    return overview


@router.get("/items", response_model=list[ConfidenceTrainingItem])
def list_confidence_training_items(
    batch_id: str = Query("", max_length=128),
    confidence_band: str = Query("", max_length=8),
    annotation_status: str = Query("", pattern="^(|pending|annotated)$"),
    training_status: str = Query("", max_length=32),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    batch = batch_id.strip()
    band = confidence_band.strip()
    training = training_status.strip()
    if not band and not training and not annotation_status:
        # 只需要 batch_id（或完全不筛选）时派生字段不参与过滤，
        # 直接用 SQL 的 ORDER BY + LIMIT 取数即可。
        return _fetch_recent_items(db, batch, limit)
    item_ids = _filtered_item_ids(
        db,
        batch_id=batch,
        confidence_band=band,
        annotation_status=annotation_status,
        training_status=training,
        limit=limit,
    )
    return _fetch_items_by_id(db, item_ids)


@router.patch("/items/{ingestion_id}", response_model=ConfidenceTrainingItem)
def update_confidence_training_item(
    ingestion_id: str,
    body: ConfidenceTrainingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    item = (
        db.query(IntegrationIngestion)
        .filter(IntegrationIngestion.id == ingestion_id)
        .first()
    )
    if not item:
        raise HTTPException(404, "候选评测记录不存在。")
    if body.human_label is not None or body.truth_status is not None:
        raise HTTPException(409, "人工真值由候选价值复核产生，不能在置信度评测页重复输入。")
    values = body.model_dump(exclude_unset=True)
    values["truth_reviewer"] = current_user.username
    values["truth_reviewed_at"] = __import__("datetime").datetime.utcnow().isoformat()
    payload = update_item(item, values)
    db.add(item)
    db.commit()
    db.refresh(item)
    return payload


@router.post("/items/{ingestion_id}/correct")
def correct_confidence_training_item(
    ingestion_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    """Run a shadow correction while preserving the initial model output and human truth."""
    item = (
        db.query(IntegrationIngestion)
        .filter(IntegrationIngestion.id == ingestion_id)
        .first()
    )
    if not item:
        raise HTTPException(404, "候选评测记录不存在。")
    metadata = dict(item.review_metadata or {})
    evaluation = dict(metadata.get("confidence_training") or {})
    human = dict((item.candidate_payload or {}).get("human_review") or {})
    human.update(dict(metadata.get("human_review") or {}))
    human_label = human.get("knowledge_value")
    if not human_label:
        raise HTTPException(409, "请先完成并确认人工真值，再让 DeepSeek-flash 纠错。")
    model_review = dict((item.candidate_payload or {}).get("model_review") or metadata.get("model_review") or {})
    candidate = {
        "title": str((item.candidate_payload or {}).get("knowledge", {}).get("title") or ""),
        "draft": {
            "content": (item.candidate_payload or {}).get("knowledge", {}).get("content"),
            "recommended_reply": (item.candidate_payload or {}).get("knowledge", {}).get("recommended_reply"),
        },
        "evidence_excerpt": str((item.candidate_payload or {}).get("knowledge", {}).get("evidence_excerpt") or ""),
        "model_review": model_review,
        "human_truth": {
            "knowledge_value": human_label,
            "draft_disposition": human.get("draft_disposition"),
        },
        "review_note": str(evaluation.get("review_note") or ""),
    }
    try:
        corrected = correct_with_deepseek_flash(candidate)
    except ModelAnnotationError as exc:
        raise HTTPException(503, str(exc)) from exc
    history = list(evaluation.get("correction_history") or [])
    round_number = len(history) + 1
    history.append({
        "correction_round": round_number,
        "model_name": corrected.get("model_name", "deepseek-flash"),
        "prompt_version": corrected.get("prompt_version"),
        "result": corrected,
        "requested_by": current_user.username,
    })
    evaluation.update({
        "correction_round": round_number,
        "correction_status": "pending_human_confirmation",
        "corrected_model_review": corrected,
        "correction_history": history,
    })
    metadata["confidence_training"] = evaluation
    item.review_metadata = metadata
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "status": "pending_human_confirmation",
        "model": corrected.get("model_name", "deepseek-flash"),
        "correction_round": round_number,
        "corrected_model_review": corrected,
        "item": item_payload(item),
    }


@router.get("/settings", response_model=ConfidenceTrainingSettings)
def get_confidence_training_settings(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    return settings_snapshot(db)


@router.patch("/settings", response_model=ConfidenceTrainingSettings)
def patch_confidence_training_settings(
    body: ConfidenceTrainingSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    return update_settings(
        db,
        body.model_dump(),
        updated_by=current_user.username,
    )


@router.get("/model-connection", response_model=ModelConnectionConfig)
def get_confidence_model_connection(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    """The editable DeepSeek-flash route. The API key is always masked."""
    return model_config_snapshot(db)


@router.patch("/model-connection", response_model=ModelConnectionConfig)
def patch_confidence_model_connection(
    body: ModelConnectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    """Store an override so the next model call picks it up immediately."""
    try:
        return update_model_config(
            db,
            body.model_dump(),
            updated_by=current_user.username,
        )
    except ModelConnectionError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/model-connection-test")
def test_confidence_training_model_connection(
    body: ModelConnectionTest | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    """Use a fixed non-business payload to verify the internal model route.

    The optional body carries unsaved dialog values, so an operator can test an
    address or key before saving it.
    """
    try:
        config = model_config_for_test(db, body.model_dump() if body else None)
        result = test_deepseek_flash_connection(config)
    except ModelAnnotationError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "status": "connected",
        "requested_model": result.get("requested_model"),
        "resolved_model_version": result.get("resolved_model_version"),
        "prompt_version": result.get("prompt_version"),
        "base_url": result.get("base_url"),
        "model": result.get("model"),
        "timeout_seconds": result.get("timeout_seconds"),
        "api_key_masked": result.get("api_key_masked"),
        "latency_ms": result.get("latency_ms"),
    }


@router.get("/jobs", response_model=list[ConfidenceTrainingJobSchema])
def list_confidence_training_jobs(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    jobs = (
        db.query(ConfidenceTrainingJob)
        .order_by(ConfidenceTrainingJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [_training_job_payload(job) for job in jobs]


@router.post("/jobs", response_model=ConfidenceTrainingJobSchema)
def create_confidence_training_job(
    background_tasks: BackgroundTasks,
    target: str = Query("review", pattern="^(review|draft_generation)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    if not settings_snapshot(db)["training_candidate_collection_enabled"]:
        raise HTTPException(409, "请先开启“允许收集训练候选”，再人工发起训练任务。")
    source_rows = _rows(db)
    dataset, counts = (
        _draft_prompt_training_dataset(source_rows)
        if target == "draft_generation"
        else _manual_training_dataset(source_rows)
    )
    if len(dataset) < 3:
        raise HTTPException(
            422,
            (
                "至少需要 3 条有修改前后版本的知识草稿，才能优化草稿生成 Prompt。"
                if target == "draft_generation"
                else "至少需要 3 条已确认的人工真值，才能生成独立训练集、验证集和测试集。"
            ),
        )
    serialized = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    job = ConfidenceTrainingJob(
        id=f"ctj-{uuid.uuid4().hex[:16]}",
        status="analyzing_errors",
        stage=(
            "DeepSeek-flash 正在分析原始草稿与人工最终稿差异"
            if target == "draft_generation"
            else "DeepSeek-flash 正在分析人工真值与模型错误"
        ),
        model_name=MODEL_NAME,
        evaluation_scope=EVALUATION_SCOPE,
        dataset_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        dataset_payload=dataset,
        sample_count=len(dataset),
        train_count=counts["train"],
        validation_count=counts["validation"],
        test_count=counts["test"],
        requested_by=current_user.username,
        error_message="",
        analysis_result={"optimization_target": target},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(_run_prompt_optimization_analysis, job.id)
    return _training_job_payload(job)


@router.post("/jobs/{job_id}/shadow-rerun", response_model=ConfidenceTrainingJobSchema)
def start_confidence_training_shadow_rerun(
    job_id: str,
    split: str = Query("validation", pattern="^(validation|test)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    if dict(job.analysis_result or {}).get("optimization_target") == "draft_generation":
        raise HTTPException(409, "草稿生成 Prompt 需要接入 Answer Hub 离线回放后再做影子验收。")
    allowed_statuses = {
        "validation": {
            "prompt_review_pending",
            "shadow_rerun_interrupted",
            "shadow_rerun_failed",
        },
        "test": {"shadow_validation_review_pending"},
    }
    if job.status not in allowed_statuses[split] or not job.candidate_prompt:
        raise HTTPException(409, "当前任务没有可继续的候选 Prompt 影子复跑。")
    previous_evaluation = dict(job.shadow_evaluation or {})
    if split == "test":
        comparison = dict(previous_evaluation.get("comparison") or {})
        if not comparison.get("gate_passed"):
            raise HTTPException(409, "验证集尚未显示总体改善，不能进入测试集。")
    job.status = "shadow_rerun_queued"
    job.stage = f"候选 Prompt {split} 集影子复跑排队中"
    same_split = previous_evaluation.get("target_split") == split
    previous_rows = list(previous_evaluation.get("rows") or []) if same_split else []
    previous_skipped = list(previous_evaluation.get("skipped_samples") or []) if same_split else []
    phase_results = dict(previous_evaluation.get("phase_results") or {})
    if split == "test" and previous_evaluation.get("status") == "completed":
        phase_results["validation"] = previous_evaluation
    total_samples = len(_shadow_rerun_samples(list(job.dataset_payload or []), split))
    job.shadow_evaluation = {
        "status": "queued",
        "target_split": split,
        "processed_samples": len(previous_rows) + len(previous_skipped),
        "evaluated_samples": len(previous_rows),
        "skipped_sample_count": len(previous_skipped),
        "total_samples": total_samples,
        "evaluation_scope": EVALUATION_SCOPE,
        "phase_results": phase_results,
        "resumed_rows": len(previous_rows),
        "rows": previous_rows,
        "skipped_samples": previous_skipped,
    }
    db.add(job)
    db.commit()
    db.refresh(job)
    return _training_job_payload(job)


@router.get("/jobs/{job_id}/regressions")
def list_confidence_training_regressions(
    job_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    return {
        "job_id": job.id,
        "rows": _regressed_rows(job),
        "reviews": dict(job.regression_reviews or {}),
        "next_action": "review_regressions",
    }


@router.patch("/jobs/{job_id}/regressions/{ingestion_id}")
def review_confidence_training_regression(
    job_id: str,
    ingestion_id: str,
    body: ConfidenceRegressionReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    if (job.shadow_evaluation or {}).get("status") != "completed":
        raise HTTPException(409, "只有影子复跑完成后才能复核退化样本。")
    row = next((item for item in _regressed_rows(job) if str(item.get("id")) == ingestion_id), None)
    if not row:
        raise HTTPException(404, "该样本不在当前候选 Prompt 的退化集合中。")
    reviews = dict(job.regression_reviews or {})
    reviews[ingestion_id] = {
        "classification": body.classification,
        "note": body.note.strip(),
        "keep_as_regression_case": body.keep_as_regression_case,
        "reviewed_by": current_user.username,
        "reviewed_at": datetime.utcnow().isoformat(),
        "next_action": "candidate_review" if body.classification == "human_truth_correction_required" else "prompt_revision",
    }
    job.regression_reviews = reviews
    job.stage = (
        "退化样本已复核，需回候选价值复核修正真值"
        if body.classification == "human_truth_correction_required"
        else "退化样本已复核，可生成下一版候选 Prompt"
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _training_job_payload(job)


def _run_prompt_revision(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
        if not job:
            return
        try:
            result = revise_confidence_training_prompt(_prompt_revision_snapshot(job))
            revision = {
                "version": "v1.1-candidate",
                "prompt": str(result.get("candidate_prompt") or ""),
                "status": "validation_pending",
                "source": "regression_review",
                "created_at": datetime.utcnow().isoformat(),
                "diagnostic_job_id": job.id,
                "analysis_summary": result.get("analysis_summary"),
                "prompt_changes": result.get("prompt_changes") or [],
                "expected_risks": result.get("expected_risks") or [],
                "recommended_next_action": result.get("recommended_next_action") or "validation_shadow_rerun",
            }
            versions = list(job.prompt_versions or [])
            for old in versions:
                if old.get("version") == "v1-candidate" and old.get("status") == "diagnostic_pending":
                    old["status"] = "diagnostic_failed"
                    comparison = dict((job.shadow_evaluation or {}).get("comparison") or {})
                    old["evaluation_summary"] = {
                        "improved_count": comparison.get("improved_count", 0),
                        "regressed_count": comparison.get("regressed_count", 0),
                        "gate_passed": comparison.get("gate_passed", False),
                    }
            versions.append(revision)
            job.prompt_versions = versions
            job.status = "validation_pending"
            job.stage = "v1.1 候选 Prompt 已生成，等待验证集影子复跑"
            job.error_message = "shadow_only：v1.1 仍未启用，下一步只允许 validation/test 分阶段影子验收。"
            job.resolved_model_version = str(result.get("resolved_model_version") or job.resolved_model_version or "")
        except ModelAnnotationError as exc:
            job.status = "prompt_revision_failed"
            job.stage = "v1.1 候选 Prompt 生成失败"
            job.error_message = str(exc)
        db.add(job)
        db.commit()
    finally:
        db.close()


@router.post("/jobs/{job_id}/prompt-revisions", response_model=ConfidenceTrainingJobSchema)
def create_confidence_prompt_revision(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    job = db.query(ConfidenceTrainingJob).filter(ConfidenceTrainingJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "提示词优化任务不存在。")
    ok, reason = _revision_preconditions(job)
    if not ok:
        raise HTTPException(409, reason)
    if job.status == "prompt_revision_generating":
        return _training_job_payload(job)
    job.status = "prompt_revision_generating"
    job.stage = "DeepSeek-flash 正在根据退化归因生成 v1.1 候选 Prompt"
    job.error_message = ""
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(_run_prompt_revision, job.id)
    return _training_job_payload(job)


@router.get("/export")
def export_confidence_training(
    format: str = Query("evaluation", pattern="^(review|evaluation|training|errors)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    items = [item_payload(row) for row in _rows(db)]
    if format == "training":
        rows: list[dict[str, Any]] = [
            {
                "id": item["id"],
                "input": item["title"],
                "model_label": item["model_label"],
                "human_label": item["human_label"],
                "dataset_split": item["dataset_split"] or "train",
            }
            for item in items
            if item.get("training_candidate_status") in {"recommended", "qualified"}
            and item.get("truth_status") == "confirmed"
        ]
        content = "\n".join(json.dumps({**row, "evaluation_scope": EVALUATION_SCOPE, "not_for_weight_training": True}, ensure_ascii=False) for row in rows) + ("\n" if rows else "")
        return Response(content, media_type="application/jsonl", headers={"Content-Disposition": 'attachment; filename="shadow-training-candidates.jsonl"'})
    if format == "errors":
        items = [item for item in items if item.get("model_correctness") == "wrong" or item.get("error_severity")]
    if format == "evaluation":
        overview, bands = aggregate(items)
        payload: Any = {"notice": "shadow_only; 不改变生产路由、不进入正式训练真值", "overview": overview, "bands": bands, "items": items}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="confidence-evaluation-report.json"'})
    output = io.StringIO()
    fields = ["id", "event_id", "title", "model_label", "model_confidence", "confidence_band", "human_label", "truth_status", "model_correctness", "strict_correct", "acceptable_correct", "error_type", "error_severity", "training_candidate_status", "dataset_split", "review_status"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in items:
        writer.writerow({key: json.dumps(item.get(key), ensure_ascii=False) if isinstance(item.get(key), list) else item.get(key, "") for key in fields})
    filename = "confidence-review-workbook.csv" if format == "review" else "confidence-errors.csv"
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
