from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
import json
import os
import tempfile

import streamlit as st

from .analysis import build_weekly_report, import_source_file, run_weekly_analysis
from .collectors import collect_with_endpoint_profile, discover_network_requests
from .schema import (
    ANALYSIS_COLUMNS,
    REVIEW_EDIT_COLUMNS,
    SOLUTION_METHOD_OPTIONS,
    TRANSFER_REASON_OPTIONS,
)
from .store import TransferAnalysisStore


def _default_week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


@st.cache_resource(show_spinner=False)
def _store(path: str) -> TransferAnalysisStore:
    return TransferAnalysisStore(path)


def _save_uploaded(uploaded_file, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = Path(uploaded_file.name).name
    path = directory / safe_name
    path.write_bytes(uploaded_file.getvalue())
    return path


def _metric_row(metrics: list[tuple[str, Any]]) -> None:
    with st.container(horizontal=True):
        for label, value in metrics:
            st.metric(label, value, border=True)


def _render_import(store: TransferAnalysisStore) -> None:
    st.subheader("数据导入与采集")
    st.caption(
        "真实接口尚未配置时，可以先导入曼哈顿和百晓生的 Excel、CSV 或 JSON。"
        "接口模板不得保存 Cookie、Authorization 或密码。"
    )
    import_file = st.file_uploader(
        "上传系统数据",
        type=["xlsx", "csv", "json"],
        key="transfer_import_file",
    )
    import_system = st.segmented_control(
        "数据来源",
        ["manhattan", "baixiaosheng"],
        default="manhattan",
        key="transfer_import_system",
    )
    if st.button(
        "导入数据",
        type="primary",
        disabled=import_file is None,
        key="transfer_import_submit",
    ):
        try:
            with tempfile.TemporaryDirectory(prefix="transfer-import-") as temp_dir:
                path = _save_uploaded(import_file, Path(temp_dir))
                summary = import_source_file(path, import_system, store)
            st.success(
                f"导入完成：列表 {summary['transfer_records']} 条，"
                f"会话详情 {summary['conversation_records']} 条。"
            )
        except Exception as exc:
            st.error(f"导入失败：{exc}")

    with st.expander("后台接口采集", expanded=False):
        profile_file = st.file_uploader(
            "上传接口模板 JSON",
            type=["json"],
            key="transfer_endpoint_profile",
        )
        date_range = st.date_input(
            "采集日期",
            value=(_default_week_start(), date.today()),
            key="transfer_collect_dates",
        )
        work_order_text = st.text_area(
            "百晓生工单ID（每行一个，仅百晓生模板需要）",
            key="transfer_collect_work_orders",
        )
        transfer_id_text = st.text_area(
            "曼哈顿转人工ID（每行一个，仅抓详情时需要）",
            key="transfer_collect_ids",
        )
        show_browser = st.checkbox(
            "显示浏览器",
            value=False,
            key="transfer_collect_show_browser",
        )
        if st.button(
            "运行接口采集",
            disabled=profile_file is None,
            key="transfer_collect_submit",
        ):
            try:
                if not isinstance(date_range, tuple) or len(date_range) != 2:
                    raise ValueError("请选择开始和结束日期")
                start, end = date_range
                with tempfile.TemporaryDirectory(prefix="transfer-profile-") as temp_dir:
                    profile_path = _save_uploaded(profile_file, Path(temp_dir))
                    summary = collect_with_endpoint_profile(
                        profile_path,
                        store,
                        start_date=start.isoformat(),
                        end_date=(end + timedelta(days=1)).isoformat(),
                        work_order_ids=[
                            line.strip()
                            for line in work_order_text.splitlines()
                            if line.strip()
                        ],
                        transfer_ids=[
                            line.strip()
                            for line in transfer_id_text.splitlines()
                            if line.strip()
                        ],
                        headless=not show_browser,
                    )
                st.success(
                    f"采集完成：列表 {summary['list_records']} 条，"
                    f"详情 {summary['detail_records']} 条。"
                )
            except Exception as exc:
                st.error(f"接口采集失败：{exc}")

    with st.expander("首次登录与接口勘探", expanded=False):
        st.warning(
            "勘探会打开可见Chrome窗口。完成登录、日期查询、翻页、打开会话和查看召回后，"
            "关闭浏览器即可生成脱敏后的请求结构记录。"
        )
        discovery_system = st.segmented_control(
            "勘探系统",
            ["manhattan", "baixiaosheng"],
            default="manhattan",
            key="transfer_discovery_system",
        )
        login_url = st.text_input(
            "登录地址",
            key="transfer_discovery_login_url",
        )
        timeout_minutes = st.number_input(
            "最长等待分钟数",
            min_value=1,
            max_value=60,
            value=15,
            key="transfer_discovery_timeout",
        )
        if st.button(
            "启动接口勘探",
            disabled=not login_url.strip(),
            key="transfer_discovery_submit",
        ):
            try:
                output = (
                    Path("outputs")
                    / "transfer-analysis"
                    / "discovery"
                    / f"{discovery_system}-network.ndjson"
                )
                profile_dir = Path("data") / "browser_profiles" / discovery_system
                summary = discover_network_requests(
                    discovery_system,
                    login_url.strip(),
                    output,
                    profile_dir,
                    timeout_seconds=int(timeout_minutes * 60),
                )
                st.success(
                    f"勘探完成，记录请求 {summary['request_records']} 条、"
                    f"响应 {summary['response_records']} 条。"
                )
                if output.is_file():
                    st.download_button(
                        "下载接口勘探记录",
                        data=output.read_bytes(),
                        file_name=output.name,
                        mime="application/x-ndjson",
                    )
            except Exception as exc:
                st.error(f"接口勘探失败：{exc}")

    runs = store.list_collection_runs(limit=20)
    if runs:
        st.markdown("**最近采集任务**")
        st.dataframe(
            [
                {
                    "系统": row["system"],
                    "开始日期": row["start_date"],
                    "结束日期": row["end_date"],
                    "状态": row["status"],
                    "列表数": row["metrics"].get("list_records", 0),
                    "详情数": row["metrics"].get("detail_records", 0),
                    "错误": row["error"],
                    "更新时间": row["updated_at"],
                }
                for row in runs
            ],
            hide_index=True,
        )


def _render_analysis(store: TransferAnalysisStore) -> None:
    st.subheader("周度抽样与模型标注")
    with st.form("transfer_analysis_form"):
        week_start = st.date_input(
            "周开始日期",
            value=_default_week_start(),
            key="transfer_analysis_week",
        )
        standards = st.file_uploader(
            "当前有效知识主表",
            type=["xlsx", "json"],
            key="transfer_analysis_standards",
        )
        sample_size = int(
            st.number_input(
                "抽样数量",
                min_value=1,
                max_value=1000,
                value=350,
                step=10,
            )
        )
        use_mimo = st.checkbox(
            "启用MiMo模型标注",
            value=True,
            help="未配置MiMo或调用失败时自动使用规则初标，并强制人工复核。",
        )
        submitted = st.form_submit_button(
            "开始周度分析",
            type="primary",
            disabled=standards is None,
        )
    if submitted:
        try:
            output_dir = Path("outputs") / "transfer-analysis" / week_start.isoformat()
            with tempfile.TemporaryDirectory(prefix="transfer-standards-") as temp_dir:
                standards_path = _save_uploaded(standards, Path(temp_dir))
                progress = st.progress(0.0, text="准备抽样")
                status = st.status("正在运行转人工分析", expanded=True)

                def update_progress(event: dict[str, Any]) -> None:
                    current = int(event.get("current") or 0)
                    total = max(1, int(event.get("total") or 1))
                    stage = event.get("stage") or ""
                    progress.progress(
                        min(current / total, 1.0),
                        text=f"{stage}：{current}/{total}",
                    )

                summary = run_weekly_analysis(
                    store,
                    week_start,
                    standards_path,
                    output_dir,
                    sample_size=sample_size,
                    use_mimo=use_mimo,
                    progress_callback=update_progress,
                )
            status.update(label="转人工分析完成", state="complete", expanded=False)
            st.session_state.transfer_analysis_summary = summary
            st.success(
                f"完成 {summary['annotation_records']} 条标注，"
                f"待复核 {summary['review_records']} 条。"
            )
        except Exception as exc:
            st.error(f"周度分析失败：{exc}")

    summary = st.session_state.get("transfer_analysis_summary")
    if summary:
        _metric_row(
            [
                ("一周转人工列表", summary.get("source_records", 0)),
                ("抽样", summary.get("sample_records", 0)),
                ("完成标注", summary.get("annotation_records", 0)),
                ("待人工复核", summary.get("review_records", 0)),
                ("规则回退", summary.get("fallback_records", 0)),
            ]
        )
        report = Path(summary.get("report_file") or "")
        if report.is_file():
            st.download_button(
                "下载本周分析报告",
                data=report.read_bytes(),
                file_name=report.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )


def _option_index(options: list[str], value: str) -> int:
    return options.index(value) if value in options else 0


def _render_review(store: TransferAnalysisStore) -> None:
    st.subheader("人工复核")
    week_start = st.date_input(
        "复核周",
        value=_default_week_start(),
        key="transfer_review_week",
    ).isoformat()
    rows = store.list_annotation_rows(week_start, only_needs_review=True)
    if not rows:
        st.info("当前周没有待复核数据。请先运行周度分析。")
        return
    counts = store.annotation_count(week_start)
    _metric_row(
        [
            ("待复核", counts["needs_review"]),
            ("已审核", counts["reviewed"]),
            ("模型失败", counts["failed"]),
        ]
    )
    pending = [row for row in rows if row.get("审核状态") != "已审核"]
    show_rows = pending or rows
    selected_id = st.selectbox(
        "选择会话",
        options=[row["_transfer_id"] for row in show_rows],
        format_func=lambda transfer_id: next(
            (
                f"{row.get('工单ID')}｜{row.get('问题') or '无首轮问题'}｜{row.get('审核状态')}"
                for row in show_rows
                if row["_transfer_id"] == transfer_id
            ),
            transfer_id,
        ),
        key="transfer_review_selected",
    )
    row = next(item for item in show_rows if item["_transfer_id"] == selected_id)
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**百晓生会话**")
            st.text_area(
                "百晓生完整会话",
                value=str(row.get("百晓生完整会话") or ""),
                height=320,
                disabled=True,
                label_visibility="collapsed",
                key=f"bxs_{selected_id}",
            )
            st.markdown("**召回知识**")
            st.code(str(row.get("召回知识") or "无召回记录"), language=None)
            st.caption(
                f"Top相似度：{row.get('Top相似度') or '-'} · "
                f"生产阈值：{row.get('生产阈值') or '-'}"
            )
    with right:
        with st.container(border=True):
            st.markdown("**转人工后会话**")
            st.text_area(
                "转人工后完整会话",
                value=str(row.get("转人工后完整会话") or ""),
                height=320,
                disabled=True,
                label_visibility="collapsed",
                key=f"manual_{selected_id}",
            )
            st.markdown("**工具记录**")
            st.write(
                {
                    "所需工具": row.get("所需工具") or "-",
                    "是否调用": row.get("工具是否调用") or "-",
                    "调用结果": row.get("工具调用结果") or "-",
                    "归因": row.get("工具归因标签") or "-",
                }
            )
    with st.form(f"transfer_review_form_{selected_id}"):
        st.markdown("**校正结果**")
        form_left, form_right = st.columns(2)
        intent_clear = form_left.selectbox(
            "意图是否明确",
            ["是", "否"],
            index=_option_index(["是", "否"], str(row.get("意图是否明确") or "")),
        )
        valid_question = form_right.selectbox(
            "是否有效问",
            ["是", "否"],
            index=_option_index(["是", "否"], str(row.get("是否有效问") or "")),
        )
        true_intent = st.text_input(
            "真实意图",
            value=str(row.get("真实意图") or ""),
        )
        reason = st.selectbox(
            "转人工原因(校正)",
            list(TRANSFER_REASON_OPTIONS),
            index=_option_index(
                list(TRANSFER_REASON_OPTIONS),
                str(row.get("转人工原因(校正)") or ""),
            ),
        )
        remark = st.text_area(
            "备注（诊断标签写在这里）",
            value=str(row.get("备注") or ""),
            height=140,
            help="建议格式：【诊断】标签；【事实】证据；【建议】动作。",
        )
        solvable = st.selectbox(
            "大模型是否可以解决",
            ["是", "否", "不确定"],
            index=_option_index(
                ["是", "否", "不确定"],
                str(row.get("大模型是否可以解决") or ""),
            ),
        )
        solution_method = st.selectbox(
            "解决方式",
            list(SOLUTION_METHOD_OPTIONS),
            index=_option_index(
                list(SOLUTION_METHOD_OPTIONS),
                str(row.get("解决方式") or ""),
            ),
        )
        reviewer = st.text_input(
            "审核人",
            value=str(row.get("审核人") or ""),
        )
        saved = st.form_submit_button("保存复核", type="primary")
    if saved:
        review = {
            field: row.get(field, "")
            for field in REVIEW_EDIT_COLUMNS
        }
        review.update(
            {
                "意图是否明确": intent_clear,
                "真实意图": true_intent.strip(),
                "转人工原因(校正)": reason,
                "备注": remark.strip(),
                "大模型是否可以解决": solvable,
                "是否有效问": valid_question,
                "解决方式": solution_method,
            }
        )
        store.save_review(week_start, selected_id, reviewer, review)
        st.success("复核结果已保存。")
        st.rerun()


def _render_report(store: TransferAnalysisStore) -> None:
    st.subheader("周度分析报告")
    week_start = st.date_input(
        "报告周",
        value=_default_week_start(),
        key="transfer_report_week",
    ).isoformat()
    counts = store.annotation_count(week_start)
    _metric_row(
        [
            ("标注总数", counts["total"]),
            ("需要复核", counts["needs_review"]),
            ("已审核", counts["reviewed"]),
            ("模型失败", counts["failed"]),
        ]
    )
    rows = store.list_annotation_rows(week_start)
    if rows:
        st.dataframe(
            [
                {
                    "工单ID": row.get("工单ID"),
                    "原原因": row.get("转人工原因"),
                    "校正原因": row.get("转人工原因(校正)"),
                    "可解决": row.get("大模型是否可以解决"),
                    "责任方": row.get("建议优化责任方"),
                    "是否复核": row.get("是否需要人工复核"),
                    "审核状态": row.get("审核状态"),
                    "备注": row.get("备注"),
                }
                for row in rows
            ],
            hide_index=True,
        )
    if st.button(
        "重新生成周报",
        type="primary",
        disabled=not rows,
        key="transfer_report_generate",
    ):
        output = (
            Path("outputs")
            / "transfer-analysis"
            / week_start
            / f"转人工分析周报_{week_start}.xlsx"
        )
        summary = build_weekly_report(store, week_start, output)
        st.session_state.transfer_report_path = summary["output_file"]
        st.success(f"报告已生成：{summary['total_records']} 条。")
    report_path = Path(st.session_state.get("transfer_report_path") or "")
    if report_path.is_file():
        st.download_button(
            "下载周报Excel",
            data=report_path.read_bytes(),
            file_name=report_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


def render_transfer_analysis() -> None:
    st.markdown("## 转人工会话分析")
    st.caption(
        "采集曼哈顿与百晓生会话，结合召回轨迹、知识主表和百晓生能力边界，"
        "生成可追溯标注、低置信度复核队列和周度badcase报告。"
    )
    database_path = os.getenv(
        "TRANSFER_ANALYSIS_DB_PATH",
        "data/transfer_analysis.db",
    )
    store = _store(database_path)
    view = st.segmented_control(
        "转人工分析步骤",
        ["数据采集", "周度分析", "人工复核", "分析报告"],
        default="数据采集",
        key="transfer_analysis_page",
        label_visibility="collapsed",
        persist_state="session",
    )
    if view == "数据采集":
        _render_import(store)
    elif view == "周度分析":
        _render_analysis(store)
    elif view == "人工复核":
        _render_review(store)
    else:
        _render_report(store)
