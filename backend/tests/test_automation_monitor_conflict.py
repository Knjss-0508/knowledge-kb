"""Answer Hub 409 分类与用户提示的回归测试。

背景：生产上点「开始自动化流程」会拿到 Answer Hub 的 409，报文是
``{"error": "..."}``，而旧实现只读 ``detail``/``message``，于是永远落到
``rejected``，用户只看到一句「当前自动化状态不允许执行该操作」。

这里同时锁住两件事：
1. 每个真实原因都能分到独立类别；
2. 分类不能只靠中文关键词 —— 真实报文里的中文会因为编码不一致整句变成
   U+FFFD 替换字符（本文件里的 PRODUCTION_MOJIBAKE 就是生产原文逐字节复制）。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.services import automation_monitor as monitor


# 生产 409 报文里的 error 字段原文（逐字节从 kb-backend 抓取）。
# 真实含义是 `错误: 系统中不存在指定的任务名 "AnswerHubAutomationQueue"。`，
# 但 Answer Hub 用 UTF-8 解码 schtasks 的 GBK 中文输出，整句已退化为替换字符。
PRODUCTION_MOJIBAKE = (
    "\ufffd\ufffd\ufffd\ufffd: \u03f5\u0373\ufffd\u0432"
    "\ufffd\ufffd\ufffd\ufffd\ufffd\u05b8"
    "\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd"
    " \"AnswerHubAutomationQueue\"\ufffd\ufffd\r\n"
)

TASK_NOT_FOUND_ZH = '错误: 系统中不存在指定的任务名 "AnswerHubAutomationQueue"。'
TASK_NOT_FOUND_EN = (
    'ERROR: The specified task name "AnswerHubAutomationQueue" does not exist in the system.'
)


def _classify(body: dict[str, object]) -> tuple[str, str]:
    return monitor.classify_conflict(body, json.dumps(body, ensure_ascii=False))


def test_production_mojibake_conflict_is_not_reported_as_generic_rejection() -> None:
    """生产原文（整句乱码）必须分到计划任务不可用，而不是笼统的 rejected。"""
    kind, detail = _classify({"error": PRODUCTION_MOJIBAKE})
    assert kind == "scheduler_unavailable"
    assert detail == PRODUCTION_MOJIBAKE.strip()
    message = monitor.monitor_error_detail(kind, detail)
    assert "计划任务" in message
    assert "当前自动化状态不允许" not in message


def test_chinese_and_english_task_not_found_map_to_same_kind() -> None:
    for text in (TASK_NOT_FOUND_ZH, TASK_NOT_FOUND_EN):
        kind, detail = _classify({"error": text})
        assert kind == "scheduler_unavailable", text
        message = monitor.monitor_error_detail(kind, detail)
        assert "还没有安装这个自动化计划任务" in message


def test_queue_blocked_gets_its_own_actionable_message() -> None:
    """队列存在未解决的失败文件 —— 本任务新增的类别。"""
    for text in (
        "队列存在未解决的 failed 文件。",
        "队列中有未解决的失败文件，请先处理。",
        "queue has unresolved failed items",
    ):
        kind, _ = _classify({"error": text})
        assert kind == "queue_blocked", text
    message = monitor.monitor_error_detail("queue_blocked", "队列存在未解决的 failed 文件。")
    assert "失败文件" in message
    assert "查看运行日志" in message


def test_already_running_behaviour_is_unchanged() -> None:
    """保留旧实现的 already_running 判定与文案。"""
    for text in (
        "已有自动化任务正在运行。",
        "自动化正在运行",
        "并发执行被拒绝",
        "another run is already running",
    ):
        kind, _ = _classify({"error": text})
        assert kind == "already_running", text
    assert (
        monitor.monitor_error_detail("already_running", "已有自动化任务正在运行。")
        == "已有自动化任务正在运行，请等待当前任务完成后再执行。"
    )


def test_structured_fields_win_over_free_text() -> None:
    """结构化字段优先：文本是英文噪声时也按 code 分类。"""
    kind, _ = _classify({"code": "queue_blocked", "error": "HTTP 409 Conflict"})
    assert kind == "queue_blocked"
    kind, _ = _classify({"detail": {"kind": "task_not_installed"}, "error": "Conflict"})
    assert kind == "scheduler_unavailable"


def test_detail_field_is_read_as_a_fallback_field_name() -> None:
    """error 缺失时仍能从 detail/message/reason 取到原因。"""
    assert _classify({"detail": TASK_NOT_FOUND_ZH})[0] == "scheduler_unavailable"
    assert _classify({"message": "队列存在未解决的 failed 文件。"})[0] == "queue_blocked"
    assert _classify({"reason": "自动化已暂停，请先启用后再立即执行。"})[0] == "paused"


def test_paused_conflict_keeps_the_actionable_wording() -> None:
    kind, _ = _classify({"error": "自动化已暂停，请先启用后再立即执行。"})
    assert kind == "paused"
    assert "开始自动化流程" in monitor.monitor_error_detail(kind)


def test_unknown_conflict_still_falls_back_to_rejected() -> None:
    kind, _ = _classify({"error": "some brand new upstream wording"})
    assert kind == "rejected"
    assert monitor.monitor_error_detail(kind) == monitor.monitor_error_message("rejected")


def test_every_kind_has_a_non_empty_chinese_message() -> None:
    for kind in (
        "not_configured", "authentication", "rejected", "invalid_request",
        "unavailable", "invalid_response", "queue_blocked", "scheduler_unavailable",
        "paused", "a_kind_nobody_declared",
    ):
        message = monitor.monitor_error_message(kind)
        assert message and message.endswith("。"), kind


def test_gateway_reads_conflict_body_from_the_error_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：网关必须从 409 报文里真的读到原因，而不是丢掉。"""
    body = json.dumps({"error": PRODUCTION_MOJIBAKE}, ensure_ascii=True).encode("utf-8")
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        captured["url"] = request.full_url
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(monitor, "urlopen", fake_urlopen)
    gateway = monitor.AutomationMonitorGateway("http://answer-hub.invalid", "k" * 8, 5)
    with pytest.raises(monitor.AutomationMonitorError) as excinfo:
        gateway.request("PATCH", "/api/v1/automation/control", {"enabled": True})

    error = excinfo.value
    assert error.kind == "scheduler_unavailable"
    assert error.status_code == 409
    assert captured["url"] == "http://answer-hub.invalid/api/v1/automation/control"
    # 用户可见文案里不能出现上游原文，避免泄露路径或任务名细节。
    visible = monitor.monitor_error_detail(error.kind, error.detail)
    assert "AnswerHubAutomationQueue" not in visible
    assert "计划任务" in visible
