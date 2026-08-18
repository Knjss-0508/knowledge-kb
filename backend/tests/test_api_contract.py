import json
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.main import app
from app.models.knowledge import KnowledgeStatus
from app.routes.knowledge import (
    _filtered_knowledge_query,
    _has_knowledge_export_filter,
    _import_review_metadata,
    _require_manual_applicable_category,
)


class ApiContractTests(unittest.TestCase):
    def test_embedding_model_upload_and_deploy_are_not_exposed(self):
        paths = app.openapi()["paths"]
        self.assertNotIn(
            "/api/v1/embedding-model/models/{model_id}/upload",
            paths,
        )
        self.assertNotIn(
            "/api/v1/embedding-model/models/{model_id}/deploy",
            paths,
        )

    def test_task_scoped_embedding_runner_routes_are_exposed(self):
        specification = app.openapi()
        paths = specification["paths"]
        route_schemas = {
            "probe": "EmbeddingRunnerHeartbeat",
            "claim": "EmbeddingRunnerHeartbeat",
            "heartbeat": "EmbeddingRunnerHeartbeat",
            "progress": "EmbeddingRunnerProgress",
            "complete": "EmbeddingRunnerComplete",
            "fail": "EmbeddingRunnerFailure",
        }

        for action, request_schema in route_schemas.items():
            with self.subTest(action=action):
                operation = paths[
                    f"/api/v1/embedding-model/runner/tasks/{{job_id}}/{action}"
                ]["post"]
                parameters = {
                    (parameter["name"], parameter["in"])
                    for parameter in operation["parameters"]
                }
                request_ref = operation["requestBody"]["content"][
                    "application/json"
                ]["schema"]["$ref"]

                self.assertIn(("job_id", "path"), parameters)
                self.assertIn(
                    ("X-Embedding-Task-Token", "header"),
                    parameters,
                )
                self.assertEqual(
                    request_ref,
                    f"#/components/schemas/{request_schema}",
                )

    def test_excel_import_routes_are_exposed(self):
        paths = app.openapi()["paths"]
        self.assertIn("/api/v1/knowledge/import/template", paths)
        self.assertIn("/api/v1/knowledge/import/excel", paths)
        self.assertIn("/api/v1/knowledge/import/tasks", paths)
        self.assertIn("/api/v1/knowledge/import/tasks/{task_id}", paths)
        self.assertIn("/api/v1/knowledge/export/excel", paths)
        self.assertIn(
            "202",
            paths["/api/v1/knowledge/import/excel"]["post"]["responses"],
        )
        schemas = app.openapi()["components"]["schemas"]
        result_statuses = schemas["ExcelImportRowResult"]["properties"]["status"]["enum"]
        self.assertIn("review_required", result_statuses)
        self.assertIn("review_pending", result_statuses)
        self.assertIn("deprecated", result_statuses)
        task_properties = schemas["KnowledgeImportTaskResponse"]["properties"]
        self.assertIn("pending_review", task_properties)
        self.assertIn("deprecated", task_properties)
        self.assertTrue(
            {
                "import_type",
                "created",
                "updated",
                "unchanged",
            }.issubset(task_properties)
        )
        self.assertIn(
            "review_task_id",
            schemas["ExcelImportRowResult"]["properties"],
        )
        self.assertIn(
            "operation",
            schemas["ExcelImportRowResult"]["properties"],
        )

        template_parameters = {
            parameter["name"]: parameter
            for parameter in paths[
                "/api/v1/knowledge/import/template"
            ]["get"]["parameters"]
        }
        self.assertEqual(
            template_parameters["import_type"]["schema"]["enum"],
            ["knowledge", "model_configuration"],
        )
        self.assertEqual(
            template_parameters["import_type"]["schema"]["default"],
            "knowledge",
        )

        upload_schema_ref = paths[
            "/api/v1/knowledge/import/excel"
        ]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"][
            "$ref"
        ]
        upload_schema = schemas[upload_schema_ref.rsplit("/", 1)[-1]]
        self.assertEqual(
            upload_schema["properties"]["import_type"]["enum"],
            ["knowledge", "model_configuration"],
        )
        self.assertEqual(
            upload_schema["properties"]["import_type"]["default"],
            "knowledge",
        )
        detail_parameters = {
            parameter["name"]: parameter
            for parameter in paths[
                "/api/v1/knowledge/import/tasks/{task_id}"
            ]["get"]["parameters"]
        }
        self.assertEqual(
            detail_parameters["result_limit"]["schema"]["maximum"],
            5000,
        )
        self.assertEqual(
            task_properties["imported"]["description"],
            "成功处理数",
        )

    def test_knowledge_list_exposes_applicability_filters(self):
        operation = app.openapi()["paths"]["/api/v1/knowledge"]["get"]
        parameter_names = {
            parameter["name"] for parameter in operation["parameters"]
        }
        self.assertTrue(
            {
                "business_type",
                "applicable_category_ids",
                "brand_ids",
                "model_ids",
            }.issubset(parameter_names)
        )
        self.assertIn("X-Total-Count", operation["responses"]["200"]["headers"])
        keyword_parameter = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "keyword"
        )
        self.assertIn("副标题", keyword_parameter["description"])
        response_properties = app.openapi()["components"]["schemas"][
            "KnowledgeResponse"
        ]["properties"]
        self.assertIn("import_review_metadata", response_properties)

    def test_import_review_metadata_only_exposes_review_evidence(self):
        item = SimpleNamespace(
            source="excel",
            status=KnowledgeStatus.REVIEW,
            source_fields={
                "校验备注": "审核原因：关联标准项需要人工确认",
                "来源追溯": "文件=a.xlsx；Sheet=知识库主表；行=12",
                "知识内容": "不应通过审核接口暴露",
            },
        )

        self.assertEqual(
            _import_review_metadata(item),
            {
                "validation_remark": "审核原因：关联标准项需要人工确认",
                "source_trace": "文件=a.xlsx；Sheet=知识库主表；行=12",
            },
        )

        item.status = KnowledgeStatus.PUBLISHED
        self.assertEqual(_import_review_metadata(item), {})

    def test_knowledge_keyword_search_covers_non_title_fields(self):
        session = Session()
        try:
            query = _filtered_knowledge_query(
                session,
                SimpleNamespace(role="editor"),
                keyword="屏幕",
            )
            statement = str(
                query.statement.compile(dialect=postgresql.dialect())
            )
        finally:
            session.close()

        self.assertIn("knowledge_items.title", statement)
        self.assertIn("knowledge_items.subtitles", statement)
        self.assertIn("knowledge_items.content", statement)
        self.assertIn("knowledge_items.related_standard_items", statement)
        self.assertIn("knowledge_items.applicable_scenes", statement)
        self.assertIn("categories.name", statement)
        self.assertIn("jsonb_path_exists", statement)
        self.assertNotIn("CAST(knowledge_items.content AS TEXT)", statement)

    def test_export_requires_at_least_one_active_filter(self):
        self.assertFalse(
            _has_knowledge_export_filter(
                status=None,
                category_id=None,
                applicable_category_ids=[],
                brand_ids=[],
                model_ids=[],
                keyword="  ",
            )
        )
        self.assertTrue(
            _has_knowledge_export_filter(
                status="published",
                category_id="cat-qc-standard",
                applicable_category_ids=["tablet"],
                brand_ids=[],
                model_ids=[],
                keyword=None,
            )
        )
        self.assertTrue(
            _has_knowledge_export_filter(
                status=None,
                business_type="self_operated",
                category_id=None,
                applicable_category_ids=[],
                brand_ids=[],
                model_ids=[],
                keyword=None,
            )
        )

    def test_manual_quality_knowledge_requires_at_least_one_applicable_category(self):
        with self.assertRaises(HTTPException) as raised:
            _require_manual_applicable_category(
                source="manual",
                category_id="cat-qc-standard",
                applicable_categories=[],
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail,
            "适用类目至少选择一项，可多选。",
        )

        _require_manual_applicable_category(
            source="manual",
            category_id="cat-qc-process",
            applicable_categories=["tablet", "phone"],
        )
        _require_manual_applicable_category(
            source="excel",
            category_id="cat-qc-standard",
            applicable_categories=[],
        )
        _require_manual_applicable_category(
            source="manual",
            category_id="cat-case-analysis",
            applicable_categories=[],
        )

    def test_candidate_review_routes_are_exposed(self):
        paths = app.openapi()["paths"]

        self.assertIn(
            "post",
            paths["/api/v1/integration/knowledge-review-candidates:batch"],
        )
        self.assertIn("get", paths["/api/v1/integration/candidate-reviews"])
        self.assertIn(
            "patch",
            paths["/api/v1/integration/candidate-reviews/{ingestion_id}"],
        )
        self.assertIn(
            "post",
            paths["/api/v1/integration/candidate-reviews:batch-submit"],
        )
        schemas = app.openapi()["components"]["schemas"]
        self.assertIn(
            "confirm_dedup_review",
            schemas["CandidateReviewUpdate"]["properties"],
        )
        self.assertIn(
            "business_type",
            schemas["IntegrationKnowledgePayload"]["properties"],
        )
        self.assertIn(
            "business_type",
            schemas["CandidateReviewUpdate"]["properties"],
        )
        self.assertIn(
            "business_type",
            schemas["CandidateReviewListItem"]["properties"],
        )
        self.assertIn(
            "business_type",
            schemas["IntegrationDedupMatch"]["properties"],
        )
        self.assertIn(
            "business_types",
            schemas["IntegrationTaxonomyResponse"]["properties"],
        )
        self.assertIn(
            "review_required",
            schemas["IntegrationCandidateResult"]["properties"]["status"]["enum"],
        )
        parameters = {
            parameter["name"]
            for parameter in paths["/api/v1/integration/candidate-reviews"]["get"]["parameters"]
        }
        self.assertIn("deduplication_required", parameters)

    def test_batch_approve_route_is_exposed(self):
        paths = app.openapi()["paths"]
        self.assertIn(
            "post",
            paths["/api/v1/knowledge/review:batch-approve"],
        )
        self.assertIn(
            "get",
            paths["/api/v1/knowledge/review:selection"],
        )

    def test_integration_processing_exposes_plugin_contract(self):
        schemas = app.openapi()["components"]["schemas"]
        processing = schemas["IntegrationProcessing"]["properties"]

        self.assertIn("plugin_name", processing)
        self.assertIn("plugin_version", processing)

    def test_standard_provider_search_contract_is_exposed(self):
        specification = app.openapi()
        operation = specification["paths"][
            "/api/v1/integration/standard-search"
        ]["post"]
        parameter_names = {
            parameter["name"] for parameter in operation["parameters"]
        }
        self.assertIn("X-Integration-Key", parameter_names)
        self.assertIn("X-Conversation-Id", parameter_names)
        self.assertIn("X-Request-Id", parameter_names)

        schemas = specification["components"]["schemas"]
        request_properties = schemas[
            "IntegrationStandardSearchRequest"
        ]["properties"]
        candidate_properties = schemas[
            "IntegrationStandardSearchCandidate"
        ]["properties"]
        response_properties = schemas[
            "IntegrationStandardSearchResponse"
        ]["properties"]
        request_required = set(
            schemas["IntegrationStandardSearchRequest"].get("required", [])
        )

        self.assertIn("normalizedQuestion", request_properties)
        self.assertIn("conversationId", request_properties)
        self.assertIn("requestId", request_properties)
        self.assertIn("conversationId", request_required)
        self.assertIn("requestId", request_required)
        self.assertIn("knowledgeOrigin", request_properties)
        self.assertNotIn("knowledgeOrigin", request_required)
        self.assertIn("businessType", request_properties)
        self.assertIn("productType", request_properties)
        self.assertIn("businessType", candidate_properties)
        self.assertIn("finalScore", candidate_properties)
        self.assertIn("sourceRef", candidate_properties)
        self.assertIn("retrievalMode", response_properties)
        self.assertIn("knowledgeVersion", response_properties)
        self.assertIn("conversationId", response_properties)
        self.assertIn("requestId", response_properties)

        feedback_schema = schemas["RetrievalQualityEventPayload"]
        feedback_properties = feedback_schema["properties"]
        feedback_required = set(feedback_schema.get("required", []))
        self.assertIn("conversation_id", feedback_properties)
        self.assertIn("request_id", feedback_properties)
        self.assertIn("conversation_id", feedback_required)
        self.assertIn("request_id", feedback_required)

    def test_openapi_no_longer_exposes_knowledge_layer(self):
        specification = json.dumps(app.openapi(), ensure_ascii=False)
        self.assertNotIn('"layer"', specification)
        self.assertNotIn("知识层级", specification)
        self.assertNotIn("applicable_business_types", specification)
        self.assertNotIn("is_model_personal", specification)
        self.assertNotIn("适用业务", specification)
        self.assertNotIn("机型个性化", specification)


if __name__ == "__main__":
    unittest.main()
