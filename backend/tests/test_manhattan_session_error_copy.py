"""曼哈顿 Cookie 验证失败时，页面必须给出可操作的中文原因。

这组文案曾只存在于线上镜像、未回填仓库，导致每次部署都要手工保留一份
``manhattan.py``。用测试锁住，避免再次分叉。
"""

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.routes import manhattan


class _FakeRequest:
    async def json(self):
        return {"cookie": "SESSION=test"}


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            httpx.ConnectTimeout("boom"),
            "知识库服务器连接曼哈顿后台超时，请检查容器网络后重试。",
        ),
        (httpx.ReadTimeout("boom"), "曼哈顿后台响应超时，请稍后重试。"),
        (
            httpx.ConnectError("boom"),
            "知识库服务器无法连接曼哈顿后台，请检查容器网络后重试。",
        ),
        (
            httpx.NetworkError("boom"),
            "知识库服务器与曼哈顿后台网络通信失败，请稍后重试。",
        ),
        (httpx.HTTPError("boom"), "曼哈顿后台验证请求失败，请稍后重试。"),
    ],
)
def test_session_verification_error_is_actionable_chinese(
    monkeypatch, exc, expected
) -> None:
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise exc

    monkeypatch.setattr(manhattan.httpx, "AsyncClient", _Client)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            manhattan.set_manhattan_session(
                _FakeRequest(), current_user=object()
            )
        )

    assert caught.value.status_code == 502
    assert caught.value.detail == expected
