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

_KNOWLEDGE_ID_ALPHABET = string.ascii_uppercase
_SOURCE_FIELD_NAMES = {
    "category_id": "品类ID",
    "category_name": "品类",
    "brand_id": "品牌ID",
    "brand_name": "品牌",
    "model_id": "型号ID",
    "model_name": "型号",
}
_NORMALIZED_NAME_KEY_FIELD = "_model_configuration_normalized_name_key"


def _model_configuration_source_knowledge_key(
    category_id: str,
    brand_id: str,
    model_id: str,
) -> str:
    return f"model-configuration:{category_id}:{brand_id}:{model_id}"


class ModelConfigurationSyncError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
class ModelConfigurationSyncResult:
    total: int
    created: int
    updated: int
    unchanged: int


@dataclass(frozen=True)
class ModelConfigurationMatch:
    item: Knowledge
    match_mode: str


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


def _required_text(
    payload: dict[str, Any],
    key: str,
    *,
    max_length: int,
) -> str:
    value = _clean_text(payload.get(key))
    if not value:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_FIELD_REQUIRED",
            f"机型配置字段 {key} 不能为空。",
        )
    if len(value) > max_length:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_FIELD_TOO_LONG",
            f"机型配置字段 {key} 超过 {max_length} 个字符。",
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


def _record_values(record: ModelConfigurationRecord) -> dict[str, Any]:
    source_fields = dict(record.source_fields)
    source_fields[_NORMALIZED_NAME_KEY_FIELD] = _normalized_name_key(
        category_name=record.category_name,
        brand_name=record.brand_name,
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
) -> Knowledge | None:
    by_record_id = (
        db.query(Knowledge)
        .filter(
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
            Knowledge.source_record_id == record.source_record_id,
        )
        .all()
    )
    by_knowledge_key = (
        db.query(Knowledge)
        .filter(
            Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
            Knowledge.source_knowledge_key == record.source_knowledge_key,
        )
        .all()
    )
    if len(by_record_id) > 1 or len(by_knowledge_key) > 1:
        raise ModelConfigurationSyncError(
            "SOURCE_IDENTIFIER_AMBIGUOUS",
            (
                f"机型配置 {record.source_record_id}/{record.source_knowledge_key} "
                "匹配到多条知识。"
            ),
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
        )
    if record_match and (
        record_match.source_knowledge_key
        and record_match.source_knowledge_key != record.source_knowledge_key
    ):
        raise ModelConfigurationSyncError(
            "SOURCE_RECORD_ID_REUSED",
            (
                f"上游知识ID {record.source_record_id} 已绑定其他机型键，"
                "拒绝覆盖。"
            ),
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
) -> ModelConfigurationSyncResult:
    if not (
        db.query(Category.id)
        .filter(Category.id == MODEL_CONFIGURATION_CATEGORY_ID)
        .first()
    ):
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_CATEGORY_MISSING",
            (
                "知识分类 cat-extra-knowledge 不存在，"
                "请先完成数据库迁移初始化。"
            ),
        )

    created = updated = unchanged = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for record in records:
        existing = _find_existing_record(db, record)
        values = _record_values(record)
        if existing is None:
            db.add(
                Knowledge(
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
            )
            created += 1
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
    db.flush()
    return ModelConfigurationSyncResult(
        total=len(records),
        created=created,
        updated=updated,
        unchanged=unchanged,
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
) -> bool:
    for dimension in ("category", "brand", "model"):
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
    if any(
        not (values["id"] or values["name"])
        for values in scope.values()
    ):
        return None

    base_query = db.query(Knowledge).filter(
        Knowledge.status == KnowledgeStatus.PUBLISHED,
        Knowledge.knowledge_origin == MODEL_CONFIGURATION_ORIGIN,
    )
    if all(scope[dimension]["id"] for dimension in ("category", "brand", "model")):
        source_knowledge_key = _model_configuration_source_knowledge_key(
            scope["category"]["id"],
            scope["brand"]["id"],
            scope["model"]["id"],
        )
        id_matches = (
            base_query.filter(
                Knowledge.source_knowledge_key == source_knowledge_key,
            )
            .limit(2)
            .all()
        )
        if len(id_matches) == 1:
            return ModelConfigurationMatch(
                item=id_matches[0],
                match_mode="id",
            )
        if len(id_matches) > 1:
            return None

    if not all(
        scope[dimension]["name"]
        for dimension in ("category", "brand", "model")
    ):
        return None

    normalized_name_key = _normalized_name_key(
        category_name=scope["category"]["name"],
        brand_name=scope["brand"]["name"],
        model_name=scope["model"]["name"],
    )
    normalized_name_key_expression = Knowledge.source_fields[
        _NORMALIZED_NAME_KEY_FIELD
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
    if len(keyed_candidates) > 1:
        return None

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
        if _matches_scope(item, scope, prefer_ids=False)
    ]
    return (
        ModelConfigurationMatch(
            item=matches[0],
            match_mode="name",
        )
        if len(matches) == 1
        else None
    )
