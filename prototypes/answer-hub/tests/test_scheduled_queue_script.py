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


def test_queue_block_is_recorded_as_a_deferral_not_a_failure() -> None:
    """队列没清干净是「本轮跳过」，必须走 defer，不能走 failure。

    线上故障：两个脚本都把队列阻塞记成 failure，连续三晚之后
    auto_advance_paused 变成 true，prepare 直接抛错、脚本再也走不到队列检查，
    游标永远停在原地（本机实测 2026-10-11 起连停 11 天）。
    """
    assert "-m answer_hub.automation_schedule defer" in SCRIPT
    assert "--reason \"队列存在未解决的 $bucket 文件。\"" in SCRIPT
    # 队列阻塞这一条不能再调用 failure；真失败（拉取三次失败 / 队列退出码）保留。
    assert 'automation_schedule failure --plan "$plan_path" --today "$(/usr/bin/date +%F)" --reason "队列存在未解决的' not in SCRIPT
    assert '--reason "第二部分拉取连续 3 次失败。"' in SCRIPT
    assert '--reason "自动化队列退出码：$exit_code"' in SCRIPT

    assert "automation_schedule defer" in WINDOWS_SCRIPT
    assert '--reason "队列存在未解决的 $b 文件。"' in WINDOWS_SCRIPT
    assert 'automation_schedule failure --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "队列存在未解决的' not in WINDOWS_SCRIPT
    assert '--reason "第二部分拉取连续 3 次失败。"' in WINDOWS_SCRIPT
    assert '--reason "自动化队列退出码：$c"' in WINDOWS_SCRIPT


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
    """本机夜间脚本必须与服务器脚本一样全量读取。

    `--sync-to-cz-review` 由 .env 的 ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW 决定：
    true 时送「候选价值复核」（人工终审，不自动发布），false 时不送。
    自动发布相关的闸（AUTO_REVIEW_KILL_SWITCH）仍然必须为 true。
    """
    assert 'SECOND_PART_QUERY_LIMIT' in WINDOWS_SCRIPT
    assert '$env:SECOND_PART_QUERY_LIMIT = "10000"' in WINDOWS_SCRIPT
    # 全量读取：不限页数、跳过已存在的记录（参数可能以数组或行内形式书写）
    assert "--max-pages 0" in WINDOWS_SCRIPT or '"--max-pages","0"' in WINDOWS_SCRIPT
    assert "--exclude-existing-records" in WINDOWS_SCRIPT
    # 送审开关必须由 .env 控制，且只在 true 时附加参数。
    assert '$syncToCzReview = ($envSync -eq "true") -or ($envSubmit -eq "true")' in WINDOWS_SCRIPT
    assert '"--sync-to-cz-review"' in WINDOWS_SCRIPT
    # 自动审核/自动发布必须保持关闭。
    assert 'AUTO_REVIEW_KILL_SWITCH' in WINDOWS_SCRIPT


def test_local_nightly_scheduler_skips_empty_days_and_continues() -> None:
    """第二部分某天返回 0 条时，应当提交该天并继续下一天，而不是停住。

    第二部分存在整天无数据的日期（截图中 10-06/10-07/10-10~10-13 等）。
    旧实现只处理一天就结束：空日虽然推进了游标，但当晚不会再继续，
    后面的日期要等下一次计划任务才补。
    """
    # 必须有逐日推进的循环
    assert "while ($processedWindows -lt $maxWindowsPerRun)" in WINDOWS_SCRIPT
    # 空日判定基于 fetched_records，判断后直接 commit 并 continue
    assert "$pulledRecords -le 0" in WINDOWS_SCRIPT
    assert "& $PY -m answer_hub.automation_schedule commit" in WINDOWS_SCRIPT
    assert "continue" in WINDOWS_SCRIPT
    # 空日不能记成失败，否则会累积 consecutive_failure_count
    assert "空日" in WINDOWS_SCRIPT
    empty_branch = WINDOWS_SCRIPT.split("$pulledRecords -le 0", 1)[1].split("continue", 1)[0]
    assert "automation_schedule failure" not in empty_branch
    # 单次运行要有上限，避免一次跑太久
    assert "ANSWER_HUB_MAX_WINDOWS_PER_RUN" in WINDOWS_SCRIPT


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
