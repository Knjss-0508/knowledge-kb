from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_automation_monitor_filters_by_display_status_mapping() -> None:
    assert "automationMonitorStatusMatches: function(job, filter)" in FRONTEND
    assert "['completed','done','review_pending'].indexOf(status)!==-1" in FRONTEND
    assert "['failed','stalled','attention'].indexOf(health)!==-1" in FRONTEND
    assert "self.automationMonitorStatusMatches(j,m.filter.status)" in FRONTEND


def test_automation_monitor_keeps_partial_date_after_validation_error() -> None:
    assert "if (!!fromDate !== !!toDate)" in FRONTEND
    assert "第二部分采集开始日期和结束日期必须同时填写。" in FRONTEND
    assert "second_part_query_from_date:fromDate" in FRONTEND
    assert "second_part_query_to_date:toDate" in FRONTEND
    assert ".catch(function(e){alert(e.message);})" in FRONTEND
    assert ".catch(function(e){alert(e.message);self.loadAutomationMonitor();})" not in FRONTEND
