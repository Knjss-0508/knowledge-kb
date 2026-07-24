from __future__ import annotations

from io import BytesIO
import json
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError

from answer_hub.cz_integration import (
    CzIntegrationAdapter,
    CzIntegrationConfig,
    select_submittable_candidates,
)


class _JsonResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _candidate(index: int = 1, product_type: str = "手机") -> dict:
    return {
        "主题ID": f"TOP-{index:03d}",
        "主标题": f"候选知识{index}",
        "副标题": "副标题A；副标题B",
        "知识内容": f"这是第{index}条知识内容。",
        "知识分类": "检测方法",
        "产品类型": product_type,
        "适用范围": f"{product_type}-通用",
        "推荐回复": f"您好，请按第{index}条流程处理。",
        "是否值得沉淀": "是",
        "是否可用": "是",
        "主题来源记录ID": f"R-{index:03d}",
        "主题问题意图": "查询",
        "主题对象/部位": "屏幕",
        "主题异常现象": "显示异常",
        "主题置信度": 0.91,
        "来源版本": "qc-20260721",
    }


class CzIntegrationTests(unittest.TestCase):
    def test_readiness_exposes_real_api_state(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        readiness = adapter.readiness()

        self.assertTrue(readiness["configured"])
        self.assertEqual(readiness["status"], "已配置")
        self.assertEqual(readiness["taxonomy_endpoint"], "/api/v1/integration/taxonomy")
        self.assertEqual(
            readiness["review_candidate_endpoint"],
            "/api/v1/integration/knowledge-review-candidates:batch",
        )

    def test_endpoint_normalizes_api_prefix_and_trailing_slash(self) -> None:
        config = CzIntegrationConfig("https://kb.example/api/v1/", "test-key")

        self.assertEqual(
            config.endpoint("/api/v1/integration/taxonomy"),
            "https://kb.example/api/v1/integration/taxonomy",
        )
        self.assertEqual(
            config.endpoint("health"),
            "https://kb.example/api/v1/health",
        )

    def test_unmapped_category_blocks_local_payload(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        candidate = {"主题ID": "TOP-001", "主标题": "机型查询流程", "知识内容": "查询步骤", "知识分类": "检测方法"}

        with self.assertRaisesRegex(ValueError, "未映射 category_id"):
            adapter.build_batch_payload([candidate], {})

    def test_candidate_payload_includes_recommended_reply_and_review_evidence(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(), "如何修改": "精简步骤", "问题反馈": "措辞偏长"}],
            {"手机": "cat-phone"},
        )[0]

        self.assertEqual(payload["knowledge"]["recommended_reply"], "您好，请按第1条流程处理。")
        self.assertEqual(payload["knowledge"]["category_id"], "cat-phone")
        self.assertEqual(payload["knowledge"]["applicable_categories"], ["手机"])
        self.assertEqual(payload["knowledge"]["scene_tags"], ["查询", "屏幕", "显示异常"])
        self.assertTrue(payload["selection"]["eligible"])
        self.assertIn("如何修改：精简步骤", payload["selection"]["reasons"])
        self.assertIn("问题反馈：措辞偏长", payload["selection"]["reasons"])
        self.assertEqual(
            payload["processing"]["plugin_name"],
            "answer-hub-topic-transcription",
        )
        self.assertEqual(payload["processing"]["plugin_version"], "2026-07-22")
        self.assertNotIn("skill_name", payload["processing"])
        self.assertNotIn("skill_version", payload["processing"])
        self.assertNotIn("layer", payload["knowledge"])
        self.assertNotIn("applicable_business_types", payload["knowledge"])

    def test_candidate_payload_carries_model_and_human_review_metadata(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [
                {
                    **_candidate(),
                    "模型初标状态": "topic_initial_reviewed_model",
                    "模型初标结论": "建议沉淀",
                    "模型初标是否值得沉淀": "是",
                    "模型初标置信度": "0.93",
                    "模型初标重点复核": "是",
                    "模型初标原因": "案例证据充分",
                    "如何修改": "精简首段",
                    "问题反馈": "标题可更具体",
                    "审核结论": "修改后通过",
                    "是否进入训练集": "是",
                }
            ],
            {"手机": "cat-phone"},
        )[0]

        self.assertEqual(payload["model_review"]["knowledge_value"], "worthy")
        self.assertEqual(payload["model_review"]["confidence"], 0.93)
        self.assertTrue(payload["model_review"]["priority_review"])
        self.assertEqual(payload["human_review"]["decision"], "approved_with_changes")
        self.assertEqual(payload["human_review"]["modification_notes"], "精简首段")
        self.assertEqual(payload["human_review"]["training_eligible"], "是")

    def test_existing_standard_reference_is_preserved_as_a_review_hold(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(), "关联标准项": "STD-OLD-001"}],
            {"手机": "cat-phone"},
            require_eligible=False,
        )[0]

        self.assertFalse(payload["selection"]["eligible"])
        self.assertIn(
            "已有标准关联（仅审计，未自动映射）：STD-OLD-001",
            payload["selection"]["reasons"],
        )

        with self.assertRaisesRegex(ValueError, "已有标准关联"):
            adapter.build_batch_payload(
                [{**_candidate(), "关联标准项": "STD-OLD-001"}],
                {"手机": "cat-phone"},
            )

    def test_select_submittable_candidates_maps_simple_teammate_review(self) -> None:
        selected = select_submittable_candidates(
            [
                {**_candidate(1), "是否可用": "是", "如何修改": ""},
                {**_candidate(2), "是否可用": "是", "如何修改": "精简"},
                {**_candidate(3), "是否可用": "否"},
            ]
        )

        self.assertEqual(
            [candidate["审核结论"] for candidate in selected],
            ["通过", "修改后通过"],
        )

    def test_not_worth_depositing_candidate_is_not_submittable(self) -> None:
        selected = select_submittable_candidates(
            [{**_candidate(), "是否值得沉淀": "否", "是否可用": "是"}]
        )

        self.assertEqual(selected, [])

    def test_build_payload_rejects_missing_deposition_annotation(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        with self.assertRaisesRegex(ValueError, "尚未标注为值得沉淀"):
            adapter.build_batch_payload(
                [{**_candidate(), "是否值得沉淀": ""}],
                {"手机": "cat-phone"},
            )

    def test_review_queue_payload_allows_pending_human_annotation(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(), "是否值得沉淀": "", "是否可用": "", "审核结论": ""}],
            {"手机": "cat-phone"},
            require_eligible=False,
        )[0]

        self.assertFalse(payload["selection"]["eligible"])
        self.assertEqual(payload["human_review"]["knowledge_value"], "pending")
        self.assertEqual(payload["human_review"]["usability"], "pending")

    def test_candidate_idempotency_is_stable_when_reviewers_edit_content(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        first = adapter.build_batch_payload(
            [_candidate()],
            {"手机": "cat-phone"},
        )[0]
        second = adapter.build_batch_payload(
            [
                {
                    **_candidate(),
                    "主标题": "审核后更新的标题",
                    "知识内容": "审核后更新的正文",
                    "如何修改": "已完成修改",
                }
            ],
            {"手机": "cat-phone"},
        )[0]

        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertNotEqual(
            first["selection"]["duplicate_fingerprint"],
            second["selection"]["duplicate_fingerprint"],
        )

    def test_fetch_all_qc_standards_merges_stable_snapshot_pages(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        adapter.fetch_qc_standard_snapshot = Mock(
            side_effect=[
                {
                    "snapshot_version": "qc-v1",
                    "generated_at": "2026-07-21T10:00:00",
                    "items": [{"standard_id": "STD-1"}],
                    "next_offset": 500,
                },
                {
                    "snapshot_version": "qc-v1",
                    "generated_at": "2026-07-21T10:00:00",
                    "items": [{"standard_id": "STD-2"}],
                    "next_offset": None,
                },
            ]
        )

        snapshot = adapter.fetch_all_qc_standards("cat-phone")

        self.assertEqual(snapshot["snapshot_version"], "qc-v1")
        self.assertEqual(snapshot["total_items"], 2)
        self.assertEqual(
            adapter.fetch_qc_standard_snapshot.call_args_list,
            [
                call(category_id="cat-phone", limit=500, offset=0),
                call(category_id="cat-phone", limit=500, offset=500),
            ],
        )

    def test_second_part_idempotency_is_stable_for_dict_key_order(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        first = adapter.build_second_part_payload(
            [{"事件ID": "EVT-1", "产品类型": "手机", "核心问题": "屏幕异常"}]
        )[0]
        second = adapter.build_second_part_payload(
            [{"核心问题": "屏幕异常", "产品类型": "手机", "事件ID": "EVT-1"}]
        )[0]

        self.assertEqual(first["idempotency_key"], second["idempotency_key"])

    def test_submit_candidates_splits_batches_at_one_hundred(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        request_sizes: list[int] = []

        def fake_request(method, path, payload, **kwargs):
            request_sizes.append(len(payload["items"]))
            return {
                "accepted": len(payload["items"]),
                "rejected": 0,
                "reused": 0,
                "intercepted": 1 if len(payload["items"]) == 5 else 0,
                "blocked": 0,
                "results": [],
            }

        adapter._request_json = Mock(side_effect=fake_request)

        result = adapter.submit_candidates(
            [_candidate(index) for index in range(1, 206)],
            {"手机": "cat-phone"},
        )

        self.assertEqual(request_sizes, [100, 100, 5])
        self.assertEqual(result["accepted"], 205)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(result["intercepted"], 1)
        self.assertEqual(result["blocked"], 0)

    def test_sync_review_candidates_splits_batches_at_one_hundred(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        request_sizes: list[int] = []
        request_paths: list[str] = []

        def fake_request(method, path, payload, **kwargs):
            del method, kwargs
            request_paths.append(path)
            request_sizes.append(len(payload["items"]))
            return {
                "queued": 0,
                "ready": len(payload["items"]),
                "rejected": 0,
                "reused": 0,
                "results": [],
            }

        adapter._request_json = Mock(side_effect=fake_request)

        result = adapter.sync_review_candidates(
            [_candidate(index) for index in range(1, 206)],
            {"手机": "cat-phone"},
        )

        self.assertEqual(request_sizes, [100, 100, 5])
        self.assertEqual(
            request_paths,
            [adapter.review_candidates_path] * 3,
        )
        self.assertEqual(result["ready"], 205)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["rejected"], 0)

    def test_sync_review_candidates_isolates_local_validation_failures(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        request_event_ids: list[list[str]] = []

        def fake_request(method, path, payload, **kwargs):
            del method, path, kwargs
            request_event_ids.append(
                [item["event_id"] for item in payload["items"]]
            )
            return {
                "queued": 0,
                "ready": len(payload["items"]),
                "rejected": 0,
                "reused": 0,
                "results": [],
            }

        adapter._request_json = Mock(side_effect=fake_request)

        result = adapter.sync_review_candidates(
            [
                _candidate(1),
                _candidate(2, product_type="未知品类"),
                _candidate(3),
            ],
            {"手机": "cat-phone"},
        )

        self.assertEqual(request_event_ids, [["TOP-001", "TOP-003"]])
        self.assertEqual(result["ready"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["results"][0]["event_id"], "TOP-002")
        self.assertEqual(result["results"][0]["status"], "failed")

    def test_sync_review_candidates_isolates_remote_batch_validation_failures(
        self,
    ) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        request_event_ids: list[list[str]] = []

        def fake_request(method, path, payload, **kwargs):
            del method, path, kwargs
            event_ids = [item["event_id"] for item in payload["items"]]
            request_event_ids.append(event_ids)
            if "TOP-002" in event_ids:
                raise RuntimeError("CZ接口调用失败：HTTP 422：invalid candidate")
            return {
                "queued": 0,
                "ready": len(event_ids),
                "rejected": 0,
                "reused": 0,
                "results": [],
            }

        adapter._request_json = Mock(side_effect=fake_request)

        result = adapter.sync_review_candidates(
            [_candidate(1), _candidate(2), _candidate(3)],
            {"手机": "cat-phone"},
        )

        self.assertEqual(result["ready"], 2)
        self.assertEqual(result["failed"], 1)
        failed = next(
            item for item in result["results"] if item["status"] == "failed"
        )
        self.assertEqual(failed["event_id"], "TOP-002")
        self.assertIn(["TOP-001", "TOP-002", "TOP-003"], request_event_ids)
        self.assertIn(["TOP-001"], request_event_ids)
        self.assertIn(["TOP-003"], request_event_ids)

    def test_submit_second_part_records_batches_case_only_generation(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        request_sizes: list[int] = []

        def fake_request(method, path, payload, **kwargs):
            del method, path, kwargs
            size = len(payload["items"])
            request_sizes.append(size)
            return {
                "accepted": size,
                "reused": 0,
                "rejected": 0,
                "protected": 0,
                "source_total_rows": size,
                "topic_rows": size,
                "topic_imported": size,
                "topic_refreshed": 0,
                "topic_skipped": 0,
                "knowledge_mode": "case_only",
                "standard_references_enabled": False,
                "results": [],
            }

        adapter._request_json = Mock(side_effect=fake_request)
        result = adapter.submit_second_part_records(
            [
                {
                    "事件ID": f"EVT-{index:03d}",
                    "聊天内容": f"第{index}条脱敏会话",
                    "产品类型": "手机",
                }
                for index in range(205)
            ]
        )

        self.assertEqual(request_sizes, [100, 100, 5])
        self.assertEqual(result["accepted"], 205)
        self.assertEqual(result["topic_imported"], 205)
        self.assertEqual(result["knowledge_mode"], "case_only")
        self.assertFalse(result["standard_references_enabled"])

    def test_transient_http_error_is_retried(self) -> None:
        adapter = CzIntegrationAdapter(
            CzIntegrationConfig(
                "https://kb.example",
                "test-key",
                max_retries=1,
                retry_backoff_seconds=0,
            )
        )
        transient_error = HTTPError(
            "https://kb.example/api/v1/integration/taxonomy",
            503,
            "Service unavailable",
            None,
            BytesIO(b'{"detail":"busy"}'),
        )

        with (
            patch(
                "answer_hub.cz_integration.urlopen",
                side_effect=[transient_error, _JsonResponse({"categories": []})],
            ) as mocked_urlopen,
            patch("answer_hub.cz_integration.time.sleep") as mocked_sleep,
        ):
            payload = adapter.fetch_taxonomy()

        self.assertEqual(payload, {"categories": []})
        self.assertEqual(mocked_urlopen.call_count, 2)
        mocked_sleep.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
