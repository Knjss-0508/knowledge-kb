from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = (SCRIPTS / "run_scheduled_queue.sh").read_text(encoding="utf-8")
WINDOWS_SCRIPT = (SCRIPTS / "local-migration" / "nightly-scheduler.ps1").read_text(
    encoding="utf-8"
)


def test_scheduled_queue_reads_the_window_from_the_saved_plan() -> None:
    """日期窗口必须来自计划文件的 prepare 输出，不退回「今天」。"""
    assert 'plan_path="${ANSWER_HUB_AUTOMATION_PLAN_PATH:-$project_root/data/automation-plan.json}"' in SCRIPT
    assert 'plan.get("knowledge_settle_from_date") or plan.get("second_part_query_from_date")' not in SCRIPT
    assert '-m answer_hub.automation_schedule prepare --plan "$plan_path"' in SCRIPT
    assert 'from_date="$(printf \'%s\' "$window_json"' in SCRIPT
    assert 'to_date="$(printf \'%s\' "$window_json"' in SCRIPT
    assert 'state_file="data/second-part-pull/scheduled-${from_date//-/}-${to_date//-/}-state.json"' in SCRIPT


def test_scheduled_queue_pulls_everything_the_interface_returns() -> None:
    """拉取必须显式设置 limit 并读取全部分页。

    接口不传 limit 时只返回 1000 条；#130（升级运行监管日期游标调度）把这三项删掉后，
    服务器每天的定时拉取会静默截断到 1000 条。README「第二部分拉取」也要求
    「不得改回固定单页读取」，因此这里逐项锁死。
    """
    assert 'export SECOND_PART_QUERY_LIMIT="${ANSWER_HUB_SECOND_PART_QUERY_LIMIT:-10000}"' in SCRIPT
    assert '--max-pages 0' in SCRIPT
    assert '--exclude-existing-records' in SCRIPT


def test_local_nightly_scheduler_matches_the_server_pull_parameters() -> None:
    """本机夜间脚本必须与服务器脚本一样全量读取，且绝不向 CZ 推送。"""
    assert 'SECOND_PART_QUERY_LIMIT' in WINDOWS_SCRIPT
    assert '$env:SECOND_PART_QUERY_LIMIT = "10000"' in WINDOWS_SCRIPT
    assert '"--max-pages","0"' in WINDOWS_SCRIPT
    assert '"--exclude-existing-records"' in WINDOWS_SCRIPT
    # 只允许出现在注释里：绝不作为参数传给 automation-queue。
    assert '"--sync-to-cz-review"' not in WINDOWS_SCRIPT
