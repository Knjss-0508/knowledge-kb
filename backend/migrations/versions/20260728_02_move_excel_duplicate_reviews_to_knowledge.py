"""Move Excel duplicate confirmations into the knowledge review queue.

Revision ID: 20260728_02
Revises: 20260728_01
Create Date: 2026-07-28
"""

import json
import string

import sqlalchemy as sa
from alembic import op


revision = "20260728_02"
down_revision = "20260728_01"
branch_labels = None
depends_on = None

_ALPHA = string.ascii_uppercase


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _as_list(value) -> list:
    return list(value) if isinstance(value, list) else []


def _next_knowledge_id(bind) -> str:
    sequence_number = bind.execute(
        sa.text("SELECT nextval('knowledge_item_number_seq')")
    ).scalar_one()
    letter_index, number = divmod(sequence_number - 1, 99999)
    if letter_index >= len(_ALPHA):
        raise RuntimeError("Knowledge ID limit reached while migrating Excel reviews.")
    return f"{_ALPHA[letter_index]}-{number + 1:05d}"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, candidate_payload, review_metadata, created_at, updated_at
            FROM integration_ingestions
            WHERE source_system = 'excel'
              AND knowledge_id IS NULL
              AND error_code = 'DUPLICATE_REVIEW_REQUIRED'
              AND COALESCE(review_metadata ->> 'deduplication_only', 'false') = 'true'
            ORDER BY created_at ASC, id ASC
            FOR UPDATE
            """
        )
    ).mappings().all()

    knowledge_items = sa.table(
        "knowledge_items",
        sa.column("id", sa.String()),
        sa.column("title", sa.String()),
        sa.column("subtitles", sa.JSON()),
        sa.column("content", sa.JSON()),
        sa.column("category_id", sa.String()),
        sa.column(
            "status",
            sa.Enum(
                "DRAFT",
                "REVIEW",
                "PUBLISHED",
                "DEPRECATED",
                name="knowledgestatus",
            ),
        ),
        sa.column("source", sa.String()),
        sa.column("source_session_id", sa.String()),
        sa.column("quality_score", sa.Float()),
        sa.column("applicable_scenes", sa.JSON()),
        sa.column("applicable_categories", sa.JSON()),
        sa.column("applicable_brands", sa.JSON()),
        sa.column("applicable_models", sa.JSON()),
        sa.column("related_standard_items", sa.JSON()),
        sa.column("deduplication_metadata", sa.JSON()),
        sa.column("created_by", sa.String()),
        sa.column("updated_by", sa.String()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    ingestions = sa.table(
        "integration_ingestions",
        sa.column("id", sa.String()),
        sa.column("knowledge_id", sa.String()),
        sa.column("review_status", sa.String()),
        sa.column("status", sa.String()),
        sa.column("error_code", sa.String()),
        sa.column("error_message", sa.String()),
        sa.column("updated_at", sa.DateTime()),
    )

    for row in rows:
        payload = _as_dict(row["candidate_payload"])
        review_metadata = _as_dict(row["review_metadata"])
        knowledge = _as_dict(payload.get("knowledge"))
        deduplication = _as_dict(review_metadata.get("deduplication"))
        title = str(knowledge.get("title") or "").strip()
        category_id = str(knowledge.get("category_id") or "").strip()
        if not title or not category_id or deduplication.get("action") != "review_duplicate":
            raise RuntimeError(
                "Excel duplicate review migration encountered an invalid candidate: "
                + str(row["id"])
            )

        knowledge_id = _next_knowledge_id(bind)
        deduplication["import_source"] = "excel"
        deduplication["migrated_from_candidate_id"] = row["id"]
        deduplication["requires_duplicate_confirmation"] = True
        created_by = str(review_metadata.get("queued_by") or "excel-import")[:128]

        bind.execute(
            knowledge_items.insert().values(
                id=knowledge_id,
                title=title,
                subtitles=_as_list(knowledge.get("subtitles")),
                content=_as_dict(knowledge.get("content")) or {"blocks": []},
                category_id=category_id,
                status="REVIEW",
                source="excel",
                source_session_id=row["id"],
                quality_score=0.0,
                applicable_scenes=_as_list(knowledge.get("scene_tags")),
                applicable_categories=_as_list(knowledge.get("applicable_categories")),
                applicable_brands=_as_list(knowledge.get("applicable_brands")),
                applicable_models=_as_list(knowledge.get("applicable_models")),
                related_standard_items=_as_list(knowledge.get("related_standard_items")),
                deduplication_metadata=deduplication,
                created_by=created_by,
                updated_by=created_by,
                created_at=row["created_at"],
                updated_at=row["updated_at"] or row["created_at"],
            )
        )
        bind.execute(
            ingestions.update()
            .where(ingestions.c.id == row["id"])
            .values(
                knowledge_id=knowledge_id,
                review_status=None,
                status="migrated_to_knowledge_review",
                error_code=None,
                error_message=None,
                updated_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    # This data migration deliberately preserves migrated knowledge records.
    pass
