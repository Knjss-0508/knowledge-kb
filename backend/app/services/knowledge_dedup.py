from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.knowledge import (
    Knowledge,
    KnowledgeEmbedding,
    KnowledgeSearchEmbedding,
    KnowledgeStatus,
    KnowledgeTag,
)
from app.services.embedding import embed_texts


DedupAction = Literal["create", "review_duplicate", "block_duplicate"]
DedupMatchType = Literal["exact", "semantic", "content_containment"]


@dataclass
class DedupMatch:
    knowledge_id: str
    title: str
    status: str
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


def _has_enough_semantic_content(content_text: str) -> bool:
    """Short fragments do not provide reliable semantic duplicate evidence."""
    return len(content_text.strip()) >= settings.DEDUP_MIN_SEMANTIC_CONTENT_CHARS


def _normalized_containment_text(content_text: str) -> str:
    return "".join(content_text.split()).casefold()


def _has_content_containment(left: str, right: str) -> bool:
    """Detect meaningful literal inclusion that embedding similarity can miss."""
    normalized_left = _normalized_containment_text(left)
    normalized_right = _normalized_containment_text(right)
    if (
        len(normalized_left) < settings.DEDUP_MIN_CONTAINMENT_CONTENT_CHARS
        or len(normalized_right) < settings.DEDUP_MIN_CONTAINMENT_CONTENT_CHARS
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
    exclude_knowledge_id: str | None = None,
) -> DedupDecision:
    text, title_text, content_text = build_dedup_documents(title, content)
    if not text:
        raise ValueError("Knowledge content is empty after normalization.")
    content_hash = content_hash_for_text(text)
    query = db.query(Knowledge).join(
        KnowledgeEmbedding,
        KnowledgeEmbedding.knowledge_id == Knowledge.id,
    ).filter(
        Knowledge.status.in_([KnowledgeStatus.REVIEW, KnowledgeStatus.PUBLISHED]),
        KnowledgeEmbedding.embedding_model == settings.EMBEDDING_MODEL,
    )
    if exclude_knowledge_id:
        query = query.filter(Knowledge.id != exclude_knowledge_id)
    title_matches = (
        query.filter(Knowledge.title == title.strip())
        .order_by(Knowledge.updated_at.desc())
        .limit(settings.DEDUP_MAX_CANDIDATES)
        .all()
    )
    exact_title_and_content_matches = [
        item
        for item in title_matches
        if _content_to_text(item.content) == content_text
    ]
    if exact_title_and_content_matches:
        return DedupDecision(
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
                    category_id=item.category_id,
                    match_type="exact",
                    similarity=1.0,
                )
                for item in exact_title_and_content_matches
            ],
        )
    exact_matches = (
        query.filter(KnowledgeEmbedding.content_hash == content_hash)
        .order_by(Knowledge.updated_at.desc())
        .limit(settings.DEDUP_MAX_CANDIDATES)
        .all()
    )
    if exact_matches:
        return DedupDecision(
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
                    category_id=item.category_id,
                    match_type="exact",
                    similarity=1.0,
                )
                for item in exact_matches[: settings.DEDUP_MAX_CANDIDATES]
            ],
        )

    query_vector, title_vector, content_vector = embed_texts(
        [text, title_text, content_text]
    )
    containment_matches = [
        item
        for item in query.all()
        if _has_content_containment(content_text, _content_to_text(item.content))
    ]
    if containment_matches:
        return DedupDecision(
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
                    category_id=item.category_id,
                    match_type="content_containment",
                    similarity=1.0,
                )
                for item in containment_matches[: settings.DEDUP_MAX_CANDIDATES]
            ],
        )
    if not _has_enough_semantic_content(content_text):
        return DedupDecision(
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
        .limit(settings.DEDUP_MAX_CANDIDATES)
        .all()
    )
    matches: list[DedupMatch] = []
    for item, _ in candidates:
        record = _find_embedding(db, item.id)
        if not record:
            continue
        if (
            record.title_embedding_vector is None
            or record.content_embedding_vector is None
        ):
            _upsert_embeddings(
                db,
                [item],
                [_knowledge_text(item)],
                [content_hash_for_text(_knowledge_text(item))],
            )
            record = _find_embedding(db, item.id)
        if not record or record.title_embedding_vector is None or record.content_embedding_vector is None:
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
        if item.similarity >= settings.DEDUP_REVIEW_THRESHOLD
    ][: settings.DEDUP_MAX_CANDIDATES]
    top_score = matches[0].similarity if matches else 0.0
    action: DedupAction = "create"
    if top_score >= settings.DEDUP_BLOCK_THRESHOLD:
        action = "block_duplicate"
    elif top_score >= settings.DEDUP_REVIEW_THRESHOLD:
        action = "review_duplicate"

    return DedupDecision(
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


def _split_search_chunks(text: str) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    chunk_size = max(settings.SEARCH_CHUNK_SIZE, 100)
    overlap = min(max(settings.SEARCH_CHUNK_OVERLAP, 0), chunk_size - 1)
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


def build_search_documents(item: Knowledge) -> list[tuple[str, int, str]]:
    """Build independent search documents without polluting the dedup vector."""
    documents: list[tuple[str, int, str]] = []
    title = item.title.strip()
    for index, subtitle in enumerate(item.subtitles or []):
        subtitle_text = _content_to_text(subtitle)
        if subtitle_text:
            documents.append(("subtitle", index, f"{title}\n{subtitle_text}"))

    content_text = _content_to_text(item.content)
    for index, chunk in enumerate(_split_search_chunks(content_text)):
        documents.append(("content", index, f"{title}\n{chunk}"))

    if not documents and title:
        documents.append(("title", 0, title))
    return documents


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


def ensure_search_embeddings(db: Session, item: Knowledge) -> int:
    """Create or refresh subtitle and content chunk vectors for one knowledge item."""
    documents = build_search_documents(item)
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
    if missing:
        vectors = embed_texts([text for _, _, text, _ in missing])
        for (kind, index, text, content_hash), vector in zip(missing, vectors):
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
    category_id: str | None = None,
    tags: list[str] | None = None,
    top_k: int = 10,
) -> list[tuple[Knowledge, float]]:
    """Semantic search in PostgreSQL, aggregated by the parent knowledge item."""
    query_vector = embed_texts([query.strip()])[0]
    distance = KnowledgeSearchEmbedding.embedding_vector.cosine_distance(query_vector)
    item_query = (
        db.query(Knowledge, distance.label("distance"))
        .join(
            KnowledgeSearchEmbedding,
            KnowledgeSearchEmbedding.knowledge_id == Knowledge.id,
        )
        .filter(
            Knowledge.status == KnowledgeStatus.PUBLISHED,
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
