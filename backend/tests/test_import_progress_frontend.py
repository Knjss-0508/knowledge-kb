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
    assert "self.importTaskNotice = '导入任务已创建，处理进度会自动更新。';" in success_block


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
