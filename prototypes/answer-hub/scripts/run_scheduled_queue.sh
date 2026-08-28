#!/usr/bin/env bash
set -euo pipefail
project_root=/opt/knowledge-kb/prototypes/answer-hub
cd "$project_root"
today=${ANSWER_HUB_SCHEDULE_DATE:-$(/usr/bin/date +%F)}
case "$today" in
  2026-08-24) from_date=2026-08-07; to_date=2026-08-14 ;;
  2026-08-25) from_date=2026-08-15; to_date=2026-08-21 ;;
  2026-08-26) from_date=2026-08-22; to_date=2026-08-26 ;;
  *)
    if [[ "$today" < 2026-08-27 ]]; then
      echo "No scheduled Answer Hub batch for $today."
      exit 0
    fi
    from_date="$today"
    to_date="$today"
    ;;
esac
for bucket in pending processing failed; do
  if find "data/automation-queue/$bucket" -maxdepth 1 -type f -name '*.xlsx' -print -quit | grep -q .; then
    echo "Queue has unresolved $bucket work; skip $from_date to $to_date."
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
  if "$project_root/.venv/bin/python" -m answer_hub.cli second-part-pull --profile config/second-part-pull.powerzhuan.local.json --queue-dir data/automation-queue --output-dir outputs/automation-runs --state-file "$state_file" --max-pages 1; then
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
