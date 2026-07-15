from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import sys
from typing import Any

import streamlit as st
from openpyxl import Workbook, load_workbook


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from answer_hub.workflow import (  # noqa: E402
    ERROR_TYPES,
    KNOWLEDGE_MASTER_COLUMNS,
    REVIEW_DECISIONS,
    TOPIC_CANDIDATE_COLUMNS,
    TOPIC_REVIEW_COLUMNS,
    finalize_topic_review_rows,
    initial_label_from_workbook,
)
from answer_hub.cz_integration import CzIntegrationAdapter  # noqa: E402


st.set_page_config(
    page_title="手机主题知识工作台",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root {
      --ink: #172033;
      --muted: #667085;
      --line: #d9e1ea;
      --line-strong: #c5d0dc;
      --canvas: #f3f6fa;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --accent: #176b87;
      --accent-dark: #12566d;
      --accent-soft: #e8f4f7;
      --indigo: #4f5d95;
      --warning: #a15c07;
      --warning-soft: #fff5e6;
      --danger: #b42318;
      --success: #087443;
    }
    #MainMenu, footer, header { visibility: hidden; }
    .stApp {
      background: var(--canvas);
    }
    .block-container {
      max-width: 1660px;
      padding: 1.2rem 2.1rem 3rem;
    }
    [data-testid="stSidebar"] {
      background: var(--surface);
      border-right: 1px solid var(--line);
    }
    h1, h2, h3 {
      color: var(--ink);
      letter-spacing: 0;
    }
    h1 { font-size: 1.7rem !important; margin-bottom: 0.15rem !important; }
    h2 { font-size: 1.15rem !important; }
    h3 { font-size: 1rem !important; }
    .stCaption {
      color: var(--muted) !important;
      line-height: 1.45;
    }
    .workspace-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1.5rem;
      background: var(--surface);
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      padding: 1.15rem 1.35rem 1.05rem;
      margin-bottom: 0.8rem;
      box-shadow: 0 3px 12px rgba(23, 32, 51, 0.04);
    }
    .workspace-header-copy {
      min-width: 0;
    }
    .workspace-header-meta {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 0.45rem;
      color: var(--muted);
      font-size: 0.76rem;
      white-space: nowrap;
    }
    .meta-pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0.32rem 0.58rem;
      background: #fbfcfe;
    }
    .page-heading {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 1rem;
      margin: 1.35rem 0 0.85rem;
    }
    .page-heading h2 {
      margin: 0;
      font-size: 1.25rem !important;
    }
    .page-heading p {
      margin: 0.22rem 0 0;
      color: var(--muted);
      font-size: 0.88rem;
    }
    .section-label {
      color: var(--muted);
      font-size: 0.74rem;
      font-weight: 750;
      letter-spacing: 0.02em;
      margin: 0.7rem 0 0.42rem;
      text-transform: uppercase;
    }
    .section-label::before {
      content: "";
      display: inline-block;
      width: 4px;
      height: 12px;
      margin-right: 7px;
      vertical-align: -1px;
      border-radius: 2px;
      background: var(--accent);
    }
    .status-strip {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      padding: 0.65rem 0.8rem;
      color: var(--muted);
      font-size: 0.8rem;
      line-height: 1.35;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      flex: 0 0 auto;
      border-radius: 50%;
      background: var(--accent);
    }
    .status-dot.warning { background: #d28a12; }
    .status-dot.success { background: var(--success); }
    .status-dot.danger { background: var(--danger); }
    .action-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      padding: 0.95rem 1rem 0.8rem;
      box-shadow: 0 2px 8px rgba(23, 32, 51, 0.03);
    }
    .action-panel .section-label {
      margin-top: 0;
    }
    [data-testid="stMetric"] {
      min-height: 92px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0.8rem 0.9rem;
      box-shadow: 0 2px 8px rgba(23, 32, 51, 0.03);
    }
    [data-testid="stMetricValue"] { color: var(--ink); font-size: 1.35rem; }
    [data-testid="stMetricLabel"] { color: var(--muted); }
    .stButton > button, .stDownloadButton > button {
      min-height: 2.45rem;
      border-radius: 6px;
      border-color: var(--line-strong);
      font-weight: 600;
      box-shadow: none;
    }
    .stButton > button[kind="primary"] {
      background: var(--accent);
      border-color: var(--accent);
    }
    .stButton > button[kind="primary"]:hover {
      background: var(--accent-dark);
      border-color: var(--accent-dark);
    }
    .stDownloadButton > button {
      background: var(--surface);
      color: var(--ink);
    }
    [data-testid="stFileUploader"] {
      border: 1px dashed var(--line-strong);
      border-radius: 7px;
      background: #fbfcfe;
      padding: 0.35rem 0.55rem 0.5rem;
    }
    [data-testid="stFileUploader"] section {
      padding: 0.45rem 0.55rem;
      border: 0;
      background: transparent;
    }
    [data-testid="stFileUploaderDropzone"] {
      min-height: 104px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    [data-testid="stDataFrame"] {
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow: hidden;
      background: var(--surface);
      box-shadow: 0 2px 8px rgba(23, 32, 51, 0.03);
    }
    [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-testid="stSelectbox"] [data-baseweb="select"] > div {
      border-color: var(--line-strong);
      border-radius: 6px;
      background: var(--surface);
    }
    [data-testid="stDataFrame"] {
      margin-top: 0.25rem;
    }
    [data-testid="stTextArea"] textarea {
      background: #fbfcfe;
      border-color: var(--line);
      font-size: 0.88rem;
      line-height: 1.55;
    }
    [data-testid="stCheckbox"] label,
    [data-testid="stRadio"] label {
      color: var(--ink);
      font-size: 0.86rem;
    }
    [data-testid="stRadio"] > div {
      gap: 0;
      padding: 0.2rem;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      width: fit-content;
    }
    [data-testid="stRadio"] label {
      padding: 0.35rem 0.85rem;
      border-radius: 5px;
    }
    [data-testid="stRadio"] label:has(input:checked) {
      background: var(--accent-soft);
      color: var(--accent-dark);
      font-weight: 700;
    }
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
      gap: 0.15rem;
      border-bottom: 1px solid var(--line);
    }
    [data-testid="stTabs"] button[role="tab"] {
      height: 2.35rem;
      border-radius: 5px 5px 0 0;
      color: var(--muted);
      font-weight: 600;
    }
    [data-testid="stTabs"] button[aria-selected="true"] {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: var(--accent-soft);
    }
    [data-testid="stExpander"] {
      border-color: var(--line);
      border-radius: 7px;
      background: var(--surface);
    }
    .workspace-kicker {
      color: var(--accent);
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      margin-bottom: 0.35rem;
    }
    .workspace-title {
      color: var(--ink);
      font-size: 1.55rem;
      font-weight: 700;
      line-height: 1.2;
      margin: 0;
    }
    .workspace-subtitle {
      color: var(--muted);
      margin: 0.45rem 0 1.35rem;
      font-size: 0.92rem;
    }
    @media (max-width: 900px) {
      .block-container { padding: 0.9rem 0.8rem 2rem; }
      .workspace-header { display: block; }
      .workspace-header-meta {
        justify-content: flex-start;
        margin-top: 0.75rem;
      }
      .page-heading { display: block; }
      .workspace-title { font-size: 1.35rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if item not in (None, ""))
    return str(value)


def _load_topic_workbook(
    workbook_bytes: bytes,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    workbook = load_workbook(BytesIO(workbook_bytes), data_only=False)
    if "topic_review_queue" not in workbook.sheetnames:
        raise ValueError("工作簿缺少 topic_review_queue 工作表，请上传主题级候选工作簿。")

    def read_sheet(sheet_name: str) -> list[dict[str, Any]]:
        if sheet_name not in workbook.sheetnames:
            return []
        worksheet = workbook[sheet_name]
        values = list(worksheet.iter_rows(values_only=True))
        if not values:
            return []
        headers = [str(value).strip() if value is not None else "" for value in values[0]]
        rows = []
        for row_index, values_row in enumerate(values[1:], start=2):
            row = {
                header: values_row[column_index] if column_index < len(values_row) else None
                for column_index, header in enumerate(headers)
                if header
            }
            if any(value not in (None, "") for value in row.values()):
                row["_review_row_index"] = row_index
                rows.append(row)
        return rows

    topic_rows = read_sheet("topic_review_queue")
    expected = TOPIC_CANDIDATE_COLUMNS + TOPIC_REVIEW_COLUMNS
    if topic_rows:
        missing = [field for field in expected if field not in topic_rows[0]]
        if missing:
            raise ValueError(f"主题复核工作簿缺少字段：{', '.join(missing)}")
    return (
        topic_rows,
        read_sheet("topic_source_mapping"),
        read_sheet("evidence_gap_rows"),
        read_sheet("pending_cluster_rows"),
        read_sheet("topic_model_drafts"),
    )


def _update_topic_workbook(workbook_bytes: bytes, changes: dict[int, dict[str, str]]) -> bytes:
    workbook = load_workbook(BytesIO(workbook_bytes))
    worksheet = workbook["topic_review_queue"]
    header_map = {
        str(cell.value).strip(): cell.column
        for cell in worksheet[1]
        if cell.value is not None and str(cell.value).strip()
    }
    for row_index, updates in changes.items():
        for field, value in updates.items():
            worksheet.cell(row=row_index, column=header_map[field], value=value)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _rows_to_xlsx_bytes(sheet_name: str, columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    worksheet.append(columns)
    for row in rows:
        worksheet.append(
            [
                "\n".join(str(item) for item in value) if isinstance((value := row.get(column, "")), list) else value
                for column in columns
            ]
        )
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows).encode("utf-8")


def _read_only_text(label: str, value: Any, height: int = 160, key: str | None = None) -> None:
    st.text_area(
        label,
        value=_text(value),
        height=height,
        disabled=True,
        label_visibility="visible",
        key=key,
    )


def _reset_editor(row: dict[str, Any]) -> None:
    st.session_state.selected_topic_row = row["_review_row_index"]
    for field in [*KNOWLEDGE_MASTER_COLUMNS, *TOPIC_REVIEW_COLUMNS]:
        st.session_state[f"topic_{field}"] = _text(row.get(field))


def _selected_row(rows: list[dict[str, Any]], row_index: int | None) -> dict[str, Any] | None:
    return next((row for row in rows if row["_review_row_index"] == row_index), None)


def _filtered_rows(rows: list[dict[str, Any]], keyword: str, decision: str, focus_only: bool) -> list[dict[str, Any]]:
    query = keyword.strip().lower()
    result = []
    for row in rows:
        row_decision = _text(row.get("审核结论"))
        if decision == "未审核" and row_decision:
            continue
        if decision and decision != "未审核" and row_decision != decision:
            continue
        if focus_only and _text(row.get("模型初标重点复核")) != "是":
            continue
        if query and not any(
            query in _text(row.get(field)).lower()
            for field in ("主题ID", "主标题", "主题来源记录ID", "关联标准项", "知识分类")
        ):
            continue
        result.append(row)
    return result


def _mapping_for_topic(mapping_rows: list[dict[str, Any]], topic_id: str) -> list[dict[str, Any]]:
    return [row for row in mapping_rows if _text(row.get("主题ID")) == topic_id]


def _page_heading(title: str, description: str) -> None:
    st.markdown(
        f"""
        <div class="page-heading">
          <div>
            <h2>{title}</h2>
            <p>{description}</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _run_generation(
    source_file: Any,
    standards_file: Any,
    use_mimo: bool,
    clustering_mode: str,
    semantic_threshold: float,
) -> tuple[bytes, dict[str, Any], Path]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "outputs" / "topic-workbench" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    source_path = run_dir / source_file.name
    standards_path = run_dir / standards_file.name
    source_path.write_bytes(source_file.getvalue())
    standards_path.write_bytes(standards_file.getvalue())
    summary = initial_label_from_workbook(
        source_path=source_path,
        standards_path=standards_path,
        output_dir=run_dir,
        product_type="手机",
        use_mimo=use_mimo,
        clustering_mode=clustering_mode,
        semantic_threshold=semantic_threshold,
    )
    review_path = Path(summary["topic_review_file"])
    return review_path.read_bytes(), summary, run_dir


def _render_generation() -> None:
    _page_heading(
        "生成主题候选",
        "导入方向二数据和标准主表，生成可进入人工复核的主题级知识草稿。",
    )
    st.markdown("<div class='section-label'>输入材料</div>", unsafe_allow_html=True)
    upload_left, upload_right = st.columns(2, gap="large")
    with upload_left:
        source_file = st.file_uploader(
            "方向二数据 Excel",
            type=["xlsx"],
            key="source_file",
            help="上传已完成脱敏的方向二会话数据。",
        )
    with upload_right:
        standards_file = st.file_uploader(
            "cz 手机标准主表",
            type=["xlsx", "json"],
            key="standards_file",
            help="上传当前有效的手机质检标准目录。",
        )

    st.markdown("<div class='section-label'>执行配置</div>", unsafe_allow_html=True)
    mode_column, threshold_column, mimo_column = st.columns([0.9, 1.1, 1.35], gap="large")
    with mode_column:
        clustering_mode = st.selectbox(
            "聚类方式",
            ["semantic", "rule"],
            format_func=lambda value: "语义聚类" if value == "semantic" else "规则聚类",
            help="语义聚类调用 Embedding 服务；服务不可用时自动回退规则聚类。",
        )
    with threshold_column:
        semantic_threshold = st.slider(
            "语义相似度阈值",
            min_value=0.60,
            max_value=0.98,
            value=0.84,
            step=0.01,
            disabled=clustering_mode != "semantic",
            help="阈值越高，主题拆分越细；阈值越低，越容易合并为同一主题。",
        )
    with mimo_column:
        use_mimo = st.checkbox(
            "调用 MiMo：主题转写 + 模型初标审核",
            value=False,
            help="仅在本机 .env 已配置 MiMo 时启用。聚类使用独立的 Embedding 服务。",
        )
    config_left, config_right = st.columns([1.4, 0.6], gap="large")
    with config_left:
        if source_file and standards_file:
            status_text = "两个输入文件已就绪，可以开始生成主题候选。"
            status_class = "success"
        else:
            status_text = "请先上传方向二数据和标准主表。"
            status_class = "warning"
        st.markdown(
            f"<div class='status-strip'><span class='status-dot {status_class}'></span>{status_text}</div>",
            unsafe_allow_html=True,
        )
    with config_right:
        generate_clicked = st.button(
            "生成主题候选",
            type="primary",
            disabled=not (source_file and standards_file),
            use_container_width=True,
        )

    if generate_clicked:
        with st.spinner("正在清洗数据、检索标准并聚合主题..."):
            try:
                workbook_bytes, summary, run_dir = _run_generation(
                    source_file,
                    standards_file,
                    use_mimo,
                    clustering_mode,
                    semantic_threshold,
                )
            except Exception as exc:
                st.error(f"主题候选生成失败：{exc}")
                return
        st.session_state.generated_topic_workbook = workbook_bytes
        st.session_state.generated_topic_summary = summary
        st.session_state.generated_topic_run_dir = str(run_dir)
        st.success("主题候选已生成，可切换到“审核与反馈”继续审核。")

    summary = st.session_state.get("generated_topic_summary")
    workbook_bytes = st.session_state.get("generated_topic_workbook")
    if summary and workbook_bytes:
        st.markdown("<div class='section-label'>本次运行结果</div>", unsafe_allow_html=True)
        metrics = st.columns(4)
        metrics[0].metric("输入手机记录", summary.get("eligible_rows", 0))
        metrics[1].metric("主题候选", summary.get("topic_rows", 0))
        metrics[2].metric("证据缺口", summary.get("evidence_gap_rows", 0))
        metrics[3].metric("字段/品类排除", summary.get("excluded_rows", 0))
        effective_mode = summary.get("clustering_effective_mode", "")
        requested_mode = summary.get("clustering_requested_mode", "")
        clustering_model = summary.get("clustering_model", "")
        clustering_error = summary.get("clustering_error", "")
        if requested_mode == "semantic" and effective_mode == "semantic":
            st.markdown(
                (
                    "<div class='status-strip'><span class='status-dot success'></span>"
                    f"语义聚类已生效；模型：{clustering_model or '-'}；"
                    f"阈值：{summary.get('clustering_threshold', '-')}。</div>"
                ),
                unsafe_allow_html=True,
            )
        elif requested_mode == "semantic":
            st.markdown(
                (
                    "<div class='status-strip'><span class='status-dot warning'></span>"
                    f"语义聚类未生效，已回退规则聚类：{clustering_error or 'Embedding 服务不可用'}。</div>"
                ),
                unsafe_allow_html=True,
            )
        st.caption(f"本次运行目录：{st.session_state.get('generated_topic_run_dir', '')}")
        st.download_button(
            "下载主题复核工作簿",
            data=workbook_bytes,
            file_name="topic_review_queue.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )


def _render_review() -> None:
    _page_heading(
        "审核与反馈",
        "逐个检查主题草稿、证据链和模型初标结果，保存人工复核结论并导出提交材料。",
    )
    api_state = CzIntegrationAdapter().readiness()
    st.markdown(
        f"""
        <div class="status-strip">
          <span class="status-dot {'success' if api_state['status'] == 'ready' else 'warning'}"></span>
          <span>cz 对接状态：{api_state['status']}。当前工作台只做本地复核和导出，不主动发起网络请求。</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("<div class='section-label'>审核数据源</div>", unsafe_allow_html=True)
    generated_bytes = st.session_state.get("generated_topic_workbook")
    uploaded_file = st.file_uploader("上传 topic_review_queue.xlsx", type=["xlsx"], key="topic_review_upload")
    use_generated = st.checkbox(
        "使用本次生成的主题候选",
        value=bool(generated_bytes) and uploaded_file is None,
        disabled=not generated_bytes,
    )
    workbook_bytes = generated_bytes if use_generated else (uploaded_file.getvalue() if uploaded_file else None)
    if workbook_bytes is None:
        st.info("先在“生成主题候选”中运行，或上传已有的 topic_review_queue.xlsx。")
        return

    file_token = f"{len(workbook_bytes)}:{hash(workbook_bytes)}"
    if st.session_state.get("topic_review_file_token") != file_token:
        try:
            rows, mapping_rows, evidence_gap_rows, pending_cluster_rows, model_draft_rows = _load_topic_workbook(workbook_bytes)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.session_state.topic_review_file_token = file_token
        st.session_state.topic_review_rows = rows
        st.session_state.topic_mapping_rows = mapping_rows
        st.session_state.topic_evidence_gap_rows = evidence_gap_rows
        st.session_state.topic_pending_cluster_rows = pending_cluster_rows
        st.session_state.topic_model_draft_rows = model_draft_rows
        st.session_state.topic_review_changes = {}
        draft_by_topic = {
            _text(draft.get("主题ID")): draft
            for draft in model_draft_rows
        }
        st.session_state.topic_initial_drafts = {
            row["_review_row_index"]: {
                field: _text(draft_by_topic.get(_text(row.get("主题ID")), {}).get(field) or row.get(field))
                for field in KNOWLEDGE_MASTER_COLUMNS
            }
            for row in rows
        }
        st.session_state.selected_topic_row = None

    rows: list[dict[str, Any]] = st.session_state.topic_review_rows
    mapping_rows: list[dict[str, Any]] = st.session_state.topic_mapping_rows
    evidence_gap_rows: list[dict[str, Any]] = st.session_state.topic_evidence_gap_rows
    pending_cluster_rows: list[dict[str, Any]] = st.session_state.topic_pending_cluster_rows
    model_draft_rows: list[dict[str, Any]] = st.session_state.get("topic_model_draft_rows", [])
    changes: dict[int, dict[str, str]] = st.session_state.topic_review_changes
    initial_drafts: dict[int, dict[str, str]] = st.session_state.get("topic_initial_drafts", {})

    st.markdown("<div class='section-label'>筛选与审核概览</div>", unsafe_allow_html=True)
    filter_keyword, filter_decision, filter_focus = st.columns([1.65, 1, 0.72])
    keyword = filter_keyword.text_input("搜索主题、标准或记录 ID", placeholder="例如：屏幕、机型、TOP-")
    decision = filter_decision.selectbox(
        "审核状态",
        ["", "未审核", *REVIEW_DECISIONS],
        format_func=lambda value: value or "全部",
    )
    focus_only = filter_focus.checkbox("仅重点复核")
    filtered_rows = _filtered_rows(rows, keyword, decision, focus_only)

    pending_count = sum(not _text(row.get("审核结论")) for row in rows)
    priority_count = sum(_text(row.get("模型初标重点复核")) == "是" for row in rows)
    model_pass_count = sum(_text(row.get("模型初标结论")) == "通过" for row in rows)
    metrics = st.columns(5)
    metrics[0].metric("主题候选", len(rows))
    metrics[1].metric("待审核", pending_count)
    metrics[2].metric("模型初标通过", model_pass_count)
    metrics[3].metric("模型重点复核", priority_count)
    metrics[4].metric("证据缺口记录", len(evidence_gap_rows))

    with st.expander(f"证据缺口记录（{len(evidence_gap_rows)}）", expanded=False):
        st.dataframe(
            [
                {
                    "数据ID": _text(row.get("数据ID")),
                    "工单ID": _text(row.get("工单ID")),
                    "核心问题": _text(row.get("核心问题")),
                    "证据缺口原因": _text(row.get("证据缺口原因")),
                }
                for row in evidence_gap_rows
            ],
            use_container_width=True,
            hide_index=True,
            height=220,
        )
    with st.expander(f"待聚合记录（{len(pending_cluster_rows)}）", expanded=False):
        st.dataframe(
            [
                {
                    "数据ID": _text(row.get("数据ID")),
                    "工单ID": _text(row.get("工单ID")),
                    "核心问题": _text(row.get("核心问题")),
                    "主题特征": " / ".join(
                        _text(row.get(field))
                        for field in ("问题意图", "对象/部位", "异常现象", "解题方式")
                        if _text(row.get(field))
                    ),
                    "待聚合原因": _text(row.get("待聚合原因")),
                }
                for row in pending_cluster_rows
            ],
            use_container_width=True,
            hide_index=True,
            height=220,
        )

    st.markdown("<div class='section-label'>主题审核工作区</div>", unsafe_allow_html=True)
    left, right = st.columns([0.86, 1.34], gap="large")
    with left:
        st.markdown(f"<div class='section-label'>主题队列 · {len(filtered_rows)} / {len(rows)}</div>", unsafe_allow_html=True)
        options = [row["_review_row_index"] for row in filtered_rows]
        if not options:
            st.warning("没有符合筛选条件的主题。")
            return
        selected = st.selectbox(
            "当前主题",
            options,
            index=options.index(st.session_state.selected_topic_row)
            if st.session_state.selected_topic_row in options
            else 0,
            format_func=lambda row_index: next(
                f"{_text(row.get('主题ID'))} · {_text(row.get('主标题'))}"
                for row in filtered_rows
                if row["_review_row_index"] == row_index
            ),
            label_visibility="collapsed",
        )
        selected_row = _selected_row(rows, selected)
        if selected_row is not None and selected != st.session_state.selected_topic_row:
            _reset_editor(selected_row)
        st.dataframe(
            [
                {
                    "主题": _text(row.get("主标题")),
                    "样本": _text(row.get("主题样本数")),
                    "证据": _text(row.get("主题证据等级")),
                    "模型初标": _text(row.get("模型初标结论")) or "待初标",
                    "人工复标": _text(row.get("审核结论")) or "待审核",
                }
                for row in filtered_rows
            ],
            use_container_width=True,
            hide_index=True,
            height=580,
        )

    with right:
        row = _selected_row(rows, st.session_state.selected_topic_row)
        if row is None:
            st.info("请选择一个主题。")
            return
        st.markdown(f"### {_text(row.get('主标题'))}")
        context_left, context_right, context_status = st.columns([1.1, 1.1, 0.85])
        context_left.caption(f"主题 ID · {_text(row.get('主题ID'))}")
        context_right.caption(f"关联标准 · {_text(row.get('关联标准项')) or '待人工补充'}")
        context_status.caption(
            f"模型初标：{_text(row.get('模型初标结论')) or '待初标'} · "
            f"人工复标：{_text(row.get('审核结论')) or '待审核'}"
        )

        workbench_tab, evidence_tab = st.tabs(["审核工作区", "证据与标准"])
        with workbench_tab:
            preview_column, edit_column = st.columns([0.9, 1.1], gap="large")
            with preview_column:
                model_draft = next(
                    (
                        item
                        for item in model_draft_rows
                        if _text(item.get("主题ID")) == _text(row.get("主题ID"))
                    ),
                    {},
                )
                initial_draft = initial_drafts.get(
                    row["_review_row_index"],
                    {
                        field: _text(model_draft.get(field) or row.get(field))
                        for field in KNOWLEDGE_MASTER_COLUMNS
                    },
                )
                provider = _text(model_draft.get("转写提供方") or row.get("主题模型提供方"))
                transcription_status = _text(row.get("模型阶段状态"))
                if provider == "mimo" and transcription_status == "topic_model_labeled":
                    st.success("已完成 MiMo 主题转写")
                elif provider == "mimo":
                    st.error("MiMo 主题转写失败，已使用规则草稿")
                else:
                    st.warning("当前为规则转写草稿，未完成 MiMo 主题转写")
                trace_left, trace_right = st.columns(2)
                trace_left.caption(f"转写模型 · {_text(model_draft.get('转写模型名称') or row.get('主题模型名称')) or '-'}")
                trace_right.caption(f"转写置信度 · {_text(model_draft.get('转写置信度') or row.get('主题置信度')) or '-'}")
                st.caption(
                    f"Prompt · {_text(model_draft.get('转写Prompt版本') or row.get('主题Prompt版本')) or '-'}  |  "
                    f"运行 ID · {_text(model_draft.get('转写模型运行ID') or row.get('主题模型运行ID')) or '-'}"
                )
                _read_only_text("主题转写主标题", initial_draft.get("主标题"), height=66)
                _read_only_text("主题转写副标题", initial_draft.get("副标题"), height=72)
                _read_only_text("主题转写知识内容", initial_draft.get("知识内容"), height=220)
                _read_only_text("主题转写标准引用", initial_draft.get("关联标准项"), height=76)

                st.markdown("<div class='section-label'>模型初标审核</div>", unsafe_allow_html=True)
                initial_provider = _text(row.get("模型初标提供方"))
                initial_status = _text(row.get("模型初标状态"))
                if initial_provider == "mimo" and initial_status == "topic_initial_reviewed_model":
                    st.success("已完成 MiMo 模型初标审核")
                elif initial_provider == "mimo":
                    st.error("MiMo 模型初标失败，已回退为规则初标")
                else:
                    st.warning("当前为规则模型初标，待用 MiMo 验证")
                review_left, review_right = st.columns(2)
                review_left.metric("初标结论", _text(row.get("模型初标结论")) or "-")
                review_right.metric("初标置信度", _text(row.get("模型初标置信度")) or "-")
                st.caption(
                    f"{_text(row.get('模型初标模型名称')) or '-'} · "
                    f"{_text(row.get('模型初标Prompt版本')) or '-'} · "
                    f"{_text(row.get('模型初标运行ID')) or '-'}"
                )
                st.caption(
                    f"标准一致性：{_text(row.get('模型初标标准一致性')) or '-'}；"
                    f"证据充分性：{_text(row.get('模型初标证据充分性')) or '-'}；"
                    f"重点复核：{_text(row.get('模型初标重点复核')) or '-'}"
                )
                _read_only_text("模型初标原因", row.get("模型初标原因"), height=96)

            with edit_column:
                editor_tab, feedback_tab = st.tabs(["完整编辑", "审核反馈"])
                with editor_tab:
                    st.caption("右侧内容是最终 13 列候选草稿；保存后会直接写入复核工作簿。")
                    with st.form("topic_content_edit_form"):
                        st.markdown("<div class='section-label'>基础信息</div>", unsafe_allow_html=True)
                        basic_left, basic_right = st.columns(2)
                        basic_left.text_input("主标题", key="topic_主标题")
                        basic_right.text_input("副标题", key="topic_副标题")
                        basic_left.text_input("知识分类", key="topic_知识分类")
                        basic_right.text_input("适用范围", key="topic_适用范围")
                        basic_left.text_input("知识来源", key="topic_知识来源")
                        basic_right.selectbox(
                            "生效状态",
                            ["待审核", "生效中", "已失效"],
                            key="topic_生效状态",
                        )

                        st.markdown("<div class='section-label'>知识正文</div>", unsafe_allow_html=True)
                        st.text_area("知识内容", height=260, key="topic_知识内容")

                        st.markdown("<div class='section-label'>来源与检索</div>", unsafe_allow_html=True)
                        source_left, source_right = st.columns(2)
                        source_left.text_area("关联标准项", height=88, key="topic_关联标准项")
                        source_right.text_area("检索关键词", height=88, key="topic_检索关键词")
                        source_left.text_input("来源版本", key="topic_来源版本")
                        source_right.text_input("变更类型", key="topic_变更类型")
                        source_left.text_input("失效原因", key="topic_失效原因")
                        source_right.text_area("校验备注", height=64, key="topic_校验备注")
                        edit_submitted = st.form_submit_button("保存候选草稿", type="primary")
                    if edit_submitted:
                        updates = {
                            field: _text(st.session_state.get(f"topic_{field}"))
                            for field in KNOWLEDGE_MASTER_COLUMNS
                        }
                        row.update(updates)
                        changes[row["_review_row_index"]] = {
                            **changes.get(row["_review_row_index"], {}),
                            **updates,
                        }
                        st.session_state.topic_review_changes = changes
                        st.success(f"已保存主题 {_text(row.get('主题ID'))} 的 13 列草稿。")

                with feedback_tab:
                    st.caption("审核反馈只记录结论与错误信息；候选内容请在“完整编辑”中维护。")
                    with st.form("topic_review_form"):
                        review_top_left, review_top_right, review_top_train = st.columns([1, 1, 0.85])
                        decision_value = st.session_state.get("topic_审核结论", "")
                        review_top_left.selectbox(
                            "审核结论",
                            ["", *REVIEW_DECISIONS],
                            index=(["", *REVIEW_DECISIONS].index(decision_value) if decision_value in ["", *REVIEW_DECISIONS] else 0),
                            format_func=lambda value: value or "未审核",
                            key="topic_审核结论",
                        )
                        review_top_right.selectbox("错误类型", ["", *ERROR_TYPES], key="topic_错误类型")
                        review_top_train.selectbox("进入训练集", ["", "是", "否"], key="topic_是否进入训练集")
                        st.text_area("审核备注", height=68, key="topic_审核备注")
                        st.text_area("错误原因", height=68, key="topic_错误原因")
                        reviewer_left, reviewer_right = st.columns(2)
                        reviewer_left.text_input("审核人", key="topic_审核人")
                        reviewer_right.text_input(
                            "审核时间",
                            placeholder="YYYY-MM-DD HH:mm:ss",
                            key="topic_审核时间",
                        )
                        submitted = st.form_submit_button("保存审核反馈", type="primary")

                    if submitted:
                        updates = {field: _text(st.session_state.get(f"topic_{field}")) for field in TOPIC_REVIEW_COLUMNS}
                        if not updates["审核时间"] and updates["审核结论"]:
                            updates["审核时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        row.update(updates)
                        changes[row["_review_row_index"]] = {
                            **changes.get(row["_review_row_index"], {}),
                            **updates,
                        }
                        st.session_state.topic_review_changes = changes
                        st.success(f"已暂存主题 {_text(row.get('主题ID'))} 的审核反馈。")

        with evidence_tab:
            evidence_meta_left, evidence_meta_right, evidence_meta_score = st.columns(3)
            evidence_meta_left.metric("聚合样本", _text(row.get("主题样本数")) or "0")
            evidence_meta_right.metric("模型置信度", _text(row.get("主题置信度")) or "-")
            evidence_meta_score.metric("证据等级", _text(row.get("主题证据等级")) or "-")
            _read_only_text("主题聚类键", row.get("主题聚类键"), height=88)
            _read_only_text("主题证据摘要", row.get("主题证据摘要"), height=160)
            _read_only_text("检索标准 Top5", row.get("主题检索标准Top5"), height=190)
            topic_mapping = _mapping_for_topic(mapping_rows, _text(row.get("主题ID")))
            st.markdown("<div class='section-label'>原始聊天内容</div>", unsafe_allow_html=True)
            chat_rows = [
                item
                for item in topic_mapping
                if _text(item.get("聊天内容")).strip()
            ]
            if chat_rows:
                for chat_index, item in enumerate(chat_rows, start=1):
                    source_id = _text(item.get("来源记录ID")) or _text(item.get("工单ID")) or f"记录 {chat_index}"
                    question = _text(item.get("核心问题"))
                    with st.expander(f"{chat_index:02d} · {source_id}", expanded=chat_index == 1):
                        if question:
                            st.caption(f"核心问题：{question}")
                        _read_only_text(
                            "会话内容",
                            item.get("聊天内容"),
                            height=180,
                            key=f"topic_chat_{_text(row.get('主题ID'))}_{chat_index}",
                        )
            else:
                st.info("当前工作簿没有携带原始聊天内容；可以查看上方证据摘要或重新生成主题工作簿。")
            st.markdown("<div class='section-label'>来源记录</div>", unsafe_allow_html=True)
            st.dataframe(
                [
                    {
                        "记录 ID": _text(item.get("来源记录ID")),
                        "工单 ID": _text(item.get("工单ID")),
                        "核心问题": _text(item.get("核心问题")),
                        "证据": _text(item.get("证据等级")),
                    }
                    for item in topic_mapping
                ],
                use_container_width=True,
                hide_index=True,
                height=210,
            )

    if changes:
        reviewed_workbook = _update_topic_workbook(workbook_bytes, changes)
        final_rows, feedback_rows, training_rows = finalize_topic_review_rows(rows)
        st.divider()
        downloads = st.columns(3)
        downloads[0].download_button(
            "下载审核工作簿",
            data=reviewed_workbook,
            file_name="topic_review_queue_reviewed.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        downloads[1].download_button(
            "下载13列候选",
            data=_rows_to_xlsx_bytes("候选知识", KNOWLEDGE_MASTER_COLUMNS, final_rows),
            file_name="candidate_knowledge_for_submission.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        downloads[2].download_button(
            "下载训练反馈",
            data=_jsonl_bytes(training_rows),
            file_name="topic_training_samples.jsonl",
            mime="application/jsonl",
            use_container_width=True,
        )
        st.caption(f"已审核反馈 {len(feedback_rows)} 条；可进入训练集 {len(training_rows)} 条；可提交 cz 网站的主题候选 {len(final_rows)} 条。")


st.markdown(
    """
    <div class="workspace-header">
      <div class="workspace-header-copy">
        <div class="workspace-kicker">Answer Hub · Phone Knowledge</div>
        <div class="workspace-title">手机主题知识工作台</div>
        <div class="workspace-subtitle">主题沉淀、证据核验、人工审核与训练反馈在同一工作区完成；正式发布仍由 cz 知识库网站处理。</div>
      </div>
      <div class="workspace-header-meta">
        <span class="meta-pill">主题级沉淀</span>
        <span class="meta-pill">人工复核</span>
        <span class="meta-pill">可追溯导出</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)
page = st.radio(
    "工作区",
    ["生成主题候选", "审核与反馈"],
    horizontal=True,
    label_visibility="collapsed",
)
if page == "生成主题候选":
    _render_generation()
else:
    _render_review()
