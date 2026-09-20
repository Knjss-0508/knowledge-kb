import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.routes import (
    answer_hub,
    automation_monitor,
    auth,
    business_type,
    category,
    embedding_admin,
    integration,
    knowledge,
    knowledge_origin,
    manhattan,
    media,
    tag,
)
from app.services.media_deletion import run_media_deletion_worker
from app.services.knowledge_import_worker import run_knowledge_import_worker
from app.services.knowledge_vector_worker import run_knowledge_vector_worker
from app.services.media_storage import _normalize_remote_media_path_prefix


logger = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _background_workers_should_start() -> bool:
    """Return whether this process may consume shared background task tables."""

    return bool(settings.BACKGROUND_WORKERS_ENABLED and not settings.MEDIA_GATEWAY_ONLY)


def _gateway_path_allowed(path: str) -> bool:
    """Keep a gateway-only instance limited to health and private media routes."""

    media_prefix = _normalize_remote_media_path_prefix(settings.REMOTE_MEDIA_PATH_PREFIX)
    return path in {"/health", "/ready", media_prefix} or path.startswith(
        media_prefix + "/"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_event = asyncio.Event()
    workers: list[asyncio.Task] = []
    if _background_workers_should_start():
        workers = [
            asyncio.create_task(run_media_deletion_worker(stop_event)),
            asyncio.create_task(
                run_knowledge_import_worker(
                    stop_event,
                    knowledge.process_next_knowledge_import_task,
                )
            ),
            asyncio.create_task(
                run_knowledge_vector_worker(
                    stop_event,
                    knowledge.process_next_knowledge_vector_task,
                )
            ),
        ]
    try:
        yield
    finally:
        if workers:
            stop_event.set()
            await asyncio.gather(*workers)


app = FastAPI(
    title="答疑中台 - 知识库管理",
    description="知识运营与标注模块后端 API，提供知识条目 CRUD、审核流程、分类管理、标签管理、检索和反馈接口。",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request, call_next):
    if settings.MEDIA_GATEWAY_ONLY and not _gateway_path_allowed(request.url.path):
        response = JSONResponse(status_code=404, content={"detail": "Not Found"})
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


app.include_router(knowledge.router, prefix=settings.API_V1_PREFIX)
app.include_router(business_type.router, prefix=settings.API_V1_PREFIX)
app.include_router(knowledge_origin.router, prefix=settings.API_V1_PREFIX)
app.include_router(category.router, prefix=settings.API_V1_PREFIX)
app.include_router(tag.router, prefix=settings.API_V1_PREFIX)
app.include_router(manhattan.router, prefix=settings.API_V1_PREFIX)
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(answer_hub.router, prefix=settings.API_V1_PREFIX)
app.include_router(automation_monitor.router, prefix=settings.API_V1_PREFIX)
app.include_router(integration.router, prefix=settings.API_V1_PREFIX)
app.include_router(embedding_admin.router, prefix=settings.API_V1_PREFIX)
app.include_router(media.router)

app.mount("/lib", StaticFiles(directory=str(FRONTEND_DIR / "lib")), name="lib")
app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


@app.get("/app")
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html", headers=HTML_NO_CACHE_HEADERS)


@app.get("/")
def serve_root():
    return FileResponse(FRONTEND_DIR / "auth.html", headers=HTML_NO_CACHE_HEADERS)


@app.get("/login")
def serve_login():
    return FileResponse(FRONTEND_DIR / "auth.html", headers=HTML_NO_CACHE_HEADERS)


@app.get("/feedback-records")
def serve_feedback_records():
    return FileResponse(
        FRONTEND_DIR / "feedback-records.html",
        headers=HTML_NO_CACHE_HEADERS,
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "答疑中台知识库", "version": settings.VERSION}


@app.get("/ready")
def ready():
    """Readiness probe for Docker and upstream traffic routing."""
    errors: dict[str, str] = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Database readiness check failed.")
        errors["database"] = "unavailable"

    # A gateway-only host has no embedding service by design.  It still checks
    # the shared database so load balancers do not route to a stale gateway.
    if settings.MEDIA_GATEWAY_ONLY:
        if errors:
            raise HTTPException(status_code=503, detail={"status": "not_ready", "errors": errors})
        return {"status": "ready"}

    health_url = settings.EMBEDDING_HEALTHCHECK_URL.strip()
    if not health_url:
        base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        health_url = f"{base_url}/health"
    try:
        response = httpx.get(health_url, timeout=3.0)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Embedding readiness check failed.")
        errors["embedding"] = "unavailable"

    if errors:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "errors": errors})
    return {"status": "ready"}
