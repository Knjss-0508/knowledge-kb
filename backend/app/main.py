import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.routes import auth, category, integration, knowledge, manhattan, media, tag
from app.services.media_deletion import run_media_deletion_worker


logger = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_event = asyncio.Event()
    worker = asyncio.create_task(run_media_deletion_worker(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await worker


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
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


app.include_router(knowledge.router, prefix=settings.API_V1_PREFIX)
app.include_router(category.router, prefix=settings.API_V1_PREFIX)
app.include_router(tag.router, prefix=settings.API_V1_PREFIX)
app.include_router(manhattan.router, prefix=settings.API_V1_PREFIX)
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(integration.router, prefix=settings.API_V1_PREFIX)
app.include_router(media.router)

app.mount("/lib", StaticFiles(directory=str(FRONTEND_DIR / "lib")), name="lib")


@app.get("/app")
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html", headers=HTML_NO_CACHE_HEADERS)


@app.get("/")
def serve_root():
    return FileResponse(FRONTEND_DIR / "auth.html", headers=HTML_NO_CACHE_HEADERS)


@app.get("/login")
def serve_login():
    return FileResponse(FRONTEND_DIR / "auth.html", headers=HTML_NO_CACHE_HEADERS)


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
