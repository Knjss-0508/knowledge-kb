#!/usr/bin/env bash
set -euo pipefail
project_root=/opt/knowledge-kb/prototypes/answer-hub
cd "$project_root"
today=${ANSWER_HUB_SCHEDULE_DATE:-$(/usr/bin/date +%F)}
plan_file=${ANSWER_HUB_AUTOMATION_PLAN_PATH:-$project_root/data/automation-plan.json}
plan_from=""
plan_to=""
if [[ -z "${ANSWER_HUB_SCHEDULE_FROM_DATE:-}" || -z "${ANSWER_HUB_SCHEDULE_TO_DATE:-}" ]] && [[ -f "$plan_file" ]]; then
  plan_values=$("$project_root/.venv/bin/python" -c '
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    payload = {}
from_date = str(payload.get("knowledge_settle_from_date") or payload.get("second_part_query_from_date") or "")
to_date = str(payload.get("knowledge_settle_to_date") or payload.get("second_part_query_to_date") or "")
print(from_date)
print(to_date)
' "$plan_file")
  plan_from=$(printf '%s\n' "$plan_values" | /usr/bin/sed -n '1p')
  plan_to=$(printf '%s\n' "$plan_values" | /usr/bin/sed -n '2p')
fi
from_date=${ANSWER_HUB_SCHEDULE_FROM_DATE:-${plan_from:-$today}}
to_date=${ANSWER_HUB_SCHEDULE_TO_DATE:-${plan_to:-$today}}
for bucket in pending processing failed; do
  if find "data/automation-queue/$bucket" -maxdepth 1 -type f -name '*.xlsx' -print -quit | grep -q .; then
    echo "Queue has unresolved $bucket work; skip $from_date to $to_date."
    exit 1
  fi
done
state_file="data/second-part-pull/scheduled-${from_date//-/}-${to_date//-/}-state.json"
export SECOND_PART_QUERY_FROM_DATE="$from_date"
export SECOND_PART_QUERY_TO_DATE="$to_date"
export SECOND_PART_QUERY_LIMIT="${ANSWER_HUB_SECOND_PART_QUERY_LIMIT:-10000}"
export PYTHONUTF8=1
export PYTHONPATH="$project_root/src"
pull_succeeded=0
for attempt in 1 2 3; do
  if "$project_root/.venv/bin/python" -m answer_hub.cli second-part-pull --profile config/second-part-pull.powerzhuan.local.json --queue-dir data/automation-queue --output-dir outputs/automation-runs --state-file "$state_file" --max-pages 0 --exclude-existing-records; then
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
  echo "Second-part pull failed after 3 attempts; MiMo was not started."
  exit 1
fi
"$project_root/.venv/bin/python" -m answer_hub.cli automation-queue --queue-dir data/automation-queue --standards data/standards/active_standards.json --output-dir outputs/automation-runs --clustering-mode direct_mimo --max-files 10 --stale-after-seconds 7200 --sync-to-cz-review
