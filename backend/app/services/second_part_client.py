from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from app.core.config import settings
except ModuleNotFoundError:  # Allows pure client tests without backend extras.
    class _Settings:
        SECOND_PART_API_URL = ""
        SECOND_PART_API_TOKEN = ""
        SECOND_PART_API_TIMEOUT_SECONDS = 30.0
        SECOND_PART_API_QUERY_LIMIT = 10000

    settings = _Settings()


# The source currently returns at most 5,000 rows even when `limit=10000`.
# It provides neither total-count nor pagination metadata, so a response at
# this boundary is split by date to avoid silently undercounting high-frequency
# questions. A single day that still reaches the boundary remains flagged.
_SOURCE_RESPONSE_CAP = 5000
_MAX_DATE_SPLIT_DEPTH = 16
_DATE_FETCH_MAX_WORKERS = 4


class SecondPartClientError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SecondPartFetchResult:
    records: list[dict]
    possibly_truncated: bool
    request_count: int


def _fetch_once(from_date: str, to_date: str) -> list[dict]:
    params = {"limit": max(1, min(int(settings.SECOND_PART_API_QUERY_LIMIT), 10000))}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    request = Request(
        settings.SECOND_PART_API_URL + "?" + urlencode(params),
        headers={
            "Authorization": f"Bearer {settings.SECOND_PART_API_TOKEN}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(
            request,
            timeout=max(1.0, settings.SECOND_PART_API_TIMEOUT_SECONDS),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise SecondPartClientError(
                "第二部分接口鉴权失败，请更新 SECOND_PART_API_TOKEN 后再查询。",
                502,
            ) from exc
        raise SecondPartClientError("第二部分接口暂时不可用，请稍后重试。", 502) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise SecondPartClientError("第二部分接口暂时不可用，请稍后重试。", 502) from exc

    rows = payload.get("records", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise SecondPartClientError("第二部分接口返回格式异常，请联系管理员检查接口。", 502)
    return rows


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _fetch_date_windows(
    from_day: date,
    to_day: date,
) -> tuple[list[dict], bool, int]:
    """Fetch a date range completely without serially waiting for each split.

    The source has no pagination metadata and caps a response around 5,000
    records. Windows that reach that cap are bisected. Each split level runs
    with bounded concurrency so a seven-day dashboard query does not turn into
    a long sequence of independent upstream waits.
    """

    pending = [(from_day, to_day, 0)]
    records: list[dict] = []
    possibly_truncated = False
    request_count = 0
    with ThreadPoolExecutor(max_workers=_DATE_FETCH_MAX_WORKERS) as executor:
        while pending:
            current, pending = pending, []
            windows_and_rows = list(
                executor.map(
                    lambda item: (
                        item,
                        _fetch_once(item[0].isoformat(), item[1].isoformat()),
                    ),
                    current,
                )
            )
            request_count += len(windows_and_rows)
            for (window_from, window_to, depth), rows in windows_and_rows:
                if len(rows) < _SOURCE_RESPONSE_CAP:
                    records.extend(rows)
                    continue
                if window_from >= window_to or depth >= _MAX_DATE_SPLIT_DEPTH:
                    records.extend(rows)
                    possibly_truncated = True
                    continue

                day_count = (window_to - window_from).days
                middle = window_from + timedelta(days=day_count // 2)
                pending.extend(
                    [
                        (window_from, middle, depth + 1),
                        (middle + timedelta(days=1), window_to, depth + 1),
                    ]
                )

    return records, possibly_truncated, request_count


def fetch_records(from_date: str = "", to_date: str = "") -> SecondPartFetchResult:
    if not settings.SECOND_PART_API_URL:
        raise SecondPartClientError(
            "第二部分接口地址尚未配置，请设置 SECOND_PART_API_URL 后再查询。",
            503,
        )
    if not settings.SECOND_PART_API_TOKEN:
        raise SecondPartClientError(
            "第二部分接口凭据尚未配置，请设置 SECOND_PART_API_TOKEN 后再查询。",
            503,
        )

    from_day = _parse_date(from_date)
    to_day = _parse_date(to_date)
    if from_date and to_date and from_day and to_day:
        if from_day > to_day:
            raise SecondPartClientError("开始日期不能晚于结束日期。", 422)
        rows, possibly_truncated, request_count = _fetch_date_windows(from_day, to_day)
        return SecondPartFetchResult(rows, possibly_truncated, request_count)

    rows = _fetch_once(from_date, to_date)
    return SecondPartFetchResult(
        records=rows,
        possibly_truncated=len(rows) >= _SOURCE_RESPONSE_CAP,
        request_count=1,
    )
