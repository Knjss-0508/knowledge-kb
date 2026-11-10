"""预检必须扛住短暂的链路抖动，但不能把配置错误也拖成重试。"""

from __future__ import annotations

import pytest

import answer_hub.automation as automation
from answer_hub.mimo import MimoError


class _FakeClient:
    def __init__(self, errors: list[str]) -> None:
        self._errors = list(errors)
        self.calls = 0

    def check_availability(self) -> dict:
        self.calls += 1
        if self._errors:
            raise MimoError(self._errors.pop(0))
        return {"passed": True, "model": "deepseek-flash"}


def _install(monkeypatch, errors: list[str]):
    client = _FakeClient(errors)
    sleeps: list[float] = []
    monkeypatch.setattr(
        automation.MimoClient,
        "from_env",
        classmethod(lambda cls: client),
    )
    monkeypatch.setattr(automation.time, "sleep", lambda seconds: sleeps.append(seconds))
    return client, sleeps


def test_transient_ssl_error_is_retried_until_the_gateway_recovers(monkeypatch):
    """2026-11-05 的现场：一次 TLS EOF 就让整批任务失败并卡住 5 天。"""

    client, sleeps = _install(
        monkeypatch,
        [
            "MiMo 网络错误：[SSL: UNEXPECTED_EOF_WHILE_READING] "
            "EOF occurred in violation of protocol (_ssl.c:1010)",
            "MiMo 网络错误：<urlopen error [WinError 10054]>",
        ],
    )

    result = automation.run_mimo_preflight()

    assert result["passed"] is True
    assert client.calls == 3
    assert sleeps == [5.0, 10.0]


@pytest.mark.parametrize(
    "message",
    [
        "MiMo HTTP 401: invalid api key",
        "MiMo 预检响应不是合法 JSON",
        "MiMo 预检响应缺少 ok=true",
    ],
)
def test_configuration_errors_are_not_retried(monkeypatch, message):
    client, sleeps = _install(monkeypatch, [message])

    with pytest.raises(automation.MimoPreflightError):
        automation.run_mimo_preflight()

    assert client.calls == 1
    assert sleeps == []


def test_transient_errors_give_up_after_the_configured_attempts(monkeypatch):
    client, sleeps = _install(
        monkeypatch,
        ["MiMo 网络错误：timed out"] * 4,
    )

    with pytest.raises(automation.MimoPreflightError):
        automation.run_mimo_preflight()

    assert client.calls == 4
    assert sleeps == [5.0, 10.0, 20.0]


def test_http_503_counts_as_transient(monkeypatch):
    client, sleeps = _install(
        monkeypatch,
        ["MiMo HTTP 503: service unavailable"],
    )

    assert automation.run_mimo_preflight()["passed"] is True
    assert client.calls == 2
    assert sleeps == [5.0]


def test_mimo_client_bypasses_the_desktop_system_proxy():
    """2026-11-05 的 TLS EOF 来自系统代理劫持内网网关，客户端必须直连。"""

    from urllib.request import ProxyHandler

    from answer_hub import mimo

    active_proxies = [
        handler
        for handler in mimo._MODEL_OPENER.handlers
        if isinstance(handler, ProxyHandler)
        and handler.proxies
    ]
    assert active_proxies == [], "不能沿用系统代理"
