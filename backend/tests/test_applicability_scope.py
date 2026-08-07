from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.applicability import (
    filter_applicable_rows,
    resolve_applicability_scope,
)
from app.services.knowledge_dedup import search_embeddings


def _row(
    knowledge_id: str,
    *,
    categories=None,
    brands=None,
    models=None,
):
    return SimpleNamespace(
        knowledge_id=knowledge_id,
        applicable_categories=categories or [],
        applicable_brands=brands or [],
        applicable_models=models or [],
    )


def test_category_brand_model_funnel_excludes_cross_category_knowledge() -> None:
    cache = {
        "applicable_categories": [
            {
                "categoryId": 119,
                "categoryName": "平板电脑",
                "bizType": 0,
            },
            {
                "categoryId": 1100000016,
                "categoryName": "笔记本",
                "bizType": 0,
            },
        ],
        "brands_by_category": {
            "1100000016": [
                {"brandId": 10530, "brandName": "苹果"},
                {"brandId": 10544, "brandName": "联想"},
            ]
        },
        "models": [
            {
                "modelId": 7000,
                "modelName": "拯救者 R7000P 2022",
                "categoryId": "1100000016",
                "brandId": "10544",
            },
            {
                "modelId": 7001,
                "modelName": "拯救者 Y7000P 2022",
                "categoryId": "1100000016",
                "brandId": "10544",
            },
        ],
        "options_by_business_type": {
            "self_operated": {
                "applicable_categories": [],
                "brands_by_category": {},
                "models": [],
            }
        },
    }
    scope = resolve_applicability_scope(
        cache,
        "self_operated",
        category_values=["笔记本电脑"],
        brand_values=["联想"],
        model_values=["联想 拯救者 R7000P 2022"],
    )

    assert "1100000016" in scope["categories"]
    assert "10544" in scope["brands"]
    assert "7000" in scope["models"]

    rows = [
        _row("generic"),
        _row("laptop-generic", categories=["1100000016"]),
        _row("tablet", categories=["119"]),
        _row(
            "wrong-brand",
            categories=["笔记本"],
            brands=[{"brandId": 10530, "brandName": "苹果"}],
        ),
        _row(
            "wrong-model",
            categories=[{"categoryId": 1100000016}],
            brands=["10544"],
            models=["7001"],
        ),
        _row(
            "matched",
            categories=[{"categoryId": 1100000016, "categoryName": "笔记本"}],
            brands=[{"brandId": 10544, "brandName": "联想"}],
            models=[{"modelId": 7000, "modelName": "拯救者 R7000P 2022"}],
        ),
    ]

    filtered = filter_applicable_rows(
        rows,
        category_keys=scope["categories"],
        brand_keys=scope["brands"],
        model_keys=scope["models"],
    )

    assert [row.knowledge_id for row in filtered] == [
        "generic",
        "laptop-generic",
        "matched",
    ]


def test_missing_brand_and_model_context_skips_lower_funnel_layers() -> None:
    rows = [
        _row("generic"),
        _row("lenovo", categories=["笔记本"], brands=["联想"]),
        _row("apple", categories=["笔记本电脑"], brands=["苹果"]),
        _row("tablet", categories=["平板电脑"]),
    ]

    filtered = filter_applicable_rows(
        rows,
        category_keys={"笔记本", "笔记本电脑"},
    )

    assert [row.knowledge_id for row in filtered] == [
        "generic",
        "lenovo",
        "apple",
    ]


@patch("app.services.knowledge_dedup._query_embedding")
def test_scope_funnel_runs_before_query_vector_ranking(query_embedding) -> None:
    db = MagicMock()
    scope_query = MagicMock()
    db.query.return_value = scope_query
    scope_query.filter.return_value = scope_query
    scope_query.all.return_value = [
        _row("tablet", categories=["平板电脑"]),
    ]

    result = search_embeddings(
        db,
        query="笔记本屏幕问题",
        knowledge_origin="headquarters_standard",
        business_type="self_operated",
        applicable_category_keys={"笔记本", "笔记本电脑"},
    )

    assert result == []
    query_embedding.assert_not_called()
