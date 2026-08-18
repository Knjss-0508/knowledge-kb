from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_import_review_hides_source_trace_but_keeps_validation_remark() -> None:
    assert '<div class="import-review-block-label">来源追溯</div>' not in FRONTEND
    assert "{{validationSourceTrace(fm)}}" not in FRONTEND
    assert "validationSourceTrace: function" not in FRONTEND
    assert '<div class="import-review-block-label">原始校验备注</div>' in FRONTEND
    assert "{{validationRemark(fm)}}" in FRONTEND
