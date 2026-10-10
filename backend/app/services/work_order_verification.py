"""上游工单号真实性校验（盲标池建单门禁）。

背景：答疑助手（插件 0.5.8 及更早）会把「当前页面号码」同时填进
``conversationId`` 与 ``workOrderId``，于是上游的**会话号**也会被当成工单号
落到 ``retrieval_quality_events.question_form_id``，再被盲标池物化成工单 ——
标注员按工单号去上游「工单管理」查询只会看到「暂无数据」。

2026-10-10 用曼哈顿反代访问日志复核（见
``docs/blind-label-work-order-number-audit-20261010.md``）：1,292 条盲标工单里
216 条（16.7%）在上游只有聊天记录、工单详情查不到。

本模块提供建单前的真实性强校验：

* 上游明确「工单详情查不到」-> 返回 ``False``（拒绝建单）；
* 上游有工单详情数据 -> 返回 ``True``（放行，并把结论落库备查）；
* 没有 Cookie / 超时 / 上游异常 -> 返回 ``None``（**fail-open**，不拦截，
  避免把盲标池饿死；上游不可用时行为与改造前一致）。

判据来自实测：``GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<号>``
有数据时响应体 534–1128 字节，查不到时只有 123/124 字节
（``docs/qa-web-assistant-work-order-id-spec.md``；2026-10-10 又用已确认的真
工单号在真实日志里复核：639/650 字节 vs 聊天接口 68 字节）。
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

WORK_ORDER_DETAIL_PATH = "/nmhtapi/qa/queryQuestionFormDetail"
WORK_ORDER_DETAIL_SCENE_TYPE = 2
# 上游「查不到」时只返回 123/124 字节的表头；有数据时 534–1128 字节。
# 落在两者之间的响应对不上任何已知形态，一律按「无法判定」处理（不拦截）。
MISSING_RESPONSE_MAX_BYTES = 128
PRESENT_RESPONSE_MIN_BYTES = 300
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_CHECKS = 40
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
_HTML_MARKERS = ("<html", "<!doctype html", "统一登录平台")


def active_cookie() -> str:
    """当前生效的曼哈顿 Cookie（运行时粘贴的优先，其次环境变量）。

    运行时 Cookie 只保存在内存里，服务重启即失效；没有 Cookie 时返回空串，
    调用方据此跳过校验（fail-open）。
    """

    try:
        from app.routes.manhattan import active_cookie as manhattan_cookie
    except Exception:  # pragma: no cover - 校验是可选能力，导入失败不影响建单
        return ""
    try:
        return str(manhattan_cookie() or "").strip()
    except Exception:  # pragma: no cover
        return ""


def classify_work_order_detail_response(
    *,
    status_code: int,
    content: bytes | str | None,
    content_type: str = "",
) -> bool | None:
    """把上游响应归类为「工单存在 / 不存在 / 无法判定」。

    只使用实测过的响应体大小判据；登录页、401/403、非 200、空响应体一律
    返回 ``None``（不拦截建单）。
    """

    if status_code in (301, 302, 303, 307, 308, 401, 403):
        return None
    if status_code != 200:
        return None
    if content is None:
        return None
    if isinstance(content, bytes):
        body = content
    else:
        body = str(content).encode("utf-8", "ignore")
    size = len(body)
    if size <= 0:
        return None
    if "html" in str(content_type or "").lower():
        return None
    head = body[:400].decode("utf-8", "ignore").lstrip().lower()
    if head.startswith("<") or any(marker in head for marker in _HTML_MARKERS):
        return None
    if size <= MISSING_RESPONSE_MAX_BYTES:
        return False
    if size >= PRESENT_RESPONSE_MIN_BYTES:
        return True
    return None


def verify_question_form_id(
    question_form_id: str,
    *,
    cookie: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> bool | None:
    """向上游查一次工单详情，返回 ``True``/``False``/``None``。"""

    number = str(question_form_id or "").strip()
    if not number:
        return None
    active = cookie if cookie is not None else active_cookie()
    active = str(active or "").strip()
    if not active:
        return None
    url = settings.NMHT_BASE_URL.rstrip("/") + WORK_ORDER_DETAIL_PATH
    headers = {"User-Agent": "Mozilla/5.0", "Cookie": active}
    params = {
        "questionFormId": number,
        "sceneType": WORK_ORDER_DETAIL_SCENE_TYPE,
    }
    try:
        if client is not None:
            response = client.get(url, headers=headers, params=params, timeout=timeout)
        else:
            response = httpx.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
                follow_redirects=False,
            )
    except Exception as exc:  # 上游不可用时 fail-open
        logger.warning("Work order verification failed for %s: %s", number, exc)
        return None
    return classify_work_order_detail_response(
        status_code=response.status_code,
        content=response.content,
        content_type=response.headers.get("content-type", ""),
    )


class WorkOrderVerifier:
    """一次物化过程中的校验器：带缓存、带上限、失败即停用（fail-open）。"""

    def __init__(
        self,
        *,
        cookie: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_checks: int = DEFAULT_MAX_CHECKS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self._cookie = str(cookie if cookie is not None else active_cookie()).strip()
        self._client = client
        self._timeout = float(timeout)
        self._max_checks = max(0, int(max_checks))
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._cache: dict[str, bool | None] = {}
        self._checks = 0
        self._undecided_streak = 0
        self._disabled = not self._cookie
        self.stats = {"checks": 0, "present": 0, "missing": 0, "undecided": 0}

    @property
    def enabled(self) -> bool:
        return (
            not self._disabled
            and self._max_checks > 0
            and self._checks < self._max_checks
        )

    def verify(self, question_form_id: str) -> bool | None:
        number = str(question_form_id or "").strip()
        if not number:
            return None
        if number in self._cache:
            return self._cache[number]
        if not self.enabled:
            return None
        self._checks += 1
        verdict = verify_question_form_id(
            number,
            cookie=self._cookie,
            timeout=self._timeout,
            client=self._client,
        )
        self._cache[number] = verdict
        self.stats["checks"] += 1
        if verdict is True:
            self.stats["present"] += 1
            self._undecided_streak = 0
        elif verdict is False:
            self.stats["missing"] += 1
            self._undecided_streak = 0
        else:
            self.stats["undecided"] += 1
            self._undecided_streak += 1
            if self._undecided_streak >= self._max_consecutive_failures:
                self._disabled = True
                logger.warning(
                    "Work order verification disabled after %s undecided responses.",
                    self._undecided_streak,
                )
        return verdict
