from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from answer_hub.workflow import TOPIC_CANDIDATE_COLUMNS, TOPIC_REVIEW_COLUMNS


def _topic_workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "topic_review_queue"
    headers = TOPIC_CANDIDATE_COLUMNS + TOPIC_REVIEW_COLUMNS
    sheet.append(headers)
    row = {
        "主题ID": "TOP-AUTO-UI-001",
        "主题状态": "review_pending",
        "主题样本数": 2,
        "主标题": "手机屏幕异常核验",
        "知识内容": "先清洁屏幕，再切换纯色背景检查并记录异常现象。",
        "知识分类": "检测方法",
        "知识来源": "方向二主题候选",
        "关联标准项": "STD-PHONE-001",
        "适用范围": "手机-通用",
        "生效状态": "待审核",
        "来源版本": "qc-test",
        "变更类型": "新增",
        "模型初标结论": "通过",
        "模型初标是否值得沉淀": "值得沉淀",
        "模型初标置信度": 0.93,
        "模型初标重点复核": "否",
        "模型初标提供方": "mimo",
        "模型初标模型名称": "mimo-v2.5-pro",
        "模型初标Prompt版本": "multi-category-topic-initial-review-v3",
        "模型初标状态": "topic_initial_reviewed_model",
        "模型初标标准一致性": "一致",
        "模型初标证据充分性": "充分",
        "模型初标内容一致性": "一致",
        "模型初标标题质量": "清晰",
        "模型初标图片必要性": "不需要",
        "是否重点复核": "否",
        "自动审核状态": "validation_auto_approve",
        "自动审核原因": "满足模型自动放行条件",
        "自动审核策略版本": "model-auto-review-v1",
    }
    sheet.append([row.get(header, "") for header in headers])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_streamlit_auto_review_workspace_renders_validation_metrics() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "审核与反馈"
    app.session_state["generated_topic_workbook"] = _topic_workbook_bytes()
    app.run(timeout=30)

    assert not app.exception
    assert any(metric.label == "模型可自动放行" for metric in app.metric)
    assert any(button.label == "提交验证通过候选" for button in app.button)
