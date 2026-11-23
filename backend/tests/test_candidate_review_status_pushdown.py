"""候选列表按状态下推后，结果必须与原来的「全量取回再过滤」逐项一致。"""

from unittest.mock import MagicMock

from app.routes.integration import (
    _candidate_review_light_query,
    _candidate_review_light_rows,
)


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


def test_candidate_review_light_rows_streams_with_a_fixed_buffer() -> None:
    db = MagicMock()
    result = db.execute.return_value
    result.__enter__.return_value = result
    result.__iter__.return_value = iter(["row"])
    result.all.side_effect = AssertionError("must not buffer the entire result")

    assert list(_candidate_review_light_rows(db, keyword="")) == ["row"]
    query = db.execute.call_args.args[0]
    assert 0 < query.get_execution_options()["yield_per"] <= 500
    result.__exit__.assert_called_once()
