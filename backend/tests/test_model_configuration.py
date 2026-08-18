import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.models.knowledge import KnowledgeStatus
from app.routes.integration import (
    submit_candidate_reviews,
    update_candidate_review,
)
from app.routes.knowledge import (
    _create_knowledge_item,
    _model_configuration_detail,
    delete_knowledge,
    delete_media,
    deprecate_knowledge,
    restore_knowledge,
    update_media,
    upload_media,
)
from app.schemas.integration import (
    CandidateReviewBatchSubmit,
    CandidateReviewUpdate,
)
from app.schemas.knowledge import KnowledgeCreate
from app.services.model_configuration import (
    MODEL_CONFIGURATION_ATTRIBUTE_FIELDS,
    ModelConfigurationAmbiguousError,
    ModelConfigurationMatch,
    ModelConfigurationSyncError,
    find_exact_model_configuration,
    parse_model_configuration_payload,
    sync_model_configurations,
)


def _payload_record(index: int = 1):
    return {
        "source_record_id": str(56000 + index),
        "title": f"测试平板 {index} 机型的硬件与基础信息",
        "brand_id": "10530",
        "brand_name": "苹果",
        "model_id": str(97000 + index),
        "model_name": f"测试平板 {index}",
        "content": "指纹识别：有指纹；",
        "source_fields": {
            "知识ID": str(56000 + index),
            "品牌ID": "10530",
            "品牌": "苹果",
            "型号ID": str(97000 + index),
            "型号": f"测试平板 {index}",
            "指纹识别": "有指纹",
        },
    }


def _knowledge(
    *,
    category_id: str = "119",
    category_name: str = "平板电脑",
    brand_id: str = "10530",
    brand_name: str = "苹果",
    model_id: str = "97519",
    model_name: str = "iPad 10 (2022) 10.9英寸",
):
    return SimpleNamespace(
        id="A-00001",
        source_fields={
            "品类ID": category_id,
            "品类": category_name,
            "品牌ID": brand_id,
            "品牌": brand_name,
            "型号ID": model_id,
            "型号": model_name,
        },
    )


def _db_with_query_results(*results):
    db = MagicMock()
    query = MagicMock()
    query.filter.return_value = query
    query.limit.return_value = query
    query.all.side_effect = list(results)
    db.query.return_value = query
    return db


class ModelConfigurationTests(unittest.TestCase):
    def test_payload_accepts_more_than_generic_excel_limit(self):
        payload = {
            "category_id": "119",
            "category_name": "平板电脑",
            "records": [_payload_record(index) for index in range(1, 833)],
        }

        records = parse_model_configuration_payload(payload)

        self.assertEqual(len(records), 832)
        self.assertEqual(records[0].category_id, "119")
        self.assertEqual(records[-1].source_record_id, str(56000 + 832))

    def test_payload_rejects_duplicate_source_or_model_ids(self):
        duplicated_source = _payload_record(1)
        duplicated_model = _payload_record(2)
        duplicated_model["model_id"] = duplicated_source["model_id"]
        with self.assertRaisesRegex(
            ModelConfigurationSyncError,
            "型号ID组合",
        ):
            parse_model_configuration_payload(
                {"records": [duplicated_source, duplicated_model]}
            )

        duplicate_record = _payload_record(1)
        duplicate_record["model_id"] = "99999"
        with self.assertRaisesRegex(
            ModelConfigurationSyncError,
            "知识ID",
        ):
            parse_model_configuration_payload(
                {"records": [_payload_record(1), duplicate_record]}
            )

    def test_brand_triplet_has_priority_over_mismatched_names(self):
        item = _knowledge()
        db = _db_with_query_results([item])
        match = find_exact_model_configuration(
            db,
            category_id="119",
            category_name="其他品类",
            brand_id="10530",
            brand_name="其他品牌",
            model_id="97519",
            model_name="其他机型",
        )

        self.assertIsInstance(match, ModelConfigurationMatch)
        self.assertIs(match.item, item)
        self.assertEqual(match.match_mode, "id")
        self.assertEqual(db.query.call_count, 1)
        db.query.return_value.limit.assert_called_once_with(2)
        query_expressions = " ".join(
            str(expression)
            for call in db.query.return_value.filter.call_args_list
            for expression in call.args
        )
        self.assertIn(
            "knowledge_items.source_knowledge_key",
            query_expressions,
        )

    def test_category_model_id_pair_matches_when_brand_is_absent(self):
        item = _knowledge()
        db = _db_with_query_results([item])

        match = find_exact_model_configuration(
            db,
            category_id="119",
            model_id="97519",
        )

        self.assertIsNotNone(match)
        self.assertIs(match.item, item)
        self.assertEqual(match.match_mode, "id")
        query_expressions = " ".join(
            str(expression)
            for call in db.query.return_value.filter.call_args_list
            for expression in call.args
        )
        self.assertIn("knowledge_items.source_fields", query_expressions)

    def test_brand_name_triplet_is_strict_nfkc_fallback(self):
        item = _knowledge(
            model_name="ＩＰＡＤ 10   (2022) 10.9英寸",
        )
        db = _db_with_query_results([item], [])
        match = find_exact_model_configuration(
            db,
            category_name=" 平板电脑 ",
            brand_name="苹果",
            model_name="ipad 10 (2022) 10.9英寸",
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.match_mode, "name")
        self.assertEqual(db.query.call_count, 2)
        self.assertEqual(db.query.return_value.limit.call_count, 1)

    def test_category_model_name_pair_uses_hidden_key_without_brand(self):
        item = _knowledge(
            model_name="ＩＰＡＤ 10   (2022) 10.9英寸",
        )
        db = _db_with_query_results([item], [])

        match = find_exact_model_configuration(
            db,
            category_name=" 平板电脑 ",
            model_name="ipad 10 (2022) 10.9英寸",
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.match_mode, "name")
        self.assertEqual(db.query.call_count, 2)
        self.assertEqual(db.query.return_value.limit.call_count, 1)
        bound_values = [
            value
            for call in db.query.return_value.filter.call_args_list
            for expression in call.args
            for value in expression.compile().params.values()
        ]
        self.assertIn(
            "_model_configuration_normalized_category_model_key",
            bound_values,
        )

    def test_missing_or_wrong_scope_returns_no_match(self):
        item = _knowledge()
        missing_db = _db_with_query_results()
        self.assertIsNone(
            find_exact_model_configuration(
                missing_db,
                category_name="平板电脑",
                brand_name="苹果",
                model_name="",
            )
        )
        missing_db.query.assert_not_called()

        self.assertIsNone(
            find_exact_model_configuration(
                _db_with_query_results([], [item]),
                category_name="平板电脑",
                brand_name="华为",
                model_name="iPad 10 (2022) 10.9英寸",
            )
        )
        self.assertIsNone(
            find_exact_model_configuration(
                _db_with_query_results([]),
                category_id="119",
                category_name="平板电脑",
                brand_id="错误品牌ID",
                brand_name="苹果",
                model_id="97519",
                model_name="iPad 10 (2022) 10.9英寸",
            )
        )

    def test_multiple_exact_rows_fail_closed(self):
        duplicate_matches = [_knowledge(), _knowledge()]
        with self.assertRaises(ModelConfigurationAmbiguousError) as raised:
            find_exact_model_configuration(
                _db_with_query_results(duplicate_matches),
                category_id="119",
                model_id="97519",
            )
        self.assertEqual(raised.exception.match_mode, "id")
        self.assertEqual(
            raised.exception.knowledge_ids,
            ("A-00001", "A-00001"),
        )

        with self.assertRaises(ModelConfigurationAmbiguousError) as raised:
            find_exact_model_configuration(
                _db_with_query_results(duplicate_matches, []),
                category_name="平板电脑",
                model_name="iPad 10 (2022) 10.9英寸",
            )
        self.assertEqual(raised.exception.match_mode, "name")

    def test_managed_origin_rejects_generic_create_path(self):
        with self.assertRaises(ValidationError):
            KnowledgeCreate(
                knowledge_origin="model_configuration",
                business_type="self_operated",
                title="iPad 机型配置",
                content="配置正文",
                category_id="cat-extra-knowledge",
            )

        with self.assertRaises(HTTPException) as raised:
            _create_knowledge_item(
                SimpleNamespace(knowledge_origin="model_configuration"),
                MagicMock(),
                SimpleNamespace(username="tester"),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("飞书专用同步", raised.exception.detail)

    def test_managed_origin_rejects_manual_lifecycle_changes(self):
        item = SimpleNamespace(
            knowledge_origin="model_configuration",
            status=KnowledgeStatus.PUBLISHED,
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = item

        for operation, args in (
            (delete_knowledge, ("A-00001", db, None)),
            (deprecate_knowledge, ("A-00001", db, None)),
            (
                restore_knowledge,
                ("A-00001", db, SimpleNamespace(username="tester")),
            ),
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(HTTPException) as raised:
                    operation(*args)
                self.assertEqual(raised.exception.status_code, 422)

    def test_managed_origin_rejects_media_mutations(self):
        item = SimpleNamespace(knowledge_origin="model_configuration")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = item
        user = SimpleNamespace(username="tester")

        with self.assertRaises(HTTPException) as upload_error:
            asyncio.run(
                upload_media(
                    "A-00001",
                    MagicMock(),
                    db=db,
                    current_user=user,
                )
            )
        self.assertEqual(upload_error.exception.status_code, 422)

        for operation in (update_media, delete_media):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(HTTPException) as raised:
                    operation(
                        "A-00001",
                        "image.png",
                        db=db,
                        current_user=user,
                    )
                self.assertEqual(raised.exception.status_code, 422)

    def test_historical_managed_candidate_cannot_be_edited_or_submitted(self):
        edit_item = SimpleNamespace(
            review_status="ready",
            candidate_payload={
                "knowledge": {
                    "knowledge_origin": "model_configuration",
                }
            },
        )
        edit_db = MagicMock()
        edit_db.query.return_value.filter.return_value.first.return_value = (
            edit_item
        )

        with self.assertRaises(HTTPException) as raised:
            update_candidate_review(
                "ing-managed-edit",
                CandidateReviewUpdate(title="不能修改"),
                edit_db,
                SimpleNamespace(username="reviewer"),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("历史候选", raised.exception.detail)
        edit_db.commit.assert_not_called()

        submit_item = SimpleNamespace(
            id="ing-managed-submit",
            knowledge_id=None,
            review_status="ready",
            status="candidate_ready",
            candidate_payload={
                "knowledge": {
                    "knowledge_origin": "model_configuration",
                }
            },
            review_metadata={},
            error_code=None,
            error_message=None,
        )
        submit_db = MagicMock()
        submit_db.query.return_value.filter.return_value.first.return_value = (
            submit_item
        )

        response = submit_candidate_reviews(
            CandidateReviewBatchSubmit(
                ingestion_ids=[submit_item.id],
            ),
            submit_db,
            SimpleNamespace(username="reviewer"),
        )

        self.assertEqual(response.failed, 1)
        self.assertEqual(
            response.results[0].error_code,
            "KNOWLEDGE_ORIGIN_MANAGED",
        )
        self.assertEqual(submit_item.review_status, "failed")
        self.assertIn("飞书专用同步", submit_item.error_message)

    @patch(
        "app.services.model_configuration._generate_knowledge_id",
        return_value="A-00001",
    )
    @patch(
        "app.services.model_configuration._find_existing_record",
        return_value=None,
    )
    def test_dedicated_sync_creates_published_items_without_embeddings(
        self,
        _find_existing,
        _generate_id,
    ):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = (
            "cat-extra-knowledge"
        )
        personalized_attributes = {
            "是否有卡槽": "蜂窝版有卡槽",
            "Home键": "不支持",
            "指纹识别": "屏下指纹",
            "3D面容": "支持",
            "内置手写笔": "支持",
            "闪光灯": "双闪光灯",
            "蜂窝网络": "支持 5G",
            "光线传感器": "支持",
        }
        payload_record = _payload_record(1)
        payload_record["source_fields"].update(personalized_attributes)
        record = parse_model_configuration_payload(
            {"records": [payload_record]}
        )[0]

        result = sync_model_configurations(
            db,
            [record],
            actor="test-sync",
        )

        self.assertEqual(result.created, 1)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].knowledge_id, "A-00001")
        self.assertEqual(result.items[0].operation, "created")
        created = db.add.call_args.args[0]
        self.assertEqual(created.id, "A-00001")
        self.assertEqual(created.knowledge_origin, "model_configuration")
        self.assertEqual(created.business_type, "self_operated")
        self.assertEqual(created.status, KnowledgeStatus.PUBLISHED)
        self.assertEqual(created.applicable_categories, ["119"])
        self.assertEqual(created.applicable_brands, ["10530"])
        self.assertEqual(created.applicable_models, ["97001"])
        self.assertEqual(
            {
                field: created.source_fields[field]
                for field in MODEL_CONFIGURATION_ATTRIBUTE_FIELDS
            },
            personalized_attributes,
        )
        self.assertEqual(
            _model_configuration_detail(created)["attributes"],
            personalized_attributes,
        )
        self.assertEqual(
            created.source_fields[
                "_model_configuration_normalized_name_key"
            ],
            '["平板电脑","苹果","测试平板 1"]',
        )
        self.assertEqual(
            created.source_fields[
                "_model_configuration_normalized_category_model_key"
            ],
            '["平板电脑","测试平板 1"]',
        )


if __name__ == "__main__":
    unittest.main()
