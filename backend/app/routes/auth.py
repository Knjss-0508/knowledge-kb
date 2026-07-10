import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.user import User


router = APIRouter(prefix="/auth", tags=["Auth"])
_sessions: dict[str, str] = {}

ROLES = {
    "visitor": {"label": "游客用户", "permissions": {"knowledge:view"}},
    "junior_support": {"label": "小小答疑", "permissions": {"knowledge:view", "knowledge:create", "knowledge:submit", "password:reset_self"}},
    "senior_support": {"label": "大大答疑", "permissions": {"knowledge:view", "knowledge:create", "knowledge:submit", "knowledge:approve", "password:reset_self"}},
    "super_support": {"label": "超级答疑", "permissions": {"knowledge:view", "knowledge:create", "knowledge:submit", "knowledge:approve", "knowledge:publish", "knowledge:deprecate", "password:reset_self"}},
    "super_admin": {"label": "超级管理员", "permissions": {"*", "account:manage"}},
}


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str = "123456"
    role: str = "visitor"


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str = "123456"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return "pbkdf2_sha256$120000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        method, rounds, salt_b64, digest_b64 = password_hash.split("$", 3)
        if method != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def public_user(user: User) -> dict:
    role = user.role if user.role in ROLES else "visitor"
    return {
        "id": user.id,
        "username": user.username,
        "role": role,
        "role_label": ROLES[role]["label"],
        "permissions": sorted(ROLES[role]["permissions"]),
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def has_permission(user: User, permission: str) -> bool:
    role = user.role if user.role in ROLES else "visitor"
    perms = ROLES[role]["permissions"]
    return "*" in perms or permission in perms


def require_permission(permission: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, permission):
            raise HTTPException(403, "Permission denied.")
        return user
    return checker


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not logged in.")
    token = authorization.replace("Bearer ", "", 1).strip()
    user_id = _sessions.get(token)
    if not user_id:
        raise HTTPException(401, "Session expired.")
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(401, "User disabled.")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not has_permission(user, "account:manage"):
        raise HTTPException(403, "Super admin required.")
    return user


@router.get("/roles")
def list_roles(user: User = Depends(get_current_user)):
    return [{"value": key, "label": val["label"]} for key, val in ROLES.items()]


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    username = payload.username.strip()
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password.")
    token = secrets.token_urlsafe(32)
    _sessions[token] = user.id
    return {"token": token, "user": public_user(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return public_user(user)


@router.delete("/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "", 1).strip()
        _sessions.pop(token, None)
    return {"ok": True}


@router.get("/users")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [public_user(user) for user in users]


@router.post("/users")
def create_user(payload: UserCreate, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    username = payload.username.strip()
    if not username:
        raise HTTPException(400, "Username is required.")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(400, "Username already exists.")
    role = payload.role if payload.role in ROLES else "visitor"
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(payload.password or "123456"),
        role=role,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return public_user(user)


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserUpdate, current: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found.")
    if user.id == current.id and payload.is_active is False:
        raise HTTPException(400, "Cannot disable current account.")
    if payload.role is not None:
        if payload.role not in ROLES:
            raise HTTPException(400, "Invalid role.")
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return public_user(user)


@router.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: str, payload: PasswordReset, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found.")
    user.password_hash = hash_password(payload.password or "123456")
    user.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/me/reset-password")
def reset_my_password(payload: PasswordReset, user: User = Depends(require_permission("password:reset_self")), db: Session = Depends(get_db)):
    user.password_hash = hash_password(payload.password or "123456")
    user.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}
