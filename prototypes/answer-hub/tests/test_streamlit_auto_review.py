from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from answer_hub.automation_queue import (
    AutomationQueue,
    write_queue_job_metadata,
)
from answer_hub.run_feedback import RunFeedbackStore
from answer_hub.workflow import TOPIC_CANDIDATE_COLUMNS, TOPIC_REVIEW_COLUMNS


def _topic_workbook_bytes(
    *,
    include_human_topic_stage: bool = True,
    include_auto_review_status: bool = True,
    include_pending_cluster: bool = False,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "topic_review_queue"
    headers = TOPIC_CANDIDATE_COLUMNS + TOPIC_REVIEW_COLUMNS
    if not include_human_topic_stage:
        headers = [
            header
            for header in headers
            if header != "人工主题问题分类"
        ]
    if not include_auto_review_status:
        headers = [
            header
            for header in headers
            if header != "自动审核状态"
        ]
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
        "主题问题分类": "质检标准",
        "主题沉淀价值": "值得沉淀",
        "人工主题问题分类": "质检标准",
        "是否值得沉淀": "是",
        "是否可用": "是",
        "自动审核状态": "validation_auto_approve",
        "自动审核原因": "满足模型自动放行条件",
        "自动审核策略版本": "model-auto-review-v1",
    }
    sheet.append([row.get(header, "") for header in headers])
    if include_pending_cluster:
        pending = workbook.create_sheet("pending_cluster_rows")
        pending_headers = [
            "数据ID",
            "工单ID",
            "核心问题",
            "待聚合状态",
            "待聚合原因",
            "聚类准入状态",
            "聚类准入置信度",
            "聚类准入原因",
        ]
        pending.append(pending_headers)
        pending_row = {
            "数据ID": "PENDING-CLUSTER-001",
            "工单ID": "PENDING-CLUSTER-001",
            "核心问题": "手机屏幕色斑如何判定",
            "待聚合状态": "pending_cluster_review",
            "待聚合原因": "聚类置信度低于阈值",
            "聚类准入状态": "待人工聚类复核",
            "聚类准入置信度": 0.68,
            "聚类准入原因": "聚类准入置信度 0.680 低于自动放行阈值 0.750",
        }
        pending.append(
            [pending_row.get(header, "") for header in pending_headers]
        )
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
    assert any(metric.label == "问题分类准确率" for metric in app.metric)
    assert any(metric.label == "沉淀价值准确率" for metric in app.metric)
    assert any(metric.label == "模型问题分类" for metric in app.metric)
    assert any(metric.label == "模型沉淀价值" for metric in app.metric)
    assert any(selectbox.label == "人工问题分类" for selectbox in app.selectbox)
    assert not any(
        button.label in {"提交验证通过候选", "提交模型自动通过候选"}
        for button in app.button
    )
    assert any(
        "Streamlit 仅用于准确性验证" in element.value
        for element in [*app.info, *app.warning]
    )


def test_streamlit_accepts_old_topic_workbook_when_saving_human_topic_stage() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "审核与反馈"
    app.session_state["generated_topic_workbook"] = _topic_workbook_bytes(
        include_human_topic_stage=False,
        include_auto_review_status=False,
    )
    app.run(timeout=30)

    assert not app.exception
    category = next(
        selectbox
        for selectbox in app.selectbox
        if selectbox.label == "人工问题分类"
    )
    category.set_value("质检标准")
    next(
        button
        for button in app.button
        if button.label == "保存验证结果"
    ).click()
    app.run(timeout=30)

    assert not app.exception
    assert (
        app.session_state["topic_review_changes"][2]["人工主题问题分类"]
        == "质检标准"
    )


def test_streamlit_reports_corrupt_topic_workbook_without_crashing() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "审核与反馈"
    app.session_state["generated_topic_workbook"] = b"not-an-xlsx"
    app.run(timeout=30)

    assert not app.exception
    assert any("无法读取主题复核工作簿" in error.value for error in app.error)


def test_streamlit_shows_cluster_admission_pending_queue() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "审核与反馈"
    app.session_state["generated_topic_workbook"] = _topic_workbook_bytes(
        include_pending_cluster=True,
    )
    app.run(timeout=30)

    assert not app.exception
    assert any(
        "聚类待复核 / 待聚合记录（1）" in expander.label
        for expander in app.expander
    )


def test_streamlit_automation_workspace_shows_mimo_confirmation_alert() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "自动化看板"
    app.session_state["automation_view"] = "手动验证"
    app.session_state["automation_manifest"] = {
        "run_id": "run-needs-confirmation",
        "status": "needs_confirmation",
        "status_label": "等待人工确认",
        "error": "MiMo API 预检失败：MiMo HTTP 401: unauthorized。已停止自动生成，请人工确认是否继续。",
        "alerts": [
            "MiMo API 预检失败：MiMo HTTP 401: unauthorized。已停止自动生成，请人工确认是否修复配置后重跑，或明确允许规则兜底生成。",
            "确认继续后，请使用 --continue-on-mimo-unavailable 或队列任务选项 continue_on_mimo_unavailable=true。",
        ],
        "summary": {
            "mimo_preflight": {
                "passed": False,
                "error": "MiMo HTTP 401: unauthorized",
                "continued_with_rule_fallback": False,
            }
        },
        "stages": [],
        "artifacts": {},
        "run_dir": "outputs/automation-runs/run-needs-confirmation",
    }
    app.session_state["automation_pending_confirmation"] = {
        "source_name": "sample.xlsx",
        "source_bytes": b"placeholder workbook",
        "product_label": "全部",
        "use_mimo": True,
        "clustering_mode": "direct_mimo",
        "semantic_threshold": 0.84,
        "cluster_review_floor": 0.75,
        "cluster_auto_merge_threshold": 0.92,
        "cluster_review_limit": 100,
    }
    app.run(timeout=30)

    assert not app.exception
    assert any("MiMo API 预检失败" in element.value for element in app.warning)
    assert any(button.label == "确认规则兜底继续生成" for button in app.button)


def test_streamlit_automation_start_button_is_clickable_before_file_upload() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "自动化看板"
    app.session_state["automation_view"] = "手动验证"
    app.run(timeout=30)

    assert not app.exception
    start_buttons = [
        button for button in app.button if button.label == "启动自动化运行"
    ]
    assert start_buttons
    assert start_buttons[0].disabled is False


def test_streamlit_automation_defaults_to_persistent_run_monitor() -> None:
    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "自动化看板"
    app.run(timeout=30)

    assert not app.exception
    view = next(
        control
        for control in app.segmented_control
        if control.label == "自动化视图"
    )
    assert view.value == "运行记录"
    assert view.options == ["运行记录", "手动验证"]
    assert any(
        "线上运行记录" in element.value
        for element in app.markdown
    )
    assert any(button.label == "立即刷新" for button in app.button)


def test_streamlit_run_monitor_handles_stalled_job_feedback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    feedback_path = tmp_path / "run_feedback.db"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.processing / "job-stalled--sample.xlsx"
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-stalled",
            "status": "processing",
            "created_at": "2026-08-07T08:00:00+08:00",
            "updated_at": "2026-08-07T08:30:00+08:00",
            "original_filename": "sample.xlsx",
            "run_id": "",
            "options": {},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )
    monkeypatch.setenv("ANSWER_HUB_AUTOMATION_QUEUE", str(queue_root))
    monkeypatch.setenv("ANSWER_HUB_AUTOMATION_OUTPUT", str(output_root))
    monkeypatch.setenv("ANSWER_HUB_RUN_FEEDBACK_PATH", str(feedback_path))

    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "自动化看板"
    app.run(timeout=30)

    assert not app.exception
    assert any(metric.label == "疑似卡住" for metric in app.metric)
    status = next(
        item for item in app.selectbox if item.label == "处理状态"
    )
    owner = next(
        item for item in app.text_input if item.label == "负责人"
    )
    cause = next(
        item for item in app.selectbox if item.label == "原因分类"
    )
    note = next(
        item for item in app.text_area if item.label == "处理反馈"
    )
    actor = next(
        item for item in app.text_input if item.label == "记录人"
    )
    status.set_value("in_progress")
    owner.set_value("小张")
    cause.set_value("clustering")
    note.set_value("已确认卡在主题聚类，准备恢复。")
    actor.set_value("管理员")
    next(
        button for button in app.button if button.label == "保存处理反馈"
    ).click()
    app.run(timeout=30)

    assert not app.exception
    saved = RunFeedbackStore(feedback_path).get("job-stalled")
    assert saved is not None
    assert saved["status"] == "in_progress"
    assert saved["owner"] == "小张"
    assert saved["cause_type"] == "clustering"


def test_streamlit_run_monitor_uses_configured_stale_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.processing / "job-old--sample.xlsx"
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-old",
            "status": "processing",
            "created_at": "2026-08-07T08:00:00+08:00",
            "updated_at": "2026-08-07T08:30:00+08:00",
            "original_filename": "sample.xlsx",
            "run_id": "",
            "options": {},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )
    monkeypatch.setenv("ANSWER_HUB_AUTOMATION_QUEUE", str(queue_root))
    monkeypatch.setenv("ANSWER_HUB_AUTOMATION_OUTPUT", str(output_root))
    monkeypatch.setenv(
        "ANSWER_HUB_RUN_FEEDBACK_PATH",
        str(tmp_path / "run_feedback.db"),
    )
    monkeypatch.setenv(
        "ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS",
        "10000000",
    )

    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "运行监管"
    app.run(timeout=30)

    assert not app.exception
    stalled_metric = next(
        metric for metric in app.metric if metric.label == "疑似卡住"
    )
    assert str(stalled_metric.value) == "0"


def test_streamlit_run_monitor_reports_corrupt_feedback_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    feedback_path = tmp_path / "run_feedback.db"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.pending / "manual.xlsx"
    source.write_bytes(b"sample")
    feedback_path.write_bytes(b"not-a-valid-sqlite-database")
    monkeypatch.setenv("ANSWER_HUB_AUTOMATION_QUEUE", str(queue_root))
    monkeypatch.setenv("ANSWER_HUB_AUTOMATION_OUTPUT", str(output_root))
    monkeypatch.setenv("ANSWER_HUB_RUN_FEEDBACK_PATH", str(feedback_path))

    app = AppTest.from_file("streamlit_app.py")
    app.session_state["workspace_page"] = "运行监管"
    app.run(timeout=30)

    assert not app.exception
    assert any(
        "监管反馈存储不可用" in error.value
        for error in app.error
    )
