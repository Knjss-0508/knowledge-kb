from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_candidate_review_has_requested_filters() -> None:
    assert "全部品类" in FRONTEND
    assert "全部标注状态" in FRONTEND
    assert "已标注" in FRONTEND
    assert "未标注" in FRONTEND
    assert "模型初标：值得沉淀" in FRONTEND
    assert "模型初标：不值得沉淀" in FRONTEND
    assert "模型初标：待确定" in FRONTEND
    assert "product_category" in FRONTEND
    assert "annotation_status" in FRONTEND
    assert "model_knowledge_value" in FRONTEND
    assert "data.product_categories" in FRONTEND
