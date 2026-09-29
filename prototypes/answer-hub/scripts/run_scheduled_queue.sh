#!/usr/bin/env bash
set -euo pipefail

project_root=/opt/knowledge-kb/prototypes/answer-hub
cd "$project_root"
python_bin="$project_root/.venv/bin/python"
plan_path="${ANSWER_HUB_AUTOMATION_PLAN_PATH:-$project_root/data/automation-plan.json}"

window_json="$($python_bin -m answer_hub.automation_schedule prepare --plan "$plan_path")"
from_date="$(printf '%s' "$window_json" | "$python_bin" -c 'import json,sys; print(json.load(sys.stdin)["from_date"])')"
to_date="$(printf '%s' "$window_json" | "$python_bin" -c 'import json,sys; print(json.load(sys.stdin)["to_date"])')"

for bucket in pending processing failed; do
  if find "data/automation-queue/$bucket" -maxdepth 1 -type f -name '*.xlsx' -print -quit | grep -q .; then
    echo "Queue has unresolved $bucket work; skip $from_date to $to_date."
    "$python_bin" -m answer_hub.automation_schedule failure --plan "$plan_path" --today "$(/usr/bin/date +%F)" --reason "队列存在未解决的 $bucket 文件。" || true
    exit 1
  fi
done

state_file="data/second-part-pull/scheduled-${from_date//-/}-${to_date//-/}-state.json"
export SECOND_PART_QUERY_FROM_DATE="$from_date"
export SECOND_PART_QUERY_TO_DATE="$to_date"
export PYTHONUTF8=1
export PYTHONPATH="$project_root/src"

pull_succeeded=0
for attempt in 1 2 3; do
  if "$python_bin" -m answer_hub.cli second-part-pull --profile config/second-part-pull.powerzhuan.local.json --queue-dir data/automation-queue --output-dir outputs/automation-runs --state-file "$state_file" --max-pages 1; then
    pull_succeeded=1
    break
  fi
  echo "Second-part pull attempt $attempt/3 failed."
  if [[ "$attempt" -lt 3 ]]; then
    echo "Retrying in 60 seconds."
    /usr/bin/sleep 60
  fi
done
if [[ "$pull_succeeded" -ne 1 ]]; then
  echo "Second-part pull failed after 3 attempts; cursor stays at $from_date."
  "$python_bin" -m answer_hub.automation_schedule failure --plan "$plan_path" --today "$(/usr/bin/date +%F)" --reason "第二部分拉取连续 3 次失败。" || true
  exit 1
fi

if "$python_bin" -m answer_hub.cli automation-queue --queue-dir data/automation-queue --standards data/standards/active_standards.json --output-dir outputs/automation-runs --clustering-mode direct_mimo --max-files 10 --stale-after-seconds 7200 --sync-to-cz-review; then
  "$python_bin" -m answer_hub.automation_schedule commit --plan "$plan_path" --from-date "$from_date" --to-date "$to_date"
else
  exit_code=$?
  echo "Automation queue failed with exit code $exit_code; cursor stays at $from_date."
  "$python_bin" -m answer_hub.automation_schedule failure --plan "$plan_path" --today "$(/usr/bin/date +%F)" --reason "自动化队列退出码：$exit_code" || true
  exit "$exit_code"
fi
