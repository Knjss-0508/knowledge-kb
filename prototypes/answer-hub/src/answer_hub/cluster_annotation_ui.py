from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from .cluster_annotation import (
    CLUSTER_ACTIONS,
    CLUSTER_DECISIONS,
    TITLE_DECISIONS,
    ClusterAnnotation,
    ClusterAnnotationStore,
    annotation_csv_bytes,
    annotation_export_rows,
    annotation_summary,
    annotation_validation_errors,
    load_cluster_payload,
    split_media_urls,
)
from .mimo import _primary_conversation_evidence


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLUSTER_PAYLOAD = (
    ROOT
    / "outputs"
    / "cluster-full-current-379"
    / "cluster_titles.json"
)
DEFAULT_ANNOTATION_DB = (
    ROOT
    / "outputs"
    / "cluster-full-current-379"
    / "cluster_annotations.db"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


@st.cache_data(show_spinner=False, max_entries=5)
def _load_payload(path: str, modified_time_ns: int) -> dict[str, Any]:
    del modified_time_ns
    return load_cluster_payload(path)


@st.cache_resource(show_spinner=False, max_entries=5)
def _annotation_store(path: str) -> ClusterAnnotationStore:
    return ClusterAnnotationStore(path)


def _format_accuracy(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "待标注"


def _annotation_status(annotation: ClusterAnnotation) -> str:
    if (
        annotation.cluster_decision in {"正确", "错误"}
        and annotation.title_decision in {"正确", "错误"}
    ):
        return "已完成"
    if (
        annotation.cluster_decision
        or annotation.title_decision
        or annotation.notes
    ):
        return "标注中"
    return "待标注"


def _filter_clusters(
    clusters: list[dict[str, Any]],
    annotations: dict[str, ClusterAnnotation],
    *,
    query: str,
    status: str,
    priority: str,
    products: list[str],
) -> list[dict[str, Any]]:
    normalized_query = query.strip().lower()
    filtered: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_id = _text(cluster.get("cluster_id"))
        annotation = annotations.get(
            cluster_id,
            ClusterAnnotation(cluster_id=cluster_id),
        )
        if status != "全部" and _annotation_status(annotation) != status:
            continue
        if priority == "多成员簇" and int(cluster.get("member_count") or 0) < 2:
            continue
        if priority == "模型建议复核" and not bool(cluster.get("requires_review")):
            continue
        if products and _text(cluster.get("product_category")) not in products:
            continue
        searchable = "\n".join(
            [
                cluster_id,
                _text(cluster.get("theme_title")),
                _text(cluster.get("shared_knowledge_definition")),
                *[
                    _text(member.get("normalized_issue"))
                    for member in cluster.get("members") or []
                ],
                *[
                    _text(member.get("source_core_problem"))
                    for member in cluster.get("members") or []
                ],
            ]
        ).lower()
        if normalized_query and normalized_query not in searchable:
            continue
        filtered.append(cluster)
    return filtered


def _render_media(member: dict[str, Any], member_key: str) -> None:
    image_urls = split_media_urls(member.get("image_links"))
    video_urls = split_media_urls(member.get("video_links"))
    media = member.get("media_analysis") or {}
    image_summary = _text(media.get("image_summary"))
    video_summary = _text(media.get("video_summary"))

    if image_summary or video_summary:
        with st.container(border=True):
            st.markdown("**模型媒体事实**")
            if image_summary:
                st.caption(f"图片：{image_summary}")
            if video_summary:
                st.caption(f"视频：{video_summary}")

    if image_urls:
        st.image(
            image_urls[:6],
            caption=[
                f"{member_key} · 图片 {index}"
                for index in range(1, min(len(image_urls), 6) + 1)
            ],
            width=220,
        )
        if len(image_urls) > 6:
            st.caption(f"另有 {len(image_urls) - 6} 张图片未展开，可查看原始链接。")

    for index, video_url in enumerate(video_urls[:3], start=1):
        with st.container(border=True):
            st.caption(f"{member_key} · 视频 {index}")
            try:
                st.video(video_url)
            except Exception as exc:
                st.warning(f"视频加载失败：{exc}")
                st.code(video_url, language=None)

    if not image_urls and not video_urls:
        st.caption("当前成员没有图片或视频链接。")


def _render_member(member: dict[str, Any], index: int) -> None:
    sample_id = _text(member.get("sample_id")) or f"成员{index}"
    atomic_id = _text(member.get("unit_id"))
    title = _text(member.get("normalized_issue")) or "问题待确认"
    with st.expander(
        f"{index:02d} · {sample_id} · {title}",
        expanded=index == 1,
    ):
        with st.container(horizontal=True):
            st.caption(f"原子ID：{atomic_id}")
            st.caption(f"产品：{_text(member.get('product_category')) or '待确认'}")
            st.caption(f"机型：{_text(member.get('device_model')) or '未填写'}")
            st.caption(
                f"模型建议复核：{'是' if member.get('requires_review') else '否'}"
            )

        raw_conversation = _text(member.get("source_conversation"))
        primary_conversation = _primary_conversation_evidence(
            raw_conversation,
            12000,
        )
        st.markdown("**实际聊天内容（主证据）**")
        st.text_area(
            "实际聊天内容",
            value=primary_conversation,
            height=260,
            disabled=True,
            label_visibility="collapsed",
            key=f"cluster_chat_{atomic_id}",
        )
        if raw_conversation and raw_conversation != primary_conversation:
            with st.expander("查看转人工元数据和原始完整记录"):
                st.caption(
                    "其中“问题类型、问题描述、转人工原因”仅用于追溯，"
                    "不能作为主题判断主依据。"
                )
                st.text_area(
                    "原始完整记录",
                    value=raw_conversation,
                    height=180,
                    disabled=True,
                    label_visibility="collapsed",
                    key=f"cluster_raw_chat_{atomic_id}",
                )

        core_problem = _text(member.get("source_core_problem"))
        if core_problem:
            st.markdown("**上游问题摘要（仅作弱参考）**")
            st.caption(
                "该字段可能来自转人工问题描述或旧流程摘要；"
                "与完整聊天冲突时，以完整聊天为准。"
            )
            st.write(core_problem)

        evidence_left, evidence_right = st.columns(2)
        with evidence_left:
            st.markdown("**标准化问题**")
            st.write(title)
            st.markdown("**证据摘要**")
            st.write(_text(member.get("evidence_summary")) or "无")
        with evidence_right:
            st.markdown("**聚类字段**")
            st.write(
                {
                    "适用范围": _text(member.get("scope_type")),
                    "一级分类": _text(member.get("category_l1")),
                    "二级分类": _text(member.get("category_l2")),
                    "对象/部位": _text(member.get("subject")),
                    "异常现象": _text(member.get("phenomenon")),
                    "判定目标": _text(member.get("judgment_target")),
                    "处理方式": _text(member.get("resolution_mode")),
                    "阈值/例外": _text(member.get("threshold_or_exception")),
                }
            )

        st.markdown("**图片和视频证据**")
        _render_media(member, sample_id)
        with st.expander("查看原始媒体链接"):
            links = [
                *split_media_urls(member.get("image_links")),
                *split_media_urls(member.get("video_links")),
            ]
            st.code("\n".join(links) or "无", language=None)


def _next_cluster_id(
    clusters: list[dict[str, Any]],
    current_cluster_id: str,
    step: int,
) -> str:
    cluster_ids = [_text(cluster.get("cluster_id")) for cluster in clusters]
    if not cluster_ids:
        return ""
    try:
        current_index = cluster_ids.index(current_cluster_id)
    except ValueError:
        current_index = 0
    target_index = max(0, min(len(cluster_ids) - 1, current_index + step))
    return cluster_ids[target_index]


def _queue_cluster_selection(cluster_id: str) -> None:
    st.session_state.cluster_annotation_pending_id = cluster_id


def _sync_cluster_selection(available_ids: list[str]) -> str:
    pending_id = st.session_state.pop("cluster_annotation_pending_id", "")
    selected_id = (
        pending_id
        or st.session_state.get("cluster_annotation_selector")
        or st.session_state.get("cluster_annotation_selected_id")
    )
    if selected_id not in available_ids:
        selected_id = available_ids[0]
    st.session_state.cluster_annotation_selected_id = selected_id
    st.session_state.cluster_annotation_selector = selected_id
    return selected_id


def render_cluster_annotation() -> None:
    st.subheader("完整聚类标注")
    st.caption(
        "逐簇查看实际聊天、图片/视频证据和弱参考摘要，标注归簇与主题标题。"
        "转人工问题描述不作为主证据。"
        "结果实时写入本地 SQLite，不依赖浏览器会话。"
    )

    payload_path = DEFAULT_CLUSTER_PAYLOAD
    if not payload_path.is_file():
        st.error(f"找不到默认聚类结果：{payload_path}")
        return

    payload = _load_payload(
        str(payload_path),
        payload_path.stat().st_mtime_ns,
    )
    clusters = payload["clusters"]
    store = _annotation_store(str(DEFAULT_ANNOTATION_DB))
    annotations = store.list_all()
    summary = annotation_summary(clusters, annotations)

    with st.container(horizontal=True):
        st.metric("主题簇", summary["cluster_count"], border=True)
        st.metric(
            "已完成",
            summary["completed_count"],
            delta=f"剩余 {summary['pending_count']}",
            border=True,
        )
        st.metric(
            "归簇准确率",
            _format_accuracy(summary["cluster_accuracy"]),
            delta=f"已复核 {summary['cluster_reviewed_count']} 簇",
            border=True,
        )
        st.metric(
            "标题准确率",
            _format_accuracy(summary["title_accuracy"]),
            delta=f"已复核 {summary['title_reviewed_count']} 簇",
            border=True,
        )
    progress_value = (
        summary["completed_count"] / summary["cluster_count"]
        if summary["cluster_count"]
        else 0.0
    )
    st.progress(
        progress_value,
        text=f"标注进度：{summary['completed_count']} / {summary['cluster_count']}",
    )

    with st.container(border=True):
        st.markdown("**筛选与定位**")
        filter_row = st.columns([1.4, 1, 1, 1.3])
        query = filter_row[0].text_input(
            "搜索",
            placeholder="标题、问题、样本ID",
            key="cluster_annotation_query",
        )
        status = filter_row[1].selectbox(
            "标注状态",
            ["待标注", "标注中", "已完成", "全部"],
            key="cluster_annotation_status",
        )
        priority = filter_row[2].selectbox(
            "任务范围",
            ["全部", "多成员簇", "模型建议复核"],
            key="cluster_annotation_priority",
        )
        products = filter_row[3].multiselect(
            "产品类型",
            sorted(
                {
                    _text(cluster.get("product_category"))
                    for cluster in clusters
                    if _text(cluster.get("product_category"))
                }
            ),
            key="cluster_annotation_products",
        )

    filtered = _filter_clusters(
        clusters,
        annotations,
        query=query,
        status=status,
        priority=priority,
        products=products,
    )
    if not filtered:
        st.info("当前筛选条件下没有主题簇。")
        return

    cluster_by_id = {
        _text(cluster.get("cluster_id")): cluster
        for cluster in filtered
    }
    available_ids = list(cluster_by_id)
    selected_id = _sync_cluster_selection(available_ids)

    selected_id = st.selectbox(
        "选择主题簇",
        available_ids,
        index=None,
        format_func=lambda cluster_id: (
            f"{cluster_id} · "
            f"{cluster_by_id[cluster_id]['theme_title']} · "
            f"{cluster_by_id[cluster_id]['member_count']}个成员 · "
            f"{_annotation_status(annotations.get(cluster_id, ClusterAnnotation(cluster_id)))}"
        ),
        key="cluster_annotation_selector",
    )
    st.session_state.cluster_annotation_selected_id = selected_id
    cluster = cluster_by_id[selected_id]
    annotation = annotations.get(
        selected_id,
        ClusterAnnotation(cluster_id=selected_id),
    )

    with st.container(horizontal=True, horizontal_alignment="distribute"):
        st.button(
            "上一簇",
            icon=":material/arrow_back:",
            disabled=selected_id == available_ids[0],
            key=f"cluster_prev_{selected_id}",
            on_click=_queue_cluster_selection,
            args=(
                _next_cluster_id(
                    filtered,
                    selected_id,
                    -1,
                ),
            ),
        )
        st.caption(f"当前筛选结果 {len(filtered)} 个主题簇")
        st.button(
            "下一簇",
            icon=":material/arrow_forward:",
            disabled=selected_id == available_ids[-1],
            key=f"cluster_next_{selected_id}",
            on_click=_queue_cluster_selection,
            args=(
                _next_cluster_id(
                    filtered,
                    selected_id,
                    1,
                ),
            ),
        )

    evidence_column, annotation_column = st.columns([1.9, 1])
    with evidence_column:
        with st.container(border=True):
            st.markdown(f"### {cluster['theme_title']}")
            with st.container(horizontal=True):
                st.caption(f"簇ID：{selected_id}")
                st.caption(f"成员数：{cluster['member_count']}")
                st.caption(
                    f"产品：{_text(cluster.get('product_category')) or '待确认'}"
                )
                st.caption(
                    f"模型建议复核：{'是' if cluster.get('requires_review') else '否'}"
                )
            shared_definition = _text(cluster.get("shared_knowledge_definition"))
            if shared_definition:
                st.markdown("**共享知识定义**")
                st.write(shared_definition)
            merge_basis = _text(cluster.get("merge_basis"))
            if merge_basis:
                st.markdown("**模型合并依据**")
                st.write(merge_basis)

        st.markdown("### 成员证据")
        for member_index, member in enumerate(cluster.get("members") or [], start=1):
            _render_member(member, member_index)

    with annotation_column:
        with st.form(
            f"cluster_annotation_form_{selected_id}",
            border=True,
            enter_to_submit=False,
        ):
            st.markdown("### 人工标注")
            if cluster["member_count"] == 1:
                st.info(
                    "单成员簇：判断它是否应该保持独立，还是应与其他主题簇合并。"
                    "不确定时可先选“待定”。",
                    icon=":material/info:",
                )
            else:
                st.info(
                    "多成员簇：判断所有成员能否由同一篇知识准确回答；"
                    "若不能，请选择拆分并勾选异常成员。",
                    icon=":material/info:",
                )
            cluster_decision = st.segmented_control(
                "归簇判断",
                CLUSTER_DECISIONS[1:],
                default=annotation.cluster_decision or "待定",
                key=f"cluster_decision_{selected_id}",
            )
            title_decision = st.segmented_control(
                "标题判断",
                TITLE_DECISIONS[1:],
                default=annotation.title_decision or "待定",
                key=f"title_decision_{selected_id}",
            )
            action = st.selectbox(
                "处理动作",
                CLUSTER_ACTIONS,
                index=CLUSTER_ACTIONS.index(annotation.action),
                key=f"cluster_action_{selected_id}",
            )
            member_options = [
                _text(member.get("unit_id"))
                for member in cluster.get("members") or []
            ]
            outlier_atomic_ids = st.multiselect(
                "需移出/单独拆分的成员",
                member_options,
                default=[
                    atomic_id
                    for atomic_id in annotation.outlier_atomic_ids
                    if atomic_id in member_options
                ],
                format_func=lambda atomic_id: next(
                    (
                        f"{atomic_id} · {_text(member.get('normalized_issue'))}"
                        for member in cluster.get("members") or []
                        if _text(member.get("unit_id")) == atomic_id
                    ),
                    atomic_id,
                ),
                key=f"cluster_outliers_{selected_id}",
            )
            gold_topic_id = st.text_input(
                "人工主题ID",
                value=annotation.gold_topic_id,
                placeholder="例如 GOLD-001",
                key=f"cluster_gold_id_{selected_id}",
            )
            gold_title = st.text_input(
                "人工主题标题",
                value=annotation.gold_title,
                placeholder="标题错误时填写正确标题",
                key=f"cluster_gold_title_{selected_id}",
            )
            cluster_title_by_id = {
                _text(candidate.get("cluster_id")): _text(
                    candidate.get("theme_title")
                )
                for candidate in clusters
                if _text(candidate.get("cluster_id"))
            }
            target_cluster_options = [
                "",
                *[
                    cluster_id
                    for cluster_id in cluster_title_by_id
                    if cluster_id and cluster_id != selected_id
                ],
            ]
            if (
                annotation.target_cluster_id
                and annotation.target_cluster_id not in target_cluster_options
            ):
                target_cluster_options.append(annotation.target_cluster_id)
            target_cluster_id = st.selectbox(
                "目标模型簇ID",
                target_cluster_options,
                index=target_cluster_options.index(annotation.target_cluster_id),
                format_func=lambda cluster_id: (
                    "请选择目标主题簇"
                    if not cluster_id
                    else f"{cluster_id} · {cluster_title_by_id.get(cluster_id, '')}"
                ),
                key=f"cluster_target_{selected_id}",
            )
            notes = st.text_area(
                "人工备注",
                value=annotation.notes,
                height=120,
                placeholder="写明对象、现象、处理路径或阈值差异",
                key=f"cluster_notes_{selected_id}",
            )
            reviewer = st.text_input(
                "审核人",
                value=annotation.reviewer,
                key=f"cluster_reviewer_{selected_id}",
            )
            auto_next = st.checkbox(
                "保存后自动进入下一簇",
                value=True,
                key=f"cluster_auto_next_{selected_id}",
            )
            submitted = st.form_submit_button(
                "保存标注",
                type="primary",
                icon=":material/save:",
                width="stretch",
            )
            if submitted:
                validation_errors = annotation_validation_errors(
                    cluster_decision=cluster_decision or "待定",
                    title_decision=title_decision or "待定",
                    action=action,
                    member_count=int(cluster.get("member_count") or 0),
                    outlier_atomic_ids=outlier_atomic_ids,
                    gold_title=gold_title,
                    target_cluster_id=target_cluster_id,
                )
                if validation_errors:
                    for error in validation_errors:
                        st.error(error, icon=":material/error:")
                else:
                    store.save(
                        cluster_id=selected_id,
                        cluster_decision=cluster_decision or "待定",
                        title_decision=title_decision or "待定",
                        action=action,
                        outlier_atomic_ids=outlier_atomic_ids,
                        gold_topic_id=gold_topic_id,
                        gold_title=gold_title,
                        target_cluster_id=target_cluster_id,
                        notes=notes,
                        reviewer=reviewer,
                    )
                    st.toast("标注已保存", icon=":material/check_circle:")
                    if auto_next and selected_id != available_ids[-1]:
                        _queue_cluster_selection(
                            _next_cluster_id(
                                filtered,
                                selected_id,
                                1,
                            )
                        )
                    st.rerun()

        with st.container(border=True):
            st.markdown("### 导出")
            export_rows = annotation_export_rows(clusters, store.list_all())
            st.download_button(
                "下载标注CSV",
                data=annotation_csv_bytes(export_rows),
                file_name="完整聚类人工标注.csv",
                mime="text/csv",
                icon=":material/download:",
                width="stretch",
            )
            st.download_button(
                "下载标注JSON",
                data=json.dumps(
                    export_rows,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                file_name="完整聚类人工标注.json",
                mime="application/json",
                icon=":material/data_object:",
                width="stretch",
            )
            st.caption(f"本地数据库：{DEFAULT_ANNOTATION_DB}")
