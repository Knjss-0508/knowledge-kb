import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.database import engine, Base
from app.models.user import User
from app.routes import knowledge, category, tag, manhattan, auth
from app.routes.auth import hash_password

Base.metadata.create_all(bind=engine)

with engine.begin() as conn:
    for sql in (
        "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS applicable_business_types JSON DEFAULT '[]'",
        "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS applicable_categories JSON DEFAULT '[]'",
        "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS applicable_brands JSON DEFAULT '[]'",
        "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS applicable_models JSON DEFAULT '[]'",
        "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS is_model_personal VARCHAR(16) DEFAULT 'false'",
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) VALUES ('cat-qc-standard', '质检标准', NULL, 1, 10, NOW()) ON CONFLICT (id) DO NOTHING",
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) VALUES ('cat-qc-process', '质检流程', NULL, 1, 20, NOW()) ON CONFLICT (id) DO NOTHING",
    ):
        conn.exec_driver_sql(sql)
    admin = conn.exec_driver_sql("SELECT id FROM users WHERE username = 'Weichizhuo'").first()
    if not admin:
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, NOW(), NOW())",
            ("super-admin", "Weichizhuo", hash_password("123456"), "super_admin", True),
        )
    conn.exec_driver_sql("UPDATE users SET role = 'visitor' WHERE role = 'user'")

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
UPLOAD_DIR = BACKEND_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
app.include_router(manhattan.router, prefix=settings.API_V1_PREFIX)
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/lib", StaticFiles(directory=str(FRONTEND_DIR / "lib")), name="lib")


@app.get("/app")
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/")
def serve_root():
    return FileResponse(FRONTEND_DIR / "auth.html")


@app.get("/login")
def serve_login():
    return FileResponse(FRONTEND_DIR / "auth.html")


@app.get("/manhattan-login")
def serve_manhattan_login():
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/minimal")
def serve_minimal():
    return FileResponse(FRONTEND_DIR / "minimal.html")


@app.get("/diag")
def serve_diag():
    return FileResponse(FRONTEND_DIR / "diag.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "答疑中台知识库", "version": settings.VERSION}
