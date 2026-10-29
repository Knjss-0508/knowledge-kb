"""候选列表按状态下推后，结果必须与原来的「全量取回再过滤」逐项一致。"""

from app.routes.integration import _candidate_review_light_query


def _where_sql(query) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True})).lower()


def test_review_status_filter_is_pushed_into_sql() -> None:
    sql = _where_sql(
        _candidate_review_light_query(
            with_keyword_fields=False,
            review_status="pending",
        )
    )

    assert "review_status" in sql
    assert "'pending'" in sql


def test_other_statuses_are_not_hardcoded() -> None:
    """下推必须使用请求里的状态，不能写死 pending。"""

    sql = _where_sql(
        _candidate_review_light_query(
            with_keyword_fields=False,
            review_status="revision_required",
        )
    )

    assert "'revision_required'" in sql
    assert "'pending'" not in sql


def test_no_status_filter_keeps_the_unfiltered_scan() -> None:
    """不带状态时保持原来的全量行为，摘要仍能覆盖所有状态。"""

    sql = _where_sql(
        _candidate_review_light_query(
            with_keyword_fields=False,
            review_status="",
        )
    )

    assert "review_status is not null" in sql
    assert "review_status =" not in sql
