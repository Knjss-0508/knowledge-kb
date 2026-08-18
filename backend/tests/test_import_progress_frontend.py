from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_import_panel_is_hidden_when_idle_and_visible_for_live_or_attention_tasks() -> None:
    assert (
        "v-if=\"can('knowledge:create') && "
        "(importing || importUploadError || visibleImportTasks().length || showImportHistory)\""
        in FRONTEND
    )
    assert "if (self.showImportHistory) return self.importTasks;" in FRONTEND
    assert "self.isImportTaskActive(task) ||" in FRONTEND
    assert "self.importTaskAttentionIds.indexOf(id) !== -1 ||" in FRONTEND
    assert "self.importTaskTransientIds.indexOf(id) !== -1;" in FRONTEND
    assert "return [self.importTasks[0]];" not in FRONTEND
    assert "v-if=\"importing && !importTaskNotice\"" in FRONTEND
    assert "正在上传并创建后台任务..." in FRONTEND


def test_created_import_task_is_rendered_before_polling() -> None:
    success_block_start = FRONTEND.index(
        ".then(function(data) {",
        FRONTEND.index("fetch(API + '/knowledge/import/excel'"),
    )
    success_block_end = FRONTEND.index(
        ".catch(function(error)",
        success_block_start,
    )
    success_block = FRONTEND[success_block_start:success_block_end]

    track_index = success_block.index("self.trackCurrentImportTask(data && data.id);")
    upsert_index = success_block.index("self.upsertImportTask(data);")
    poll_index = success_block.index("self.startImportTaskPolling();")
    refresh_index = success_block.index("return self.loadImportTasks();")

    assert track_index < upsert_index < poll_index < refresh_index
    assert "self.importing = false;" not in success_block
    assert "alert('文件已上传" not in success_block
    assert (
        "self.importTaskNotice = self.importTypeLabel(selectedImportType)"
        in success_block
    )
    assert "'导入任务已创建，处理进度会自动更新。';" in success_block


def test_terminal_import_tasks_follow_success_and_failure_visibility_rules() -> None:
    assert "this.showImportTaskTransient(task, 3000);" in FRONTEND
    assert "this.showImportTaskTransient(task, 2000);" in FRONTEND
    assert "this.addImportTaskAttention(task);" in FRONTEND
    assert "isImportTaskAttention: function(task)" in FRONTEND
    assert "dismissImportTaskAttention: function(task)" in FRONTEND
    assert "kb_import_task_attention_ids" in FRONTEND
    assert "alert('导入任务已取消。')" not in FRONTEND


def test_upload_failures_are_non_blocking_and_dismissible() -> None:
    assert "self.importUploadError = error.message || '批量导入失败';" in FRONTEND
    assert "dismissImportUploadError: function()" in FRONTEND
    assert "上传失败" in FRONTEND
    assert "关闭提醒" in FRONTEND


def test_import_task_list_ignores_stale_responses() -> None:
    assert "importTaskListRequestId: 0" in FRONTEND
    assert "var requestId = ++self.importTaskListRequestId;" in FRONTEND
    assert FRONTEND.count(
        "if (requestId !== self.importTaskListRequestId) return;"
    ) >= 2


def test_import_mode_dialog_defaults_to_regular_knowledge() -> None:
    assert "importDialogOpen: false" in FRONTEND
    assert "importType: 'knowledge'" in FRONTEND
    assert "@click=\"openImportDialog\"" in FRONTEND
    assert "用于总部标准、业务沉淀" in FRONTEND
    assert "机型配置信息" in FRONTEND
    assert "this.importType = 'knowledge';" in FRONTEND


def test_template_and_upload_send_the_selected_import_type() -> None:
    assert "API + '/knowledge/import/template?import_type='" in FRONTEND
    assert "+ encodeURIComponent(selectedImportType)" in FRONTEND
    assert "form.append('import_type', selectedImportType);" in FRONTEND
    assert "机型配置信息批量导入模板.xlsx" in FRONTEND
    assert "知识批量导入模板.xlsx" in FRONTEND


def test_legacy_tasks_fall_back_to_regular_knowledge() -> None:
    normalize_start = FRONTEND.index("normalizeImportType: function(value)")
    normalize_end = FRONTEND.index("importTypeLabel: function(value)", normalize_start)
    normalize_block = FRONTEND[normalize_start:normalize_end]

    assert (
        "return value === 'model_configuration' "
        "? 'model_configuration' : 'knowledge';"
        in normalize_block
    )
    assert "return this.importTypeLabel(task && task.import_type);" in FRONTEND


def test_model_configuration_tasks_have_separate_counts_and_operations() -> None:
    assert (
        "<span>新增</span><b>"
        "{{importTaskOperationCount(importTaskDetail.task,'created')}}</b>"
        in FRONTEND
    )
    assert (
        "<span>更新</span><b>"
        "{{importTaskOperationCount(importTaskDetail.task,'updated')}}</b>"
        in FRONTEND
    )
    assert (
        "<span>未变化</span><b>"
        "{{importTaskOperationCount(importTaskDetail.task,'unchanged')}}</b>"
        in FRONTEND
    )
    assert "{{importTaskOperationLabel(row.operation)}}" in FRONTEND
    assert "created:'新增'" in FRONTEND
    assert "updated:'更新'" in FRONTEND
    assert "unchanged:'未变化'" in FRONTEND
    assert "return this.importTaskFailureRows(task);" in FRONTEND


def test_regular_import_summary_and_polling_are_preserved() -> None:
    assert "'已创建 ' + Number(task && task.imported || 0)" in FRONTEND
    assert "' · 疑似重复 ' + Number(task && task.review_required || 0)" in FRONTEND
    assert "' · 待审核 ' + Number(task && task.pending_review || 0)" in FRONTEND
    assert "self.upsertImportTask(data);" in FRONTEND
    assert "self.startImportTaskPolling();" in FRONTEND
    assert "}, 3000);" in FRONTEND


def test_model_configuration_detail_requests_and_reports_all_results() -> None:
    assert "?include_results=true&result_limit=5000" in FRONTEND
    assert (
        "已展示 {{importTaskDisplayedResultCount(importTaskDetail.task)}}/"
        "{{importTaskDetailResultTotal(importTaskDetail.task)}} 条逐行结果"
        in FRONTEND
    )
    assert "importTaskDisplayedResultCount: function(task)" in FRONTEND
    assert "importTaskDetailResultTotal: function(task)" in FRONTEND


def test_import_task_polling_has_a_single_in_flight_request() -> None:
    load_start = FRONTEND.index("loadImportTasks: function()")
    load_end = FRONTEND.index("logout: function()", load_start)
    load_block = FRONTEND[load_start:load_end]

    assert "importTaskListLoading: false" in FRONTEND
    assert "self.importTaskListLoading" in load_block
    assert "self.importTaskListLoading = true;" in load_block
    assert "self.importTaskListLoading = false;" in load_block
    assert (
        load_block.index("self.importTaskListLoading = true;")
        < load_block.index("fetch(API + '/knowledge/import/tasks?limit=100'")
    )


def test_in_flight_refresh_keeps_local_active_task_and_detects_any_completion() -> None:
    assert "previousActiveTasksById" in FRONTEND
    assert "nextTasks.unshift(pendingTask);" in FRONTEND
    assert "var completedTaskDetected =" in FRONTEND
    assert "if (completedTaskDetected)" in FRONTEND
    assert "self.load();" in FRONTEND
    assert "self.loadStats();" in FRONTEND


def test_running_model_configuration_task_cannot_offer_cancel() -> None:
    assert 'v-if="canCancelImportTask(task)"' in FRONTEND
    assert "canCancelImportTask: function(task)" in FRONTEND
    assert "this.isModelConfigurationImportTask(task)" in FRONTEND
    assert "task.status === 'running'" in FRONTEND
    assert "if (!self.canCancelImportTask(task)" in FRONTEND


def test_model_configuration_progress_uses_atomic_sync_wording() -> None:
    assert "'已解析 ' + total + ' 行，正在整批同步'" in FRONTEND
    assert "'整批失败，' + total + ' 行全部回滚'" in FRONTEND
    assert "failed:'文件校验失败，未写入数据'" in FRONTEND
    assert "'整批同步完成，已处理 ' + processed + '/' + total + ' 行'" in FRONTEND
