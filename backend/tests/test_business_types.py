import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.main import app
from app.models.knowledge import Knowledge, KnowledgeStatus
from app.routes.business_type import list_business_types
from app.routes.knowledge import (
    _validate_business_applicable_categories,
    update_knowledge,
)
from app.schemas.knowledge import (
    CandidateSubmit,
    KnowledgeCreate,
    KnowledgeUpdate,
    SearchRequest,
)


class BusinessTypeTests(unittest.TestCase):
    def test_model_business_type_is_required_indexed_and_constrained(self):
        column = Knowledge.__table__.c.business_type
        self.assertFalse(column.nullable)
        self.assertTrue(column.index)

        check_constraints = {
            constraint.name: str(constraint.sqltext)
            for constraint in Knowledge.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        }
        self.assertEqual(
            check_constraints["ck_knowledge_items_business_type"],
            "business_type IN ('self_operated', 'aggregated')",
        )

    def test_business_type_is_required_and_restricted_in_write_schemas(self):
        base_knowledge = {
            "knowledge_origin": "business_accumulation",
            "title": "屏幕质检",
            "content": "检查屏幕显示是否正常",
            "category_id": "cat-qc-standard",
        }

        with self.assertRaises(ValidationError):
            KnowledgeCreate.model_validate(base_knowledge)
        with self.assertRaises(ValidationError):
            KnowledgeCreate.model_validate(
                {**base_knowledge, "business_type": "invalid"}
            )

        created = KnowledgeCreate.model_validate(
            {**base_knowledge, "business_type": "self_operated"}
        )
        self.assertEqual(created.business_type, "self_operated")

        self.assertIsNone(KnowledgeUpdate.model_validate({}).business_type)
        with self.assertRaises(ValidationError):
            KnowledgeUpdate.model_validate({"business_type": None})

        with self.assertRaises(ValidationError):
            CandidateSubmit.model_validate(
                {
                    "knowledge_origin": "business_accumulation",
                    "title": "候选知识",
                    "content": "候选内容",
                    "category_id": "cat-qc-standard",
                }
            )

    def test_search_filter_and_readonly_dictionary_contract(self):
        self.assertEqual(
            SearchRequest.model_validate(
                {
                    "query": "屏幕",
                    "knowledge_origin": "business_accumulation",
                    "business_type": "aggregated",
                }
            ).business_type,
            "aggregated",
        )
        with self.assertRaises(ValidationError):
            SearchRequest.model_validate(
                {
                    "query": "屏幕",
                    "knowledge_origin": "business_accumulation",
                }
            )
        with self.assertRaises(ValidationError):
            SearchRequest.model_validate(
                {
                    "query": "屏幕",
                    "knowledge_origin": "business_accumulation",
                    "business_type": "invalid",
                }
            )

        options = list_business_types()
        self.assertEqual(
            [option.model_dump() for option in options],
            [
                {"value": "self_operated", "label": "自营回收"},
                {"value": "aggregated", "label": "聚合回收"},
            ],
        )

        specification = app.openapi()
        self.assertIn("/api/v1/business-types", specification["paths"])
        response_schema = specification["paths"]["/api/v1/business-types"][
            "get"
        ]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response_schema["items"]["$ref"],
            "#/components/schemas/BusinessTypeOption",
        )

    @patch("app.routes.knowledge.cached_applicable_category_keys")
    def test_applicable_categories_are_isolated_by_business_type(
        self,
        cached_keys,
    ):
        cached_keys.side_effect = lambda business_type: {
            "self_operated": {"119", "平板电脑"},
            "aggregated": {"901", "黄金"},
        }[business_type]

        _validate_business_applicable_categories(
            business_type="self_operated",
            applicable_categories=[{"categoryId": "119"}],
        )
        _validate_business_applicable_categories(
            business_type="self_operated",
            applicable_categories=["平板电脑"],
        )

        with self.assertRaises(HTTPException) as raised:
            _validate_business_applicable_categories(
                business_type="aggregated",
                applicable_categories=["平板电脑"],
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("不属于聚合回收", raised.exception.detail)

    def test_changing_business_type_requires_resubmitting_full_applicability_scope(
        self,
    ):
        item = SimpleNamespace(
            id="A-00001",
            business_type="self_operated",
            category_id="cat-qc-standard",
            status=KnowledgeStatus.REVIEW,
            source="manual",
            created_by="tester",
            applicable_categories=["119"],
            applicable_brands=["1"],
            applicable_models=["11"],
        )
        query = MagicMock()
        query.filter.return_value.first.return_value = item
        db = MagicMock()
        db.query.return_value = query

        with self.assertRaises(HTTPException) as raised:
            update_knowledge(
                item.id,
                KnowledgeUpdate(business_type="aggregated"),
                db,
                SimpleNamespace(
                    username="admin",
                    role="super_admin",
                    permissions=[],
                ),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("必须重新提交适用类目、品牌和机型", raised.exception.detail)
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
