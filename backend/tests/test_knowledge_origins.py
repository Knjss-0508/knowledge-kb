import unittest

from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.main import app
from app.models.knowledge import Knowledge
from app.routes.integration import _candidate_payload_with_taxonomy_defaults
from app.routes.knowledge_origin import list_knowledge_origins
from app.schemas.integration import (
    CandidateReviewUpdate,
    IntegrationStandardSearchCandidate,
    IntegrationStandardSearchRequest,
)
from app.schemas.knowledge import (
    CandidateSubmit,
    KnowledgeCreate,
    KnowledgeUpdate,
    SearchRequest,
)


class KnowledgeOriginTests(unittest.TestCase):
    def test_model_origin_is_required_indexed_and_constrained(self):
        column = Knowledge.__table__.c.knowledge_origin
        self.assertFalse(column.nullable)
        self.assertTrue(column.index)

        check_constraints = {
            constraint.name: str(constraint.sqltext)
            for constraint in Knowledge.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        }
        self.assertEqual(
            check_constraints["ck_knowledge_items_knowledge_origin"],
            (
                "knowledge_origin IN "
                "('headquarters_standard', 'business_accumulation', "
                "'model_configuration')"
            ),
        )
        self.assertTrue(
            {
                "uq_knowledge_items_model_configuration_source_record_id",
                "uq_knowledge_items_model_configuration_source_knowledge_key",
            }.issubset(
                {index.name for index in Knowledge.__table__.indexes}
            )
        )
        managed_indexes = {
            index.name: index
            for index in Knowledge.__table__.indexes
            if index.name
            and index.name.startswith(
                "uq_knowledge_items_model_configuration_"
            )
        }
        for index in managed_indexes.values():
            self.assertIsNotNone(
                index.dialect_options["postgresql"]["where"]
            )
            self.assertIsNotNone(index.dialect_options["sqlite"]["where"])

    def test_origin_is_required_and_restricted_in_write_schemas(self):
        base_knowledge = {
            "business_type": "self_operated",
            "title": "屏幕质检",
            "content": "检查屏幕显示是否正常",
            "category_id": "cat-qc-standard",
        }

        with self.assertRaises(ValidationError):
            KnowledgeCreate.model_validate(base_knowledge)
        with self.assertRaises(ValidationError):
            KnowledgeCreate.model_validate(
                {**base_knowledge, "knowledge_origin": "invalid"}
            )
        with self.assertRaises(ValidationError):
            KnowledgeCreate.model_validate(
                {
                    **base_knowledge,
                    "knowledge_origin": "model_configuration",
                }
            )

        created = KnowledgeCreate.model_validate(
            {
                **base_knowledge,
                "knowledge_origin": "headquarters_standard",
            }
        )
        self.assertEqual(created.knowledge_origin, "headquarters_standard")

        self.assertIsNone(KnowledgeUpdate.model_validate({}).knowledge_origin)
        with self.assertRaises(ValidationError):
            KnowledgeUpdate.model_validate({"knowledge_origin": None})
        with self.assertRaises(ValidationError):
            KnowledgeUpdate.model_validate(
                {"knowledge_origin": "model_configuration"}
            )

        with self.assertRaises(ValidationError):
            CandidateSubmit.model_validate(
                {
                    "business_type": "self_operated",
                    "title": "候选知识",
                    "content": "候选内容",
                    "category_id": "cat-qc-standard",
                }
            )
        with self.assertRaises(ValidationError):
            CandidateSubmit.model_validate(
                {
                    "knowledge_origin": "model_configuration",
                    "business_type": "self_operated",
                    "title": "候选知识",
                    "content": "候选内容",
                    "category_id": "cat-qc-standard",
                }
            )

    def test_readonly_dictionary_contract(self):
        options = list_knowledge_origins()
        self.assertEqual(
            [option.model_dump() for option in options],
            [
                {
                    "value": "headquarters_standard",
                    "label": "总部标准",
                },
                {
                    "value": "business_accumulation",
                    "label": "业务沉淀",
                },
                {
                    "value": "model_configuration",
                    "label": "机型配置信息",
                },
            ],
        )

        specification = app.openapi()
        self.assertIn("/api/v1/knowledge-origins", specification["paths"])
        response_schema = specification["paths"]["/api/v1/knowledge-origins"][
            "get"
        ]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response_schema["items"]["$ref"],
            "#/components/schemas/KnowledgeOriginOption",
        )

    def test_automation_review_requires_origin_but_dual_retrieval_does_not(self):
        self.assertIsNone(
            CandidateReviewUpdate.model_validate({}).knowledge_origin
        )
        with self.assertRaises(ValidationError):
            CandidateReviewUpdate.model_validate({"knowledge_origin": None})
        with self.assertRaises(ValidationError):
            CandidateReviewUpdate.model_validate(
                {"knowledge_origin": "model_configuration"}
            )
        self.assertIsNone(
            CandidateReviewUpdate.model_validate({}).business_type
        )
        with self.assertRaises(ValidationError):
            CandidateReviewUpdate.model_validate({"business_type": None})

        dual_origin_request = IntegrationStandardSearchRequest.model_validate(
            {
                "conversationId": "202608100001",
                "requestId": "knowledge-origin-test-1",
                "normalizedQuestion": "屏幕漏光",
            }
        )
        self.assertIsNone(dual_origin_request.knowledge_origin)
        with self.assertRaises(ValidationError):
            SearchRequest.model_validate(
                {
                    "query": "屏幕漏光",
                    "business_type": "self_operated",
                }
            )
        with self.assertRaises(ValidationError):
            SearchRequest.model_validate(
                {
                    "query": "iPad 指纹",
                    "knowledge_origin": "model_configuration",
                    "business_type": "self_operated",
                }
            )
        search_request = SearchRequest.model_validate(
            {
                "query": "屏幕漏光",
                "knowledge_origin": "headquarters_standard",
                "business_type": "self_operated",
            }
        )
        self.assertEqual(
            search_request.knowledge_origin,
            "headquarters_standard",
        )
        request = IntegrationStandardSearchRequest.model_validate(
            {
                "conversationId": "202608100001",
                "requestId": "knowledge-origin-test-2",
                "normalizedQuestion": "屏幕漏光",
                "knowledgeOrigin": "headquarters_standard",
            }
        )
        self.assertEqual(request.knowledge_origin, "headquarters_standard")
        with self.assertRaises(ValidationError):
            IntegrationStandardSearchCandidate.model_validate(
                {
                    "id": "A-00001",
                    "title": "iPad 机型配置",
                    "text": "配置正文",
                    "score": 1,
                    "finalScore": 1,
                    "knowledgeOrigin": "model_configuration",
                    "businessType": "self_operated",
                    "sourceRef": "knowledge://A-00001",
                }
            )

    def test_legacy_candidate_payload_gets_explicit_taxonomy_defaults(self):
        payload, knowledge = _candidate_payload_with_taxonomy_defaults(
            {
                "knowledge": {
                    "title": "历史候选",
                    "content": {"blocks": []},
                    "category_id": "cat-process",
                }
            }
        )

        self.assertEqual(
            knowledge["knowledge_origin"],
            "business_accumulation",
        )
        self.assertEqual(knowledge["business_type"], "self_operated")
        self.assertEqual(payload["knowledge"], knowledge)


if __name__ == "__main__":
    unittest.main()
