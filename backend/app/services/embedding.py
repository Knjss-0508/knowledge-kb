from __future__ import annotations

import math
from typing import Any

import httpx

from app.core.config import settings


class EmbeddingServiceUnavailable(RuntimeError):
    """Raised when the internal embedding service cannot provide valid vectors."""


def _authorization_headers() -> dict[str, str]:
    if not settings.EMBEDDING_API_KEY:
        return {}
    return {"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"}


def _openai_embeddings_url() -> str:
    base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
    return base_url if base_url.endswith("/embeddings") else f"{base_url}/embeddings"


def _tei_embeddings_url() -> str:
    base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    return f"{base_url}/embed"


def _parse_openai_response(payload: dict[str, Any]) -> list[list[float]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("OpenAI-compatible response does not contain data.")
    vectors = [item.get("embedding") for item in data if isinstance(item, dict)]
    if len(vectors) != len(data) or not all(isinstance(vector, list) for vector in vectors):
        raise ValueError("OpenAI-compatible response contains an invalid embedding.")
    return _validate_vectors([[float(value) for value in vector] for vector in vectors])


def _parse_tei_response(payload: Any) -> list[list[float]]:
    if isinstance(payload, dict):
        payload = payload.get("embeddings")
    if not isinstance(payload, list) or not all(isinstance(vector, list) for vector in payload):
        raise ValueError("TEI response contains an invalid embedding.")
    return _validate_vectors([[float(value) for value in vector] for vector in payload])


def _validate_vectors(vectors: list[list[float]]) -> list[list[float]]:
    expected_dimension = settings.EMBEDDING_DIMENSIONS
    for vector in vectors:
        if len(vector) != expected_dimension:
            raise ValueError(
                f"Embedding dimension must be {expected_dimension}, got {len(vector)}."
            )
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("Embedding contains a non-finite value.")
    return vectors


def _embedding_batches(texts: list[str]) -> list[list[str]]:
    """Keep each vector request small while retaining the caller's text order."""
    max_texts = max(settings.EMBEDDING_MAX_BATCH_TEXTS, 1)
    max_chars = max(settings.EMBEDDING_MAX_BATCH_CHARS, 1)
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_chars = 0

    for text in texts:
        text_chars = len(text)
        would_exceed_limit = (
            batch
            and (
                len(batch) >= max_texts
                or batch_chars + text_chars > max_chars
            )
        )
        if would_exceed_limit:
            batches.append(batch)
            batch = []
            batch_chars = 0

        batch.append(text)
        batch_chars += text_chars

        # A single document is kept intact for semantic consistency. It forms
        # its own request even if it exceeds the batch character target.
        if len(batch) >= max_texts or batch_chars >= max_chars:
            batches.append(batch)
            batch = []
            batch_chars = 0

    if batch:
        batches.append(batch)
    return batches


def _embed_batch(
    client: httpx.Client,
    texts: list[str],
    headers: dict[str, str],
    provider: str,
) -> list[list[float]]:
    errors: list[str] = []
    if provider in {"openai_compatible", "auto"}:
        try:
            response = client.post(
                _openai_embeddings_url(),
                headers=headers,
                json={"model": settings.EMBEDDING_MODEL, "input": texts},
            )
            response.raise_for_status()
            vectors = _parse_openai_response(response.json())
            if len(vectors) != len(texts):
                raise ValueError("Embedding result count does not match input count.")
            return vectors
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"OpenAI-compatible endpoint: {exc}")

    if provider in {"tei", "auto"}:
        try:
            response = client.post(
                _tei_embeddings_url(),
                headers=headers,
                json={"inputs": texts},
            )
            response.raise_for_status()
            vectors = _parse_tei_response(response.json())
            if len(vectors) != len(texts):
                raise ValueError("Embedding result count does not match input count.")
            return vectors
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"TEI endpoint: {exc}")

    raise EmbeddingServiceUnavailable("; ".join(errors))


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate document embeddings through the private Qwen/TEI service.

    Large imports are transparently split into bounded requests so one long
    knowledge item cannot exceed the embedding service's HTTP payload limit.
    """
    if not texts:
        return []
    if any(not text.strip() for text in texts):
        raise ValueError("Embedding input must not be blank.")

    headers = _authorization_headers()
    timeout = httpx.Timeout(settings.EMBEDDING_TIMEOUT_SECONDS)
    provider = settings.EMBEDDING_PROVIDER.strip().lower()
    if provider not in {"openai_compatible", "tei", "auto"}:
        raise ValueError(
            "EMBEDDING_PROVIDER must be one of: openai_compatible, tei, auto."
        )

    with httpx.Client(timeout=timeout) as client:
        vectors: list[list[float]] = []
        for batch in _embedding_batches(texts):
            vectors.extend(_embed_batch(client, batch, headers, provider))
        return vectors
