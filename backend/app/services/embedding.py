from __future__ import annotations

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
    return [[float(value) for value in vector] for vector in vectors]


def _parse_tei_response(payload: Any) -> list[list[float]]:
    if isinstance(payload, dict):
        payload = payload.get("embeddings")
    if not isinstance(payload, list) or not all(isinstance(vector, list) for vector in payload):
        raise ValueError("TEI response contains an invalid embedding.")
    return [[float(value) for value in vector] for vector in payload]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate document embeddings through the private Qwen/TEI service."""
    if not texts:
        return []
    if any(not text.strip() for text in texts):
        raise ValueError("Embedding input must not be blank.")

    headers = _authorization_headers()
    timeout = httpx.Timeout(settings.EMBEDDING_TIMEOUT_SECONDS)
    errors: list[str] = []

    with httpx.Client(timeout=timeout) as client:
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
