from __future__ import annotations

import json
import re
import string
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.knowledge import (
    Category,
    Knowledge,
    KnowledgeChangeLog,
    KnowledgeStatus,
)


MODEL_CONFIGURATION_ORIGIN = "model_configuration"
MODEL_CONFIGURATION_BUSINESS_TYPE = "self_operated"
MODEL_CONFIGURATION_CATEGORY_ID = "cat-extra-knowledge"
MODEL_CONFIGURATION_SOURCE = "integration"
MODEL_CONFIGURATION_CATEGORY_SOURCE_ID = "119"
MODEL_CONFIGURATION_CATEGORY_NAME = "平板电脑"
MODEL_CONFIGURATION_ATTRIBUTE_FIELDS = (
    "是否有卡槽",
    "Home键",
    "指纹识别",
    "3D面容",
    "内置手写笔",
    "闪光灯",
    "蜂窝网络",
    "光线传感器",
)

_KNOWLEDGE_ID_ALPHABET = string.ascii_uppercase
_SOURCE_FIELD_NAMES = {
    "category_id": "品类ID",
    "category_name": "品类",
    "brand_id": "品牌ID",
    "brand_name": "品牌",
    "model_id": "型号ID",
    "model_name": "型号",
}
_PAYLOAD_FIELD_LABELS = {
    "source_record_id": "来源知识ID",
    "title": "标题",
    "category_id": "品类ID",
    "category_name": "品类",
    "brand_id": "品牌ID",
    "brand_name": "品牌",
    "model_id": "型号ID",
    "model_name": "型号",
    "content": "综合内容",
}
_NORMALIZED_NAME_KEY_FIELD = "_model_configuration_normalized_name_key"
_NORMALIZED_CATEGORY_MODEL_KEY_FIELD = (
    "_model_configuration_normalized_category_model_key"
)


def _model_configuration_source_knowledge_key(
    category_id: str,
    brand_id: str,
    model_id: str,
) -> str:
    return f"model-configuration:{category_id}:{brand_id}:{model_id}"


class ModelConfigurationSyncError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        source_record_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.source_record_id = source_record_id


@dataclass(frozen=True)
class ModelConfigurationRecord:
    source_record_id: str
    title: str
    category_id: str
    category_name: str
    brand_id: str
    brand_name: str
    model_id: str
    model_name: str
    content: str
    source_fields: dict[str, str]

    @property
    def source_knowledge_key(self) -> str:
        return _model_configuration_source_knowledge_key(
            self.category_id,
            self.brand_id,
            self.model_id,
        )


@dataclass(frozen=True)
class ModelConfigurationSyncItemResult:
    source_record_id: str
    knowledge_id: str
    operation: str


@dataclass(frozen=True)
class ModelConfigurationSyncResult:
    total: int
    created: int
    updated: int
    unchanged: int
    items: tuple[ModelConfigurationSyncItemResult, ...] = ()


@dataclass(frozen=True)
class ModelConfigurationMatch:
    item: Knowledge
    match_mode: str


class ModelConfigurationAmbiguousError(LookupError):
    code = "MODEL_CONFIGURATION_AMBIGUOUS"

    def __init__(
        self,
        *,
        match_mode: str,
        category_value: str,
        model_value: str,
        knowledge_ids: list[str],
    ):
        super().__init__(
            "同一类目和机型匹配到多条机型配置信息，已拒绝任选一条。"
        )
        self.match_mode = match_mode
        self.category_value = category_value
        self.model_value = model_value
        self.match_count = len(knowledge_ids)
        self.knowledge_ids = tuple(knowledge_ids[:10])


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalized_exact_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _normalized_name_key(
    *,
    category_name: Any,
    brand_name: Any,
    model_name: Any,
) -> str:
    return json.dumps(
        [
            _normalized_exact_text(category_name),
            _normalized_exact_text(brand_name),
            _normalized_exact_text(model_name),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalized_category_model_key(
    *,
    category_name: Any,
    model_name: Any,
) -> str:
    return json.dumps(
        [
            _normalized_exact_text(category_name),
            _normalized_exact_text(model_name),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _required_text(
    payload: dict[str, Any],
    key: str,
    *,
    max_length: int,
) -> str:
    value = _clean_text(payload.get(key))
    label = _PAYLOAD_FIELD_LABELS.get(key, key)
    if not value:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_FIELD_REQUIRED",
            f"机型配置字段“{label}”不能为空。",
        )
    if len(value) > max_length:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_FIELD_TOO_LONG",
            f"机型配置字段“{label}”超过 {max_length} 个字符。",
        )
    return value


def parse_model_configuration_payload(
    payload: dict[str, Any],
) -> list[ModelConfigurationRecord]:
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_RECORDS_REQUIRED",
            "机型配置同步文件必须包含非空 records 数组。",
        )

    default_category_id = _clean_text(
        payload.get("category_id")
    ) or MODEL_CONFIGURATION_CATEGORY_SOURCE_ID
    default_category_name = _clean_text(
        payload.get("category_name")
    ) or MODEL_CONFIGURATION_CATEGORY_NAME

    records: list[ModelConfigurationRecord] = []
    seen_record_ids: set[str] = set()
    seen_model_keys: set[tuple[str, str, str]] = set()
    for index, raw_record in enumerate(raw_records, start=1):
        if not isinstance(raw_record, dict):
            raise ModelConfigurationSyncError(
                "MODEL_CONFIGURATION_RECORD_INVALID",
                f"第 {index} 条机型配置不是对象。",
            )
        source_record_id = _required_text(
            raw_record,
            "source_record_id",
            max_length=256,
        )
        category_id = (
            _clean_text(raw_record.get("category_id"))
            or default_category_id
        )
        category_name = (
            _clean_text(raw_record.get("category_name"))
            or default_category_name
        )
        brand_id = _required_text(
            raw_record,
            "brand_id",
            max_length=128,
        )
        brand_name = _required_text(
            raw_record,
            "brand_name",
            max_length=500,
        )
        model_id = _required_text(
            raw_record,
            "model_id",
            max_length=128,
        )
        model_name = _required_text(
            raw_record,
            "model_name",
            max_length=500,
        )
        title = _required_text(raw_record, "title", max_length=256)
        content = _required_text(raw_record, "content", max_length=100_000)

        model_key = (category_id, brand_id, model_id)
        if source_record_id in seen_record_ids:
            raise ModelConfigurationSyncError(
                "MODEL_CONFIGURATION_RECORD_ID_DUPLICATED",
                f"上游知识ID {source_record_id} 在同步文件中重复。",
            )
        if model_key in seen_model_keys:
            raise ModelConfigurationSyncError(
                "MODEL_CONFIGURATION_MODEL_ID_DUPLICATED",
                (
                    "品类/品牌/型号ID组合 "
                    f"{category_id}/{brand_id}/{model_id} 在同步文件中重复。"
                ),
            )
        seen_record_ids.add(source_record_id)
        seen_model_keys.add(model_key)

        raw_source_fields = raw_record.get("source_fields")
        source_fields = {
            _clean_text(key): _clean_text(value)
            for key, value in (
                raw_source_fields.items()
                if isinstance(raw_source_fields, dict)
                else ()
            )
            if _clean_text(key)
        }
        source_fields.update(
            {
                "知识ID": source_record_id,
                "标题": title,
                "品类ID": category_id,
                "品类": category_name,
                "品牌ID": brand_id,
                "品牌": brand_name,
                "型号ID": model_id,
                "型号": model_name,
                "综合内容": content,
            }
        )
        records.append(
            ModelConfigurationRecord(
                source_record_id=source_record_id,
                title=title,
                category_id=category_id,
                category_name=category_name,
                brand_id=brand_id,
                brand_name=brand_name,
                model_id=model_id,
                model_name=model_name,
                content=content,
                source_fields=source_fields,
            )
        )
    return records


def _generate_knowledge_id(db: Session) -> str:
    sequence_number = db.execute(
        text("SELECT nextval('knowledge_item_number_seq')")
    ).scalar_one()
    if sequence_number > len(_KNOWLEDGE_ID_ALPHABET) * 99999:
        raise ModelConfigurationSyncError(
            "KNOWLEDGE_ID_LIMIT_REACHED",
            "知识ID已达到系统上限。",
        )
    letter_index, number = divmod(sequence_number - 1, 99999)
    return f"{_KNOWLEDGE_ID_ALPHABET[letter_index]}-{number + 1:05d}"


def acquire_model_configuration_write_lock(db: Session) -> None:
    """串行化所有机型配置写入，避免 Excel 与人工编辑互相覆盖。"""
    locked_category = (
        db.query(Category.id)
        .filter(Category.id == MODEL_CONFIGURATION_CATEGORY_ID)
        .with_for_update()
        .first()
    )
    if not locked_category:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_CATEGORY_MISSING",
            (
                "知识分类 cat-extra-knowledge 不存在，"
                "请先完成数据库迁移初始化。"
            ),
        )


def _record_values(record: ModelConfigurationRecord) -> dict[str, Any]:
    source_fields = dict(record.source_fields)
    source_fields[_NORMALIZED_NAME_KEY_FIELD] = _normalized_name_key(
        category_name=record.category_name,
        brand_name=record.brand_name,
        model_name=record.model_name,
    )
    source_fields[
        _NORMALIZED_CATEGORY_MODEL_KEY_FIELD
    ] = _normalized_category_model_key(
        category_name=record.category_name,
        model_name=record.model_name,
    )
    return {
        "title": record.title,
        "content": {
            "blocks": [{"type": "text", "value": record.content}]
        },
        "category_id": MODEL_CONFIGURATION_CATEGORY_ID,
        "status": KnowledgeStatus.PUBLISHED,
        "source": MODEL_CONFIGURATION_SOURCE,
        "quality_score": 1.0,
        "applicable_scenes": [],
        "applicable_categories": [record.category_id],
        "applicable_brands": [record.brand_id],
        "applicable_models": [record.model_id],
        "related_standard_items": [],
        "source_record_id": record.source_record_id,
        "source_knowledge_key": record.source_knowledge_key,
        "source_fields": source_fields,
    }


def _find_existing_record(
    db: Session,
    record: ModelConfigurationRecord,
    *,
    allow_source_key_change_for: str | None = None,
) -> Knowledge | None:
    by_record_id = (
        db.query(Knowledge)
        .populate_existing()
        .filter(
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
            Knowledge.source_record_id == record.source_record_id,
        )
        .with_for_update()
        .all()
    )
    by_knowledge_key = (
        db.query(Knowledge)
        .populate_existing()
        .filter(
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
            Knowledge.source_knowledge_key == record.source_knowledge_key,
        )
        .with_for_update()
        .all()
    )
    if len(by_record_id) > 1 or len(by_knowledge_key) > 1:
        raise ModelConfigurationSyncError(
            "SOURCE_IDENTIFIER_AMBIGUOUS",
            (
                f"机型配置 {record.source_record_id}/{record.source_knowledge_key} "
                "匹配到多条知识。"
            ),
            source_record_id=record.source_record_id,
        )
    record_match = by_record_id[0] if by_record_id else None
    key_match = by_knowledge_key[0] if by_knowledge_key else None
    if record_match and key_match and record_match.id != key_match.id:
        raise ModelConfigurationSyncError(
            "SOURCE_IDENTIFIER_CONFLICT",
            (
                f"上游知识ID {record.source_record_id} 与机型键 "
                f"{record.source_knowledge_key} 指向不同知识。"
            ),
            source_record_id=record.source_record_id,
        )
    if record_match and (
        record_match.source_knowledge_key
        and record_match.source_knowledge_key != record.source_knowledge_key
        and record_match.id != allow_source_key_change_for
    ):
        raise ModelConfigurationSyncError(
            "SOURCE_RECORD_ID_REUSED",
            (
                f"上游知识ID {record.source_record_id} 已绑定其他机型键，"
                "拒绝覆盖。"
            ),
            source_record_id=record.source_record_id,
        )
    return record_match or key_match


def _snapshot(item: Knowledge) -> dict[str, Any]:
    return {
        "title": item.title,
        "content": item.content,
        "category_id": item.category_id,
        "status": (
            item.status.value
            if isinstance(item.status, KnowledgeStatus)
            else item.status
        ),
        "source": item.source,
        "quality_score": item.quality_score,
        "applicable_scenes": item.applicable_scenes,
        "applicable_categories": item.applicable_categories,
        "applicable_brands": item.applicable_brands,
        "applicable_models": item.applicable_models,
        "related_standard_items": item.related_standard_items,
        "source_record_id": item.source_record_id,
        "source_knowledge_key": item.source_knowledge_key,
        "source_fields": item.source_fields,
    }


def sync_model_configurations(
    db: Session,
    records: list[ModelConfigurationRecord],
    *,
    actor: str,
    allow_source_key_change_for: str | None = None,
) -> ModelConfigurationSyncResult:
    acquire_model_configuration_write_lock(db)

    created = updated = unchanged = 0
    item_results: list[ModelConfigurationSyncItemResult] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    existing_records = [
        (
            record,
            _find_existing_record(
                db,
                record,
                allow_source_key_change_for=allow_source_key_change_for,
            ),
        )
        for record in records
    ]
    for record, existing in existing_records:
        values = _record_values(record)
        if existing is None:
            existing = Knowledge(
                id=_generate_knowledge_id(db),
                knowledge_origin=MODEL_CONFIGURATION_ORIGIN,
                business_type=MODEL_CONFIGURATION_BUSINESS_TYPE,
                subtitles=[],
                source_topic_key=None,
                deduplication_metadata={},
                created_by=actor,
                updated_by=actor,
                created_at=now,
                updated_at=now,
                **values,
            )
            db.add(existing)
            created += 1
            item_results.append(
                ModelConfigurationSyncItemResult(
                    source_record_id=record.source_record_id,
                    knowledge_id=existing.id,
                    operation="created",
                )
            )
            continue

        before = _snapshot(existing)
        changed_fields: list[str] = []
        for field, value in values.items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed_fields.append(field)
        if existing.knowledge_origin != MODEL_CONFIGURATION_ORIGIN:
            existing.knowledge_origin = MODEL_CONFIGURATION_ORIGIN
            changed_fields.append("knowledge_origin")
        if existing.business_type != MODEL_CONFIGURATION_BUSINESS_TYPE:
            existing.business_type = MODEL_CONFIGURATION_BUSINESS_TYPE
            changed_fields.append("business_type")
        if not changed_fields:
            unchanged += 1
            item_results.append(
                ModelConfigurationSyncItemResult(
                    source_record_id=record.source_record_id,
                    knowledge_id=existing.id,
                    operation="unchanged",
                )
            )
            continue

        existing.updated_by = actor
        existing.updated_at = now
        db.add(
            KnowledgeChangeLog(
                id=f"kcl-{uuid.uuid4().hex[:12]}",
                knowledge_id=existing.id,
                changed_by=actor,
                changed_fields=changed_fields,
                before_data=before,
                after_data=_snapshot(existing),
                created_at=now,
            )
        )
        updated += 1
        item_results.append(
            ModelConfigurationSyncItemResult(
                source_record_id=record.source_record_id,
                knowledge_id=existing.id,
                operation="updated",
            )
        )
    db.flush()
    return ModelConfigurationSyncResult(
        total=len(records),
        created=created,
        updated=updated,
        unchanged=unchanged,
        items=tuple(item_results),
    )


def _source_field(item: Knowledge, field: str) -> str:
    source_fields = (
        item.source_fields if isinstance(item.source_fields, dict) else {}
    )
    return _clean_text(source_fields.get(_SOURCE_FIELD_NAMES[field]))


def _request_scope(
    *,
    category_id: Any = None,
    category_name: Any = None,
    brand_id: Any = None,
    brand_name: Any = None,
    model_id: Any = None,
    model_name: Any = None,
) -> dict[str, dict[str, str]]:
    return {
        "category": {
            "id": _clean_text(category_id),
            "name": _clean_text(category_name),
        },
        "brand": {
            "id": _clean_text(brand_id),
            "name": _clean_text(brand_name),
        },
        "model": {
            "id": _clean_text(model_id),
            "name": _clean_text(model_name),
        },
    }


def _matches_scope(
    item: Knowledge,
    scope: dict[str, dict[str, str]],
    *,
    prefer_ids: bool,
    dimensions: tuple[str, ...] = ("category", "brand", "model"),
) -> bool:
    for dimension in dimensions:
        requested = scope[dimension]
        comparison_field = (
            f"{dimension}_id"
            if prefer_ids and requested["id"]
            else f"{dimension}_name"
        )
        requested_value = (
            requested["id"]
            if comparison_field.endswith("_id")
            else requested["name"]
        )
        if not requested_value:
            return False
        if _normalized_exact_text(
            _source_field(item, comparison_field)
        ) != _normalized_exact_text(requested_value):
            return False
    return True


def find_exact_model_configuration(
    db: Session,
    *,
    category_id: Any = None,
    category_name: Any = None,
    brand_id: Any = None,
    brand_name: Any = None,
    model_id: Any = None,
    model_name: Any = None,
) -> ModelConfigurationMatch | None:
    scope = _request_scope(
        category_id=category_id,
        category_name=category_name,
        brand_id=brand_id,
        brand_name=brand_name,
        model_id=model_id,
        model_name=model_name,
    )
    brand_supplied = bool(
        scope["brand"]["id"] or scope["brand"]["name"]
    )
    match_dimensions = (
        ("category", "brand", "model")
        if brand_supplied
        else ("category", "model")
    )
    if any(
        not (scope[dimension]["id"] or scope[dimension]["name"])
        for dimension in match_dimensions
    ):
        return None

    if all(scope[dimension]["id"] for dimension in match_dimensions):
        base_query = db.query(Knowledge).filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
        )
        if brand_supplied:
            id_query = base_query.filter(
                Knowledge.source_knowledge_key
                == _model_configuration_source_knowledge_key(
                    scope["category"]["id"],
                    scope["brand"]["id"],
                    scope["model"]["id"],
                )
            )
        else:
            category_id_expression = Knowledge.source_fields[
                _SOURCE_FIELD_NAMES["category_id"]
            ].as_string()
            model_id_expression = Knowledge.source_fields[
                _SOURCE_FIELD_NAMES["model_id"]
            ].as_string()
            id_query = base_query.filter(
                category_id_expression == scope["category"]["id"],
                model_id_expression == scope["model"]["id"],
            )
        id_matches = (
            id_query
            .limit(2)
            .all()
        )
        if len(id_matches) == 1:
            return ModelConfigurationMatch(
                item=id_matches[0],
                match_mode="id",
            )
        if len(id_matches) > 1:
            raise ModelConfigurationAmbiguousError(
                match_mode="id",
                category_value=scope["category"]["id"],
                model_value=scope["model"]["id"],
                knowledge_ids=[item.id for item in id_matches],
            )
        if brand_supplied:
            return None

    if not all(
        scope[dimension]["name"]
        for dimension in match_dimensions
    ):
        return None

    if brand_supplied:
        normalized_name_key = _normalized_name_key(
            category_name=scope["category"]["name"],
            brand_name=scope["brand"]["name"],
            model_name=scope["model"]["name"],
        )
        normalized_name_key_expression = Knowledge.source_fields[
            _NORMALIZED_NAME_KEY_FIELD
        ].as_string()
    else:
        normalized_name_key = _normalized_category_model_key(
            category_name=scope["category"]["name"],
            model_name=scope["model"]["name"],
        )
        normalized_name_key_expression = Knowledge.source_fields[
            _NORMALIZED_CATEGORY_MODEL_KEY_FIELD
        ].as_string()

    keyed_candidates = (
        db.query(Knowledge)
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
            normalized_name_key_expression == normalized_name_key,
        )
        .limit(2)
        .all()
    )
    legacy_candidates = (
        db.query(Knowledge)
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
            normalized_name_key_expression.is_(None),
        )
        .all()
    )
    matches = [
        item
        for item in [*keyed_candidates, *legacy_candidates]
        if _matches_scope(
            item,
            scope,
            prefer_ids=False,
            dimensions=match_dimensions,
        )
    ]
    if len(matches) == 1:
        return ModelConfigurationMatch(
            item=matches[0],
            match_mode="name",
        )
    if len(matches) > 1:
        raise ModelConfigurationAmbiguousError(
            match_mode="name",
            category_value=scope["category"]["name"],
            model_value=scope["model"]["name"],
            knowledge_ids=[item.id for item in matches],
        )
    return None
