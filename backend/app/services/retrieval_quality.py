from datetime import datetime

from sqlalchemy import String, case, cast, func, literal, or_, select

from app.models.integration import RetrievalQualityEvent

_ANONYMOUS_CONVERSATION_PREFIX = "anonymous:"
_CONVERSATION_PREFIX = "conversation:"
_EVENT_REQUEST_PREFIX = "event:"
_REQUEST_PREFIX = "request:"


def _request_identity_columns():
    """Narrow identity columns for one logical retrieval request.

    ``request_partition`` grew into a ~200 byte concatenation that had to be both the
    window partition key and the join key.  Postgres therefore spilled ~50MB to disk
    per ranking pass on ``retrieval_quality_events`` and never chose the parallel plan.
    These columns describe exactly the same groups with narrow keys; the concatenated
    labels are rebuilt after the sort, where they no longer cost a disk spill.
    """

    conversation_id = func.nullif(
        func.trim(RetrievalQualityEvent.conversation_id),
        "",
    )
    request_id = func.nullif(
        func.trim(RetrievalQualityEvent.request_id),
        "",
    )
    # An event without both ids is a request of its own, which the marker models by
    # making the partition key unique per row instead of concatenating into it.
    anonymous_marker = case(
        (
            or_(
                conversation_id.is_(None),
                request_id.is_(None),
            ),
            cast(RetrievalQualityEvent.id, String),
        ),
        else_=literal(""),
    )
    return conversation_id, request_id, anonymous_marker


def _request_event_ranks():
    """One row per event, carrying the rank of the event inside its request."""

    conversation_id, request_id, anonymous_marker = _request_identity_columns()
    return select(
        RetrievalQualityEvent.id.label("event_id"),
        RetrievalQualityEvent.created_at.label("event_created_at"),
        RetrievalQualityEvent.source_kind.label("source_kind"),
        conversation_id.label("normalized_conversation_id"),
        request_id.label("normalized_request_id"),
        anonymous_marker.label("anonymous_marker"),
        func.row_number()
        .over(
            partition_by=(
                conversation_id,
                request_id,
                anonymous_marker,
            ),
            order_by=(
                RetrievalQualityEvent.created_at.desc(),
                RetrievalQualityEvent.id.desc(),
            ),
        )
        .label("request_event_rank"),
    )


def _conversation_partition(events):
    return case(
        (
            events.c.normalized_conversation_id.is_(None),
            literal(_ANONYMOUS_CONVERSATION_PREFIX)
            + cast(events.c.event_id, String),
        ),
        else_=literal(_CONVERSATION_PREFIX)
        + events.c.normalized_conversation_id,
    )


def _request_partition(events):
    return case(
        (
            events.c.anonymous_marker != literal(""),
            literal(_EVENT_REQUEST_PREFIX) + cast(events.c.event_id, String),
        ),
        else_=(
            literal(_REQUEST_PREFIX)
            + events.c.normalized_conversation_id
            + literal(":")
            + events.c.normalized_request_id
        ),
    )


def _source_partition(events):
    return case(
        (
            events.c.source_kind.in_(("reply", "standard")),
            events.c.source_kind,
        ),
        else_="combined",
    )


def _ranked_retrieval_quality_events(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
):
    """Rank every event by request, then pick the surviving request per conversation.

    The ranking itself intentionally does *not* pre-filter by time window.  A request
    belongs to the window that holds its latest source-pool event, and a partition
    level pre-filter only ever removed whole requests that the representative filter
    below drops anyway, while forcing an extra full scan plus ``DISTINCT``.
    """

    event_ranks = _request_event_ranks().cte("event_ranks")
    ranked = (
        select(
            event_ranks.c.event_id,
            event_ranks.c.event_created_at,
            event_ranks.c.source_kind,
            _conversation_partition(event_ranks).label("conversation_partition"),
            _request_partition(event_ranks).label("request_partition"),
            event_ranks.c.request_event_rank,
        )
        .subquery()
    )
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
    # The source-pool rank is only needed for the requests that survive the
    # conversation filter.  Restricting to those requests keeps every window
    # partition whole (the filter is on the partition key itself), so the ranks are
    # identical to ranking the entire table while touching ~4x fewer rows.
    surviving_events = (
        select(
            ranked.c.event_id,
            ranked.c.event_created_at,
            ranked.c.request_partition,
            _source_partition(ranked).label("source_partition"),
        )
        .where(
            ranked.c.request_partition.in_(latest_request_partitions),
        )
        .subquery()
    )
    ranked_sources = (
        select(
            surviving_events.c.event_id,
            func.row_number()
            .over(
                partition_by=(
                    surviving_events.c.request_partition,
                    surviving_events.c.source_partition,
                ),
                order_by=(
                    surviving_events.c.event_created_at.desc(),
                    surviving_events.c.event_id.desc(),
                ),
            )
            .label("source_event_rank"),
        )
        .subquery()
    )
    return select(ranked_sources.c.event_id).where(
        ranked_sources.c.source_event_rank == 1
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
