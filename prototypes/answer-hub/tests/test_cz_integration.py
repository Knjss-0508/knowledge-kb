from __future__ import annotations

import ast
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError

from answer_hub.cz_integration import (
    CzIntegrationAdapter,
    CzIntegrationConfig,
    CzPublishedKnowledgeItem,
    select_submittable_candidates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cz_required_schema_fields(class_name: str) -> set[str]:
    schema_path = (
        PROJECT_ROOT
        / "cz-knowledge-kb"
        / "knowledge-kb-master"
        / "backend"
        / "app"
        / "schemas"
        / "integration.py"
    )
    module = ast.parse(schema_path.read_text(encoding="utf-8"))
    schema_class = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    required: set[str] = set()
    for statement in schema_class.body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        if not isinstance(statement.target, ast.Name):
            continue
        value = statement.value
        if value is None:
            required.add(statement.target.id)
            continue
        if not isinstance(value, ast.Call):
            continue
        if not isinstance(value.func, ast.Name) or value.func.id != "Field":
            continue
        if (
            value.args
            and isinstance(value.args[0], ast.Constant)
            and value.args[0].value is Ellipsis
        ):
            required.add(statement.target.id)
    return required


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
        "知识分类": "质检流程",
        "产品类型": product_type,
        "适用范围": product_type,
        "适用品牌": "",
        "适用机型": "",
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
    @staticmethod
    def _published_search_candidate(
        knowledge_id: str,
        knowledge_origin: str,
        *,
        title: str,
        score: float,
        boundary_projection: dict[str, str] | None = None,
    ) -> dict:
        return {
            "id": knowledge_id,
            "title": title,
            "text": f"{title}正文",
            "score": score,
            "finalScore": score,
            "status": "published",
            "knowledgeOrigin": knowledge_origin,
            "businessType": "self_operated",
            "categoryId": "cat-notebook",
            "level1Label": "基本情况",
            "productType": "笔记本",
            "models": ["MateBook 14"],
            "keywords": ["硬盘", "品牌件"],
            "sourceRef": f"knowledge-kb://knowledge/{knowledge_id}",
            "boundaryProjection": boundary_projection or {},
        }

    def test_published_search_splits_standard_and_business_knowledge(self) -> None:
        adapter = CzIntegrationAdapter(
            CzIntegrationConfig(
                "https://kb.example",
                "",
                retrieval_key="retrieval-key",
            )
        )
        adapter._request_json = Mock(
            return_value={
                "status": "success",
                "retrievalMode": "semantic",
                "knowledgeVersion": "knowledge-v7",
                "scoreThreshold": 0.6,
                "candidates": [
                    self._published_search_candidate(
                        "STD-001",
                        "headquarters_standard",
                        title="笔记本硬盘总部标准",
                        score=0.94,
                    ),
                    self._published_search_candidate(
                        "BIZ-001",
                        "business_accumulation",
                        title="笔记本硬盘是否为品牌件",
                        score=0.91,
                        boundary_projection={
                            "objectKey": "硬盘品牌",
                            "queryTarget": "硬盘品牌属性",
                            "conclusionState": "硬盘是否为品牌件",
                        },
                    ),
                ],
            }
        )

        result = adapter.search_published_knowledge(
            conversation_id="2085229024728059925",
            normalized_question="笔记本硬盘品牌是否为第三方",
            business_type="self_operated",
            product_type="笔记本",
            model="MateBook 14",
        )

        self.assertEqual(len(result.headquarters_standards), 1)
        standard, standard_score = result.headquarters_standards[0]
        self.assertEqual(standard.standard_id, "STD-001")
        self.assertEqual(standard.knowledge_type, "总部标准")
        self.assertEqual(standard_score, 0.94)
        self.assertEqual(len(result.business_knowledge), 1)
        business = result.business_knowledge[0]
        self.assertIsInstance(business, CzPublishedKnowledgeItem)
        self.assertEqual(business.knowledge_id, "BIZ-001")
        self.assertEqual(business.knowledge_origin, "business_accumulation")
        self.assertEqual(business.version, "knowledge-v7")
        self.assertEqual(business.models, ("MateBook 14",))
        self.assertEqual(business.text, "笔记本硬盘是否为品牌件正文")
        self.assertEqual(
            business.boundary_projection["object_key"],
            "硬盘品牌",
        )
        self.assertEqual(result.audit["headquarters_standard_ids"], ["STD-001"])
        self.assertEqual(result.audit["business_accumulation_ids"], ["BIZ-001"])
        self.assertEqual(result.audit["business_accumulation_count"], 1)
        adapter._request_json.assert_called_once()
        request_args = adapter._request_json.call_args.args
        request_kwargs = adapter._request_json.call_args.kwargs
        self.assertEqual(request_args[:2], ("POST", adapter.standard_search_path))
        self.assertEqual(
            request_args[2]["knowledgeOrigin"],
            "headquarters_standard",
        )
        self.assertEqual(request_kwargs["api_key"], "retrieval-key")
        self.assertEqual(
            request_kwargs["extra_headers"]["X-Conversation-Id"],
            "2085229024728059925",
        )

    def test_headquarters_search_keeps_business_knowledge_out_of_standards(self) -> None:
        adapter = CzIntegrationAdapter(
            CzIntegrationConfig(
                "https://kb.example",
                "",
                retrieval_key="retrieval-key",
            )
        )
        adapter._request_json = Mock(
            return_value={
                "status": "success",
                "retrievalMode": "semantic",
                "knowledgeVersion": "knowledge-v8",
                "scoreThreshold": 0.6,
                "candidates": [
                    self._published_search_candidate(
                        "BIZ-002",
                        "business_accumulation",
                        title="线上已有硬盘品牌知识",
                        score=0.9,
                    ),
                ],
            }
        )

        standards, audit = adapter.search_headquarters_standards(
            conversation_id="2085229024728059925",
            normalized_question="笔记本硬盘是否为品牌件",
            business_type="self_operated",
            product_type="笔记本",
        )

        self.assertEqual(standards, [])
        self.assertEqual(audit["standard_ids"], [])
        self.assertEqual(audit["ignored_business_accumulation_count"], 1)
        self.assertEqual(
            audit["business_accumulation_candidates"][0]["knowledge_id"],
            "BIZ-002",
        )
        self.assertNotIn("text", audit["business_accumulation_candidates"][0])

    def test_published_search_rejects_review_or_unknown_origin_candidates(self) -> None:
        adapter = CzIntegrationAdapter(
            CzIntegrationConfig(
                "https://kb.example",
                "",
                retrieval_key="retrieval-key",
            )
        )
        review_candidate = self._published_search_candidate(
            "BIZ-REVIEW",
            "business_accumulation",
            title="待复核知识",
            score=0.9,
        )
        review_candidate["status"] = "review"
        adapter._request_json = Mock(
            return_value={"candidates": [review_candidate]}
        )

        with self.assertRaisesRegex(RuntimeError, "未发布知识"):
            adapter.search_published_knowledge(
                conversation_id="2085229024728059925",
                normalized_question="笔记本硬盘是否为品牌件",
                business_type="self_operated",
                product_type="笔记本",
            )

        unknown_candidate = self._published_search_candidate(
            "UNKNOWN-001",
            "unknown_origin",
            title="未知来源知识",
            score=0.9,
        )
        adapter._request_json = Mock(
            return_value={"candidates": [unknown_candidate]}
        )
        with self.assertRaisesRegex(RuntimeError, "未知知识来源"):
            adapter.search_published_knowledge(
                conversation_id="2085229024728059925",
                normalized_question="笔记本硬盘是否为品牌件",
                business_type="self_operated",
                product_type="笔记本",
            )

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
        self.assertFalse(readiness["published_knowledge_retrieval_configured"])

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
        candidate = {"主题ID": "TOP-001", "主标题": "机型查询流程", "知识内容": "查询步骤", "知识分类": "售后服务"}

        with self.assertRaisesRegex(ValueError, "未映射 category_id"):
            adapter.build_batch_payload([candidate], {})

    def test_category_id_never_falls_back_to_product_type(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        with self.assertRaisesRegex(ValueError, "未映射 category_id"):
            adapter.build_batch_payload(
                [{**_candidate(), "知识分类": "售后服务"}],
                {"手机": "cat-phone"},
            )

    def test_uncertain_category_uses_case_analysis_fallback_when_cz_lacks_uncertain(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(product_type="手机"), "知识分类": "不确定"}],
            {"案例解析": "cat-case-analysis"},
        )[0]

        self.assertEqual(payload["knowledge"]["category_id"], "cat-case-analysis")
        self.assertEqual(payload["knowledge"]["applicable_categories"], ["手机"])

    def test_candidate_payload_includes_recommended_reply_and_review_evidence(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(), "如何修改": "精简步骤", "问题反馈": "措辞偏长"}],
            {"质检流程": "cat-process"},
        )[0]

        self.assertEqual(payload["knowledge"]["recommended_reply"], "您好，请按第1条流程处理。")
        self.assertEqual(payload["knowledge"]["category_id"], "cat-process")
        self.assertEqual(
            payload["knowledge"]["knowledge_origin"],
            "business_accumulation",
        )
        self.assertEqual(
            payload["knowledge"]["business_type"],
            "self_operated",
        )
        self.assertEqual(payload["knowledge"]["applicable_categories"], ["手机"])
        self.assertEqual(payload["knowledge"]["scene_tags"], ["查询", "屏幕", "显示异常"])
        self.assertTrue(payload["selection"]["eligible"])
        self.assertIn("如何修改：精简步骤", payload["selection"]["reasons"])
        self.assertIn("问题反馈：措辞偏长", payload["selection"]["reasons"])

    def test_candidate_payload_falls_back_to_source_reply_when_recommendation_is_empty(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(), "推荐回复": "", "历史实际回复": "请先核对防水标状态。"}],
            {"质检流程": "cat-process"},
        )[0]

        self.assertEqual(payload["knowledge"]["recommended_reply"], "请先核对防水标状态。")
        self.assertEqual(
            payload["processing"]["plugin_name"],
            "answer-hub-topic-transcription",
        )
        self.assertEqual(
            payload["processing"]["plugin_version"],
            "2026-08-06-evidence-facts-v1",
        )
        self.assertNotIn("skill_name", payload["processing"])
        self.assertNotIn("skill_version", payload["processing"])
        self.assertNotIn("layer", payload["knowledge"])
        self.assertNotIn("applicable_business_types", payload["knowledge"])

    def test_candidate_payload_satisfies_local_cz_required_contract(self) -> None:
        adapter = CzIntegrationAdapter(
            CzIntegrationConfig("https://kb.example", "test-key")
        )
        payload = adapter.build_batch_payload(
            [_candidate()],
            {"质检流程": "cat-process"},
        )[0]

        self.assertEqual(
            _cz_required_schema_fields("IntegrationCandidate") - payload.keys(),
            set(),
        )
        self.assertEqual(
            _cz_required_schema_fields("IntegrationKnowledgePayload")
            - payload["knowledge"].keys(),
            set(),
        )

    def test_candidate_payload_attaches_matching_case_images_and_fact_trace(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [
                {
                    **_candidate(),
                    "图例": (
                        "https://cdn.example.com/case-a.jpg\n"
                        "https://cdn.example.com/case-b.png"
                    ),
                    "主题来源记录ID": "R-001\nR-002",
                    "主题事实引用": (
                        "[F01] 代表记录=R-001\n"
                        "[F02] 来源记录=R-002"
                    ),
                    "主题事实证据包": json.dumps(
                        {
                            "representative_facts": [
                                {
                                    "fact_id": "F01",
                                    "source_record_id": "R-001",
                                    "image_urls": [
                                        "https://cdn.example.com/case-a.jpg"
                                    ],
                                },
                                {
                                    "fact_id": "F02",
                                    "source_record_id": "R-002",
                                    "image_urls": [
                                        "https://cdn.example.com/case-b.png"
                                    ],
                                },
                            ],
                            "source_fact_refs": [
                                "[F01] 来源记录=R-001",
                                "[F02] 来源记录=R-002",
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    "主题图例来源": (
                        "[F01] 来源记录=R-001 | 图片=https://cdn.example.com/case-a.jpg\n"
                        "[F02] 来源记录=R-002 | 图片=https://cdn.example.com/case-b.png"
                    ),
                    "主题证据摘要": (
                        "[F01] 人工核心问题：屏幕异常如何判断 | "
                        "人工判定结论：案例A属于显示异常"
                    ),
                }
            ],
            {"质检流程": "cat-process"},
        )[0]

        blocks = payload["knowledge"]["content"]["blocks"]
        self.assertEqual(blocks[0], {"type": "text", "value": "这是第1条知识内容。"})
        self.assertEqual(
            [block["external_url"] for block in blocks[1:]],
            [
                "https://cdn.example.com/case-a.jpg",
                "https://cdn.example.com/case-b.png",
            ],
        )
        self.assertTrue(all(block["type"] == "image" for block in blocks[1:]))
        self.assertIn("主题事实引用", payload["knowledge"]["evidence_excerpt"])
        self.assertIn("R-001", payload["knowledge"]["evidence_excerpt"])
        self.assertIn("case-a.jpg", payload["knowledge"]["evidence_excerpt"])

    def test_candidate_payload_attaches_traced_case_video_for_manual_review(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [
                {
                    **_candidate(),
                    "图例": "https://cdn.example.com/case-a.jpg",
                    "主题视频链接": "https://cdn.example.com/case-a.mp4",
                    "主题事实引用": "[F01] 代表记录=R-001",
                    "主题事实证据包": json.dumps(
                        {
                            "representative_facts": [
                                {
                                    "fact_id": "F01",
                                    "source_record_id": "R-001",
                                    "image_urls": [
                                        "https://cdn.example.com/case-a.jpg"
                                    ],
                                    "video_urls": [
                                        "https://cdn.example.com/case-a.mp4"
                                    ],
                                }
                            ],
                            "source_fact_refs": ["[F01] 来源记录=R-001"],
                        },
                        ensure_ascii=False,
                    ),
                    "主题图例来源": (
                        "[F01] 来源记录=R-001 | "
                        "图片=https://cdn.example.com/case-a.jpg"
                    ),
                    "主题视频来源": (
                        "[F01] 来源记录=R-001 | "
                        "视频=https://cdn.example.com/case-a.mp4"
                    ),
                }
            ],
            {"质检流程": "cat-process"},
        )[0]

        blocks = payload["knowledge"]["content"]["blocks"]
        self.assertEqual(
            [block["type"] for block in blocks],
            ["text", "image", "video"],
        )
        self.assertEqual(
            blocks[-1]["external_url"],
            "https://cdn.example.com/case-a.mp4",
        )
        self.assertEqual(blocks[-1]["caption"], "来源案例视频（仅供人工播放）")

    def test_candidate_submission_rejects_private_case_media_urls(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        private_url = "https://127.0.0.1/internal-case.mp4"
        candidate = {
            **_candidate(),
            "主题视频链接": private_url,
            "主题事实引用": "[F01] 代表记录=R-001",
            "主题事实证据包": json.dumps(
                {
                    "representative_facts": [
                        {
                            "fact_id": "F01",
                            "source_record_id": "R-001",
                            "video_urls": [private_url],
                        }
                    ],
                    "source_fact_refs": ["[F01] 来源记录=R-001"],
                },
                ensure_ascii=False,
            ),
            "主题视频来源": f"[F01] 来源记录=R-001 | 视频={private_url}",
        }

        with self.assertRaisesRegex(ValueError, "不安全"):
            adapter.build_batch_payload(
                [candidate],
                {"质检流程": "cat-process"},
            )

    def test_candidate_submission_rejects_case_image_without_fact_trace(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        candidate = {
            **_candidate(),
            "图例": "https://cdn.example.com/untraced.jpg",
            "主题图例来源": "",
        }

        with self.assertRaisesRegex(ValueError, "案例图缺少来源事实"):
            adapter.build_batch_payload(
                [candidate],
                {"质检流程": "cat-process"},
            )

        queued = adapter.build_batch_payload(
            [candidate],
            {"质检流程": "cat-process"},
            require_eligible=False,
        )[0]
        self.assertEqual(
            queued["knowledge"]["content"]["blocks"],
            [{"type": "text", "value": "这是第1条知识内容。"}],
        )

    def test_candidate_submission_rejects_forged_case_image_fact_trace(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        candidate = {
            **_candidate(),
            "图例": "https://cdn.example.com/forged.jpg",
            "主题事实引用": "[F01] 代表记录=R-001",
            "主题事实证据包": json.dumps(
                {
                    "representative_facts": [
                        {
                            "fact_id": "F01",
                            "source_record_id": "R-001",
                            "image_urls": [
                                "https://cdn.example.com/real.jpg"
                            ],
                        }
                    ],
                    "source_fact_refs": [
                        "[F01] 来源记录=R-001"
                    ],
                },
                ensure_ascii=False,
            ),
            "主题图例来源": (
                "[F99] 来源记录=R-001 | "
                "图片=https://cdn.example.com/forged.jpg"
            ),
        }

        with self.assertRaisesRegex(ValueError, "案例图缺少来源事实"):
            adapter.build_batch_payload(
                [candidate],
                {"质检流程": "cat-process"},
            )

        queued = adapter.build_batch_payload(
            [candidate],
            {"质检流程": "cat-process"},
            require_eligible=False,
        )[0]
        evidence_excerpt = queued["knowledge"]["evidence_excerpt"] or ""
        self.assertNotIn("F99", evidence_excerpt)
        self.assertNotIn("forged.jpg", evidence_excerpt)

    def test_candidate_payload_reserves_cz_business_hierarchy_mapping(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [
                {
                    **_candidate(),
                    "回收业务层级": "聚合回收",
                    "CZ适用类目ID": "cz-aggregate-phone",
                }
            ],
            {"质检流程": "cat-process"},
        )[0]

        self.assertEqual(
            payload["knowledge"]["applicable_categories"],
            ["cz-aggregate-phone"],
        )
        self.assertEqual(
            payload["knowledge"]["knowledge_origin"],
            "business_accumulation",
        )
        self.assertEqual(
            payload["knowledge"]["business_type"],
            "aggregated",
        )
        self.assertIn(
            "CZ适用类目路径：聚合回收/手机",
            payload["selection"]["reasons"],
        )

    def test_scope_suffix_is_ignored_and_specific_fields_map_separately(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        candidate = {
            **_candidate(),
            "适用范围": "手机-iOS",
            "适用品牌": "苹果",
            "适用机型": "iPhone 15 Pro",
        }

        payload = adapter.build_batch_payload(
            [candidate],
            {"质检流程": "cat-process"},
        )[0]

        self.assertEqual(
            payload["knowledge"]["applicable_categories"],
            ["手机"],
        )
        self.assertEqual(
            payload["knowledge"]["applicable_brands"],
            ["苹果"],
        )
        self.assertEqual(
            payload["knowledge"]["applicable_models"],
            ["iPhone 15 Pro"],
        )

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
            {"质检流程": "cat-process"},
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
            {"质检流程": "cat-process"},
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
                {"质检流程": "cat-process"},
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
                {"质检流程": "cat-process"},
            )

    def test_build_payload_rejects_unfinished_transcription_placeholders(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        for require_eligible in (True, False):
            with self.subTest(require_eligible=require_eligible):
                with self.assertRaisesRegex(ValueError, "未完成知识转写"):
                    adapter.build_batch_payload(
                        [
                            {
                                **_candidate(),
                                "知识内容": (
                                    "该主题未形成包含具体事实、条件和处理结论的知识正文，"
                                    "已阻止通用模板作为知识草稿进入初标。请在候选价值复核中确认主题价值；"
                                    "如改判为值得沉淀，需要补充完整、可追溯的知识内容后再送审。"
                                ),
                                "是否值得沉淀": "是",
                                "是否可用": "是",
                            }
                        ],
                        {"质检流程": "cat-process"},
                        require_eligible=require_eligible,
                    )

    def test_review_queue_payload_allows_pending_human_annotation(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [{**_candidate(), "是否值得沉淀": "", "是否可用": "", "审核结论": ""}],
            {"质检流程": "cat-process"},
            require_eligible=False,
        )[0]

        self.assertFalse(payload["selection"]["eligible"])
        self.assertEqual(payload["human_review"]["knowledge_value"], "pending")
        self.assertEqual(payload["human_review"]["usability"], "pending")

    def test_payload_uses_work_orders_when_source_record_ids_are_empty(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        payload = adapter.build_batch_payload(
            [
                {
                    **_candidate(),
                    "主题来源记录ID": "",
                    "主题工单ID": "WO-001\nWO-002",
                }
            ],
            {"质检流程": "cat-process"},
            require_eligible=False,
        )[0]

        self.assertEqual(payload["source"]["message_ids"], ["WO-001", "WO-002"])

    def test_review_queue_sync_rejects_unfinished_transcription_without_remote_call(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        adapter._request_json = Mock()

        result = adapter.sync_review_candidates(
            [
                {
                    **_candidate(),
                    "知识内容": "主题未进入知识转写。",
                    "是否值得沉淀": "",
                    "是否可用": "",
                    "审核结论": "",
                }
            ],
            {"质检流程": "cat-process"},
        )

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["ready"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(result["results"][0]["error_code"], "TRANSCRIPTION_NOT_READY")
        adapter._request_json.assert_not_called()

    def test_review_queue_sync_collapses_exact_duplicate_payloads(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        sent_event_ids: list[list[str]] = []

        def fake_request(method, path, payload, **kwargs):
            del method, path, kwargs
            event_ids = [item["event_id"] for item in payload["items"]]
            sent_event_ids.append(event_ids)
            return {
                "queued": len(event_ids),
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "results": [],
            }

        adapter._request_json = Mock(side_effect=fake_request)
        duplicate = {
            **_candidate(2),
            "主标题": "屏幕显示异常如何通过图片核验",
            "知识内容": "请补充异常部位、现象和清晰图片后再核验。",
        }
        canonical = {
            **_candidate(1),
            "主标题": "屏幕显示异常如何通过图片核验",
            "知识内容": "请补充异常部位、现象和清晰图片后再核验。",
        }

        result = adapter.sync_review_candidates(
            [canonical, duplicate],
            {"质检流程": "cat-process"},
        )

        self.assertEqual(sent_event_ids, [["TOP-001"]])
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["deduplicated"], 1)
        self.assertEqual(result["failed"], 0)
        duplicate_result = next(
            item
            for item in result["results"]
            if item["status"] == "deduplicated"
        )
        self.assertEqual(duplicate_result["event_id"], "TOP-002")
        self.assertEqual(duplicate_result["canonical_event_id"], "TOP-001")

    def test_candidate_idempotency_is_stable_when_reviewers_edit_content(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))

        first = adapter.build_batch_payload(
            [_candidate()],
            {"质检流程": "cat-process"},
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
            {"质检流程": "cat-process"},
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
            {"质检流程": "cat-process"},
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
            {"质检流程": "cat-process"},
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
                {**_candidate(2), "知识分类": "售后服务"},
                _candidate(3),
            ],
            {"质检流程": "cat-process"},
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
            {"质检流程": "cat-process"},
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
