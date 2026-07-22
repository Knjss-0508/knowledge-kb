from __future__ import annotations

from array import array
from collections import Counter, OrderedDict
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import sqlite3
from time import sleep
from threading import Lock
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmbeddingError(RuntimeError):
    """The configured embedding service could not return valid vectors."""


def _load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("'"))


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: int = 120
    batch_size: int = 8
    max_retries: int = 3
    batch_char_limit: int = 1600
    max_text_chars: int = 1600
    cache_size: int = 5000

    @classmethod
    def from_env(cls) -> "EmbeddingConfig | None":
        _load_dotenv()
        base_url = os.getenv("EMBEDDING_BASE_URL", "").strip()
        model = os.getenv("EMBEDDING_MODEL", "").strip()
        if not base_url or not model:
            return None
        try:
            timeout = max(10, min(int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "120")), 600))
        except ValueError:
            timeout = 120
        try:
            batch_size = max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "8")))
        except ValueError:
            batch_size = 8
        try:
            max_retries = max(0, min(int(os.getenv("EMBEDDING_MAX_RETRIES", "3")), 10))
        except ValueError:
            max_retries = 3
        try:
            batch_char_limit = max(256, int(os.getenv("EMBEDDING_BATCH_CHAR_LIMIT", "1600")))
        except ValueError:
            batch_char_limit = 1600
        try:
            max_text_chars = max(256, int(os.getenv("EMBEDDING_MAX_TEXT_CHARS", "1600")))
        except ValueError:
            max_text_chars = 1600
        try:
            cache_size = max(0, int(os.getenv("EMBEDDING_CACHE_SIZE", "5000")))
        except ValueError:
            cache_size = 5000
        return cls(
            base_url=base_url,
            model=model,
            api_key=os.getenv("EMBEDDING_API_KEY", "").strip(),
            timeout_seconds=timeout,
            batch_size=batch_size,
            max_retries=max_retries,
            batch_char_limit=batch_char_limit,
            max_text_chars=max_text_chars,
            cache_size=cache_size,
        )

    def embeddings_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/embeddings") else f"{base}/embeddings"


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._cache: OrderedDict[str, array[float]] = OrderedDict()
        self._cache_lock = Lock()

    @classmethod
    def from_env(cls) -> "EmbeddingClient | None":
        config = EmbeddingConfig.from_env()
        return cls(config) if config else None

    def embed_texts(
        self,
        texts: list[str],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []

        prepared = [self._prepare_text(text) for text in texts]
        keys = [self._cache_key(text) for text in prepared]
        key_counts = Counter(keys)
        resolved: dict[str, list[float]] = {}
        missing: list[tuple[str, str]] = []
        seen_missing: set[str] = set()

        for key, text in zip(keys, prepared):
            cached = self._cache_get(key)
            if cached is not None:
                resolved[key] = cached
            elif key not in seen_missing:
                missing.append((key, text))
                seen_missing.add(key)

        total = len(texts)
        completed = sum(key_counts[key] for key in resolved)
        if progress_callback:
            progress_callback(completed, total)

        for batch in self._iter_batches(missing):
            batch_keys = [key for key, _text in batch]
            batch_texts = [text for _key, text in batch]
            try:
                batch_vectors = self._embed_batch(batch_texts)
            except EmbeddingError as exc:
                raise EmbeddingError(
                    f"{exc} (Embedding progress {completed}/{total})"
                ) from exc
            for key, vector in zip(batch_keys, batch_vectors):
                resolved[key] = self._cache_set(key, vector)
                completed += key_counts[key]
            if progress_callback:
                progress_callback(min(completed, total), total)

        return [list(resolved[key]) for key in keys]

    def _prepare_text(self, text: str) -> str:
        value = str(text or "").strip()
        limit = self.config.max_text_chars
        if len(value) <= limit:
            return value
        marker = "\n[...truncated...]\n"
        if limit <= len(marker) + 2:
            return value[:limit]
        remaining = max(1, limit - len(marker))
        head_size = max(1, int(remaining * 0.7))
        tail_size = max(0, remaining - head_size)
        return f"{value[:head_size]}{marker}{value[-tail_size:] if tail_size else ''}"

    def _cache_key(self, text: str) -> str:
        payload = f"{self.config.model}\0{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _cache_get(self, key: str) -> list[float] | None:
        if not self.config.cache_size:
            return None
        with self._cache_lock:
            vector = self._cache.get(key)
            if vector is None:
                return None
            self._cache.move_to_end(key)
            return list(vector)

    def _cache_set(self, key: str, vector: list[float]) -> list[float]:
        compact_vector = array("f", vector)
        if not self.config.cache_size:
            return list(compact_vector)
        with self._cache_lock:
            self._cache[key] = compact_vector
            self._cache.move_to_end(key)
            while len(self._cache) > self.config.cache_size:
                self._cache.popitem(last=False)
        return list(compact_vector)

    def _iter_batches(
        self,
        items: list[tuple[str, str]],
    ):
        batch: list[tuple[str, str]] = []
        batch_chars = 0
        for item in items:
            text_chars = max(1, len(item[1]))
            exceeds_count = len(batch) >= self.config.batch_size
            exceeds_chars = batch and batch_chars + text_chars > self.config.batch_char_limit
            if exceeds_count or exceeds_chars:
                yield batch
                batch = []
                batch_chars = 0
            batch.append(item)
            batch_chars += text_chars
        if batch:
            yield batch

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.config.model,
            "input": texts,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = Request(
            self.config.embeddings_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        raw = ""
        for attempt in range(self.config.max_retries + 1):
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                break
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code == 429 and attempt < self.config.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = max(0.1, float(retry_after)) if retry_after else float(2**attempt)
                    except ValueError:
                        delay = float(2**attempt)
                    sleep(min(delay, 8.0))
                    continue
                raise EmbeddingError(f"Embedding HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                if isinstance(exc.reason, TimeoutError) and attempt < self.config.max_retries:
                    sleep(min(float(2**attempt), 8.0))
                    continue
                raise EmbeddingError(f"Embedding network error: {exc.reason}") from exc
            except TimeoutError as exc:
                if attempt < self.config.max_retries:
                    sleep(min(float(2**attempt), 8.0))
                    continue
                raise EmbeddingError(f"Embedding network error: {exc}") from exc
            except (HTTPException, OSError) as exc:
                raise EmbeddingError(f"Embedding network error: {exc}") from exc

        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmbeddingError("Embedding response is not valid JSON") from exc

        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError("Embedding response size does not match input size")

        ordered = sorted(data, key=lambda item: int(item.get("index", 0)) if isinstance(item, dict) else 0)
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("Embedding response contains an invalid vector")
            try:
                vectors.append([float(value) for value in vector])
            except (TypeError, ValueError) as exc:
                raise EmbeddingError("Embedding response contains a non-numeric vector") from exc
        return vectors


class PersistentEmbeddingClient:
    """Persist vectors in SQLite so interrupted CPU runs can resume."""

    def __init__(
        self,
        client: EmbeddingClient,
        cache_path: str | Path,
    ) -> None:
        self.client = client
        self.config = client.config
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.cache_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                model TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                vector BLOB NOT NULL,
                dimension INTEGER NOT NULL,
                PRIMARY KEY (model, cache_key)
            )
            """
        )
        self._connection.commit()

    def _key(self, text: str) -> str:
        payload = f"{self.config.model}\0{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _get(self, key: str) -> list[float] | None:
        row = self._connection.execute(
            """
            SELECT vector, dimension
            FROM embedding_cache
            WHERE model = ? AND cache_key = ?
            """,
            (self.config.model, key),
        ).fetchone()
        if not row:
            return None
        values = array("f")
        values.frombytes(row[0])
        if len(values) != int(row[1]):
            return None
        return list(values)

    def _save_batch(
        self,
        entries: list[tuple[str, list[float]]],
    ) -> None:
        self._connection.executemany(
            """
            INSERT OR REPLACE INTO embedding_cache (
                model,
                cache_key,
                vector,
                dimension
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    self.config.model,
                    key,
                    array("f", vector).tobytes(),
                    len(vector),
                )
                for key, vector in entries
            ],
        )
        self._connection.commit()

    def embed_texts(
        self,
        texts: list[str],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        keys = [self._key(text) for text in texts]
        key_counts = Counter(keys)
        resolved: dict[str, list[float]] = {}
        missing: list[tuple[str, str]] = []
        seen_missing: set[str] = set()
        for key, text in zip(keys, texts):
            cached = self._get(key)
            if cached is not None:
                resolved[key] = cached
            elif key not in seen_missing:
                missing.append((key, text))
                seen_missing.add(key)

        total = len(texts)
        completed = sum(key_counts[key] for key in resolved)
        if progress_callback:
            progress_callback(completed, total)

        # The CPU Candle backend used by the handoff compose file advertises
        # a maximum effective batch size of four.
        batch_size = max(1, min(self.config.batch_size, 4))
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            vectors: list[list[float]] | None = None
            for attempt in range(4):
                try:
                    vectors = self.client.embed_texts(
                        [text for _key, text in batch]
                    )
                    break
                except EmbeddingError as exc:
                    overloaded = (
                        "429" in str(exc)
                        or "overloaded" in str(exc).lower()
                    )
                    if not overloaded or attempt == 3:
                        raise
                    sleep(10.0 * (attempt + 1))
            if vectors is None:
                raise EmbeddingError("Embedding batch did not return vectors")
            entries = [
                (key, vector)
                for (key, _text), vector in zip(batch, vectors)
            ]
            self._save_batch(entries)
            for key, vector in entries:
                resolved[key] = vector
                completed += key_counts[key]
            if progress_callback:
                progress_callback(min(completed, total), total)

        return [resolved[key] for key in keys]
