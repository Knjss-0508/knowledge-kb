from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_candidate_review_queue_uses_page_size_and_offset_pagination() -> None:
    """The review queue must not render every filtered candidate at once."""

    for page_size in ("20", "50", "100"):
        assert f'<option :value="{page_size}">{page_size} 条/页</option>' in FRONTEND

    assert "candidateReviews.pageSize" in FRONTEND
    assert "candidateReviews.page" in FRONTEND
    assert "params.set('offset'" in FRONTEND
    assert "上一页" in FRONTEND
    assert "下一页" in FRONTEND
