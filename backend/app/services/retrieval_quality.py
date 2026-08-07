from sqlalchemy import case, func, select

from app.models.integration import RetrievalQualityEvent


def latest_retrieval_quality_event_ids():
    """Return one latest event per work order while retaining anonymous events."""
    normalized_conversation_id = func.nullif(
        func.trim(RetrievalQualityEvent.conversation_id),
        "",
    )
    partition_key = case(
        (
            normalized_conversation_id.is_(None),
            RetrievalQualityEvent.id,
        ),
        else_=normalized_conversation_id,
    )
    ranked = (
        select(
            RetrievalQualityEvent.id.label("event_id"),
            func.row_number()
            .over(
                partition_by=partition_key,
                order_by=(
                    RetrievalQualityEvent.created_at.desc(),
                    RetrievalQualityEvent.id.desc(),
                ),
            )
            .label("event_rank"),
        )
        .subquery()
    )
    return select(ranked.c.event_id).where(ranked.c.event_rank == 1)
