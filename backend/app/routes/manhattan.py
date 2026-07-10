import asyncio
import json
import os
from datetime import datetime
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from app.core.config import settings


router = APIRouter(prefix="/manhattan", tags=["Manhattan options"])
_runtime_cookie = ""
_refresh_lock = asyncio.Lock()
_refresh_status = {
    "running": False,
    "stage": "idle",
    "message": "Not started.",
    "current": 0,
    "total": 0,
    "percent": 0,
    "counts": {},
    "error": "",
    "updated_at": None,
}
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
CACHE_FILE = os.path.join(DATA_DIR, "manhattan_options.json")
REQUEST_DELAY_SECONDS = 0.25
ALLOWED_CATEGORY_NAMES = {
    "手机",
    "平板电脑",
    "耳机/耳麦",
    "笔记本",
    "游戏机",
    "游戏卡带",
    "单电/微单机身",
    "单反机身",
    "相机镜头",
    "手写笔",
    "学习机",
    "智能手表",
}


OPTION_PATHS = {
    "knowledge-types": "/nmhtapi/quality/queryQcKnowledgeTypes",
    "category-tree": "/nmhtapi/quality/queryQcKnowledgeCategoryTree",
    "applicable-categories": "/nmhtapi/station/getAllSupportCategory",
    "brands": "/nmhtapi/common/getAllBrandByCategory",
    "models": "/nmhtapi/common/batchGetAllModel",
}


def _configured_path(kind: str) -> str:
    return OPTION_PATHS.get(kind, "")


def _active_cookie() -> str:
    return _runtime_cookie or settings.NMHT_COOKIE


def _headers(cookie: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": "Mozilla/5.0",
    }
    active_cookie = cookie if cookie is not None else _active_cookie()
    if active_cookie:
        headers["Cookie"] = active_cookie
    return headers


def _url(path: str) -> str:
    return urljoin(settings.NMHT_BASE_URL.rstrip("/") + "/", path.lstrip("/"))


def _json_or_auth_error(resp: httpx.Response):
    content_type = resp.headers.get("content-type", "")
    text = resp.text[:500]
    if resp.status_code in (301, 302, 303, 307, 308):
        raise HTTPException(401, "Manhattan login expired. Please paste Cookie again at /login.")
    if "text/html" in content_type or "<!DOCTYPE html" in text or "统一登录平台" in text:
        raise HTTPException(401, "Manhattan returned login page. Please paste Cookie again at /login.")
    try:
        return json.loads(resp.content.decode("utf-8-sig"))
    except ValueError:
        try:
            return resp.json()
        except ValueError:
            raise HTTPException(502, f"Manhattan API returned non-JSON data: {text}")


def _read_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {
            "updated_at": None,
            "applicable_categories": [],
            "brands_by_category": {},
            "models": [],
        }
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_cache(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_file = CACHE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, CACHE_FILE)


def _extract_items(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    for key in ("respData", "data", "records", "list", "result", "options"):
        val = raw.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            nested = _extract_items(val)
            if nested:
                return nested
    return []


def _collect_values(raw, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []

    def walk(v):
        if isinstance(v, list):
            for item in v:
                walk(item)
            return
        if not isinstance(v, dict):
            return
        for key in keys:
            val = v.get(key)
            if val is not None and str(val) not in values:
                values.append(str(val))
                break
        for child_key in ("respData", "children", "childList", "list", "records", "data", "result", "options"):
            if child_key in v:
                walk(v[child_key])

    walk(raw)
    return values


def _category_name(category: dict) -> str:
    for key in ("categoryName", "name", "label", "title", "text"):
        val = category.get(key)
        if val:
            return str(val).strip()
    return ""


def _category_id(category: dict) -> str:
    for key in ("categoryId", "id", "code", "value"):
        val = category.get(key)
        if val is not None:
            return str(val)
    return ""


def _filter_allowed_categories(categories: list) -> list:
    return [
        category
        for category in categories
        if isinstance(category, dict) and _category_name(category) in ALLOWED_CATEGORY_NAMES
    ]


def _set_refresh_status(**kwargs) -> None:
    _refresh_status.update(kwargs)


async def _fetch_json(client: httpx.AsyncClient, method: str, kind: str, *, params=None, body=None, cookie=None):
    path = _configured_path(kind)
    if not path:
        raise HTTPException(400, f"Unknown Manhattan option source: {kind}")
    if method == "GET":
        resp = await client.get(_url(path), headers=_headers(cookie), params=params or {})
    else:
        resp = await client.post(_url(path), headers=_headers(cookie), json=body or {})
    if resp.status_code >= 400 and resp.status_code not in (401, 403):
        raise HTTPException(resp.status_code, f"Manhattan API failed: {resp.text[:300]}")
    return _json_or_auth_error(resp)


@router.get("/options/{kind}")
async def get_manhattan_options(
    kind: str,
    bizType: str = Query("-2"),
    categoryId: str | None = Query(None),
):
    path = _configured_path(kind)
    if not path:
        raise HTTPException(400, f"Unknown Manhattan option source: {kind}")

    params = {}
    if kind == "applicable-categories":
        params["bizType"] = bizType
    if kind == "brands":
        if not categoryId:
            raise HTTPException(400, "categoryId is required for brands.")
        params["categoryId"] = categoryId
    if kind == "models":
        raise HTTPException(405, "Use POST /api/v1/manhattan/options/models for models.")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(_url(path), headers=_headers(), params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Manhattan API request failed: {exc}")

    if resp.status_code >= 400 and resp.status_code not in (401, 403):
        raise HTTPException(resp.status_code, f"Manhattan API failed: {resp.text[:300]}")
    return _json_or_auth_error(resp)


@router.get("/session")
def get_manhattan_session():
    return {
        "logged_in": bool(_active_cookie()),
        "source": "runtime" if _runtime_cookie else ("env" if settings.NMHT_COOKIE else ""),
    }


@router.get("/cache")
def get_manhattan_cache():
    return _read_cache()


async def _refresh_manhattan_cache_job(cookie: str) -> None:
    async with _refresh_lock:
        try:
            _set_refresh_status(
                running=True,
                stage="categories",
                message="正在获取适用类目...",
                current=0,
                total=0,
                percent=5,
                counts={},
                error="",
                updated_at=None,
            )
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                categories_raw = await _fetch_json(
                    client,
                    "GET",
                    "applicable-categories",
                    params={"bizType": "-2"},
                    cookie=cookie,
                )
                categories = _filter_allowed_categories(_extract_items(categories_raw))
                category_ids = [_category_id(category) for category in categories]
                category_ids = [category_id for category_id in category_ids if category_id]

                brands_by_category = {}
                brand_ids: list[str] = []
                total_categories = len(category_ids)
                _set_refresh_status(
                    stage="brands",
                    message=f"正在获取适用品牌：0/{total_categories}",
                    current=0,
                    total=total_categories,
                    percent=10,
                    counts={"categories": total_categories},
                )
                for index, category_id in enumerate(category_ids, start=1):
                    await asyncio.sleep(REQUEST_DELAY_SECONDS)
                    brands_raw = await _fetch_json(
                        client,
                        "GET",
                        "brands",
                        params={"categoryId": category_id},
                        cookie=cookie,
                    )
                    brands_by_category[category_id] = _extract_items(brands_raw)
                    for brand_id in _collect_values(brands_raw, ("brandId", "id", "code", "value")):
                        if brand_id not in brand_ids:
                            brand_ids.append(brand_id)
                    percent = 10 + int((index / max(total_categories, 1)) * 70)
                    _set_refresh_status(
                        message=f"正在获取适用品牌：{index}/{total_categories}",
                        current=index,
                        total=total_categories,
                        percent=percent,
                        counts={
                            "categories": total_categories,
                            "brand_groups": len(brands_by_category),
                            "brands": len(brand_ids),
                        },
                    )

                models = []
                seen_model_ids: set[str] = set()
                if category_ids and brand_ids:
                    _set_refresh_status(
                        stage="models",
                        message=f"正在获取适用机型：0/{total_categories}",
                        current=0,
                        total=total_categories,
                        percent=85,
                    )
                    for index, category_id in enumerate(category_ids, start=1):
                        category_brand_ids = _collect_values(
                            brands_by_category.get(category_id, []),
                            ("brandId", "id", "code", "value"),
                        )
                        if not category_brand_ids:
                            continue
                        await asyncio.sleep(REQUEST_DELAY_SECONDS)
                        models_raw = await _fetch_json(
                            client,
                            "POST",
                            "models",
                            body={"categoryId": category_id, "brandIdList": category_brand_ids},
                            cookie=cookie,
                        )
                        for model in _extract_items(models_raw):
                            model_key = str(model.get("modelId") or model.get("id") or model.get("code") or model.get("modelName") or model)
                            if model_key in seen_model_ids:
                                continue
                            seen_model_ids.add(model_key)
                            models.append(model)
                        percent = 80 + int((index / max(total_categories, 1)) * 15)
                        _set_refresh_status(
                            message=f"正在获取适用机型：{index}/{total_categories}",
                            current=index,
                            total=total_categories,
                            percent=percent,
                            counts={
                                "categories": total_categories,
                                "brand_groups": len(brands_by_category),
                                "brands": len(brand_ids),
                                "models": len(models),
                            },
                        )

            counts = {
                "categories": len(category_ids),
                "brand_groups": len(brands_by_category),
                "brands": len(brand_ids),
                "models": len(models),
            }
            cache = {
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "applicable_categories": categories,
                "brands_by_category": brands_by_category,
                "models": models,
                "counts": counts,
            }
            _set_refresh_status(stage="saving", message="正在写入本地缓存...", percent=95, counts=counts)
            _write_cache(cache)
            _set_refresh_status(
                running=False,
                stage="done",
                message="更新完成。",
                current=1,
                total=1,
                percent=100,
                counts=counts,
                updated_at=cache["updated_at"],
            )
        except Exception as exc:
            _set_refresh_status(
                running=False,
                stage="error",
                message="更新失败。",
                error=str(getattr(exc, "detail", exc)),
                percent=0,
            )


@router.post("/cache/refresh")
async def refresh_manhattan_cache(background_tasks: BackgroundTasks):
    cookie = _active_cookie()
    if not cookie:
        raise HTTPException(401, "Manhattan cookie is required. Go to /login first.")
    if _refresh_status.get("running"):
        return {"started": False, "status": _refresh_status}
    background_tasks.add_task(_refresh_manhattan_cache_job, cookie)
    return {"started": True, "status": _refresh_status}


@router.get("/cache/refresh/status")
def get_refresh_status():
    return _refresh_status


@router.post("/session")
async def set_manhattan_session(request: Request):
    global _runtime_cookie
    body = await request.json()
    cookie = str(body.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, "Cookie cannot be empty.")

    path = _configured_path("applicable-categories")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(_url(path), headers=_headers(cookie), params={"bizType": "-2"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Cookie verification request failed: {exc}")

    if resp.status_code >= 400 and resp.status_code not in (401, 403):
        raise HTTPException(resp.status_code, f"Cookie verification failed: {resp.text[:300]}")
    _json_or_auth_error(resp)
    _runtime_cookie = cookie
    return {"ok": True}


@router.delete("/session")
def clear_manhattan_session():
    global _runtime_cookie
    _runtime_cookie = ""
    return {"ok": True}


@router.post("/options/models")
async def get_manhattan_models(request: Request):
    path = _configured_path("models")
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            resp = await client.post(_url(path), headers=_headers(), json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Manhattan API request failed: {exc}")

    if resp.status_code >= 400 and resp.status_code not in (401, 403):
        raise HTTPException(resp.status_code, f"Manhattan API failed: {resp.text[:300]}")
    return _json_or_auth_error(resp)
