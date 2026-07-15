from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
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
    timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "EmbeddingConfig | None":
        _load_dotenv()
        base_url = os.getenv("EMBEDDING_BASE_URL", "").strip()
        model = os.getenv("EMBEDDING_MODEL", "").strip()
        if not base_url or not model:
            return None
        try:
            timeout = max(5, min(int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "30")), 180))
        except ValueError:
            timeout = 30
        return cls(
            base_url=base_url,
            model=model,
            api_key=os.getenv("EMBEDDING_API_KEY", "").strip(),
            timeout_seconds=timeout,
        )

    def embeddings_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/embeddings") else f"{base}/embeddings"


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> "EmbeddingClient | None":
        config = EmbeddingConfig.from_env()
        return cls(config) if config else None

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
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
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise EmbeddingError(f"Embedding HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise EmbeddingError(f"Embedding network error: {exc.reason}") from exc

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
