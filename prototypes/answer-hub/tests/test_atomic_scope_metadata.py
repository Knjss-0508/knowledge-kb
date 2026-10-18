from copy import deepcopy
import json
from types import SimpleNamespace

from answer_hub import workflow
from answer_hub.mimo import MimoLabelResult


def test_verified_atomic_metadata_does_not_inherit_a_sibling_model_or_suggested_steps(monkeypatch):
    common = {
        "数据ID": "TWO-QUESTIONS", "产品类型": "笔记本",
        "原始核心问题": "2019款16寸MacBook Pro耳机孔规格是多少；另一台笔记本耳机插不进。",
        "原始判定结论": "先查询规格并补拍，再按异常处理。",
        "判定依据": "建议查询型号、确认外观并补拍近景。",
        "解题方式": "先查询型号，再拍照核验",
        "机型": "2019款16寸MacBook Pro", "品牌": "Apple",
        "适用机型": "2019款16寸MacBook Pro", "适用品牌": "Apple",
        "图片证据摘要": "无图片，建议补拍近景并核对外观。",
        "聊天内容": (
            "回收师：2019款16寸MacBook Pro耳机孔多大\n答疑：3.5的\n"
            "回收师：另一台笔记本耳机无法插入\n"
            "答疑：耳机接口功能异常(失灵/变形导致无法使用)"
        ),
    }
    rows = [
        {
            **common, "_原子知识ID": "TWO-QUESTIONS-U1",
            "核心问题": "笔记本｜耳机孔｜2019款16寸MacBook Pro耳机孔规格查询",
            "对象/部位": "耳机孔", "异常现象": "接口规格查询",
            "_原子机型范围": "2019款16寸MacBook Pro", "_原子品牌": "Apple",
            "_原子实际回复": "3.5的",
        },
        {
            **common, "_原子知识ID": "TWO-QUESTIONS-U2",
            "核心问题": "笔记本｜耳机接口｜耳机无法插入",
            "对象/部位": "耳机接口", "异常现象": "无法插入耳机",
            "_原子机型范围": "通用", "_原子品牌": "通用",
            "_原子实际回复": "耳机接口功能异常(失灵/变形导致无法使用)",
        },
    ]
    original = deepcopy(rows)
    captured = []
    reviewed_drafts = []

    def label(topic, matches, **kwargs):
        captured.append(topic)
        spec = topic["features"]["对象/部位"] == "耳机孔"
        return MimoLabelResult(candidate={
            "title": "2019款16寸MacBook Pro的耳机孔是什么规格" if spec else "笔记本耳机插不进如何判定",
            "subtitles": ["2019款16寸MacBook Pro的耳机孔是什么规格？"] if spec else [],
            "content": (
                "2019款16寸MacBook Pro的耳机孔为3.5mm规格。"
                if spec else "笔记本耳机无法插入耳机接口时，按耳机接口功能异常处理。"
            ),
            "recommended_reply": "3.5的" if spec else "耳机接口功能异常(失灵/变形导致无法使用)",
            "knowledge_form": "具体判定", "content_type": "定义型",
            "applicable_scope": "笔记本", "applicable_brands": ["Apple"],
            "applicable_models": ["2019款16寸MacBook Pro"],
            "standard_refs": [], "confidence": 0.95,
        }, request_audit={}, response_audit={})

    def review(topic, draft, matches, **kwargs):
        reviewed_drafts.append(draft)
        return MimoLabelResult(candidate={
            "decision": "通过", "knowledge_value": topic["knowledge_value"],
            "error_type": "", "reason": "当前原子问题来源支持。",
            "standard_consistency": "无可信标准", "evidence_sufficiency": "充分",
            "content_consistency": "一致", "image_necessity": "不需要",
            "title_quality": "清晰", "confidence": 0.95, "priority_review": True,
        }, request_audit={}, response_audit={})

    monkeypatch.setattr(workflow, "_direct_mimo_topic_groups", lambda *a, **k: (
        [(("frozen", str(i)), [row]) for i, row in enumerate(rows)],
        {"effective_mode": "direct_mimo"},
    ))
    topics, mapping, gaps, _ = workflow.build_topic_review_rows(
        rows, use_mimo=True,
        mimo_client=SimpleNamespace(
            config=SimpleNamespace(model="test"), label_topic=label, review_topic=review,
        ),
        clustering_mode="direct_mimo", use_standard_references=False,
        transcribe_all_admitted_topics=True,
    )
    assert not gaps and len(topics) == len(mapping) == 2
    by_subject = {topic["主题对象/部位"]: topic for topic in topics}
    spec = by_subject["耳机孔"]
    generic = by_subject["耳机接口"]
    assert "2019款16寸MacBook Pro" in spec["适用机型"]
    assert "2019款16寸MacBook Pro" in spec["副标题"]
    assert not generic["适用机型"] and not generic["适用品牌"]
    assert all(topic["正文类型"] == "定义型" for topic in topics)
    assert "补拍" not in json.dumps(captured, ensure_ascii=False)
    assert "查询型号" not in json.dumps(captured, ensure_ascii=False)
    assert len(reviewed_drafts) == 2
    assert all(draft.get("推荐回复") for draft in reviewed_drafts)
    assert "2019款16寸MacBook Pro" not in json.dumps(
        next(topic for topic in captured if topic["features"]["对象/部位"] == "耳机接口"),
        ensure_ascii=False,
    )
    assert rows == original
    assert all("另一台" in item["聊天内容"] for item in mapping)
