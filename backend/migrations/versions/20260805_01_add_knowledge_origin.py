"""add knowledge origin to knowledge items

Revision ID: 20260805_01
Revises: 20260804_01
Create Date: 2026-08-05
"""

import os

import sqlalchemy as sa
from alembic import op


revision = "20260805_01"
down_revision = "20260804_01"
branch_labels = None
depends_on = None

_VALID_KNOWLEDGE_ORIGINS = {
    "headquarters_standard",
    "business_accumulation",
}


def _resolve_legacy_knowledge_origin(bind) -> str:
    has_legacy_knowledge = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM knowledge_items "
            "WHERE knowledge_origin IS NULL"
            ")"
        )
    ).scalar_one()
    has_legacy_candidate = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM integration_ingestions
                WHERE candidate_payload IS NOT NULL
                  AND json_typeof(candidate_payload) = 'object'
                  AND json_typeof(candidate_payload -> 'knowledge') = 'object'
                  AND NULLIF(btrim(
                      candidate_payload -> 'knowledge' ->> 'knowledge_origin'
                  ), '') IS NULL
            )
            """
        )
    ).scalar_one()
    configured = os.getenv("KNOWLEDGE_ORIGIN_BACKFILL", "").strip()
    if configured:
        if configured not in _VALID_KNOWLEDGE_ORIGINS:
            raise RuntimeError(
                "KNOWLEDGE_ORIGIN_BACKFILL must be "
                "'headquarters_standard' or 'business_accumulation'."
            )
        return configured
    if has_legacy_knowledge or has_legacy_candidate:
        raise RuntimeError(
            "Legacy knowledge requires an explicit "
            "KNOWLEDGE_ORIGIN_BACKFILL before migration."
        )
    return "business_accumulation"


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("knowledge_origin", sa.String(length=32), nullable=True),
    )
    bind = op.get_bind()
    legacy_origin = _resolve_legacy_knowledge_origin(bind)
    bind.execute(
        sa.text(
            "UPDATE knowledge_items "
            "SET knowledge_origin = :knowledge_origin "
            "WHERE knowledge_origin IS NULL"
        ),
        {"knowledge_origin": legacy_origin},
    )
    op.alter_column(
        "knowledge_items",
        "knowledge_origin",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_knowledge_items_knowledge_origin",
        "knowledge_items",
        "knowledge_origin IN "
        "('headquarters_standard', 'business_accumulation')",
    )
    op.create_index(
        "ix_knowledge_items_knowledge_origin",
        "knowledge_items",
        ["knowledge_origin"],
        unique=False,
    )
    bind.execute(
        sa.text(
            """
            UPDATE integration_ingestions
            SET candidate_payload = jsonb_set(
                candidate_payload::jsonb,
                '{knowledge,knowledge_origin}',
                to_jsonb(CAST(:knowledge_origin AS text)),
                true
            )::json
            WHERE candidate_payload IS NOT NULL
              AND json_typeof(candidate_payload) = 'object'
              AND json_typeof(candidate_payload -> 'knowledge') = 'object'
              AND NULLIF(btrim(
                  candidate_payload -> 'knowledge' ->> 'knowledge_origin'
              ), '') IS NULL
            """
        ),
        {"knowledge_origin": legacy_origin},
    )
    op.execute(
        """
        UPDATE integration_ingestions
        SET candidate_payload = jsonb_set(
            candidate_payload::jsonb,
            '{knowledge,business_type}',
            to_jsonb('self_operated'::text),
            true
        )::json
        WHERE candidate_payload IS NOT NULL
          AND json_typeof(candidate_payload) = 'object'
          AND json_typeof(candidate_payload -> 'knowledge') = 'object'
          AND NULLIF(btrim(
              candidate_payload -> 'knowledge' ->> 'business_type'
          ), '') IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE integration_ingestions
        SET candidate_payload = (
            candidate_payload::jsonb
            #- '{knowledge,knowledge_origin}'
        )::json
        WHERE candidate_payload IS NOT NULL
          AND json_typeof(candidate_payload) = 'object'
          AND json_typeof(candidate_payload -> 'knowledge') = 'object'
          AND (
              candidate_payload::jsonb -> 'knowledge'
          ) ? 'knowledge_origin'
        """
    )
    op.drop_index(
        "ix_knowledge_items_knowledge_origin",
        table_name="knowledge_items",
    )
    op.drop_constraint(
        "ck_knowledge_items_knowledge_origin",
        "knowledge_items",
        type_="check",
    )
    op.drop_column("knowledge_items", "knowledge_origin")
