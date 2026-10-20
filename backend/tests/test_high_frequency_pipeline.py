import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.high_frequency import (
    HighFrequencyAtomicQuestion,
    HighFrequencyProcessingTask,
    HighFrequencySourceRecord,
    HighFrequencyStandardQuestion,
)
from app.services import high_frequency_pipeline as pipeline


def _record(record_id: str, core_problem: str) -> dict:
    return {
        "工单ID": record_id,
        "分析时间": "2026-10-12 10:00:00",
        "产品类型": "手机",
        "聊天内容": "用户咨询原始问题",
        "ai_result": {
            "核心问题": core_problem,
            "判定结论": "按规则判定",
            "参考话术": "请按标准核验",
        },
    }


class HighFrequencyPipelineTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            engine,
            tables=[
                HighFrequencySourceRecord.__table__,
                HighFrequencyStandardQuestion.__table__,
                HighFrequencyAtomicQuestion.__table__,
                HighFrequencyProcessingTask.__table__,
            ],
        )
        self.session = sessionmaker(bind=engine)()

    def tearDown(self):
        self.session.close()

    def test_same_batch_is_idempotent_and_creates_no_new_task(self):
        records = [_record("1", "电池是否为正品部件"), _record("2", "电池是否为正品部件")]

        first = pipeline.ingest_records(self.session, records)
        second = pipeline.ingest_records(self.session, records)

        self.assertEqual(first["queued"], 2)
        self.assertEqual(second["unchanged"], 2)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(self.session.query(HighFrequencyProcessingTask).count(), 2)

    def test_changed_record_is_requeued_and_old_atomic_mapping_is_removed(self):
        pipeline.ingest_records(self.session, [_record("1", "电池是否为正品部件")])
        source = self.session.query(HighFrequencySourceRecord).one()
        self.session.add(
            HighFrequencyAtomicQuestion(
                id="hfaq-existing",
                source_record_id=source.id,
                atomic_index=1,
                atomic_question="电池是否为正品部件",
                normalized_question="电池是否为正品部件",
                split_status="单问题",
                processing_status="ready",
            )
        )
        self.session.commit()

        result = pipeline.ingest_records(self.session, [_record("1", "屏幕色斑如何判定")])

        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(self.session.query(HighFrequencyAtomicQuestion).count(), 0)
        self.assertEqual(
            self.session.query(HighFrequencyProcessingTask)
            .filter(HighFrequencyProcessingTask.status == "queued")
            .count(),
            1,
        )

    def test_same_failed_record_is_explicitly_requeued_by_sync(self):
        record = _record("1", "电池是否为正品部件")
        pipeline.ingest_records(self.session, [record])
        source = self.session.query(HighFrequencySourceRecord).one()
        source.processing_status = "failed"
        self.session.commit()

        result = pipeline.ingest_records(self.session, [record])

        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(result["queued"], 1)

    def test_process_reuses_existing_atomic_mapping_without_model_call(self):
        ready_source = HighFrequencySourceRecord(
            id="hfs-ready",
            record_id="ready-1",
            source_hash="a" * 64,
            analyzed_at="2026-10-11",
            category="手机",
            processing_status="ready",
        )
        standard = HighFrequencyStandardQuestion(
            id="hfsq-ready",
            category="手机",
            normalized_question="电池是否为正品部件",
            standard_question="电池是否为正品部件",
            business_theme="配件核验",
            prompt_version=pipeline.PROMPT_VERSION,
        )
        ready_atomic = HighFrequencyAtomicQuestion(
            id="hfaq-ready",
            source_record_id=ready_source.id,
            atomic_index=1,
            atomic_question="电池是否为正品部件",
            normalized_question="电池是否为正品部件",
            split_status="单问题",
            processing_status="ready",
            standard_question_id=standard.id,
        )
        source = HighFrequencySourceRecord(
            id="hfs-new",
            record_id="new-1",
            source_hash="b" * 64,
            analyzed_at="2026-10-12",
            category="手机",
            core_problem="电池是否为正品部件",
            processing_status="running",
        )
        self.session.add_all([ready_source, standard, ready_atomic, source])
        self.session.commit()

        with patch.object(pipeline, "classify_incremental_question") as classify:
            status, error = pipeline._process_source(self.session, source)
            self.session.commit()

        self.assertEqual((status, error), ("completed", ""))
        classify.assert_not_called()
        stored = (
            self.session.query(HighFrequencyAtomicQuestion)
            .filter(HighFrequencyAtomicQuestion.source_record_id == source.id)
            .one()
        )
        self.assertEqual(stored.standard_question_id, standard.id)
        self.assertEqual(stored.mapping_source, "database")

    def test_process_prefers_deterministic_rule_over_existing_model_mapping(self):
        ready_source = HighFrequencySourceRecord(
            id="hfs-earphone-ready",
            record_id="earphone-ready",
            source_hash="a" * 64,
            analyzed_at="2026-10-11",
            category="耳机",
            processing_status="ready",
        )
        old_standard = HighFrequencyStandardQuestion(
            id="hfsq-earphone-model",
            category="耳机",
            normalized_question="耳机定位查找功能正常能否回收如何判定",
            standard_question="耳机定位查找功能正常能否回收如何判定",
            business_theme="定位检测",
            prompt_version=pipeline.PROMPT_VERSION,
        )
        ready_atomic = HighFrequencyAtomicQuestion(
            id="hfaq-earphone-ready",
            source_record_id=ready_source.id,
            atomic_index=1,
            atomic_question="定位查找功能正常应如何判定",
            normalized_question="定位查找功能正常应如何判定",
            split_status="单问题",
            processing_status="ready",
            standard_question_id=old_standard.id,
            mapping_source="model",
        )
        source = HighFrequencySourceRecord(
            id="hfs-earphone-new",
            record_id="earphone-new",
            source_hash="b" * 64,
            analyzed_at="2026-10-12",
            category="耳机",
            core_problem="定位查找功能正常应如何判定",
            processing_status="running",
        )
        self.session.add_all([ready_source, old_standard, ready_atomic, source])
        self.session.commit()

        with patch.object(pipeline, "classify_incremental_question") as classify:
            status, error = pipeline._process_source(self.session, source)
            self.session.commit()

        self.assertEqual((status, error), ("completed", ""))
        classify.assert_not_called()
        stored = (
            self.session.query(HighFrequencyAtomicQuestion)
            .filter(HighFrequencyAtomicQuestion.source_record_id == source.id)
            .one()
        )
        standard = self.session.get(
            HighFrequencyStandardQuestion,
            stored.standard_question_id,
        )
        self.assertEqual(stored.mapping_source, "deterministic")
        self.assertEqual(
            standard.standard_question,
            "耳机定位查找功能正常如何判定",
        )

    def test_incomplete_incremental_title_is_automatically_excluded_not_failed(self):
        source = HighFrequencySourceRecord(
            id="hfs-review",
            record_id="review-1",
            source_hash="c" * 64,
            analyzed_at="2026-10-12",
            category="手机",
            core_problem="屏幕点状异常如何判定",
            processing_status="running",
        )
        self.session.add(source)
        self.session.commit()

        with patch.object(
            pipeline,
            "classify_incremental_question",
            return_value=(None, "DeepSeek 高频问题增量归类返回不完整。"),
        ):
            status, error = pipeline._process_source(self.session, source)
            self.session.commit()

        self.assertEqual((status, error), ("completed", ""))
        self.assertEqual(source.processing_status, "review")
        self.assertIn("自动排除", source.error_message)

    def test_repeat_sync_reconciles_new_deterministic_mapping_without_model_task(self):
        record = _record(
            "1",
            "关于本机显示电池正品部件，但一根线工具读出电池异常如何勾选",
        )
        pipeline.ingest_records(self.session, [record])
        source = self.session.query(HighFrequencySourceRecord).one()
        standard = HighFrequencyStandardQuestion(
            id="hfsq-old",
            category="手机",
            normalized_question="旧标题",
            standard_question="旧标题",
            business_theme="旧主题",
            prompt_version=pipeline.PROMPT_VERSION,
        )
        atomic = HighFrequencyAtomicQuestion(
            id="hfaq-old",
            source_record_id=source.id,
            atomic_index=1,
            atomic_question=record["ai_result"]["核心问题"],
            normalized_question="关于本机显示电池正品部件但一根线工具读出电池异常如何勾选",
            split_status="单问题",
            processing_status="ready",
            standard_question_id=standard.id,
            mapping_source="model",
        )
        source.processing_status = "ready"
        self.session.add_all([standard, atomic])
        self.session.commit()

        result = pipeline.ingest_records(self.session, [record])
        self.session.refresh(atomic)
        repaired = self.session.get(
            HighFrequencyStandardQuestion,
            atomic.standard_question_id,
        )

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["rule_reconciled"], 1)
        self.assertEqual(atomic.mapping_source, "deterministic")
        self.assertEqual(
            repaired.standard_question,
            "本机显示正品部件但工具读出电池异常如何判定",
        )

    def test_repeat_sync_reconciles_category_alias_and_semantic_mapping(self):
        record = {
            "工单ID": "earphone-1",
            "分析时间": "2026-10-12 10:00:00",
            "产品类型": "耳机/耳麦",
            "ai_result": {
                "核心问题": "耳机定位功能正常能否回收如何判定",
            },
        }
        pipeline.ingest_records(self.session, [record])
        source = self.session.query(HighFrequencySourceRecord).one()
        old_standard = HighFrequencyStandardQuestion(
            id="hfsq-earphone-old",
            category="耳机/耳麦",
            normalized_question="耳机定位功能正常能否回收如何判定",
            standard_question="耳机定位功能正常能否回收如何判定",
            business_theme="定位检测",
            prompt_version=pipeline.PROMPT_VERSION,
        )
        atomic = HighFrequencyAtomicQuestion(
            id="hfaq-earphone-old",
            source_record_id=source.id,
            atomic_index=1,
            atomic_question="耳机定位功能正常能否回收如何判定",
            normalized_question="耳机定位功能正常能否回收如何判定",
            split_status="单问题",
            processing_status="ready",
            standard_question_id=old_standard.id,
            mapping_source="model",
        )
        source.category = "耳机/耳麦"
        source.processing_status = "ready"
        self.session.add_all([old_standard, atomic])
        self.session.commit()

        result = pipeline.ingest_records(self.session, [record])
        self.session.refresh(source)
        self.session.refresh(atomic)
        repaired = self.session.get(
            HighFrequencyStandardQuestion,
            atomic.standard_question_id,
        )

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["category_reconciled"], 1)
        self.assertEqual(result["rule_reconciled"], 1)
        self.assertEqual(source.category, "耳机")
        self.assertEqual(repaired.category, "耳机")
        self.assertEqual(
            repaired.standard_question,
            "耳机定位查找功能正常如何判定",
        )

    def test_repeat_sync_requeues_mapping_removed_by_new_rule_version(self):
        record = {
            "工单ID": "hinge-gap-1",
            "分析时间": "2026-10-12 10:00:00",
            "产品类型": "笔记本",
            "ai_result": {
                "核心问题": "转轴位置存在机身缝隙但没有异响如何判定",
            },
        }
        pipeline.ingest_records(self.session, [record])
        source = self.session.query(HighFrequencySourceRecord).one()
        old_standard = HighFrequencyStandardQuestion(
            id="hfsq-hinge-old",
            category="笔记本",
            normalized_question="笔记本转轴异响如何判定",
            standard_question="笔记本转轴异响如何判定",
            business_theme="转轴检测",
            prompt_version=pipeline.PROMPT_VERSION,
        )
        atomic = HighFrequencyAtomicQuestion(
            id="hfaq-hinge-old",
            source_record_id=source.id,
            atomic_index=1,
            atomic_question="转轴位置存在机身缝隙但没有异响如何判定",
            normalized_question="转轴位置存在机身缝隙但没有异响如何判定",
            split_status="单问题",
            processing_status="ready",
            standard_question_id=old_standard.id,
            mapping_source="deterministic",
        )
        source.processing_status = "ready"
        for task in source.processing_tasks:
            task.status = "completed"
        self.session.add_all([old_standard, atomic])
        self.session.commit()

        result = pipeline.ingest_records(self.session, [record])

        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["rule_requeued"], 1)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(source.processing_status, "queued")
        self.assertEqual(
            self.session.query(HighFrequencyProcessingTask)
            .filter(HighFrequencyProcessingTask.status == "queued")
            .count(),
            1,
        )

    def test_dashboard_uses_ready_records_only_and_keeps_source_evidence(self):
        source = HighFrequencySourceRecord(
            id="hfs-ready",
            record_id="1",
            source_hash="a" * 64,
            analyzed_at="2026-10-12 10:00:00",
            analyzed_on=pipeline._as_day("2026-10-12"),
            category="手机",
            core_problem="电池是否为正品部件",
            judgment="按规则判定",
            processing_status="ready",
        )
        standard = HighFrequencyStandardQuestion(
            id="hfsq-ready",
            category="手机",
            normalized_question="电池是否为正品部件",
            standard_question="电池是否为正品部件",
            business_theme="配件核验",
            prompt_version=pipeline.PROMPT_VERSION,
        )
        atomic = HighFrequencyAtomicQuestion(
            id="hfaq-ready",
            source_record_id=source.id,
            atomic_index=1,
            atomic_question="电池是否为正品部件",
            normalized_question="电池是否为正品部件",
            split_status="单问题",
            processing_status="ready",
            standard_question_id=standard.id,
        )
        queued = HighFrequencySourceRecord(
            id="hfs-queued",
            record_id="2",
            source_hash="b" * 64,
            analyzed_at="2026-10-12 10:00:00",
            analyzed_on=pipeline._as_day("2026-10-12"),
            category="手机",
            core_problem="尚未处理的问题",
            processing_status="queued",
        )
        self.session.add_all([source, standard, atomic, queued])
        self.session.commit()

        payload = pipeline.build_persisted_dashboard(
            self.session,
            from_date="2026-10-12",
            to_date="2026-10-12",
            high_frequency_only=False,
        )

        self.assertEqual(payload["summary"]["total_records"], 2)
        self.assertEqual(payload["summary"]["ready_record_count"], 1)
        self.assertEqual(payload["summary"]["queued_record_count"], 1)
        self.assertEqual(payload["rows"][0]["案例"][0]["AI分析问题"], "电池是否为正品部件")
        self.assertEqual(payload["rows"][0]["案例"][0]["判定结论"], "按规则判定")

    def test_source_fetch_metadata_is_persisted_and_reported_by_dashboard(self):
        record = _record("metadata-1", "屏幕异常如何判定")

        pipeline.ingest_records(
            self.session,
            [record],
            source_possibly_truncated=True,
            source_request_count=4,
        )

        source = self.session.query(HighFrequencySourceRecord).one()
        self.assertTrue(source.source_possibly_truncated)
        self.assertEqual(source.source_request_count, 4)

        payload = pipeline.build_persisted_dashboard(
            self.session,
            from_date="2026-10-12",
            to_date="2026-10-12",
        )

        self.assertTrue(payload["meta"]["possibly_truncated"])
        self.assertEqual(payload["meta"]["source_request_count"], 4)
        self.assertEqual(payload["meta"]["quality_status"], "incomplete")

    def test_overlong_bounded_fields_are_clamped_instead_of_failing_the_batch(self):
        # The column is VARCHAR(128); the record keeps the full text in
        # raw_payload.  Without the clamp a single malformed work order aborts
        # the whole sync with StringDataRightTruncation.
        paragraph = "中“基本信息问题”包含“开机情况”“系统情况”等二级分类。" * 12
        record = _record("1", "什么情况下勾选仿冒产品？")
        record["一级分类"] = paragraph
        record["二级分类"] = paragraph
        record["工单ID"] = "9" * 400

        summary = pipeline.ingest_records(self.session, [record, _record("2", "电池是否为正品部件")])

        self.assertEqual(summary["queued"], 2)
        sources = self.session.query(HighFrequencySourceRecord).all()
        self.assertEqual(len(sources), 2)
        # A malformed id falls back to a content hash rather than a truncated
        # id, which could otherwise merge two distinct work orders.
        hashed = next(source for source in sources if source.record_id.startswith("content-"))
        self.assertLessEqual(len(hashed.record_id), 256)
        clamped = hashed
        self.assertEqual(len(clamped.level1), 128)
        self.assertEqual(len(clamped.level2), 128)
        # The untouched record still lands normally.
        self.assertEqual(
            self.session.query(HighFrequencyProcessingTask)
            .filter(HighFrequencyProcessingTask.status == "queued")
            .count(),
            2,
        )


if __name__ == "__main__":
    unittest.main()
