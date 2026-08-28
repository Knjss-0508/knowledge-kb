from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_automation_monitor_filters_by_display_status_mapping() -> None:
    assert "automationMonitorStatusMatches: function(job, filter)" in FRONTEND
    assert "['completed','done','review_pending'].indexOf(status)!==-1" in FRONTEND
    assert "['failed','stalled','attention'].indexOf(health)!==-1" in FRONTEND
    assert "self.automationMonitorStatusMatches(j,m.filter.status)" in FRONTEND
