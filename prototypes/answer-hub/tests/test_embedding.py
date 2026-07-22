from __future__ import annotations

from email.message import Message
from http.client import RemoteDisconnected
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

import answer_hub.embedding as embedding_module
from answer_hub.embedding import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    PersistentEmbeddingClient,
)


def test_embed_texts_wraps_remote_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    def disconnect(*_args, **_kwargs):
        raise RemoteDisconnected("server closed connection during startup")

    monkeypatch.setattr(embedding_module, "urlopen", disconnect)
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://127.0.0.1:8080/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )

    with pytest.raises(EmbeddingError, match="server closed connection during startup"):
        client.embed_texts(["测试语义聚类"])


def test_embed_texts_splits_large_inputs_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def respond(request, timeout):
        assert timeout == 120
        payload = json.loads(request.data.decode("utf-8"))
        batch = payload["input"]
        batch_sizes.append(len(batch))
        data = [
            {
                "index": index,
                "embedding": [float(text.rsplit("-", 1)[1])],
            }
            for index, text in reversed(list(enumerate(batch)))
        ]
        return FakeResponse(json.dumps({"data": data}).encode("utf-8"))

    monkeypatch.setattr(embedding_module, "urlopen", respond)
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://127.0.0.1:8080/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )

    vectors = client.embed_texts([f"文本-{index}" for index in range(503)])

    assert batch_sizes == [8] * 62 + [7]
    assert vectors == [[float(index)] for index in range(503)]


def test_embed_texts_retries_http_429(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"data":[{"index":0,"embedding":[1.0,0.0]}]}'

    def respond(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                Message(),
                BytesIO(b'{"message":"Model is overloaded"}'),
            )
        return FakeResponse()

    monkeypatch.setattr(embedding_module, "urlopen", respond)
    monkeypatch.setattr(embedding_module, "sleep", delays.append)
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://127.0.0.1:8080/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )

    assert client.embed_texts(["测试"]) == [[1.0, 0.0]]
    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_embed_texts_retries_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"data":[{"index":0,"embedding":[0.5,0.5]}]}'

    def respond(_request, timeout):
        assert timeout == 120
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("timed out")
        return FakeResponse()

    monkeypatch.setattr(embedding_module, "urlopen", respond)
    monkeypatch.setattr(embedding_module, "sleep", delays.append)
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://127.0.0.1:8080/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )

    assert client.embed_texts(["测试"]) == [[0.5, 0.5]]
    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_embed_texts_deduplicates_and_reuses_memory_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_batches: list[list[str]] = []

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def respond(request, timeout):
        assert timeout == 120
        batch = json.loads(request.data.decode("utf-8"))["input"]
        requested_batches.append(batch)
        data = [
            {"index": index, "embedding": [float(len(text))]}
            for index, text in enumerate(batch)
        ]
        return FakeResponse(json.dumps({"data": data}).encode("utf-8"))

    monkeypatch.setattr(embedding_module, "urlopen", respond)
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url="http://127.0.0.1:8080/v1",
            model="Qwen/Qwen3-Embedding-0.6B",
        )
    )

    first = client.embed_texts(["重复文本", "重复文本", "另一条"])
    second = client.embed_texts(["另一条", "重复文本"])

    assert requested_batches == [["重复文本", "另一条"]]
    assert first == [[4.0], [4.0], [3.0]]
    assert second == [[3.0], [4.0]]


def test_persistent_embedding_client_reuses_disk_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    requested_batches: list[list[str]] = []

    def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        requested_batches.append(list(texts))
        return [[float(len(text))] for text in texts]

    config = EmbeddingConfig(
        base_url="http://127.0.0.1:8080/v1",
        model="Qwen/Qwen3-Embedding-0.6B",
        batch_size=2,
    )
    first_base = EmbeddingClient(config)
    monkeypatch.setattr(first_base, "_embed_batch", fake_embed_batch)
    first = PersistentEmbeddingClient(
        first_base,
        tmp_path / "embedding-cache.sqlite3",
    )

    assert first.embed_texts(["甲", "乙", "甲"]) == [[1.0], [1.0], [1.0]]

    second_base = EmbeddingClient(config)
    monkeypatch.setattr(second_base, "_embed_batch", fake_embed_batch)
    second = PersistentEmbeddingClient(
        second_base,
        tmp_path / "embedding-cache.sqlite3",
    )

    assert second.embed_texts(["乙", "甲"]) == [[1.0], [1.0]]
    assert requested_batches == [["甲", "乙"]]
