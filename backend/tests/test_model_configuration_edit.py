import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models.knowledge import (
    Category,
    Knowledge,
    KnowledgeChangeLog,
    KnowledgeMedia,
    KnowledgeStatus,
    KnowledgeTag,
    TagDimension,
    TagValue,
)
from app.routes.auth import has_permission
from app.routes.knowledge import update_model_configuration
from app.schemas.knowledge import ModelConfigurationUpdate
from app.services.model_configuration import (
    _find_existing_record,
    acquire_model_configuration_write_lock,
    ModelConfigurationAmbiguousError,
    find_exact_model_configuration,
    parse_model_configuration_payload,
    sync_model_configurations,
)


ATTRIBUTE_FIELDS = (
    "是否有卡槽",
    "Home键",
    "指纹识别",
    "3D面容",
    "内置手写笔",
    "闪光灯",
    "蜂窝网络",
    "光线传感器",
)


def _update_body(
    *,
    category_id: str = "120",
    category_name: str = "手机",
    brand_id: str = "200",
    brand_name: str = "测试品牌",
    model_id: str = "300",
    model_name: str = "测试机型 Pro",
) -> ModelConfigurationUpdate:
    return ModelConfigurationUpdate(
        title="测试机型 Pro 配置信息",
        content="指纹识别：屏下指纹；\n蜂窝网络：支持 5G；",
        category_id=category_id,
        category_name=category_name,
        brand_id=brand_id,
        brand_name=brand_name,
        model_id=model_id,
        model_name=model_name,
        attributes={
            "是否有卡槽": "",
            "Home键": "",
            "指纹识别": "屏下指纹",
            "3D面容": "",
            "内置手写笔": "支持",
            "闪光灯": "支持",
            "蜂窝网络": "支持 5G",
            "光线传感器": "",
        },
    )


class ModelConfigurationEditTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        for table in (
            Category.__table__,
            Knowledge.__table__,
            KnowledgeChangeLog.__table__,
            TagDimension.__table__,
            TagValue.__table__,
            KnowledgeTag.__table__,
            KnowledgeMedia.__table__,
        ):
            table.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()
        self.db.add(
            Category(
                id="cat-extra-knowledge",
                name="其他知识",
                level=1,
                sort_order=99,
            )
        )
        self.db.commit()
        self.user = SimpleNamespace(
            username="editor",
            role="super_support",
        )
        self.manhattan_cache = {
            "updated_at": "2026-08-18T00:00:00Z",
            "options_by_business_type": {
                "self_operated": {
                    "applicable_categories": [
                        {
                            "categoryId": 119,
                            "categoryName": "平板电脑",
                        },
                        {
                            "categoryId": 120,
                            "categoryName": "手机",
                        },
                    ],
                    "brands_by_category": {
                        "119": [
                            {
                                "brandId": 10530,
                                "brandName": "苹果",
                            }
                        ],
                        "120": [
                            {
                                "brandId": 200,
                                "brandName": "测试品牌",
                            }
                        ],
                    },
                    "models": [
                        {
                            "modelId": 97519,
                            "modelName": "iPad 10",
                            "categoryId": 119,
                            "brandId": 10530,
                        },
                        {
                            "modelId": 300,
                            "modelName": "测试机型 Pro",
                            "categoryId": 120,
                            "brandId": 200,
                        },
                    ],
                }
            },
        }
        self.cache_patcher = patch(
            "app.routes.knowledge.cached_manhattan_options_snapshot",
            return_value=self.manhattan_cache,
        )
        self.cache_patcher.start()

    def tearDown(self):
        self.cache_patcher.stop()
        self.db.close()
        self.engine.dispose()

    def _add_model_configuration(
        self,
        *,
        knowledge_id: str = "A-00001",
        source_record_id: str | None = "56383",
        category_id: str = "119",
        category_name: str = "平板电脑",
        brand_id: str = "10530",
        brand_name: str = "苹果",
        model_id: str = "97519",
        model_name: str = "iPad 10",
    ) -> Knowledge:
        source_fields = {
            "标题": f"{model_name} 配置信息",
            "品类ID": category_id,
            "品类": category_name,
            "品牌ID": brand_id,
            "品牌": brand_name,
            "型号ID": model_id,
            "型号": model_name,
            "综合内容": "Home键：支持；",
            "是否有卡槽": "蜂窝版有卡槽",
            "Home键": "支持",
            "来源工作表": "个性化配置信息",
            "来源行号": "18",
            "是否更新": "是",
            "_model_configuration_normalized_name_key": (
                f'["{category_name.casefold()}",'
                f'"{brand_name.casefold()}",'
                f'"{model_name.casefold()}"]'
            ),
            "_model_configuration_normalized_category_model_key": (
                f'["{category_name.casefold()}",'
                f'"{model_name.casefold()}"]'
            ),
        }
        if source_record_id:
            source_fields["知识ID"] = source_record_id
        item = Knowledge(
            id=knowledge_id,
            knowledge_origin="model_configuration",
            business_type="self_operated",
            title=source_fields["标题"],
            subtitles=[],
            content={
                "blocks": [
                    {"type": "text", "value": source_fields["综合内容"]}
                ]
            },
            category_id="cat-extra-knowledge",
            status=KnowledgeStatus.PUBLISHED,
            source="integration",
            quality_score=1.0,
            applicable_scenes=[],
            applicable_categories=[category_id],
            applicable_brands=[brand_id],
            applicable_models=[model_id],
            related_standard_items=[],
            source_record_id=source_record_id,
            source_knowledge_key=(
                f"model-configuration:{category_id}:{brand_id}:{model_id}"
            ),
            source_fields=source_fields,
            deduplication_metadata={},
            created_by="sync",
            updated_by="sync",
        )
        self.db.add(item)
        self.db.commit()
        return item

    def test_edit_updates_scope_attributes_and_exact_matches_without_vectors(self):
        self._add_model_configuration()
        body = _update_body(
            category_name="伪造类目名称",
            brand_name="过期品牌名称",
            model_name="错误机型名称",
        )

        with (
            patch("app.routes.knowledge.ensure_embedding") as ensure_embedding,
            patch(
                "app.routes.knowledge.ensure_search_embeddings"
            ) as ensure_search_embeddings,
            patch("app.routes.knowledge.save_embedding") as save_embedding,
            patch("app.routes.knowledge.embed_texts") as embed_texts,
        ):
            response = update_model_configuration(
                "A-00001",
                body,
                db=self.db,
                current_user=self.user,
            )

        ensure_embedding.assert_not_called()
        ensure_search_embeddings.assert_not_called()
        save_embedding.assert_not_called()
        embed_texts.assert_not_called()

        item = self.db.get(Knowledge, "A-00001")
        self.assertEqual(item.source_record_id, "56383")
        self.assertEqual(item.knowledge_origin, "model_configuration")
        self.assertEqual(item.business_type, "self_operated")
        self.assertEqual(item.category_id, "cat-extra-knowledge")
        self.assertEqual(item.status, KnowledgeStatus.PUBLISHED)
        self.assertEqual(item.source, "integration")
        self.assertEqual(item.applicable_categories, ["120"])
        self.assertEqual(item.applicable_brands, ["200"])
        self.assertEqual(item.applicable_models, ["300"])
        self.assertEqual(
            item.source_knowledge_key,
            "model-configuration:120:200:300",
        )
        self.assertEqual(item.source_fields["来源工作表"], "个性化配置信息")
        self.assertEqual(item.source_fields["来源行号"], "18")
        self.assertEqual(item.source_fields["是否更新"], "是")
        self.assertNotIn("是否有卡槽", item.source_fields)
        self.assertNotIn("Home键", item.source_fields)
        self.assertEqual(item.source_fields["指纹识别"], "屏下指纹")
        self.assertEqual(
            item.source_fields["_model_configuration_normalized_name_key"],
            '["手机","测试品牌","测试机型 pro"]',
        )
        self.assertEqual(
            item.source_fields[
                "_model_configuration_normalized_category_model_key"
            ],
            '["手机","测试机型 pro"]',
        )
        self.assertEqual(
            item.content,
            {
                "blocks": [
                    {
                        "type": "text",
                        "value": "指纹识别：屏下指纹；\n蜂窝网络：支持 5G；",
                    }
                ]
            },
        )

        detail = response["model_configuration"]
        self.assertEqual(detail["category_name"], "手机")
        self.assertEqual(detail["brand_name"], "测试品牌")
        self.assertEqual(detail["model_name"], "测试机型 Pro")
        self.assertEqual(detail["attributes"]["指纹识别"], "屏下指纹")
        self.assertEqual(detail["attributes"]["Home键"], "")
        self.assertEqual(set(detail["attributes"]), set(ATTRIBUTE_FIELDS))

        new_id_match = find_exact_model_configuration(
            self.db,
            category_id="120",
            brand_id="200",
            model_id="300",
        )
        self.assertIsNotNone(new_id_match)
        self.assertEqual(new_id_match.item.id, "A-00001")
        new_name_match = find_exact_model_configuration(
            self.db,
            category_name="手机",
            brand_name="测试品牌",
            model_name="测试机型 PRO",
        )
        self.assertIsNotNone(new_name_match)
        self.assertEqual(new_name_match.item.id, "A-00001")
        self.assertIsNone(
            find_exact_model_configuration(
                self.db,
                category_id="119",
                brand_id="10530",
                model_id="97519",
            )
        )
        self.assertIsNone(
            find_exact_model_configuration(
                self.db,
                category_name="平板电脑",
                brand_name="苹果",
                model_name="iPad 10",
            )
        )

        logs = self.db.query(KnowledgeChangeLog).all()
        self.assertEqual(len(logs), 1)
        self.assertTrue(
            {
                "title",
                "content",
                "applicable_categories",
                "applicable_brands",
                "applicable_models",
                "source_knowledge_key",
                "source_fields",
            }.issubset(set(logs[0].changed_fields))
        )

        update_model_configuration(
            "A-00001",
            body,
            db=self.db,
            current_user=self.user,
        )
        self.assertEqual(self.db.query(KnowledgeChangeLog).count(), 1)

    def test_edit_without_external_source_id_updates_the_same_knowledge(self):
        self._add_model_configuration(source_record_id=None)

        response = update_model_configuration(
            "A-00001",
            _update_body(),
            db=self.db,
            current_user=self.user,
        )

        item = self.db.get(Knowledge, "A-00001")
        self.assertIsNone(item.source_record_id)
        self.assertEqual(
            item.source_knowledge_key,
            "model-configuration:120:200:300",
        )
        self.assertEqual(item.title, "测试机型 Pro 配置信息")
        self.assertNotIn("知识ID", item.source_fields)
        self.assertEqual(response["id"], "A-00001")

    def test_sync_accepts_multiple_records_without_external_source_ids(self):
        records = parse_model_configuration_payload(
            {
                "records": [
                    {
                        "title": "无外部ID机型一",
                        "category_id": "119",
                        "category_name": "平板电脑",
                        "brand_id": "10530",
                        "brand_name": "苹果",
                        "model_id": "97520",
                        "model_name": "无外部ID机型一",
                        "content": "配置正文一",
                    },
                    {
                        "source_record_id": "",
                        "title": "无外部ID机型二",
                        "category_id": "119",
                        "category_name": "平板电脑",
                        "brand_id": "10530",
                        "brand_name": "苹果",
                        "model_id": "97521",
                        "model_name": "无外部ID机型二",
                        "content": "配置正文二",
                    },
                ]
            }
        )

        with patch(
            "app.services.model_configuration._generate_knowledge_id",
            side_effect=["A-00010", "A-00011"],
        ):
            first_result = sync_model_configurations(
                self.db,
                records,
                actor="excel-importer",
            )
        self.db.commit()

        second_result = sync_model_configurations(
            self.db,
            records,
            actor="excel-importer",
        )
        self.db.commit()

        self.assertEqual(first_result.created, 2)
        self.assertEqual(second_result.unchanged, 2)
        saved = (
            self.db.query(Knowledge)
            .filter(Knowledge.id.in_(["A-00010", "A-00011"]))
            .order_by(Knowledge.id)
            .all()
        )
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(item.source_record_id is None for item in saved))
        self.assertTrue(
            all("知识ID" not in item.source_fields for item in saved)
        )

    def test_blank_external_source_id_clears_existing_trace_value(self):
        self._add_model_configuration()
        record = parse_model_configuration_payload(
            {
                "records": [
                    {
                        "source_record_id": "",
                        "title": "iPad 10 配置信息",
                        "category_id": "119",
                        "category_name": "平板电脑",
                        "brand_id": "10530",
                        "brand_name": "苹果",
                        "model_id": "97519",
                        "model_name": "iPad 10",
                        "content": "更新后的配置正文",
                    }
                ]
            }
        )[0]

        result = sync_model_configurations(
            self.db,
            [record],
            actor="excel-importer",
        )
        self.db.commit()

        item = self.db.get(Knowledge, "A-00001")
        self.assertEqual(result.updated, 1)
        self.assertIsNone(item.source_record_id)
        self.assertNotIn("知识ID", item.source_fields)

    def test_brand_is_strict_when_present_and_pair_is_ambiguous_without_it(self):
        self._add_model_configuration()
        self._add_model_configuration(
            knowledge_id="A-00002",
            source_record_id="56384",
            brand_id="10531",
            brand_name="另一品牌",
            model_id="97519",
            model_name="iPad 10",
        )

        strict_match = find_exact_model_configuration(
            self.db,
            category_id="119",
            brand_id="10530",
            model_id="97519",
        )
        self.assertIsNotNone(strict_match)
        self.assertEqual(strict_match.item.id, "A-00001")
        self.assertIsNone(
            find_exact_model_configuration(
                self.db,
                category_id="119",
                brand_id="不存在的品牌",
                model_id="97519",
            )
        )

        with self.assertRaises(ModelConfigurationAmbiguousError) as raised:
            find_exact_model_configuration(
                self.db,
                category_id="119",
                model_id="97519",
            )
        self.assertEqual(
            set(raised.exception.knowledge_ids),
            {"A-00001", "A-00002"},
        )

        with self.assertRaises(ModelConfigurationAmbiguousError):
            find_exact_model_configuration(
                self.db,
                category_name="平板电脑",
                model_name="iPad 10",
            )

    def test_names_can_change_without_changing_id_scope(self):
        self._add_model_configuration()
        self.manhattan_cache["options_by_business_type"]["self_operated"][
            "applicable_categories"
        ][0]["categoryName"] = "平板与二合一电脑"
        self.manhattan_cache["options_by_business_type"]["self_operated"][
            "brands_by_category"
        ]["119"][0]["brandName"] = "Apple"
        self.manhattan_cache["options_by_business_type"]["self_operated"][
            "models"
        ][0]["modelName"] = "iPad 第十代"
        body = _update_body(
            category_id="119",
            category_name="平板电脑",
            brand_id="10530",
            brand_name="苹果",
            model_id="97519",
            model_name="iPad 10",
        )

        update_model_configuration(
            "A-00001",
            body,
            db=self.db,
            current_user=self.user,
        )

        item = self.db.get(Knowledge, "A-00001")
        self.assertEqual(
            item.source_knowledge_key,
            "model-configuration:119:10530:97519",
        )
        self.assertEqual(item.source_fields["品类"], "平板与二合一电脑")
        self.assertEqual(item.source_fields["品牌"], "Apple")
        self.assertEqual(item.source_fields["型号"], "iPad 第十代")
        self.assertEqual(
            item.source_fields[
                "_model_configuration_normalized_category_model_key"
            ],
            '["平板与二合一电脑","ipad 第十代"]',
        )
        self.assertIsNotNone(
            find_exact_model_configuration(
                self.db,
                category_name="平板与二合一电脑",
                brand_name="apple",
                model_name="IPAD 第十代",
            )
        )
        self.assertIsNone(
            find_exact_model_configuration(
                self.db,
                category_name="平板电脑",
                brand_name="苹果",
                model_name="iPad 10",
            )
        )

    def test_scope_conflict_rolls_back_the_whole_edit(self):
        self._add_model_configuration()
        self._add_model_configuration(
            knowledge_id="A-00002",
            source_record_id="56384",
            category_id="120",
            category_name="手机",
            brand_id="200",
            brand_name="测试品牌",
            model_id="300",
            model_name="测试机型 Pro",
        )

        with self.assertRaises(HTTPException) as raised:
            update_model_configuration(
                "A-00001",
                _update_body(),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "SOURCE_IDENTIFIER_CONFLICT",
        )
        self.db.expire_all()
        original = self.db.get(Knowledge, "A-00001")
        self.assertEqual(
            original.source_knowledge_key,
            "model-configuration:119:10530:97519",
        )
        self.assertEqual(original.title, "iPad 10 配置信息")
        self.assertEqual(self.db.query(KnowledgeChangeLog).count(), 0)

    def test_put_and_excel_sync_share_the_same_lock_order(self):
        self._add_model_configuration()
        events: list[str] = []

        def put_lock(db):
            events.append("put-lock")
            return acquire_model_configuration_write_lock(db)

        def sync_lock(db):
            events.append("sync-lock")
            return acquire_model_configuration_write_lock(db)

        with (
            patch(
                "app.routes.knowledge.acquire_model_configuration_write_lock",
                side_effect=put_lock,
            ) as put_lock_mock,
            patch(
                "app.services.model_configuration."
                "acquire_model_configuration_write_lock",
                side_effect=sync_lock,
            ) as sync_lock_mock,
        ):
            update_model_configuration(
                "A-00001",
                _update_body(
                    category_id="119",
                    category_name="过期类目名称",
                    brand_id="10530",
                    brand_name="过期品牌名称",
                    model_id="97519",
                    model_name="过期机型名称",
                ),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(events, ["put-lock", "sync-lock"])
        put_lock_mock.assert_called_once_with(self.db)
        sync_lock_mock.assert_called_once_with(self.db)

        record = parse_model_configuration_payload(
            {
                "records": [
                    {
                        "source_record_id": "60000",
                        "title": "新机型配置",
                        "category_id": "119",
                        "category_name": "平板电脑",
                        "brand_id": "10530",
                        "brand_name": "苹果",
                        "model_id": "60001",
                        "model_name": "新机型",
                        "content": "配置正文",
                    }
                ]
            }
        )[0]
        service_events: list[str] = []
        with (
            patch(
                "app.services.model_configuration."
                "acquire_model_configuration_write_lock",
                side_effect=lambda db: service_events.append("lock"),
            ),
            patch(
                "app.services.model_configuration._find_existing_record",
                side_effect=lambda db, current, **kwargs: (
                    service_events.append("lookup") or None
                ),
            ),
            patch(
                "app.services.model_configuration._generate_knowledge_id",
                return_value="A-60000",
            ),
        ):
            sync_model_configurations(
                self.db,
                [record],
                actor="excel-importer",
            )
        self.assertEqual(service_events, ["lock", "lookup"])

    def test_locked_lookup_refreshes_a_stale_identity_map_record(self):
        self._add_model_configuration()
        stale_item = self.db.get(Knowledge, "A-00001")
        self.assertEqual(stale_item.source_fields["来源行号"], "18")

        with self.session_factory() as concurrent_db:
            concurrent_item = concurrent_db.get(Knowledge, "A-00001")
            concurrent_item.source_fields = {
                **concurrent_item.source_fields,
                "来源行号": "99",
                "并发写入标记": "Excel 已更新",
            }
            concurrent_db.commit()

        record = parse_model_configuration_payload(
            {
                "records": [
                    {
                        "source_record_id": "56383",
                        "title": "iPad 10 配置信息",
                        "category_id": "119",
                        "category_name": "平板电脑",
                        "brand_id": "10530",
                        "brand_name": "苹果",
                        "model_id": "97519",
                        "model_name": "iPad 10",
                        "content": "Home键：支持；",
                    }
                ]
            }
        )[0]

        refreshed = _find_existing_record(self.db, record)

        self.assertIs(refreshed, stale_item)
        self.assertEqual(refreshed.source_fields["来源行号"], "99")
        self.assertEqual(
            refreshed.source_fields["并发写入标记"],
            "Excel 已更新",
        )

    def test_dynamic_scope_cache_fails_closed(self):
        self._add_model_configuration()
        self.manhattan_cache["options_by_business_type"]["self_operated"][
            "brands_by_category"
        ]["120"] = [
            {
                "brandId": 201,
                "brandName": "其他品牌",
            }
        ]

        with self.assertRaises(HTTPException) as raised:
            update_model_configuration(
                "A-00001",
                _update_body(),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail["code"],
            "MODEL_CONFIGURATION_BRAND_SCOPE_INVALID",
        )

        self.manhattan_cache["options_by_business_type"]["self_operated"][
            "applicable_categories"
        ] = []
        with self.assertRaises(HTTPException) as raised:
            update_model_configuration(
                "A-00001",
                _update_body(),
                db=self.db,
                current_user=self.user,
            )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail["code"],
            "MANHATTAN_CACHE_UNAVAILABLE",
        )

    def test_non_model_configuration_is_rejected(self):
        item = Knowledge(
            id="A-10000",
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            title="普通知识",
            subtitles=[],
            content={"blocks": [{"type": "text", "value": "正文"}]},
            category_id="cat-extra-knowledge",
            status=KnowledgeStatus.PUBLISHED,
            source="manual",
            quality_score=1.0,
            applicable_scenes=[],
            applicable_categories=[],
            applicable_brands=[],
            applicable_models=[],
            related_standard_items=[],
            source_fields={},
            deduplication_metadata={},
            created_by="editor",
            updated_by="editor",
        )
        self.db.add(item)
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            update_model_configuration(
                item.id,
                _update_body(),
                db=self.db,
                current_user=self.user,
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail["code"],
            "MODEL_CONFIGURATION_ONLY",
        )

    def test_request_contract_requires_all_attributes_and_locks_source_id(self):
        payload = _update_body().model_dump(by_alias=True)
        del payload["attributes"]["Home键"]
        with self.assertRaises(ValidationError):
            ModelConfigurationUpdate.model_validate(payload)

        payload = _update_body().model_dump(by_alias=True)
        payload["source_record_id"] = "should-not-change"
        with self.assertRaises(ValidationError):
            ModelConfigurationUpdate.model_validate(payload)

    def test_openapi_and_role_contract(self):
        specification = app.openapi()
        path = (
            specification["paths"][
                "/api/v1/knowledge/{knowledge_id}/model-configuration"
            ]["put"]
        )
        request_schema_ref = path["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        self.assertEqual(
            request_schema_ref,
            "#/components/schemas/ModelConfigurationUpdate",
        )
        schemas = specification["components"]["schemas"]
        update_schema = schemas["ModelConfigurationUpdate"]
        self.assertNotIn(
            "source_record_id",
            update_schema["properties"],
        )
        attribute_schema = schemas["ModelConfigurationAttributes"]
        self.assertEqual(
            set(attribute_schema["required"]),
            set(ATTRIBUTE_FIELDS),
        )
        self.assertIn(
            "model_configuration",
            schemas["KnowledgeResponse"]["properties"],
        )

        self.assertTrue(
            has_permission(
                SimpleNamespace(role="super_support"),
                "knowledge:edit_published",
            )
        )
        self.assertTrue(
            has_permission(
                SimpleNamespace(role="super_admin"),
                "knowledge:edit_published",
            )
        )
        self.assertFalse(
            has_permission(
                SimpleNamespace(role="senior_support"),
                "knowledge:edit_published",
            )
        )


if __name__ == "__main__":
    unittest.main()
