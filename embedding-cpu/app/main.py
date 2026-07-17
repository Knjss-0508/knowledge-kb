import os
from contextlib import asynccontextmanager
from threading import Lock
from typing import Literal

import torch
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer


MODEL_NAME = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EXPECTED_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
BATCH_SIZE = int(os.getenv("EMBEDDING_CPU_BATCH_SIZE", "8"))


class EmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    encoding_format: Literal["float"] | None = None
    user: str | None = None


def _inputs(value: str | list[str]) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise HTTPException(status_code=422, detail="input must contain one or more non-empty strings")
    return values


@asynccontextmanager
async def lifespan(app: FastAPI):
    cpu_threads = int(os.getenv("EMBEDDING_CPU_THREADS", "0"))
    if cpu_threads > 0:
        torch.set_num_threads(cpu_threads)

    model = SentenceTransformer(MODEL_NAME, device="cpu", trust_remote_code=False)
    dimensions = model.get_sentence_embedding_dimension()
    if dimensions != EXPECTED_DIMENSIONS:
        raise RuntimeError(
            f"Embedding dimension mismatch: expected {EXPECTED_DIMENSIONS}, got {dimensions}."
        )

    app.state.model = model
    app.state.dimensions = dimensions
    app.state.lock = Lock()
    yield


app = FastAPI(title="Qwen CPU Embeddings", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health(request: Request):
    return {
        "status": "ready",
        "model": MODEL_NAME,
        "dimensions": request.app.state.dimensions,
        "runtime": "cpu",
    }


@app.post("/v1/embeddings")
def embeddings(payload: EmbeddingsRequest, request: Request):
    texts = _inputs(payload.input)
    model = request.app.state.model
    with request.app.state.lock:
        vectors = model.encode(
            texts,
            batch_size=BATCH_SIZE,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        )

    data = []
    for index, vector in enumerate(vectors):
        values = [float(value) for value in vector]
        if len(values) != request.app.state.dimensions:
            raise HTTPException(status_code=500, detail="Embedding service returned an invalid vector size")
        data.append({"object": "embedding", "embedding": values, "index": index})

    return {
        "object": "list",
        "data": data,
        "model": payload.model or MODEL_NAME,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
