from datetime import datetime

from sqlalchemy import String, case, cast, func, literal, or_, select

from app.models.integration import RetrievalQualityEvent


def _ranked_retrieval_quality_events(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
):
    normalized_conversation_id = func.nullif(
        func.trim(RetrievalQualityEvent.conversation_id),
        "",
    )
    normalized_request_id = func.nullif(
        func.trim(RetrievalQualityEvent.request_id),
        "",
    )
    conversation_partition = case(
        (
            normalized_conversation_id.is_(None),
            literal("anonymous:") + cast(RetrievalQualityEvent.id, String),
        ),
        else_=literal("conversation:") + normalized_conversation_id,
    )
    request_partition = case(
        (
            or_(
                normalized_conversation_id.is_(None),
                normalized_request_id.is_(None),
            ),
            literal("event:") + cast(RetrievalQualityEvent.id, String),
        ),
        else_=(
            literal("request:")
            + normalized_conversation_id
            + literal(":")
            + normalized_request_id
        ),
    )
    source_partition = case(
        (
            RetrievalQualityEvent.source_kind.in_(("reply", "standard")),
            RetrievalQualityEvent.source_kind,
        ),
        else_="combined",
    )
    ranked_query = select(
        RetrievalQualityEvent.id.label("event_id"),
        RetrievalQualityEvent.created_at.label("event_created_at"),
        conversation_partition.label("conversation_partition"),
        request_partition.label("request_partition"),
        func.row_number()
        .over(
            partition_by=request_partition,
            order_by=(
                RetrievalQualityEvent.created_at.desc(),
                RetrievalQualityEvent.id.desc(),
            ),
        )
        .label("request_event_rank"),
        func.row_number()
        .over(
            partition_by=(request_partition, source_partition),
            order_by=(
                RetrievalQualityEvent.created_at.desc(),
                RetrievalQualityEvent.id.desc(),
            ),
        )
        .label("source_event_rank"),
    )
    if start_at is not None or end_at is not None:
        candidate_request_partitions = select(
            request_partition.label("request_partition")
        )
        if start_at is not None:
            candidate_request_partitions = (
                candidate_request_partitions.where(
                    RetrievalQualityEvent.created_at >= start_at
                )
            )
        if end_at is not None:
            candidate_request_partitions = (
                candidate_request_partitions.where(
                    RetrievalQualityEvent.created_at < end_at
                )
            )
        candidate_request_partitions = (
            candidate_request_partitions.distinct().subquery()
        )
        ranked_query = ranked_query.where(
            request_partition.in_(
                select(candidate_request_partitions.c.request_partition)
            )
        )
    ranked = ranked_query.subquery()
    request_representatives = (
        select(
            ranked.c.request_partition,
            ranked.c.conversation_partition,
            ranked.c.event_created_at.label("request_created_at"),
            ranked.c.event_id.label("request_event_id"),
        )
        .where(ranked.c.request_event_rank == 1)
        .subquery()
    )
    # A logical request belongs to exactly one time window: the window that
    # contains its latest source-pool event. Once selected, all source pools
    # remain available in ``ranked`` for the merged analytics payload.
    filtered_request_representatives = select(request_representatives)
    if start_at is not None:
        filtered_request_representatives = (
            filtered_request_representatives.where(
                request_representatives.c.request_created_at >= start_at
            )
        )
    if end_at is not None:
        filtered_request_representatives = (
            filtered_request_representatives.where(
                request_representatives.c.request_created_at < end_at
            )
        )
    filtered_request_representatives = (
        filtered_request_representatives.subquery()
    )
    ranked_requests = (
        select(
            filtered_request_representatives.c.request_partition,
            func.row_number()
            .over(
                partition_by=(
                    filtered_request_representatives.c.conversation_partition
                ),
                order_by=(
                    filtered_request_representatives.c.request_created_at.desc(),
                    filtered_request_representatives.c.request_event_id.desc(),
                ),
            )
            .label("conversation_request_rank"),
        )
        .subquery()
    )
    latest_request_partitions = select(
        ranked_requests.c.request_partition
    ).where(ranked_requests.c.conversation_request_rank == 1)
    return ranked, latest_request_partitions


def latest_retrieval_quality_request_event_ids(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
):
    """Return the latest event from each source pool in the final request."""
    ranked, latest_request_partitions = _ranked_retrieval_quality_events(
        start_at=start_at,
        end_at=end_at,
    )
    return select(ranked.c.event_id).where(
        ranked.c.request_partition.in_(latest_request_partitions),
        ranked.c.source_event_rank == 1,
    )


def latest_retrieval_quality_event_ids(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
):
    """Return one representative event for the final request per work order."""
    ranked, latest_request_partitions = _ranked_retrieval_quality_events(
        start_at=start_at,
        end_at=end_at,
    )
    return select(ranked.c.event_id).where(
        ranked.c.request_partition.in_(latest_request_partitions),
        ranked.c.request_event_rank == 1,
    )
