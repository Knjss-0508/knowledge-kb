from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
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


@dataclass
class DedupMatch:
    knowledge_id: str
    title: str
    status: str
    category_id: str
    layer: str
    match_type: Literal["exact", "semantic"]
    similarity: float


@dataclass
class DedupDecision:
    action: DedupAction
    content_hash: str
    embedding: list[float] | None
    matches: list[DedupMatch]


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _content_to_text(item)))
    if isinstance(value, dict):
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


def _knowledge_text(item: Knowledge) -> str:
    return build_embedding_text(
        item.title,
        None,
        item.content,
    )


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
    vectors = embed_texts(texts)
    for item, content_hash, vector in zip(items, content_hashes, vectors):
        record = _find_embedding(db, item.id)
        if record:
            record.content_hash = content_hash
            record.embedding_dimension = len(vector)
            record.embedding = vector
            record.embedding_vector = vector
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
                )
            )
    db.flush()
    return vectors


def ensure_embedding(db: Session, item: Knowledge) -> list[float]:
    text = _knowledge_text(item)
    content_hash = content_hash_for_text(text)
    record = _find_embedding(db, item.id)
    if record and record.content_hash == content_hash and record.embedding:
        return [float(value) for value in record.embedding]
    return _upsert_embeddings(db, [item], [text], [content_hash])[0]


def _load_candidate_embeddings(db: Session, items: list[Knowledge]) -> list[list[float]]:
    vectors: list[list[float] | None] = []
    missing_items: list[Knowledge] = []
    missing_texts: list[str] = []
    missing_hashes: list[str] = []

    for item in items:
        text = _knowledge_text(item)
        content_hash = content_hash_for_text(text)
        record = _find_embedding(db, item.id)
        if record and record.content_hash == content_hash and record.embedding:
            vectors.append([float(value) for value in record.embedding])
        else:
            vectors.append(None)
            missing_items.append(item)
            missing_texts.append(text)
            missing_hashes.append(content_hash)

    if missing_items:
        generated = _upsert_embeddings(db, missing_items, missing_texts, missing_hashes)
        generated_iter = iter(generated)
        vectors = [vector if vector is not None else next(generated_iter) for vector in vectors]

    return [vector for vector in vectors if vector is not None]


def check_duplicate(
    db: Session,
    *,
    title: str,
    subtitles: list[str] | None,
    content: Any,
    scene_tags: list[str] | None,
    exclude_knowledge_id: str | None = None,
) -> DedupDecision:
    text = build_embedding_text(title, subtitles, content, scene_tags)
    if not text:
        raise ValueError("Knowledge content is empty after normalization.")
    content_hash = content_hash_for_text(text)
    query = db.query(Knowledge).filter(
        Knowledge.status.in_([KnowledgeStatus.REVIEW, KnowledgeStatus.PUBLISHED])
    )
    if exclude_knowledge_id:
        query = query.filter(Knowledge.id != exclude_knowledge_id)
    existing = query.order_by(Knowledge.updated_at.desc()).all()

    exact_matches = [
        item
        for item in existing
        if content_hash_for_text(_knowledge_text(item)) == content_hash
    ]
    if exact_matches:
        return DedupDecision(
            action="block_duplicate",
            content_hash=content_hash,
            embedding=None,
            matches=[
                DedupMatch(
                    knowledge_id=item.id,
                    title=item.title,
                    status=item.status.value,
                    category_id=item.category_id,
                    layer=item.layer.value,
                    match_type="exact",
                    similarity=1.0,
                )
                for item in exact_matches[: settings.DEDUP_MAX_CANDIDATES]
            ],
        )

    query_vector = embed_texts([text])[0]
    if not existing:
        return DedupDecision(
            action="create",
            content_hash=content_hash,
            embedding=query_vector,
            matches=[],
        )

    existing_vectors = _load_candidate_embeddings(db, existing)
    matches = [
        DedupMatch(
            knowledge_id=item.id,
            title=item.title,
            status=item.status.value,
            category_id=item.category_id,
            layer=item.layer.value,
            match_type="semantic",
            similarity=round(_cosine_similarity(query_vector, vector), 6),
        )
        for item, vector in zip(existing, existing_vectors)
    ]
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
        matches=matches,
    )


def save_embedding(
    db: Session,
    *,
    knowledge: Knowledge,
    content_hash: str,
    embedding: list[float],
) -> None:
    record = _find_embedding(db, knowledge.id)
    if record:
        record.content_hash = content_hash
        record.embedding_dimension = len(embedding)
        record.embedding = embedding
        record.embedding_vector = embedding
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
    layer: str | None = None,
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
    if layer:
        item_query = item_query.filter(Knowledge.layer == layer)
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
