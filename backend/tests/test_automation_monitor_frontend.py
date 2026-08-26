from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_automation_monitor_is_a_standalone_primary_workspace() -> None:
    assert "view==='automationMonitor'" in FRONTEND
    assert "@click=\"openAutomationMonitor\"" in FRONTEND
    assert "知识库管理" in FRONTEND
    assert "启用自动化" in FRONTEND
    assert "暂停自动化" in FRONTEND
    assert "立即执行一次" in FRONTEND
    assert "重试失败任务" in FRONTEND
    assert "查看运行日志" in FRONTEND
    assert "知识库管理</button>" not in FRONTEND
    assert "候选价值复核同步异常" in FRONTEND
    assert "返回知识库管理" not in FRONTEND
    assert "强制停止当前运行" not in FRONTEND
    assert "answerHubMonitor" in FRONTEND
    assert "knowledgeWorkspace==='monitor'" not in FRONTEND
