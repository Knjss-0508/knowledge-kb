from __future__ import annotations

import math
from collections.abc import Callable
from threading import Lock
from typing import Any

import httpx

from app.core.config import settings


class EmbeddingServiceUnavailable(RuntimeError):
    """Raised when the internal embedding service cannot provide valid vectors."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


_EMBEDDING_CLIENT_GUARD = Lock()
_EMBEDDING_CLIENT: httpx.Client | None = None
_EMBEDDING_CLIENT_BASE_URL: str | None = None
_EMBEDDING_CLIENT_FACTORY: Any = None


def _get_embedding_client() -> httpx.Client:
    """Return the process-wide HTTP client for the embedding endpoint.

    Creating a client per call forced a new TCP connection for every request.
    With the split topology the embedding service sits behind a reverse
    tunnel, so each new connection also makes the tunnel open a fresh
    connection to the GPU node. Reusing one client keeps that whole path warm;
    measured at ~21ms saved per call (113ms -> 92ms for a single query vector).

    The client is rebuilt when ``EMBEDDING_BASE_URL`` changes so tests and
    reconfiguration are not served by a stale pool.
    """
    global _EMBEDDING_CLIENT, _EMBEDDING_CLIENT_BASE_URL
    global _EMBEDDING_CLIENT_FACTORY
    base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
    client_factory = httpx.Client
    with _EMBEDDING_CLIENT_GUARD:
        if (
            _EMBEDDING_CLIENT is None
            or _EMBEDDING_CLIENT_BASE_URL != base_url
            or _EMBEDDING_CLIENT_FACTORY is not client_factory
        ):
            if _EMBEDDING_CLIENT is not None:
                _EMBEDDING_CLIENT.close()
            _EMBEDDING_CLIENT = client_factory(
                timeout=httpx.Timeout(settings.EMBEDDING_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=16,
                    max_keepalive_connections=8,
                    # Bounded so a connection killed by a tunnel restart is not
                    # reused indefinitely.
                    keepalive_expiry=60.0,
                ),
            )
            _EMBEDDING_CLIENT_BASE_URL = base_url
            _EMBEDDING_CLIENT_FACTORY = client_factory
        return _EMBEDDING_CLIENT


def close_embedding_client() -> None:
    """Close the shared embedding client (shutdown and tests)."""
    global _EMBEDDING_CLIENT, _EMBEDDING_CLIENT_BASE_URL
    global _EMBEDDING_CLIENT_FACTORY
    with _EMBEDDING_CLIENT_GUARD:
        if _EMBEDDING_CLIENT is not None:
            _EMBEDDING_CLIENT.close()
        _EMBEDDING_CLIENT = None
        _EMBEDDING_CLIENT_BASE_URL = None
        _EMBEDDING_CLIENT_FACTORY = None


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
    if not isinstance(payload, dict):
        raise ValueError("OpenAI-compatible response must be an object.")
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


def _embedding_failure_is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return (
            status_code in {408, 425, 429}
            or status_code >= 500
        )
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    return False


def _embed_batch(
    client: httpx.Client,
    texts: list[str],
    headers: dict[str, str],
    provider: str,
    timeout: httpx.Timeout | None = None,
) -> list[list[float]]:
    errors: list[tuple[str, bool]] = []
    if provider in {"openai_compatible", "auto"}:
        try:
            request_kwargs: dict[str, Any] = {
                "headers": headers,
                "json": {"model": settings.EMBEDDING_MODEL, "input": texts},
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = client.post(
                _openai_embeddings_url(),
                **request_kwargs,
            )
            response.raise_for_status()
            vectors = _parse_openai_response(response.json())
            if len(vectors) != len(texts):
                raise ValueError("Embedding result count does not match input count.")
            return vectors
        except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
            errors.append(
                (
                    f"OpenAI-compatible endpoint: {exc}",
                    _embedding_failure_is_retryable(exc),
                )
            )

    if provider in {"tei", "auto"}:
        try:
            request_kwargs = {"headers": headers, "json": {"inputs": texts}}
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = client.post(
                _tei_embeddings_url(),
                **request_kwargs,
            )
            response.raise_for_status()
            vectors = _parse_tei_response(response.json())
            if len(vectors) != len(texts):
                raise ValueError("Embedding result count does not match input count.")
            return vectors
        except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
            errors.append(
                (
                    f"TEI endpoint: {exc}",
                    _embedding_failure_is_retryable(exc),
                )
            )

    raise EmbeddingServiceUnavailable(
        "; ".join(message for message, _ in errors),
        retryable=any(retryable for _, retryable in errors),
    )


def embed_texts(
    texts: list[str],
    *,
    on_batch_complete: Callable[[int, int], None] | None = None,
    timeout_seconds: float | None = None,
) -> list[list[float]]:
    """Generate document embeddings through the private Qwen/TEI service.

    Large imports are transparently split into bounded requests so one long
    knowledge item cannot exceed the embedding service's HTTP payload limit.
    ``on_batch_complete`` is an optional progress hook invoked after each
    bounded model request, without changing the returned vector order.
    """
    if not texts:
        return []
    if any(not text.strip() for text in texts):
        raise ValueError("Embedding input must not be blank.")

    headers = _authorization_headers()
    timeout = httpx.Timeout(
        settings.EMBEDDING_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    provider = settings.EMBEDDING_PROVIDER.strip().lower()
    if provider not in {"openai_compatible", "tei", "auto"}:
        raise ValueError(
            "EMBEDDING_PROVIDER must be one of: openai_compatible, tei, auto."
        )

    client = _get_embedding_client()
    vectors: list[list[float]] = []
    processed = 0
    for batch in _embedding_batches(texts):
        vectors.extend(_embed_batch(client, batch, headers, provider, timeout))
        processed += len(batch)
        if on_batch_complete is not None:
            on_batch_complete(processed, len(texts))
    return vectors
