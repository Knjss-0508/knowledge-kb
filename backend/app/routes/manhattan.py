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
SELF_OPERATED_BUSINESS_TYPE = "self_operated"
AGGREGATED_BUSINESS_TYPE = "aggregated"
BUSINESS_TYPES = (
    SELF_OPERATED_BUSINESS_TYPE,
    AGGREGATED_BUSINESS_TYPE,
)


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
    if resp.status_code in (301, 302, 303, 307, 308, 401, 403):
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
            "options_by_business_type": _empty_options_by_business_type(),
        }
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)
    return _ensure_options_by_business_type(cache)


def cached_applicable_category_ids(business_type: str) -> set[str]:
    """返回指定业务当前缓存中的适用类目 ID，供知识写入校验复用。"""
    if business_type not in BUSINESS_TYPES:
        return set()
    cache = _read_cache()
    group = cache["options_by_business_type"].get(business_type) or {}
    return {
        _category_id(category)
        for category in group.get("applicable_categories", [])
        if isinstance(category, dict) and _category_id(category)
    }


def cached_applicable_category_keys(business_type: str) -> set[str]:
    """返回类目 ID 和名称的规范化集合，兼容前端 ID 与 Excel 中文名称。"""
    if business_type not in BUSINESS_TYPES:
        return set()
    cache = _read_cache()
    group = cache["options_by_business_type"].get(business_type) or {}
    keys: set[str] = set()
    for category in group.get("applicable_categories", []):
        if not isinstance(category, dict):
            continue
        for value in (_category_id(category), _category_name(category)):
            normalized = str(value or "").strip().casefold()
            if normalized:
                keys.add(normalized)
    return keys


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


def _model_with_context(model: dict, category_id: str, brand_id: str) -> dict:
    enriched = dict(model)
    if not (enriched.get("categoryId") or enriched.get("category_id")):
        enriched["categoryId"] = category_id
    if not (enriched.get("brandId") or enriched.get("brand_id")):
        enriched["brandId"] = brand_id
    return enriched


def _model_cache_key(model: dict, category_id: str, brand_id: str) -> str:
    model_id = model.get("modelId") or model.get("id") or model.get("code")
    if model_id is None:
        model_id = model.get("modelName") or model
    return f"{category_id}:{brand_id}:{model_id}"


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


def _category_business_type(category: dict) -> str | None:
    raw_biz_type = None
    for key in ("bizType", "biz_type"):
        if key in category:
            raw_biz_type = category.get(key)
            break

    if raw_biz_type is None or isinstance(raw_biz_type, bool):
        return None
    if isinstance(raw_biz_type, float):
        if not raw_biz_type.is_integer():
            return None
        biz_type = int(raw_biz_type)
    else:
        try:
            biz_type = int(str(raw_biz_type).strip())
        except (TypeError, ValueError):
            return None

    if biz_type == 0:
        if _category_name(category) in ALLOWED_CATEGORY_NAMES:
            return SELF_OPERATED_BUSINESS_TYPE
        return None
    return AGGREGATED_BUSINESS_TYPE


def _filter_allowed_categories(categories: list) -> list:
    return [
        category
        for category in categories
        if isinstance(category, dict) and _category_business_type(category) is not None
    ]


def _empty_business_options() -> dict:
    return {
        "applicable_categories": [],
        "brands_by_category": {},
        "models": [],
        "counts": {
            "categories": 0,
            "brand_groups": 0,
            "brands": 0,
            "models": 0,
        },
    }


def _empty_options_by_business_type() -> dict:
    return {
        business_type: _empty_business_options()
        for business_type in BUSINESS_TYPES
    }


def _model_category_id(model: dict) -> str:
    value = model.get("categoryId")
    if value is None:
        value = model.get("category_id")
    return "" if value is None else str(value)


def _business_options(
    categories: list,
    brands_by_category: dict,
    models: list,
) -> dict:
    category_ids = {
        category_id
        for category in categories
        if (category_id := _category_id(category))
    }
    grouped_brands = {
        str(category_id): brands
        for category_id, brands in brands_by_category.items()
        if str(category_id) in category_ids
    }
    grouped_models = [
        model
        for model in models
        if isinstance(model, dict) and _model_category_id(model) in category_ids
    ]
    brand_ids = {
        brand_id
        for brands in grouped_brands.values()
        for brand_id in _collect_values(
            brands,
            ("brandId", "id", "code", "value"),
        )
    }
    return {
        "applicable_categories": categories,
        "brands_by_category": grouped_brands,
        "models": grouped_models,
        "counts": {
            "categories": len(categories),
            "brand_groups": len(grouped_brands),
            "brands": len(brand_ids),
            "models": len(grouped_models),
        },
    }


def _build_options_by_business_type(
    categories: list,
    brands_by_category: dict,
    models: list,
) -> dict:
    categories_by_business_type = {
        business_type: [] for business_type in BUSINESS_TYPES
    }
    for category in categories:
        if not isinstance(category, dict):
            continue
        business_type = _category_business_type(category)
        if business_type is not None:
            categories_by_business_type[business_type].append(category)

    return {
        business_type: _business_options(
            categories_by_business_type[business_type],
            brands_by_category,
            models,
        )
        for business_type in BUSINESS_TYPES
    }


def _ensure_options_by_business_type(cache: dict) -> dict:
    normalized = dict(cache)
    normalized.setdefault("updated_at", None)
    normalized.setdefault("applicable_categories", [])
    normalized.setdefault("brands_by_category", {})
    normalized.setdefault("models", [])

    derived = _build_options_by_business_type(
        normalized["applicable_categories"],
        normalized["brands_by_category"],
        normalized["models"],
    )
    stored = normalized.get("options_by_business_type")
    if isinstance(stored, dict):
        for business_type in BUSINESS_TYPES:
            group = stored.get(business_type)
            if isinstance(group, dict):
                merged = derived[business_type]
                merged.update(
                    {
                        key: group[key]
                        for key in (
                            "applicable_categories",
                            "brands_by_category",
                            "models",
                            "counts",
                        )
                        if key in group
                    }
                )
                derived[business_type] = merged
    normalized["options_by_business_type"] = derived
    return normalized


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


async def _fetch_json_with_retry(
    client: httpx.AsyncClient,
    method: str,
    kind: str,
    *,
    params=None,
    body=None,
    cookie=None,
    attempts: int = 3,
):
    for attempt in range(attempts):
        try:
            return await _fetch_json(
                client,
                method,
                kind,
                params=params,
                body=body,
                cookie=cookie,
            )
        except HTTPException as exc:
            retryable = exc.status_code in (408, 429) or exc.status_code >= 500
            if not retryable or attempt + 1 >= attempts:
                raise
        except httpx.HTTPError:
            if attempt + 1 >= attempts:
                raise
        await asyncio.sleep(2**attempt)
    raise RuntimeError("Unreachable retry state")


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
def get_manhattan_cache(
    business_type: str | None = Query(
        None,
        description="按业务类型读取适用类目缓存：self_operated 或 aggregated",
    ),
):
    cache = _read_cache()
    if business_type is None:
        return cache

    normalized_business_type = business_type.strip().lower()
    if normalized_business_type not in BUSINESS_TYPES:
        raise HTTPException(
            400,
            "business_type must be self_operated or aggregated.",
        )

    return {
        "updated_at": cache.get("updated_at"),
        "business_type": normalized_business_type,
        **cache["options_by_business_type"][normalized_business_type],
    }


async def _refresh_manhattan_cache_job(cookie: str) -> None:
    global _runtime_cookie
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
                    model_queries = [
                        (category_id, brand_id)
                        for category_id in category_ids
                        for brand_id in _collect_values(
                            brands_by_category.get(category_id, []),
                            ("brandId", "id", "code", "value"),
                        )
                    ]
                    total_model_queries = len(model_queries)
                    completed_model_queries = 0
                    _set_refresh_status(
                        stage="models",
                        message=f"正在获取适用机型：0/{total_model_queries}",
                        current=0,
                        total=total_model_queries,
                        percent=80,
                    )

                    concurrency = max(
                        1, min(settings.NMHT_MODEL_FETCH_CONCURRENCY, 10)
                    )
                    semaphore = asyncio.Semaphore(concurrency)

                    async def fetch_models(category_id: str, brand_id: str):
                        nonlocal completed_model_queries
                        async with semaphore:
                            await asyncio.sleep(REQUEST_DELAY_SECONDS)
                            models_raw = await _fetch_json_with_retry(
                                client,
                                "POST",
                                "models",
                                body={"categoryId": category_id, "brandIdList": [brand_id]},
                                cookie=cookie,
                            )
                            completed_model_queries += 1
                            percent = 80 + int(
                                (completed_model_queries / max(total_model_queries, 1)) * 15
                            )
                            _set_refresh_status(
                                message=(
                                    "正在获取适用机型："
                                    f"{completed_model_queries}/{total_model_queries}"
                                ),
                                current=completed_model_queries,
                                total=total_model_queries,
                                percent=percent,
                                counts={
                                    "categories": total_categories,
                                    "brand_groups": len(brands_by_category),
                                    "brands": len(brand_ids),
                                    "models": len(models),
                                },
                            )
                            return category_id, brand_id, models_raw

                    tasks = [
                        asyncio.create_task(fetch_models(category_id, brand_id))
                        for category_id, brand_id in model_queries
                    ]
                    try:
                        model_results = await asyncio.gather(*tasks)
                    except Exception:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise

                    for category_id, brand_id, models_raw in model_results:
                        for model in _extract_items(models_raw):
                            if not isinstance(model, dict):
                                continue
                            model = _model_with_context(model, category_id, brand_id)
                            model_key = _model_cache_key(model, category_id, brand_id)
                            if model_key in seen_model_ids:
                                continue
                            seen_model_ids.add(model_key)
                            models.append(model)

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
                "options_by_business_type": _build_options_by_business_type(
                    categories,
                    brands_by_category,
                    models,
                ),
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
            if (
                isinstance(exc, HTTPException)
                and exc.status_code in (401, 403)
                and _runtime_cookie == cookie
            ):
                _runtime_cookie = ""
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
