import asyncio
import json

import pytest

from app.routes import manhattan
from app.routes.manhattan import _model_cache_key, _model_with_context


def test_model_with_context_fills_missing_category_and_brand() -> None:
    model = {
        "modelId": 2191,
        "modelName": "iPhone 11 Pro",
        "categoryId": None,
        "brandId": None,
    }

    enriched = _model_with_context(model, "101", "1")

    assert enriched["categoryId"] == "101"
    assert enriched["brandId"] == "1"
    assert model["categoryId"] is None
    assert model["brandId"] is None


def test_model_with_context_preserves_upstream_relationships() -> None:
    model = {
        "modelId": 984538,
        "modelName": "LIGHTDOW 420-1600mm f/8.3",
        "categoryId": "1100000180",
        "brandId": "113513",
    }

    enriched = _model_with_context(model, "101", "1")

    assert enriched["categoryId"] == "1100000180"
    assert enriched["brandId"] == "113513"


def test_model_cache_key_keeps_same_model_separate_by_category_and_brand() -> None:
    model = {"modelId": 42, "modelName": "共享机型"}

    assert _model_cache_key(model, "101", "1") != _model_cache_key(
        model, "1100000180", "1"
    )
    assert _model_cache_key(model, "101", "1") != _model_cache_key(
        model, "101", "2"
    )


def test_category_business_type_requires_recognized_biz_type_and_self_whitelist() -> None:
    assert (
        manhattan._category_business_type(
            {"categoryId": 101, "categoryName": "手机", "bizType": 0}
        )
        == manhattan.SELF_OPERATED_BUSINESS_TYPE
    )
    assert (
        manhattan._category_business_type(
            {"categoryId": 201, "categoryName": "黄金", "bizType": "2"}
        )
        == manhattan.AGGREGATED_BUSINESS_TYPE
    )
    assert (
        manhattan._category_business_type(
            {"categoryId": 102, "categoryName": "大家电", "bizType": 0}
        )
        is None
    )
    assert (
        manhattan._category_business_type(
            {"categoryId": 301, "categoryName": "未知业务"}
        )
        is None
    )
    assert (
        manhattan._category_business_type(
            {"categoryId": 302, "categoryName": "未知业务", "bizType": "unknown"}
        )
        is None
    )


def test_legacy_cache_is_grouped_as_self_operated_without_losing_top_level_fields(
    monkeypatch, tmp_path
) -> None:
    cache_file = tmp_path / "manhattan_options.json"
    legacy_cache = {
        "updated_at": "2026-08-04T00:00:00Z",
        "applicable_categories": [
            {"categoryId": 101, "categoryName": "手机", "bizType": 0}
        ],
        "brands_by_category": {
            "101": [{"brandId": 1, "brandName": "Apple"}]
        },
        "models": [
            {
                "modelId": 11,
                "modelName": "iPhone 11",
                "categoryId": "101",
                "brandId": "1",
            }
        ],
    }
    cache_file.write_text(
        json.dumps(legacy_cache, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(manhattan, "CACHE_FILE", str(cache_file))

    cache = manhattan._read_cache()

    assert cache["applicable_categories"] == legacy_cache["applicable_categories"]
    assert cache["brands_by_category"] == legacy_cache["brands_by_category"]
    assert cache["models"] == legacy_cache["models"]
    self_operated = cache["options_by_business_type"]["self_operated"]
    assert self_operated["applicable_categories"] == legacy_cache["applicable_categories"]
    assert self_operated["brands_by_category"] == legacy_cache["brands_by_category"]
    assert self_operated["models"] == legacy_cache["models"]
    assert cache["options_by_business_type"]["aggregated"]["applicable_categories"] == []


def test_cache_can_be_read_by_business_type(monkeypatch) -> None:
    cache = {
        "updated_at": "2026-08-04T00:00:00Z",
        "applicable_categories": [
            {"categoryId": 101, "categoryName": "手机", "bizType": 0},
            {"categoryId": 201, "categoryName": "黄金", "bizType": 2},
        ],
        "brands_by_category": {
            "101": [{"brandId": 1, "brandName": "Apple"}],
            "201": [{"brandId": 2, "brandName": "测试品牌"}],
        },
        "models": [
            {"modelId": 11, "categoryId": "101", "brandId": "1"},
            {"modelId": 21, "categoryId": "201", "brandId": "2"},
        ],
    }
    monkeypatch.setattr(
        manhattan,
        "_read_cache",
        lambda: manhattan._ensure_options_by_business_type(cache),
    )

    response = manhattan.get_manhattan_cache("aggregated")

    assert response["business_type"] == "aggregated"
    assert [item["categoryId"] for item in response["applicable_categories"]] == [201]
    assert list(response["brands_by_category"]) == ["201"]
    assert [item["modelId"] for item in response["models"]] == [21]

    with pytest.raises(manhattan.HTTPException) as exc_info:
        manhattan.get_manhattan_cache("unsupported")
    assert exc_info.value.status_code == 400


def test_cached_category_keys_include_ids_and_names(monkeypatch) -> None:
    cache = {
        "updated_at": "2026-08-04T00:00:00Z",
        "applicable_categories": [],
        "brands_by_category": {},
        "models": [],
        "options_by_business_type": {
            "self_operated": {
                "applicable_categories": [
                    {"categoryId": 119, "categoryName": "平板电脑", "bizType": 0}
                ],
                "brands_by_category": {},
                "models": [],
            },
            "aggregated": {
                "applicable_categories": [
                    {"categoryId": 901, "categoryName": "黄金", "bizType": 2}
                ],
                "brands_by_category": {},
                "models": [],
            },
        },
    }
    monkeypatch.setattr(manhattan, "_read_cache", lambda: cache)

    assert manhattan.cached_applicable_category_keys("self_operated") == {
        "119",
        "平板电脑",
    }
    assert manhattan.cached_applicable_category_keys("aggregated") == {
        "901",
        "黄金",
    }


def test_refresh_fetches_and_tags_models_per_brand(monkeypatch) -> None:
    model_requests: list[dict] = []
    written: dict = {}

    async def fake_fetch_json(
        client, method, kind, *, params=None, body=None, cookie=None
    ):
        if kind == "applicable-categories":
            return [
                {"categoryId": 101, "categoryName": "智能手表", "bizType": 0}
            ]
        if kind == "brands":
            return [
                {"brandId": 1, "brandName": "华为"},
                {"brandId": 2, "brandName": "Apple"},
            ]
        if kind == "models":
            model_requests.append(body)
            brand_id = body["brandIdList"][0]
            return [{"modelId": brand_id * 10, "modelName": f"model-{brand_id}"}]
        raise AssertionError(f"Unexpected source: {kind}")

    monkeypatch.setattr(manhattan, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(manhattan, "_write_cache", lambda data: written.update(data))
    monkeypatch.setattr(manhattan, "REQUEST_DELAY_SECONDS", 0)

    asyncio.run(manhattan._refresh_manhattan_cache_job("test-cookie"))

    assert model_requests == [
        {"categoryId": "101", "brandIdList": ["1"]},
        {"categoryId": "101", "brandIdList": ["2"]},
    ]
    assert [
        (model["categoryId"], model["brandId"]) for model in written["models"]
    ] == [("101", "1"), ("101", "2")]


def test_refresh_groups_all_recognized_categories_by_business_type(monkeypatch) -> None:
    category_request_params: list[dict] = []
    brand_request_category_ids: list[str] = []
    written: dict = {}

    async def fake_fetch_json(
        client, method, kind, *, params=None, body=None, cookie=None
    ):
        if kind == "applicable-categories":
            category_request_params.append(params)
            return [
                {"categoryId": 101, "categoryName": "手机", "bizType": 0},
                {"categoryId": 102, "categoryName": "大家电", "bizType": 0},
                {"categoryId": 201, "categoryName": "黄金", "bizType": 2},
                {"categoryId": 301, "categoryName": "无法识别"},
            ]
        if kind == "brands":
            category_id = params["categoryId"]
            brand_request_category_ids.append(category_id)
            return [{"brandId": int(category_id), "brandName": f"brand-{category_id}"}]
        if kind == "models":
            category_id = body["categoryId"]
            return [{"modelId": int(category_id), "modelName": f"model-{category_id}"}]
        raise AssertionError(f"Unexpected source: {kind}")

    monkeypatch.setattr(manhattan, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(manhattan, "_write_cache", lambda data: written.update(data))
    monkeypatch.setattr(manhattan, "REQUEST_DELAY_SECONDS", 0)

    asyncio.run(manhattan._refresh_manhattan_cache_job("test-cookie"))

    assert category_request_params == [{"bizType": "-2"}]
    assert brand_request_category_ids == ["101", "201"]
    assert [
        item["categoryId"] for item in written["applicable_categories"]
    ] == [101, 201]
    assert [
        item["categoryId"]
        for item in written["options_by_business_type"]["self_operated"][
            "applicable_categories"
        ]
    ] == [101]
    assert [
        item["categoryId"]
        for item in written["options_by_business_type"]["aggregated"][
            "applicable_categories"
        ]
    ] == [201]
    assert list(
        written["options_by_business_type"]["self_operated"]["brands_by_category"]
    ) == ["101"]
    assert list(
        written["options_by_business_type"]["aggregated"]["brands_by_category"]
    ) == ["201"]
    assert [
        item["categoryId"]
        for item in written["options_by_business_type"]["aggregated"]["models"]
    ] == ["201"]


def test_model_refresh_retries_transient_upstream_failure(monkeypatch) -> None:
    attempts = 0

    async def flaky_fetch_json(
        client, method, kind, *, params=None, body=None, cookie=None
    ):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise manhattan.HTTPException(503, "temporary failure")
        return [{"modelId": 1}]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(manhattan, "_fetch_json", flaky_fetch_json)
    monkeypatch.setattr(manhattan.asyncio, "sleep", no_sleep)

    result = asyncio.run(
        manhattan._fetch_json_with_retry(
            object(),
            "POST",
            "models",
            body={"categoryId": "101", "brandIdList": ["1"]},
            cookie="test-cookie",
        )
    )

    assert attempts == 3
    assert result == [{"modelId": 1}]


def test_refresh_limits_concurrent_model_requests(monkeypatch) -> None:
    active_requests = 0
    max_active_requests = 0
    written: dict = {}

    async def fake_fetch_json(
        client, method, kind, *, params=None, body=None, cookie=None
    ):
        nonlocal active_requests, max_active_requests
        if kind == "applicable-categories":
            return [
                {"categoryId": 101, "categoryName": "智能手表", "bizType": 0}
            ]
        if kind == "brands":
            return [
                {"brandId": brand_id, "brandName": f"brand-{brand_id}"}
                for brand_id in range(1, 7)
            ]
        if kind == "models":
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            await asyncio.sleep(0.01)
            active_requests -= 1
            brand_id = body["brandIdList"][0]
            return [{"modelId": brand_id, "modelName": f"model-{brand_id}"}]
        raise AssertionError(f"Unexpected source: {kind}")

    monkeypatch.setattr(manhattan, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(manhattan, "_write_cache", lambda data: written.update(data))
    monkeypatch.setattr(manhattan, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(manhattan.settings, "NMHT_MODEL_FETCH_CONCURRENCY", 3)

    asyncio.run(manhattan._refresh_manhattan_cache_job("test-cookie"))

    assert max_active_requests == 3
    assert len(written["models"]) == 6
    assert manhattan._refresh_status["stage"] == "done"


def test_refresh_clears_runtime_cookie_after_login_expires(monkeypatch) -> None:
    writes: list[dict] = []

    async def fake_fetch_json(
        client, method, kind, *, params=None, body=None, cookie=None
    ):
        if kind == "applicable-categories":
            return [
                {"categoryId": 101, "categoryName": "智能手表", "bizType": 0}
            ]
        if kind == "brands":
            return [{"brandId": 1, "brandName": "华为"}]
        if kind == "models":
            raise manhattan.HTTPException(401, "login expired")
        raise AssertionError(f"Unexpected source: {kind}")

    monkeypatch.setattr(manhattan, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(manhattan, "_write_cache", writes.append)
    monkeypatch.setattr(manhattan, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(manhattan, "_runtime_cookie", "test-cookie")

    asyncio.run(manhattan._refresh_manhattan_cache_job("test-cookie"))

    assert manhattan._runtime_cookie == ""
    assert manhattan._refresh_status["stage"] == "error"
    assert manhattan._refresh_status["error"] == "login expired"
    assert writes == []
