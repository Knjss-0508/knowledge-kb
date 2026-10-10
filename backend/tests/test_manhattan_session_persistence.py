"""曼哈顿 Cookie 持久化：写入数据卷、跨重启读回、清除与登录过期处理。

背景：盲标建单前的上游工单详情复核门禁依赖曼哈顿 Cookie，而该 Cookie 原先
只存在于 uvicorn 进程内存里，容器每次重启/重新部署都会丢失。这里验证改为
持久化到 DATA_DIR（knowledge-kb_manhattan_cache 数据卷）后的行为。
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.routes import manhattan


MANHATTAN_ROUTE_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "routes" / "manhattan.py"
).read_text(encoding="utf-8")


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: str = '{"respData": []}',
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse | None = None, **kwargs) -> None:
        self._response = response or _FakeResponse()
        self.requests: list[dict] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self._response


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class _FakeUser:
    username = "tester"


@pytest.fixture(autouse=True)
def _isolated_cookie_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(manhattan, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(manhattan, "COOKIE_FILE", str(tmp_path / "manhattan_cookie.json"))
    monkeypatch.setattr(manhattan, "_runtime_cookie", "")
    monkeypatch.setattr(manhattan, "_saved_cookie", None)
    monkeypatch.setattr(manhattan, "_saved_cookie_meta", {})
    monkeypatch.setattr(manhattan.settings, "NMHT_COOKIE", "")
    yield


def _persist(cookie: str, updated_by: str = "tester") -> None:
    assert manhattan._write_saved_cookie(cookie, updated_by) is True


def _restart_process() -> None:
    """模拟容器重启：清空内存态（运行时 Cookie 与磁盘缓存）。"""

    manhattan._runtime_cookie = ""
    manhattan._saved_cookie = None
    manhattan._saved_cookie_meta = {}


def test_session_endpoint_verifies_and_persists_cookie_to_the_data_volume(monkeypatch) -> None:
    client = _FakeAsyncClient(_FakeResponse())
    monkeypatch.setattr(manhattan.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(
        manhattan.set_manhattan_session(
            _FakeRequest({"cookie": "  MHT=abc123  "}), current_user=_FakeUser()
        )
    )

    assert result == {"ok": True, "source": "runtime", "persisted": True}
    payload = json.loads(Path(manhattan.COOKIE_FILE).read_text(encoding="utf-8"))
    assert payload["cookie"] == "MHT=abc123"
    assert payload["updated_by"] == "tester"
    assert payload["updated_at"]
    # 校验请求确实带上了待验证的 Cookie。
    assert client.requests[0]["headers"]["Cookie"] == "MHT=abc123"


@pytest.mark.skipif(os.name == "nt", reason="Windows 不保留 POSIX 文件权限")
def test_persisted_cookie_file_is_owner_only() -> None:
    _persist("MHT=abc123")

    assert os.stat(manhattan.COOKIE_FILE).st_mode & 0o777 == 0o600


def test_persisted_cookie_survives_a_container_restart() -> None:
    _persist("MHT=abc123")
    _restart_process()

    assert manhattan.active_cookie() == "MHT=abc123"
    session = manhattan.get_manhattan_session()
    assert session["logged_in"] is True
    assert session["source"] == "saved"
    assert session["persisted"] is True
    assert session["updated_by"] == "tester"
    assert session["updated_at"]


def test_active_cookie_priority_is_runtime_then_saved_then_environment(
    monkeypatch,
) -> None:
    monkeypatch.setattr(manhattan.settings, "NMHT_COOKIE", "ENV=cookie")
    assert manhattan.active_cookie() == "ENV=cookie"
    assert manhattan.get_manhattan_session()["source"] == "env"

    _persist("SAVED=cookie")
    assert manhattan.active_cookie() == "SAVED=cookie"
    assert manhattan.get_manhattan_session()["source"] == "saved"

    manhattan._runtime_cookie = "RUNTIME=cookie"
    assert manhattan.active_cookie() == "RUNTIME=cookie"
    assert manhattan.get_manhattan_session()["source"] == "runtime"


def test_clearing_the_session_removes_the_persisted_cookie() -> None:
    _persist("MHT=abc123")
    _restart_process()

    assert manhattan.clear_manhattan_session(_=_FakeUser()) == {"ok": True}

    assert manhattan.active_cookie() == ""
    assert manhattan.get_manhattan_session()["logged_in"] is False
    assert not Path(manhattan.COOKIE_FILE).exists()


def test_expired_login_drops_both_runtime_and_persisted_cookie(monkeypatch) -> None:
    _persist("MHT=abc123")
    manhattan._runtime_cookie = "MHT=abc123"

    def _raise(*args, **kwargs):
        raise manhattan.HTTPException(401, "Manhattan login expired.")

    monkeypatch.setattr(manhattan, "_fetch_json", _raise)
    monkeypatch.setattr(manhattan, "_write_cache", lambda data: None)
    monkeypatch.setattr(manhattan, "REQUEST_DELAY_SECONDS", 0)

    asyncio.run(manhattan._refresh_manhattan_cache_job("MHT=abc123"))

    assert manhattan._runtime_cookie == ""
    assert manhattan.active_cookie() == ""
    assert not Path(manhattan.COOKIE_FILE).exists()
    assert manhattan._refresh_status["stage"] == "error"


def test_another_cookie_failing_to_refresh_keeps_the_persisted_one(monkeypatch) -> None:
    _persist("SAVED=cookie")

    def _raise(*args, **kwargs):
        raise manhattan.HTTPException(401, "Manhattan login expired.")

    monkeypatch.setattr(manhattan, "_fetch_json", _raise)
    monkeypatch.setattr(manhattan, "_write_cache", lambda data: None)
    monkeypatch.setattr(manhattan, "REQUEST_DELAY_SECONDS", 0)

    asyncio.run(manhattan._refresh_manhattan_cache_job("OTHER=cookie"))

    assert manhattan.active_cookie() == "SAVED=cookie"
    assert Path(manhattan.COOKIE_FILE).exists()


def test_empty_cookie_is_rejected_before_any_upstream_call(monkeypatch) -> None:
    def _unexpected(**kwargs):
        raise AssertionError("should not call the upstream API")

    monkeypatch.setattr(manhattan.httpx, "AsyncClient", _unexpected)

    with pytest.raises(manhattan.HTTPException) as exc_info:
        asyncio.run(
            manhattan.set_manhattan_session(_FakeRequest({"cookie": "   "}), current_user=_FakeUser())
        )

    assert exc_info.value.status_code == 400
    assert not Path(manhattan.COOKIE_FILE).exists()


def test_corrupt_cookie_file_is_ignored_and_environment_is_used(monkeypatch) -> None:
    Path(manhattan.COOKIE_FILE).write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(manhattan.settings, "NMHT_COOKIE", "ENV=cookie")

    assert manhattan.active_cookie() == "ENV=cookie"
    assert manhattan.get_manhattan_session()["source"] == "env"


def test_session_write_and_clear_require_account_manage_permission() -> None:
    assert MANHATTAN_ROUTE_SOURCE.count('require_permission("account:manage")') == 2
