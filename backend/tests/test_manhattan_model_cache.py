import asyncio

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


def test_refresh_fetches_and_tags_models_per_brand(monkeypatch) -> None:
    model_requests: list[dict] = []
    written: dict = {}

    async def fake_fetch_json(
        client, method, kind, *, params=None, body=None, cookie=None
    ):
        if kind == "applicable-categories":
            return [{"categoryId": 101, "categoryName": "智能手表"}]
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
