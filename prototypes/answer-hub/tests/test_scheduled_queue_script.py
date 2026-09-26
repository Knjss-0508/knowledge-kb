from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_scheduled_queue.sh"
).read_text(encoding="utf-8")


def test_scheduled_queue_prefers_saved_plan_dates_before_today_fallback() -> None:
    assert 'plan_file=${ANSWER_HUB_AUTOMATION_PLAN_PATH:-$project_root/data/automation-plan.json}' in SCRIPT
    assert 'plan.get("knowledge_settle_from_date") or plan.get("second_part_query_from_date")' not in SCRIPT
    assert 'from_date=${ANSWER_HUB_SCHEDULE_FROM_DATE:-${plan_from:-$today}}' in SCRIPT
    assert 'to_date=${ANSWER_HUB_SCHEDULE_TO_DATE:-${plan_to:-$today}}' in SCRIPT
    assert 'state_file="data/second-part-pull/scheduled-${from_date//-/}-${to_date//-/}-state.json"' in SCRIPT
