from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models.knowledge import (
    Knowledge,
    KnowledgeEmbedding,
    KnowledgeSearchEmbedding,
    KnowledgeStatus,
    KnowledgeTag,
)
from app.services.embedding import embed_texts
from app.services.embedding_runtime import get_active_runtime_values


_QueryEmbeddingKey = tuple[str, str, str, int, str, str]
_QUERY_EMBEDDING_CACHE_MAXSIZE = 512
_QUERY_EMBEDDING_CACHE_GUARD = Lock()
_QUERY_EMBEDDING_CACHE: OrderedDict[
    _QueryEmbeddingKey, tuple[float, ...]
] = OrderedDict()


def _cache_query_embedding(
    key: _QueryEmbeddingKey,
    vector: list[float] | tuple[float, ...],
) -> tuple[float, ...]:
    normalized = tuple(float(value) for value in vector)
    with _QUERY_EMBEDDING_CACHE_GUARD:
        _QUERY_EMBEDDING_CACHE[key] = normalized
        _QUERY_EMBEDDING_CACHE.move_to_end(key)
        while len(_QUERY_EMBEDDING_CACHE) > _QUERY_EMBEDDING_CACHE_MAXSIZE:
            _QUERY_EMBEDDING_CACHE.popitem(last=False)
    return normalized


def _get_cached_query_embedding(
    key: _QueryEmbeddingKey,
) -> tuple[float, ...] | None:
    with _QUERY_EMBEDDING_CACHE_GUARD:
        vector = _QUERY_EMBEDDING_CACHE.get(key)
        if vector is not None:
            _QUERY_EMBEDDING_CACHE.move_to_end(key)
        return vector


@dataclass
class _QueryEmbeddingRequest:
    key: _QueryEmbeddingKey
    query: str
    done: Event = field(default_factory=Event)
    vector: tuple[float, ...] | None = None
    error: Exception | None = None


class _QueryEmbeddingBatcher:
    """Coalesce near-simultaneous cache misses into one model request."""

    def __init__(self) -> None:
        self._queue: Queue[_QueryEmbeddingRequest] = Queue()
        self._worker_guard = Lock()
        self._worker: Thread | None = None

    def _ensure_worker(self) -> None:
        with self._worker_guard:
            if self._worker and self._worker.is_alive():
                return
            self._worker = Thread(
                target=self._run,
                name="knowledge-query-embedding-batcher",
                daemon=True,
            )
            self._worker.start()

    def submit(
        self,
        key: _QueryEmbeddingKey,
        query: str,
    ) -> tuple[float, ...]:
        cached = _get_cached_query_embedding(key)
        if cached is not None:
            return cached

        request = _QueryEmbeddingRequest(key=key, query=query)
        self._queue.put(request)
        self._ensure_worker()
        request.done.wait()
        if request.error is not None:
            raise request.error
        if request.vector is None:
            raise RuntimeError("Query embedding batcher returned no vector.")
        return request.vector

    def _run(self) -> None:
        while True:
            first = self._queue.get()
            batch = [first]
            max_batch_size = max(int(settings.QUERY_EMBEDDING_BATCH_SIZE), 1)
            wait_seconds = max(
                int(settings.QUERY_EMBEDDING_BATCH_WAIT_MS), 0
            ) / 1000
            deadline = monotonic() + wait_seconds
            while len(batch) < max_batch_size:
                timeout = deadline - monotonic()
                if timeout <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=timeout))
                except Empty:
                    break
            self._process(batch)

    def _process(self, batch: list[_QueryEmbeddingRequest]) -> None:
        try:
            pending: dict[_QueryEmbeddingKey, list[_QueryEmbeddingRequest]] = {}
            for request in batch:
                cached = _get_cached_query_embedding(request.key)
                if cached is not None:
                    request.vector = cached
                    continue
                pending.setdefault(request.key, []).append(request)

            if pending:
                keys = list(pending)
                vectors = embed_texts([pending[key][0].query for key in keys])
                if len(vectors) != len(keys):
                    raise ValueError(
                        "Query embedding result count does not match input count."
                    )
                for key, vector in zip(keys, vectors):
                    cached = _cache_query_embedding(key, vector)
                    for request in pending[key]:
                        request.vector = cached
        except Exception as exc:
            for request in batch:
                if request.vector is None:
                    request.error = exc
        finally:
            for request in batch:
                request.done.set()


_QUERY_EMBEDDING_BATCHER = _QueryEmbeddingBatcher()


def _cached_query_embedding(
    provider: str,
    base_url: str,
    model: str,
    dimensions: int,
    version: str,
    query: str,
) -> tuple[float, ...]:
    """Return a cached vector for compatibility with existing callers."""
    key = (provider, base_url, model, dimensions, version, query)
    cached = _get_cached_query_embedding(key)
    if cached is not None:
        return cached
    return _cache_query_embedding(key, embed_texts([query])[0])


def clear_query_embedding_cache() -> None:
    """Clear process-local query vectors after test/config changes."""
    with _QUERY_EMBEDDING_CACHE_GUARD:
        _QUERY_EMBEDDING_CACHE.clear()


def _query_embedding(query: str) -> list[float]:
    normalized = query.strip()
    if not normalized:
        raise ValueError("Embedding input must not be blank.")
    key = (
        settings.EMBEDDING_PROVIDER.strip().lower(),
        settings.EMBEDDING_BASE_URL.rstrip("/"),
        settings.EMBEDDING_MODEL,
        settings.EMBEDDING_DIMENSIONS,
        settings.VERSION,
        normalized,
    )
    vector = _QUERY_EMBEDDING_BATCHER.submit(key, normalized)
    return [float(value) for value in vector]


DedupAction = Literal["create", "review_duplicate", "block_duplicate"]
DedupMatchType = Literal[
    "exact",
    "title_exact",
    "semantic",
    "content_containment",
]


@dataclass
class DedupMatch:
    knowledge_id: str
    title: str
    status: str
    knowledge_origin: str
    business_type: str
    category_id: str
    match_type: DedupMatchType
    similarity: float
    title_similarity: float | None = None
    content_similarity: float | None = None


@dataclass
class DedupDecision:
    action: DedupAction
    content_hash: str
    embedding: list[float] | None
    title_embedding: list[float] | None
    content_embedding: list[float] | None
    matches: list[DedupMatch]
    block_threshold: float = 0.96
    review_threshold: float = 0.88


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"div", "p", "li"}:
            self.parts.append("\n")

    def text(self) -> str:
        return "\n".join(
            line.strip()
            for line in "".join(self.parts).splitlines()
            if line.strip()
        )


def _rich_text_to_plain_text(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _rich_text_to_plain_text(value)
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _content_to_text(item)))
    if isinstance(value, dict):
        if value.get("type") in {"image", "video"}:
            return "\n".join(
                part
                for key in ("alt", "caption", "title")
                if key in value and (part := _content_to_text(value[key]))
            )
        blocks = value.get("blocks")
        if isinstance(blocks, list):
            return _content_to_text(blocks)
        parts = []
        for key in ("value", "text", "alt", "caption", "title"):
            if key in value:
                text = _content_to_text(value[key])
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value).strip()


def build_embedding_text(
    title: str,
    subtitles: list[str] | None,
    content: Any,
    scene_tags: list[str] | None = None,
) -> str:
    """Build the stable text used by duplicate detection.

    Subtitles are alternate search phrasings, not authoritative knowledge content.
    Keeping them out prevents a large or unrelated subtitle list from shifting the
    duplicate score.
    """
    parts = [title.strip()]
    content_text = _content_to_text(content)
    if content_text:
        parts.append(content_text)
    return "\n".join(parts).strip()


def build_dedup_documents(title: str, content: Any) -> tuple[str, str, str]:
    """Build separate title and content documents for field-aware deduplication."""
    title_text = title.strip()
    content_text = _content_to_text(content)
    if not content_text:
        content_text = title_text
    return "\n".join(part for part in (title_text, content_text) if part), title_text, content_text


def content_hash_for_text(text: str) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot_product = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot_product / (left_norm * right_norm)


def _combined_dedup_similarity(title_similarity: float, content_similarity: float) -> float:
    """Require both fields to agree instead of allowing one field to dominate."""
    return round(min(title_similarity, content_similarity), 6)


def _has_enough_semantic_content(
    content_text: str,
    minimum_chars: int | None = None,
) -> bool:
    """Short fragments do not provide reliable semantic duplicate evidence."""
    minimum = (
        settings.DEDUP_MIN_SEMANTIC_CONTENT_CHARS
        if minimum_chars is None
        else minimum_chars
    )
    return len(content_text.strip()) >= minimum


def _normalized_containment_text(content_text: str) -> str:
    return "".join(content_text.split()).casefold()


def _normalized_comparison_text(value: str) -> str:
    """Normalize visible text for deterministic title/content equality checks."""
    return "".join(value.split()).casefold()


def _has_content_containment(
    left: str,
    right: str,
    minimum_chars: int | None = None,
) -> bool:
    """Detect meaningful literal inclusion that embedding similarity can miss."""
    normalized_left = _normalized_containment_text(left)
    normalized_right = _normalized_containment_text(right)
    minimum = (
        settings.DEDUP_MIN_CONTAINMENT_CONTENT_CHARS
        if minimum_chars is None
        else minimum_chars
    )
    if (
        len(normalized_left) < minimum
        or len(normalized_right) < minimum
    ):
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def _knowledge_text(item: Knowledge) -> str:
    return build_embedding_text(
        item.title,
        None,
        item.content,
    )


def _knowledge_dedup_documents(item: Knowledge) -> tuple[str, str, str]:
    return build_dedup_documents(item.title, item.content)


def _find_embedding(db: Session, knowledge_id: str) -> KnowledgeEmbedding | None:
    return (
        db.query(KnowledgeEmbedding)
        .filter(
            KnowledgeEmbedding.knowledge_id == knowledge_id,
            KnowledgeEmbedding.embedding_model == settings.EMBEDDING_MODEL,
        )
        .first()
    )


def _upsert_embeddings(
    db: Session,
    items: list[Knowledge],
    texts: list[str],
    content_hashes: list[str],
) -> list[list[float]]:
    documents = [_knowledge_dedup_documents(item) for item in items]
    title_texts = [title_text for _, title_text, _ in documents]
    content_texts = [content_text for _, _, content_text in documents]
    embedded = embed_texts([*texts, *title_texts, *content_texts])
    count = len(items)
    vectors = embedded[:count]
    title_vectors = embedded[count : count * 2]
    content_vectors = embedded[count * 2 :]
    for item, content_hash, vector, title_vector, content_vector in zip(
        items,
        content_hashes,
        vectors,
        title_vectors,
        content_vectors,
    ):
        record = _find_embedding(db, item.id)
        if record:
            record.content_hash = content_hash
            record.embedding_dimension = len(vector)
            record.embedding = vector
            record.embedding_vector = vector
            record.title_embedding_vector = title_vector
            record.content_embedding_vector = content_vector
        else:
            db.add(
                KnowledgeEmbedding(
                    id=f"emb-{uuid.uuid4().hex[:16]}",
                    knowledge_id=item.id,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_dimension=len(vector),
                    content_hash=content_hash,
                    embedding=vector,
                    embedding_vector=vector,
                    title_embedding_vector=title_vector,
                    content_embedding_vector=content_vector,
                )
            )
    db.flush()
    return vectors


def ensure_embedding(db: Session, item: Knowledge) -> list[float]:
    text = _knowledge_text(item)
    content_hash = content_hash_for_text(text)
    record = _find_embedding(db, item.id)
    if (
        record
        and record.content_hash == content_hash
        and record.embedding
        and record.title_embedding_vector is not None
        and record.content_embedding_vector is not None
    ):
        return [float(value) for value in record.embedding]
    return _upsert_embeddings(db, [item], [text], [content_hash])[0]


def check_duplicate(
    db: Session,
    *,
    title: str,
    subtitles: list[str] | None,
    content: Any,
    scene_tags: list[str] | None,
    knowledge_origin: str,
    business_type: str,
    exclude_knowledge_id: str | None = None,
    embedding_vectors: tuple[list[float], list[float], list[float]] | None = None,
) -> DedupDecision:
    runtime = get_active_runtime_values(db)
    max_candidates = int(runtime["dedup_max_candidates"])
    block_threshold = float(runtime["dedup_block_threshold"])
    review_threshold = float(runtime["dedup_review_threshold"])
    minimum_semantic_chars = int(runtime["dedup_min_semantic_content_chars"])
    minimum_containment_chars = int(runtime["dedup_min_containment_content_chars"])

    def _decision(**values) -> DedupDecision:
        return DedupDecision(
            block_threshold=block_threshold,
            review_threshold=review_threshold,
            **values,
        )

    text, title_text, content_text = build_dedup_documents(title, content)
    if not text:
        raise ValueError("Knowledge content is empty after normalization.")
    content_hash = content_hash_for_text(text)
    normalized_title = _normalized_comparison_text(title_text)
    normalized_content = _normalized_comparison_text(content_text)
    active_statuses = [KnowledgeStatus.REVIEW, KnowledgeStatus.PUBLISHED]

    title_query = db.query(Knowledge).filter(
        Knowledge.status.in_(active_statuses),
        func.lower(func.trim(Knowledge.title)) == title_text.lower(),
        Knowledge.knowledge_origin == knowledge_origin,
        Knowledge.business_type == business_type,
    )
    if exclude_knowledge_id:
        title_query = title_query.filter(Knowledge.id != exclude_knowledge_id)
    title_matches = [
        item
        for item in (
            title_query.order_by(Knowledge.updated_at.desc())
            .limit(max_candidates)
            .all()
        )
        if _normalized_comparison_text(item.title) == normalized_title
    ]
    exact_title_and_content_matches = [
        item
        for item in title_matches
        if _normalized_comparison_text(_content_to_text(item.content))
        == normalized_content
    ]
    if exact_title_and_content_matches:
        return _decision(
            action="block_duplicate",
            content_hash=content_hash,
            embedding=None,
            title_embedding=None,
            content_embedding=None,
            matches=[
                DedupMatch(
                    knowledge_id=item.id,
                    title=item.title,
                    status=item.status.value,
                    knowledge_origin=getattr(item, "knowledge_origin", ""),
                    business_type=getattr(item, "business_type", ""),
                    category_id=item.category_id,
                    match_type="exact",
                    similarity=1.0,
                )
                for item in exact_title_and_content_matches
            ],
        )

    if embedding_vectors is None:
        query_vector, title_vector, content_vector = embed_texts(
            [text, title_text, content_text]
        )
    else:
        if len(embedding_vectors) != 3:
            raise ValueError("Precomputed deduplication embeddings are incomplete.")
        query_vector, title_vector, content_vector = embedding_vectors
    if title_matches:
        return _decision(
            action="review_duplicate",
            content_hash=content_hash,
            embedding=query_vector,
            title_embedding=title_vector,
            content_embedding=content_vector,
            matches=[
                DedupMatch(
                    knowledge_id=item.id,
                    title=item.title,
                    status=item.status.value,
                    knowledge_origin=getattr(item, "knowledge_origin", ""),
                    business_type=getattr(item, "business_type", ""),
                    category_id=item.category_id,
                    match_type="title_exact",
                    similarity=1.0,
                    title_similarity=1.0,
                )
                for item in title_matches
            ],
        )

    query = db.query(Knowledge).join(
        KnowledgeEmbedding,
        KnowledgeEmbedding.knowledge_id == Knowledge.id,
    ).filter(
        Knowledge.status.in_(active_statuses),
        KnowledgeEmbedding.embedding_model == settings.EMBEDDING_MODEL,
    )
    if exclude_knowledge_id:
        query = query.filter(Knowledge.id != exclude_knowledge_id)
    query = query.filter(
        Knowledge.knowledge_origin == knowledge_origin,
        Knowledge.business_type == business_type,
    )
    exact_matches = (
        query.filter(KnowledgeEmbedding.content_hash == content_hash)
        .order_by(Knowledge.updated_at.desc())
        .limit(max_candidates)
        .all()
    )
    if exact_matches:
        return _decision(
            action="block_duplicate",
            content_hash=content_hash,
            embedding=None,
            title_embedding=None,
            content_embedding=None,
            matches=[
                DedupMatch(
                    knowledge_id=item.id,
                    title=item.title,
                    status=item.status.value,
                    knowledge_origin=getattr(item, "knowledge_origin", ""),
                    business_type=getattr(item, "business_type", ""),
                    category_id=item.category_id,
                    match_type="exact",
                    similarity=1.0,
                )
                for item in exact_matches[:max_candidates]
            ],
        )

    containment_matches = [
        item
        for item in query.all()
        if _has_content_containment(
            content_text,
            _content_to_text(item.content),
            minimum_containment_chars,
        )
    ]
    if containment_matches:
        return _decision(
            action="review_duplicate",
            content_hash=content_hash,
            embedding=query_vector,
            title_embedding=title_vector,
            content_embedding=content_vector,
            matches=[
                DedupMatch(
                    knowledge_id=item.id,
                    title=item.title,
                    status=item.status.value,
                    knowledge_origin=getattr(item, "knowledge_origin", ""),
                    business_type=getattr(item, "business_type", ""),
                    category_id=item.category_id,
                    match_type="content_containment",
                    similarity=1.0,
                )
                for item in containment_matches[:max_candidates]
            ],
        )
    if not _has_enough_semantic_content(content_text, minimum_semantic_chars):
        return _decision(
            action="create",
            content_hash=content_hash,
            embedding=query_vector,
            title_embedding=title_vector,
            content_embedding=content_vector,
            matches=[],
        )
    distance = KnowledgeEmbedding.embedding_vector.cosine_distance(query_vector)
    candidates = (
        query.filter(KnowledgeEmbedding.embedding_vector.is_not(None))
        .with_entities(Knowledge, distance.label("distance"))
        .order_by(distance)
        .limit(max_candidates)
        .all()
    )
    records_by_id = {
        item.id: _find_embedding(db, item.id)
        for item, _ in candidates
    }
    missing_items = [
        item
        for item, _ in candidates
        if (
            records_by_id.get(item.id) is not None
            and (
                records_by_id[item.id].title_embedding_vector is None
                or records_by_id[item.id].content_embedding_vector is None
            )
        )
    ]
    if missing_items:
        missing_texts = [_knowledge_text(item) for item in missing_items]
        _upsert_embeddings(
            db,
            missing_items,
            missing_texts,
            [content_hash_for_text(text) for text in missing_texts],
        )
        records_by_id.update(
            {
                item.id: _find_embedding(db, item.id)
                for item in missing_items
            }
        )

    matches: list[DedupMatch] = []
    for item, _ in candidates:
        record = records_by_id.get(item.id)
        if (
            not record
            or record.title_embedding_vector is None
            or record.content_embedding_vector is None
        ):
            continue
        title_similarity = _cosine_similarity(
            title_vector,
            [float(value) for value in record.title_embedding_vector],
        )
        content_similarity = _cosine_similarity(
            content_vector,
            [float(value) for value in record.content_embedding_vector],
        )
        matches.append(
            DedupMatch(
                knowledge_id=item.id,
                title=item.title,
                status=item.status.value,
                knowledge_origin=getattr(item, "knowledge_origin", ""),
                business_type=getattr(item, "business_type", ""),
                category_id=item.category_id,
                match_type="semantic",
                similarity=_combined_dedup_similarity(
                    max(0.0, title_similarity),
                    max(0.0, content_similarity),
                ),
                title_similarity=round(max(0.0, title_similarity), 6),
                content_similarity=round(max(0.0, content_similarity), 6),
            )
        )
    matches.sort(key=lambda item: item.similarity, reverse=True)
    matches = [
        item
        for item in matches
        if item.similarity >= review_threshold
    ][:max_candidates]
    top_score = matches[0].similarity if matches else 0.0
    action: DedupAction = "create"
    if top_score >= block_threshold:
        action = "block_duplicate"
    elif top_score >= review_threshold:
        action = "review_duplicate"

    return _decision(
        action=action,
        content_hash=content_hash,
        embedding=query_vector,
        title_embedding=title_vector,
        content_embedding=content_vector,
        matches=matches,
    )


def save_embedding(
    db: Session,
    *,
    knowledge: Knowledge,
    content_hash: str,
    embedding: list[float],
    title_embedding: list[float],
    content_embedding: list[float],
) -> None:
    record = _find_embedding(db, knowledge.id)
    if record:
        record.content_hash = content_hash
        record.embedding_dimension = len(embedding)
        record.embedding = embedding
        record.embedding_vector = embedding
        record.title_embedding_vector = title_embedding
        record.content_embedding_vector = content_embedding
    else:
        db.add(
            KnowledgeEmbedding(
                id=f"emb-{uuid.uuid4().hex[:16]}",
                knowledge_id=knowledge.id,
                embedding_model=settings.EMBEDDING_MODEL,
                embedding_dimension=len(embedding),
                content_hash=content_hash,
                embedding=embedding,
                embedding_vector=embedding,
                title_embedding_vector=title_embedding,
                content_embedding_vector=content_embedding,
            )
        )
    db.flush()


def _split_search_chunks(
    text: str,
    *,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    chunk_size = max(
        settings.SEARCH_CHUNK_SIZE if chunk_size is None else int(chunk_size),
        100,
    )
    overlap = min(
        max(settings.SEARCH_CHUNK_OVERLAP if overlap is None else int(overlap), 0),
        chunk_size - 1,
    )
    if len(normalized) <= chunk_size:
        return [normalized]

    paragraphs = [part.strip() for part in normalized.splitlines() if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(paragraph):
                end = min(start + chunk_size, len(paragraph))
                chunks.append(paragraph[start:end])
                if end == len(paragraph):
                    break
                start = max(end - overlap, start + 1)
            continue
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


def build_search_documents_for_fields(
    title: str,
    subtitles: list[str] | None,
    content: Any,
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[tuple[str, int, str]]:
    """Build search documents from fields without changing their text format."""
    documents: list[tuple[str, int, str]] = []
    normalized_title = title.strip()
    for index, subtitle in enumerate(subtitles or []):
        subtitle_text = _content_to_text(subtitle)
        if subtitle_text:
            documents.append(("subtitle", index, f"{normalized_title}\n{subtitle_text}"))

    content_text = _content_to_text(content)
    for index, chunk in enumerate(
        _split_search_chunks(
            content_text,
            chunk_size=chunk_size,
            overlap=chunk_overlap,
        )
    ):
        documents.append(("content", index, f"{normalized_title}\n{chunk}"))

    if not documents and normalized_title:
        documents.append(("title", 0, normalized_title))
    return documents


def build_search_documents(
    item: Knowledge,
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[tuple[str, int, str]]:
    """Build independent search documents without polluting the dedup vector."""
    return build_search_documents_for_fields(
        item.title,
        item.subtitles,
        item.content,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def _find_search_embedding(
    db: Session,
    knowledge_id: str,
    embedding_kind: str,
    chunk_index: int,
) -> KnowledgeSearchEmbedding | None:
    return (
        db.query(KnowledgeSearchEmbedding)
        .filter(
            KnowledgeSearchEmbedding.knowledge_id == knowledge_id,
            KnowledgeSearchEmbedding.embedding_model == settings.EMBEDDING_MODEL,
            KnowledgeSearchEmbedding.embedding_kind == embedding_kind,
            KnowledgeSearchEmbedding.chunk_index == chunk_index,
        )
        .first()
    )


def ensure_search_embeddings(
    db: Session,
    item: Knowledge,
    *,
    precomputed_vectors: dict[tuple[str, int, str], list[float]] | None = None,
) -> int:
    """Create or refresh search vectors, reusing exact precomputed documents."""
    runtime = get_active_runtime_values(db)
    documents = build_search_documents(
        item,
        chunk_size=int(runtime["search_chunk_size"]),
        chunk_overlap=int(runtime["search_chunk_overlap"]),
    )
    existing = (
        db.query(KnowledgeSearchEmbedding)
        .filter(
            KnowledgeSearchEmbedding.knowledge_id == item.id,
            KnowledgeSearchEmbedding.embedding_model == settings.EMBEDDING_MODEL,
        )
        .all()
    )
    expected_keys = {
        (kind, index): content_hash_for_text(text)
        for kind, index, text in documents
    }
    existing_by_key = {(row.embedding_kind, row.chunk_index): row for row in existing}
    missing = [
        (kind, index, text, expected_keys[(kind, index)])
        for kind, index, text in documents
        if (
            (kind, index) not in existing_by_key
            or existing_by_key[(kind, index)].content_hash != expected_keys[(kind, index)]
        )
    ]
    stale = [
        row
        for key, row in existing_by_key.items()
        if key not in expected_keys
    ]
    for row in stale:
        db.delete(row)
    reusable_by_hash: dict[str, list[float]] = {}
    dedup_record = _find_embedding(db, item.id)
    if dedup_record:
        dedup_text, title_text, content_text = _knowledge_dedup_documents(item)
        for source_text, vector in (
            (dedup_text, dedup_record.embedding_vector),
            (title_text, dedup_record.title_embedding_vector),
            (content_text, dedup_record.content_embedding_vector),
        ):
            if vector is not None:
                reusable_by_hash[content_hash_for_text(source_text)] = [
                    float(value) for value in vector
                ]

    vectors_by_key: dict[tuple[str, int, str], list[float]] = {}
    unresolved = []
    for kind, index, text, content_hash in missing:
        key = (kind, index, content_hash)
        vector = (precomputed_vectors or {}).get(key)
        if vector is None:
            vector = reusable_by_hash.get(content_hash)
        if vector is None:
            unresolved.append((kind, index, text, content_hash))
        else:
            vectors_by_key[key] = vector

    if unresolved:
        vectors = embed_texts([text for _, _, text, _ in unresolved])
        for (kind, index, text, content_hash), vector in zip(unresolved, vectors):
            vectors_by_key[(kind, index, content_hash)] = vector

    for (kind, index, text, content_hash) in missing:
        vector = vectors_by_key[(kind, index, content_hash)]
        row = existing_by_key.get((kind, index))
        if row:
            row.content_hash = content_hash
            row.source_text = text
            row.embedding_dimension = len(vector)
            row.embedding = vector
            row.embedding_vector = vector
        else:
            db.add(
                KnowledgeSearchEmbedding(
                    id=f"se-{uuid.uuid4().hex[:16]}",
                    knowledge_id=item.id,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_kind=kind,
                    chunk_index=index,
                    content_hash=content_hash,
                    source_text=text,
                    embedding_dimension=len(vector),
                    embedding=vector,
                    embedding_vector=vector,
                )
            )
    db.flush()
    return len(documents)


def search_embeddings(
    db: Session,
    *,
    query: str,
    knowledge_origin: str,
    business_type: str,
    category_id: str | None = None,
    tags: list[str] | None = None,
    top_k: int = 10,
) -> list[tuple[Knowledge, float]]:
    """Semantic search in PostgreSQL, aggregated by the parent knowledge item."""
    query_vector = _query_embedding(query)
    distance = KnowledgeSearchEmbedding.embedding_vector.cosine_distance(query_vector)
    item_query = (
        db.query(Knowledge, distance.label("distance"))
        .options(joinedload(Knowledge.category))
        .join(
            KnowledgeSearchEmbedding,
            KnowledgeSearchEmbedding.knowledge_id == Knowledge.id,
        )
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
            Knowledge.knowledge_origin == knowledge_origin,
            Knowledge.business_type == business_type,
            KnowledgeSearchEmbedding.embedding_model == settings.EMBEDDING_MODEL,
            KnowledgeSearchEmbedding.embedding_vector.is_not(None),
        )
    )
    if category_id:
        item_query = item_query.filter(Knowledge.category_id == category_id)
    if tags:
        item_query = item_query.filter(
            Knowledge.tags.any(KnowledgeTag.tag_value_id.in_(tags))
        )

    rows = (
        item_query.order_by(distance)
        .limit(max(top_k * 12, 50))
        .all()
    )
    scores: dict[str, float] = {}
    items: dict[str, Knowledge] = {}
    for item, distance_value in rows:
        score = max(0.0, 1.0 - float(distance_value))
        items[item.id] = item
        scores[item.id] = max(scores.get(item.id, 0.0), score)
    ranked = sorted(
        ((items[knowledge_id], score) for knowledge_id, score in scores.items()),
        key=lambda pair: (pair[1], pair[0].quality_score or 0.0),
        reverse=True,
    )
    return ranked[:top_k]
