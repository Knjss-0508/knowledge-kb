"""本机模型网关代理：必须直连内网网关，并能扛住瞬时 TLS/5xx 抖动。"""

from __future__ import annotations

import importlib.util
import io
import urllib.error
import urllib.request
from pathlib import Path

import pytest


PROXY = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "local-migration"
    / "model-gateway-proxy.py"
)


def _load(monkeypatch):
    monkeypatch.setenv("MODEL_GATEWAY_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("MODEL_GATEWAY_USE_SYSTEM_PROXY", raising=False)
    spec = importlib.util.spec_from_file_location("model_gateway_proxy", PROXY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_bypasses_the_desktop_system_proxy(monkeypatch):
    """系统代理会把内网网关的 TLS 连接断开，代理必须显式直连。"""

    module = _load(monkeypatch)
    active_proxies = [
        handler
        for handler in module._OPENER.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
        and handler.proxies
    ]
    assert active_proxies == [], "代理不能沿用系统代理"
    # 空 ProxyHandler 会被 OpenerDirector 丢弃（不进入 handlers），因此同时锁定源码。
    assert "ProxyHandler({})" in PROXY.read_text(encoding="utf-8")


def test_transient_upstream_failure_is_retried(monkeypatch):
    module = _load(monkeypatch)
    calls: list[int] = []
    sleeps: list[float] = []

    def forward_once(method, raw_path, body, client_model):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(
                __import__("ssl").SSLEOFError(8, "EOF occurred in violation of protocol")
            )
        return (200, "application/json", b"{}")

    monkeypatch.setattr(module, "_forward_once", forward_once)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert module._forward("POST", "/v1/chat/completions", b"{}", None)[0] == 200
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_upstream_5xx_is_retried_then_returned(monkeypatch):
    module = _load(monkeypatch)
    calls: list[int] = []

    def forward_once(method, raw_path, body, client_model):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                "https://gateway.invalid", 502, "bad gateway", None, io.BytesIO(b"upstream")
            )
        return (200, "application/json", b"{}")

    monkeypatch.setattr(module, "_forward_once", forward_once)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    assert module._forward("POST", "/v1/chat/completions", b"{}", None)[0] == 200
    assert len(calls) == 3


def test_exhausted_retries_raise(monkeypatch):
    module = _load(monkeypatch)
    calls: list[int] = []

    def forward_once(method, raw_path, body, client_model):
        calls.append(1)
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(module, "_forward_once", forward_once)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    with pytest.raises(urllib.error.URLError):
        module._forward("POST", "/v1/chat/completions", b"{}", None)
    assert len(calls) == module.MAX_ATTEMPTS
