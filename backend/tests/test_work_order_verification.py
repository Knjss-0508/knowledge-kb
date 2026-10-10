"""上游工单号真实性校验的单元测试（不访问网络）。"""

import pytest

from app.services import work_order_verification as wov


class _Response:
    def __init__(self, status_code: int, content: bytes, content_type: str = "application/json"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _Client:
    """记录请求并按脚本返回响应的假 httpx 客户端。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setattr(wov.settings, "NMHT_BASE_URL", "https://nmht.example.com", raising=False)


def test_classify_uses_the_measured_response_sizes():
    # 实测：上游「查不到」只有 123/124 字节，有数据时 534–1128 字节。
    assert wov.classify_work_order_detail_response(status_code=200, content=b"x" * 124) is False
    assert wov.classify_work_order_detail_response(status_code=200, content=b"x" * 128) is False
    assert wov.classify_work_order_detail_response(status_code=200, content=b"x" * 639) is True
    assert wov.classify_work_order_detail_response(status_code=200, content=b"x" * 1128) is True
    # 落在两个实测区间之间：形态未知，一律不拦截。
    assert wov.classify_work_order_detail_response(status_code=200, content=b"x" * 200) is None


def test_classify_treats_login_and_broken_responses_as_undecided():
    assert wov.classify_work_order_detail_response(status_code=401, content=b"x" * 60) is None
    assert wov.classify_work_order_detail_response(status_code=302, content=b"x" * 60) is None
    assert wov.classify_work_order_detail_response(status_code=500, content=b"x" * 60) is None
    assert wov.classify_work_order_detail_response(status_code=200, content=b"") is None
    assert wov.classify_work_order_detail_response(status_code=200, content=None) is None
    assert (
        wov.classify_work_order_detail_response(
            status_code=200,
            content="<html><body>统一登录平台</body></html>".encode("utf-8") * 10,
        )
        is None
    )
    assert (
        wov.classify_work_order_detail_response(
            status_code=200,
            content=b"x" * 639,
            content_type="text/html; charset=utf-8",
        )
        is None
    )


def test_verify_question_form_id_skips_the_upstream_without_a_cookie():
    client = _Client([])
    assert wov.verify_question_form_id("2108536379561477121", cookie="", client=client) is None
    assert wov.verify_question_form_id("", cookie="SESSION=x", client=client) is None
    assert client.calls == []


def test_verify_question_form_id_asks_the_work_order_detail_endpoint():
    client = _Client([_Response(200, b"x" * 639)])
    verdict = wov.verify_question_form_id(
        "2108536379561477121",
        cookie="SESSION=x",
        timeout=2.0,
        client=client,
    )
    assert verdict is True
    call = client.calls[0]
    assert call["url"].endswith(wov.WORK_ORDER_DETAIL_PATH)
    assert call["params"] == {"questionFormId": "2108536379561477121", "sceneType": 2}
    assert call["headers"]["Cookie"] == "SESSION=x"
    assert call["timeout"] == 2.0


def test_verify_question_form_id_fails_open_on_upstream_errors():
    client = _Client([RuntimeError("boom")])
    assert wov.verify_question_form_id("2108536379561477121", cookie="SESSION=x", client=client) is None


def test_verifier_is_disabled_without_a_cookie():
    verifier = wov.WorkOrderVerifier(cookie="", client=_Client([]))
    assert verifier.enabled is False
    assert verifier.verify("2108536379561477121") is None
    assert verifier.stats["checks"] == 0


def test_verifier_caches_verdicts_and_respects_the_check_budget():
    client = _Client([_Response(200, b"x" * 639), _Response(200, b"x" * 124)])
    verifier = wov.WorkOrderVerifier(cookie="SESSION=x", client=client, max_checks=2)
    assert verifier.verify("1111111111111111111") is True
    assert verifier.verify("1111111111111111111") is True  # 命中缓存，不重复请求
    assert verifier.verify("2222222222222222222") is False
    assert len(client.calls) == 2
    # 预算用尽后不再请求上游（fail-open）
    assert verifier.enabled is False
    assert verifier.verify("3333333333333333333") is None
    assert len(client.calls) == 2
    assert verifier.stats == {"checks": 2, "present": 1, "missing": 1, "undecided": 0}


def test_verifier_disables_itself_after_consecutive_undecided_responses():
    client = _Client([_Response(200, b"x" * 200) for _ in range(3)] + [_Response(200, b"x" * 639)])
    verifier = wov.WorkOrderVerifier(
        cookie="SESSION=x",
        client=client,
        max_checks=10,
        max_consecutive_failures=3,
    )
    for index in range(3):
        assert verifier.verify(f"100000000000000000{index}") is None
    assert verifier.enabled is False
    # 上游刚恢复也不再尝试，本轮剩余事件按改造前行为处理。
    assert verifier.verify("4444444444444444444") is None
    assert len(client.calls) == 3
    assert verifier.stats["undecided"] == 3


def test_verifier_resets_the_failure_streak_on_a_decided_response():
    client = _Client(
        [
            _Response(200, b"x" * 200),
            _Response(200, b"x" * 639),
            _Response(200, b"x" * 200),
        ]
    )
    verifier = wov.WorkOrderVerifier(
        cookie="SESSION=x",
        client=client,
        max_checks=10,
        max_consecutive_failures=2,
    )
    assert verifier.verify("1000000000000000001") is None
    assert verifier.verify("1000000000000000002") is True
    assert verifier.verify("1000000000000000003") is None
    assert verifier.enabled is True
