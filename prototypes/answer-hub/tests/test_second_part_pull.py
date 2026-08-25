from __future__ import annotations

import json
from pathlib import Path

import pytest

import answer_hub.second_part_pull as second_part_pull_module
from answer_hub.automation_queue import (
    AutomationQueue,
    read_queue_job_metadata,
)
from answer_hub.cli import main
from answer_hub.excel_io import read_workbook_rows
from answer_hub.second_part_pull import (
    SecondPartPullError,
    SecondPartPullProfile,
    UrllibSecondPartPageFetcher,
    pull_second_part_to_queue,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_profile(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "second-part-test",
                "url": "https://second-part.example/api/records",
                "method": "GET",
                "batch_size": 100,
                "cursor_param": "cursor",
                "limit_param": "limit",
                "response": {
                    "items_path": "data.items",
                    "next_cursor_path": "data.next_cursor",
                    "has_more_path": "data.has_more",
                },
                "field_map": {
                    "工单ID": "work_order_id",
                    "聊天内容": "conversation",
                    "图片链接": "image_urls",
                    "产品类型": "product_type",
                    "历史实际回复": "actual_reply",
                },
                "defaults": {
                    "上传者": "第二部分接口",
                },
                "workflow": {
                    "use_mimo": True,
                    "clustering_mode": "direct_mimo",
                    "sync_to_cz_review": True,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class FakeFetcher:
    def __init__(self, pages: dict[str, dict], failures: set[str] | None = None):
        self.pages = pages
        self.failures = failures or set()
        self.cursors: list[str] = []

    def fetch_page(self, _profile, cursor: str) -> dict:
        self.cursors.append(cursor)
        if cursor in self.failures:
            raise SecondPartPullError(f"failed at {cursor}")
        return self.pages[cursor]


def test_pull_second_part_page_maps_records_and_queues_workbook(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path / "profile.json")
    fetcher = FakeFetcher(
        {
            "": {
                "data": {
                    "items": [
                        {
                            "work_order_id": "WO-001",
                            "conversation": "屏幕有亮线，应该怎么判断？",
                            "image_urls": [
                                "https://example.com/a.jpg",
                                "https://example.com/b.jpg",
                            ],
                            "product_type": "手机",
                            "actual_reply": "请补充白屏图片后核验。",
                        },
                        {
                            "work_order_id": "WO-002",
                            "conversation": "电池健康度显示评估中怎么处理？",
                            "image_urls": [],
                            "product_type": "手机",
                            "actual_reply": "请进入电池设置页核对。",
                        },
                    ],
                    "next_cursor": "cursor-2",
                    "has_more": False,
                }
            }
        }
    )

    summary = pull_second_part_to_queue(
        profile,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        state_path=tmp_path / "pull-state.json",
        fetcher=fetcher,
    )

    assert summary["status"] == "queued"
    assert summary["fetched_records"] == 2
    assert summary["queued_jobs"] == 1
    assert summary["reused_jobs"] == 0
    assert summary["cursor"] == "cursor-2"
    assert fetcher.cursors == [""]

    queue = AutomationQueue(tmp_path / "queue")
    workbook = next(queue.pending.glob("*.xlsx"))
    columns, rows = read_workbook_rows(workbook)
    assert "工单ID" in columns
    assert rows[0]["工单ID"] == "WO-001"
    assert rows[0]["图片链接"] == (
        "https://example.com/a.jpg\nhttps://example.com/b.jpg"
    )
    assert rows[0]["上传者"] == "第二部分接口"
    assert rows[1]["历史实际回复"] == "请进入电池设置页核对。"

    metadata = read_queue_job_metadata(workbook)
    assert metadata["options"]["clustering_mode"] == "direct_mimo"
    assert metadata["options"]["sync_to_cz_review"] is True
    assert metadata["options"]["source_system"] == "second-part-test"
    assert metadata["options"]["source_batch_key"]

    state = json.loads((tmp_path / "pull-state.json").read_text(encoding="utf-8"))
    assert state["cursor"] == "cursor-2"


def test_pull_reuses_existing_batch_when_state_file_is_lost(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path / "profile.json")
    page = {
        "data": {
            "items": [
                {
                    "work_order_id": "WO-001",
                    "conversation": "屏幕有亮线，应该怎么判断？",
                    "image_urls": [],
                    "product_type": "手机",
                    "actual_reply": "",
                }
            ],
            "next_cursor": "cursor-2",
            "has_more": False,
        }
    }
    fetcher = FakeFetcher({"": page})
    state_path = tmp_path / "pull-state.json"

    first = pull_second_part_to_queue(
        profile,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        state_path=state_path,
        fetcher=fetcher,
    )
    state_path.unlink()
    second = pull_second_part_to_queue(
        profile,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        state_path=state_path,
        fetcher=FakeFetcher({"": page}),
    )

    queue = AutomationQueue(tmp_path / "queue")
    assert first["queued_jobs"] == 1
    assert second["queued_jobs"] == 0
    assert second["reused_jobs"] == 1
    assert len(list(queue.pending.glob("*.xlsx"))) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["cursor"] == "cursor-2"


def test_pull_advances_only_through_successfully_queued_pages(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path / "profile.json")
    fetcher = FakeFetcher(
        {
            "": {
                "data": {
                    "items": [
                        {
                            "work_order_id": "WO-001",
                            "conversation": "屏幕有亮线，应该怎么判断？",
                            "image_urls": [],
                            "product_type": "手机",
                            "actual_reply": "",
                        }
                    ],
                    "next_cursor": "cursor-2",
                    "has_more": True,
                }
            }
        },
        failures={"cursor-2"},
    )
    state_path = tmp_path / "pull-state.json"

    with pytest.raises(SecondPartPullError, match="failed at cursor-2"):
        pull_second_part_to_queue(
            profile,
            queue_root=tmp_path / "queue",
            output_root=tmp_path / "runs",
            state_path=state_path,
            max_pages=5,
            fetcher=fetcher,
        )

    queue = AutomationQueue(tmp_path / "queue")
    assert len(list(queue.pending.glob("*.xlsx"))) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["cursor"] == "cursor-2"


def test_pull_rejects_records_missing_profile_required_fields(
    tmp_path: Path,
) -> None:
    profile = _write_profile(tmp_path / "profile.json")
    payload = json.loads(profile.read_text(encoding="utf-8"))
    payload["required_fields"] = ["工单ID", "聊天内容", "产品类型"]
    profile.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fetcher = FakeFetcher(
        {
            "": {
                "data": {
                    "items": [
                        {
                            "work_order_id": "WO-MISSING-001",
                            "conversation": "手机无法充电怎么处理？",
                            "product_type": "",
                        }
                    ],
                    "next_cursor": "",
                    "has_more": False,
                }
            }
        }
    )

    summary = pull_second_part_to_queue(
        profile,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        state_path=tmp_path / "pull-state.json",
        fetcher=fetcher,
    )

    assert summary["status"] == "rejected"
    assert summary["fetched_records"] == 1
    assert summary["queued_jobs"] == 0
    assert summary["rejected_records"] == 1
    assert len(summary["rejection_reports"]) == 1
    report = json.loads(
        Path(summary["rejection_reports"][0]["path"]).read_text(encoding="utf-8")
    )
    assert report["rejected_records"] == [
        {
            "source_index": 1,
            "source_record_id": "WO-MISSING-001",
            "missing_required_fields": ["产品类型"],
        }
    ]
    assert not list((tmp_path / "queue" / "pending").glob("*.xlsx"))


def test_http_fetcher_injects_auth_and_cursor_without_persisting_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    profile_payload = json.loads(profile_path.read_text(encoding="utf-8"))
    profile_payload["headers"] = {
        "Authorization": "Bearer ${SECOND_PART_API_TOKEN}",
    }
    profile_payload["params"] = {"status": "completed"}
    profile_path.write_text(
        json.dumps(profile_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("SECOND_PART_API_TOKEN", "private-test-token")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":{"items":[],"next_cursor":"","has_more":false}}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(second_part_pull_module, "urlopen", fake_urlopen)
    profile = SecondPartPullProfile.load(profile_path)

    payload = UrllibSecondPartPageFetcher().fetch_page(
        profile,
        "cursor-1",
    )

    request = captured["request"]
    assert payload["data"]["items"] == []
    assert "cursor=cursor-1" in request.full_url
    assert "limit=100" in request.full_url
    assert "status=completed" in request.full_url
    assert request.get_header("Authorization") == (
        "Bearer private-test-token"
    )
    assert captured["timeout"] == 30.0


def test_powerzhuan_profile_uses_documented_query_contract(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SECOND_PART_API_TOKEN", "private-test-token")
    monkeypatch.setenv("SECOND_PART_QUERY_FROM_DATE", "2026-08-08")
    monkeypatch.setenv("SECOND_PART_QUERY_TO_DATE", "2026-08-10")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"records":[]}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(second_part_pull_module, "urlopen", fake_urlopen)
    profile = SecondPartPullProfile.load(
        PROJECT_ROOT
        / "config"
        / "second-part-pull.powerzhuan.example.json"
    )
    payload = UrllibSecondPartPageFetcher().fetch_page(profile, "")

    assert profile.url == "https://qa.powerzhuan.cn/api/records"
    assert profile.items_path == "records"
    assert profile.required_fields == ("工单ID", "聊天内容", "产品类型")
    assert payload == {"records": []}
    request = captured["request"]
    assert "from=2026-08-08" in request.full_url
    assert "to=2026-08-10" in request.full_url
    assert "limit=1000" in request.full_url
    assert request.get_header("Authorization") == "Bearer private-test-token"
    assert captured["timeout"] == 30.0


def test_powerzhuan_profile_maps_observed_record_fields(
    tmp_path: Path,
) -> None:
    class PowerZhuanFetcher:
        def fetch_page(self, _profile, _cursor: str) -> dict:
            return {
                "records": [
                    {
                        "id": "record-001",
                        "analyze_time": "2026-08-10T11:00:00+08:00",
                        "ticket_id": "TICKET-001",
                        "order_no": "ORDER-001",
                        "category": "平板电脑",
                        "model": "Tablet Model",
                        "chat_content": "平板无法充电如何排查？",
                        "chat_images": [
                            "https://example.com/image-1.jpg",
                            "https://example.com/image-2.jpg",
                        ],
                        "chat_videos": ["https://example.com/video-1.mp4"],
                        "ai_result": "待人工确认",
                        "uploaded_by": "qa-operator",
                        "created_at": "2026-08-10T10:00:00+08:00",
                    }
                ]
            }

    summary = pull_second_part_to_queue(
        PROJECT_ROOT
        / "config"
        / "second-part-pull.powerzhuan.example.json",
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        state_path=tmp_path / "state.json",
        fetcher=PowerZhuanFetcher(),
    )

    assert summary["status"] == "queued"
    workbook = next((tmp_path / "queue" / "pending").glob("*.xlsx"))
    _columns, rows = read_workbook_rows(workbook)
    assert rows[0]["上传者"] == "qa-operator"
    assert rows[0]["分析时间"] == "2026-08-10T11:00:00+08:00"
    assert rows[0]["工单ID"] == "TICKET-001"
    assert rows[0]["回收单号"] == "ORDER-001"
    assert rows[0]["产品类型"] == "平板电脑"
    assert rows[0]["聊天内容"] == "平板无法充电如何排查？"
    assert rows[0]["图片链接"] == (
        "https://example.com/image-1.jpg\n"
        "https://example.com/image-2.jpg"
    )
    assert rows[0]["视频链接"] == "https://example.com/video-1.mp4"
    assert rows[0]["ai_result"] == "待人工确认"


def test_cli_second_part_pull_queues_batch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = _write_profile(tmp_path / "profile.json")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "data": {
                        "items": [
                            {
                                "work_order_id": "WO-CLI-001",
                                "conversation": "手机无法充电怎么处理？",
                                "image_urls": [],
                                "product_type": "手机",
                                "actual_reply": "请先检查充电器和线材。",
                            }
                        ],
                        "next_cursor": "cli-cursor-2",
                        "has_more": False,
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")

    monkeypatch.setattr(
        second_part_pull_module,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    exit_code = main(
        [
            "second-part-pull",
            "--profile",
            str(profile),
            "--queue-dir",
            str(tmp_path / "queue"),
            "--output-dir",
            str(tmp_path / "runs"),
            "--state-file",
            str(tmp_path / "state.json"),
            "--max-pages",
            "2",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "queued"
    assert output["fetched_records"] == 1
    assert len(list((tmp_path / "queue" / "pending").glob("*.xlsx"))) == 1
