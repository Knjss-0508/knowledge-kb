from sqlalchemy import String, case, cast, func, or_, select

from app.models.integration import RetrievalQualityEvent


def latest_retrieval_quality_event_ids():
    """Return the latest event per work order and candidate pool."""
    normalized_conversation_id = func.nullif(
        func.trim(RetrievalQualityEvent.conversation_id),
        "",
    )
    conversation_partition = case(
        (
            normalized_conversation_id.is_(None),
            "anonymous:" + cast(RetrievalQualityEvent.id, String),
        ),
        else_=normalized_conversation_id,
    )
    normalized_source_kind = case(
        (
            RetrievalQualityEvent.source_kind.in_(("reply", "standard")),
            RetrievalQualityEvent.source_kind,
        ),
        else_="combined",
    )
    has_explicit_pool = func.max(
        case(
            (
                normalized_source_kind.in_(("reply", "standard")),
                1,
            ),
            else_=0,
        )
    ).over(partition_by=conversation_partition)
    ranked = (
        select(
            RetrievalQualityEvent.id.label("event_id"),
            normalized_source_kind.label("source_kind"),
            has_explicit_pool.label("has_explicit_pool"),
            func.row_number()
            .over(
                partition_by=(
                    conversation_partition,
                    normalized_source_kind,
                ),
                order_by=(
                    RetrievalQualityEvent.created_at.desc(),
                    RetrievalQualityEvent.id.desc(),
                ),
            )
            .label("event_rank"),
        )
        .subquery()
    )
    return select(ranked.c.event_id).where(
        ranked.c.event_rank == 1,
        or_(
            ranked.c.source_kind != "combined",
            ranked.c.has_explicit_pool == 0,
        ),
    )
