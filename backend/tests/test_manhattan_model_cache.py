from app.routes.manhattan import _model_cache_key, _model_with_category


def test_model_with_category_fills_missing_category() -> None:
    model = {
        "modelId": 2191,
        "modelName": "iPhone 11 Pro",
        "categoryId": None,
    }

    enriched = _model_with_category(model, "101")

    assert enriched["categoryId"] == "101"
    assert model["categoryId"] is None


def test_model_with_category_preserves_upstream_category() -> None:
    model = {
        "modelId": 984538,
        "modelName": "LIGHTDOW 420-1600mm f/8.3",
        "categoryId": "1100000180",
    }

    enriched = _model_with_category(model, "101")

    assert enriched["categoryId"] == "1100000180"


def test_model_cache_key_keeps_same_model_separate_by_category() -> None:
    model = {"modelId": 42, "modelName": "共享机型"}

    assert _model_cache_key(model, "101") != _model_cache_key(model, "1100000180")
