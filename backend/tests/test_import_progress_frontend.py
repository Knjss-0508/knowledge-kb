from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_import_panel_is_visible_during_upload_and_keeps_the_current_task() -> None:
    assert (
        "v-if=\"can('knowledge:create') && "
        "(importing || visibleImportTasks().length || showImportHistory)\""
        in FRONTEND
    )
    assert "currentImportTaskId: ''" in FRONTEND
    assert "task.id === self.currentImportTaskId" in FRONTEND
    assert (
        "if (!visible.length && self.importTasks.length) "
        "return [self.importTasks[0]];"
        in FRONTEND
    )
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

    upsert_index = success_block.index("self.upsertImportTask(data);")
    poll_index = success_block.index("self.startImportTaskPolling();")
    refresh_index = success_block.index("return self.loadImportTasks();")

    assert upsert_index < poll_index < refresh_index
    assert "self.importing = false;" not in success_block
    assert "alert('文件已上传" not in success_block
    assert "self.importTaskNotice = '导入任务已创建，处理进度会自动更新。';" in success_block


def test_import_task_list_ignores_stale_responses() -> None:
    assert "importTaskListRequestId: 0" in FRONTEND
    assert "var requestId = ++self.importTaskListRequestId;" in FRONTEND
    assert FRONTEND.count(
        "if (requestId !== self.importTaskListRequestId) return;"
    ) >= 2
