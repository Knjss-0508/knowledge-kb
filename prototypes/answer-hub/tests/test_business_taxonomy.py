from __future__ import annotations

from answer_hub.business_taxonomy import (
    AGGREGATE_BUSINESS_LINE_NAME,
    SELF_OPERATED_BUSINESS_LINE_NAME,
    business_line_from_record,
    business_line_metadata,
    configured_business_line_names,
    cz_applicable_category_path,
    default_business_line,
    resolve_business_line,
)


def test_business_taxonomy_reserves_self_operated_and_aggregate_levels() -> None:
    assert configured_business_line_names() == (
        SELF_OPERATED_BUSINESS_LINE_NAME,
        AGGREGATE_BUSINESS_LINE_NAME,
    )
    assert default_business_line().name == SELF_OPERATED_BUSINESS_LINE_NAME
    assert resolve_business_line("聚合").name == AGGREGATE_BUSINESS_LINE_NAME

    metadata = business_line_metadata()
    assert metadata["default_code"] == "self_operated"
    assert len(metadata["digest"]) == 64
    assert metadata["lines"]["self_operated"]["product_categories_configured"] is True
    assert metadata["lines"]["aggregate"]["product_categories_configured"] is False


def test_business_line_from_record_defaults_current_products_to_self_operated() -> None:
    assert (
        business_line_from_record({"产品类型": "手机"}).name
        == SELF_OPERATED_BUSINESS_LINE_NAME
    )
    assert (
        business_line_from_record(
            {"回收业务层级": "聚合回收", "产品类型": "手机"}
        ).name
        == AGGREGATE_BUSINESS_LINE_NAME
    )
    assert (
        business_line_from_record({"产品类型": "聚合回收"}).name
        == AGGREGATE_BUSINESS_LINE_NAME
    )


def test_cz_applicable_category_path_keeps_business_hierarchy() -> None:
    assert (
        cz_applicable_category_path("自营回收", "手机")
        == "自营回收/手机"
    )
    assert cz_applicable_category_path("聚合回收", "") == "聚合回收"
