import re
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


def test_local_nightly_scheduler_does_not_clobber_the_output_dir() -> None:
    """prepare 的输出不能存进 $out。

    PowerShell 变量名不区分大小写：$out 与 $OUT（输出根目录）是同一个变量。
    2026-10-04 夜间实测：prepare 的 JSON 覆盖了 $OUT，--output-dir 收到 JSON 字符串，
    拉取在 AutomationJobStore 的 mkdir 处以 OSError WinError 123 连续失败 3 次，
    当天一条数据都没拉（调度器只把失败写进计划，不会主动报警）。
    """
    assert "$OUT" in WINDOWS_SCRIPT
    captured = re.findall(
        r"^\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*& \$PY .*prepare",
        WINDOWS_SCRIPT,
        flags=re.MULTILINE,
    )
    assert captured, "夜间脚本里应当能找到 prepare 的捕获语句"
    assert "out" not in {name.lower() for name in captured}
    assert "$prepareJson" in WINDOWS_SCRIPT


def test_local_nightly_scheduler_supports_dry_run() -> None:
    """-DryRun 只走到第 3 步参数就绪即退出，供冒烟用（不拉取、不消费、不 commit）。"""
    assert "param([switch]$DryRun)" in WINDOWS_SCRIPT
    assert "if ($DryRun) {" in WINDOWS_SCRIPT


def test_local_nightly_scheduler_is_saved_as_utf8_with_bom() -> None:
    """PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，中文吃引号后整篇语法报错。"""
    raw = (SCRIPTS / "local-migration" / "nightly-scheduler.ps1").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
