import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.database import engine, Base
from app.routes import knowledge, category, tag

Base.metadata.create_all(bind=engine)

PROJECT_ROOT = r"C:\Users\a1873\Documents\答疑中台知识库项目"
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
UPLOAD_DIR = os.path.join(BACKEND_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="答疑中台 - 知识库管理",
    description="知识运营与标注模块后端API，提供知识条目CRUD、审核流程、分类管理、标签管理、检索和反馈接口。",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(knowledge.router, prefix=settings.API_V1_PREFIX)
app.include_router(category.router, prefix=settings.API_V1_PREFIX)
app.include_router(tag.router, prefix=settings.API_V1_PREFIX)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/lib", StaticFiles(directory=os.path.join(FRONTEND_DIR, "lib")), name="lib")


@app.get("/app")
def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/minimal")
def serve_minimal():
    return FileResponse(os.path.join(FRONTEND_DIR, "minimal.html"))


@app.get("/diag")
def serve_diag():
    return FileResponse(os.path.join(FRONTEND_DIR, "diag.html"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "答疑中台知识库", "version": settings.VERSION}
