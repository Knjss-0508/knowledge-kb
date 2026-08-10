import hashlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.embedding_admin import (
    EmbeddingModelVersion,
    EmbeddingRuntimeConfig,
    EmbeddingTrainingJob,
    EmbeddingTrainingRunner,
    EmbeddingTrainingSample,
)
from app.routes.embedding_admin import (
    _task_runner_url,
    activate_embedding_config,
    claim_task_training_job,
    claim_training_job,
    complete_task_training_job,
    complete_training_job,
    create_embedding_config,
    create_training_job,
    decide_model_version,
    fail_training_job,
    probe_task_runner_access,
    regenerate_training_job_runner_access,
    runner_heartbeat,
)
from app.schemas.embedding_admin import (
    EmbeddingModelDecision,
    EmbeddingRunnerClaim,
    EmbeddingRunnerComplete,
    EmbeddingRunnerFailure,
    EmbeddingRunnerHeartbeat,
    EmbeddingRuntimeConfigCreate,
    EmbeddingRuntimeConfigValues,
    EmbeddingTrainingJobCreate,
)
from app.services.embedding_admin import build_training_dataset
from app.services.embedding_runtime import default_embedding_runtime_config


class EmbeddingAdminTests(unittest.TestCase):
    def setUp(self):
        self.public_base_url = settings.EMBEDDING_TRAINING_PUBLIC_BASE_URL
        settings.EMBEDDING_TRAINING_PUBLIC_BASE_URL = "https://kb.example.test"
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        for table in (
            EmbeddingRuntimeConfig.__table__,
            EmbeddingTrainingSample.__table__,
            EmbeddingTrainingJob.__table__,
            EmbeddingModelVersion.__table__,
            EmbeddingTrainingRunner.__table__,
        ):
            table.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()
        self.user = SimpleNamespace(username="admin")

    def tearDown(self):
        settings.EMBEDDING_TRAINING_PUBLIC_BASE_URL = self.public_base_url
        self.db.close()
        self.engine.dispose()

    def test_runtime_config_activation_versions_and_blocks_structural_change(self):
        base_values = EmbeddingRuntimeConfigValues().model_dump()
        active = EmbeddingRuntimeConfig(
            id="erc-active",
            version=1,
            status="active",
            config=base_values,
            evaluation_metrics={},
            change_reason="初始参数",
            created_by="admin",
            activated_by="admin",
            activated_at=datetime.utcnow(),
        )
        self.db.add(active)
        self.db.commit()

        tuned_values = dict(base_values)
        tuned_values["dedup_review_threshold"] = 0.86
        tuned = create_embedding_config(
            EmbeddingRuntimeConfigCreate(
                config=EmbeddingRuntimeConfigValues(**tuned_values),
                change_reason="降低人工复核阈值",
                activate=True,
            ),
            self.db,
            self.user,
        )
        self.assertEqual(tuned["version"], 2)
        self.assertEqual(tuned["status"], "active")
        self.db.refresh(active)
        self.assertEqual(active.status, "archived")

        structural_values = dict(tuned_values)
        structural_values["search_chunk_size"] = 900
        blocked = create_embedding_config(
            EmbeddingRuntimeConfigCreate(
                config=EmbeddingRuntimeConfigValues(**structural_values),
                change_reason="调整分块长度",
                activate=True,
            ),
            self.db,
            self.user,
        )
        self.assertEqual(blocked["status"], "draft")
        self.assertTrue(blocked["activation_blocked"])

        structural = (
            self.db.query(EmbeddingRuntimeConfig)
            .filter(EmbeddingRuntimeConfig.id == blocked["id"])
            .one()
        )
        structural.evaluation_metrics = {"vector_rebuild_completed": True}
        self.db.commit()
        activated = activate_embedding_config(
            structural.id,
            self.db,
            self.user,
        )
        self.assertEqual(activated["status"], "active")
        self.assertEqual(activated["activated_by"], "admin")

    def test_scoped_threshold_versions_do_not_overwrite_each_other(self):
        base_values = EmbeddingRuntimeConfigValues().model_dump()
        self.db.add(
            EmbeddingRuntimeConfig(
                id="erc-active",
                version=1,
                status="active",
                config=base_values,
                evaluation_metrics={},
                change_reason="初始参数",
                created_by="admin",
                activated_by="admin",
                activated_at=datetime.utcnow(),
            )
        )
        self.db.commit()

        dedup_draft_values = dict(base_values)
        dedup_draft_values["dedup_block_threshold"] = 0.95
        dedup_draft_values["dedup_review_threshold"] = 0.85
        dedup_draft_values["retrieval_score_threshold"] = 0.10
        dedup_draft = create_embedding_config(
            EmbeddingRuntimeConfigCreate(
                config=EmbeddingRuntimeConfigValues(**dedup_draft_values),
                change_reason="调整上传查重",
                evaluation_metrics={"config_scope": "dedup_thresholds"},
                activate=False,
            ),
            self.db,
            self.user,
        )
        self.assertEqual(dedup_draft["config"]["retrieval_score_threshold"], 0.42)

        retrieval_values = dict(base_values)
        retrieval_values["retrieval_score_threshold"] = 0.55
        retrieval_values["dedup_block_threshold"] = 0.70
        retrieval_values["dedup_review_threshold"] = 0.60
        retrieval_active = create_embedding_config(
            EmbeddingRuntimeConfigCreate(
                config=EmbeddingRuntimeConfigValues(**retrieval_values),
                change_reason="收紧知识检索",
                evaluation_metrics={"config_scope": "retrieval_thresholds"},
                activate=True,
            ),
            self.db,
            self.user,
        )
        self.assertEqual(retrieval_active["config"]["retrieval_score_threshold"], 0.55)
        self.assertEqual(retrieval_active["config"]["dedup_block_threshold"], 0.96)
        self.assertEqual(retrieval_active["config"]["dedup_review_threshold"], 0.88)

        activated_dedup = activate_embedding_config(
            dedup_draft["id"],
            self.db,
            self.user,
        )
        self.assertEqual(activated_dedup["config"]["dedup_block_threshold"], 0.95)
        self.assertEqual(activated_dedup["config"]["dedup_review_threshold"], 0.85)
        self.assertEqual(
            activated_dedup["config"]["retrieval_score_threshold"],
            0.55,
        )

    def test_task_runner_public_base_url_requires_clean_https_origin(self):
        settings.EMBEDDING_TRAINING_PUBLIC_BASE_URL = "https://kb.example.test/"
        self.assertEqual(
            _task_runner_url("etj-test"),
            "https://kb.example.test/api/v1/embedding-model/runner/tasks/etj-test",
        )

        for invalid_url in (
            "http://kb.example.test",
            "https://kb.example.test/app",
            "https://kb.example.test/api/v1",
            "https://kb.example.test/path",
            "https://user:pass@kb.example.test",
            "https://kb.example.test?source=console",
        ):
            with self.subTest(invalid_url=invalid_url):
                settings.EMBEDDING_TRAINING_PUBLIC_BASE_URL = invalid_url
                with self.assertRaises(HTTPException) as invalid:
                    _task_runner_url("etj-test")
                self.assertEqual(invalid.exception.status_code, 503)

    def _add_split_complete_samples(self):
        for index in range(1, 500):
            self.db.add(
                EmbeddingTrainingSample(
                    id=f"ets-{index:04d}",
                    task_type="retrieval" if index % 2 else "deduplication",
                    query_text=f"问题 {index}",
                    positive_text=f"正确知识 {index}",
                    negative_texts=[f"困难负样本 {index}"],
                    source_type="manual",
                    source_id=f"source-{index}",
                    status="verified",
                    reason="测试样本",
                    sample_metadata={},
                    created_by="admin",
                    reviewed_by="admin",
                    reviewed_at=datetime.utcnow(),
                )
            )
            self.db.flush()
            samples = (
                self.db.query(EmbeddingTrainingSample)
                .order_by(EmbeddingTrainingSample.created_at)
                .all()
            )
            _, _, counts = build_training_dataset(samples)
            if len(samples) >= 20 and all(counts[split] > 0 for split in counts):
                self.db.commit()
                return
        self.fail("未能构造同时包含训练、验证和测试分片的样本")

    def _prepare_training_context(self):
        runtime = default_embedding_runtime_config()
        runtime["training_min_verified_samples"] = 20
        self.db.add(
            EmbeddingRuntimeConfig(
                id="erc-training",
                version=1,
                status="active",
                config=runtime,
                evaluation_metrics={},
                change_reason="训练测试",
                created_by="admin",
                activated_by="admin",
                activated_at=datetime.utcnow(),
            )
        )
        self._add_split_complete_samples()

    def test_training_job_claim_retry_and_complete(self):
        self._prepare_training_context()
        created = create_training_job(
            EmbeddingTrainingJobCreate(
                candidate_model_name="kb-test-candidate",
                train_type="lora",
            ),
            self.db,
            self.user,
        )
        self.assertEqual(created["status"], "queued")
        self.assertEqual(created["training_config"]["quant_bits"], 4)
        self.assertEqual(created["training_config"]["max_length"], 256)
        self.assertEqual(created["training_config"]["gradient_accumulation_steps"], 16)
        self.assertEqual(created["training_config"]["evaluation_batch_size"], 1)
        self.assertEqual(created["training_config"]["min_free_gpu_memory_mb"], 3200)
        self.assertTrue(created["runner_access"]["url"].endswith(created["id"]))
        self.assertGreaterEqual(len(created["runner_access"]["token"]), 24)
        stored_job = self.db.get(EmbeddingTrainingJob, created["id"])
        self.assertEqual(
            stored_job.runner_access_token_hash,
            hashlib.sha256(
                created["runner_access"]["token"].encode("utf-8")
            ).hexdigest(),
        )
        self.assertNotEqual(
            stored_job.runner_access_token_hash,
            created["runner_access"]["token"],
        )
        # Simulate an old queued job so the legacy global Runner path remains
        # covered without allowing it to steal newly issued task-scoped jobs.
        stored_job.runner_access_token_hash = None
        stored_job.runner_access_expires_at = None
        self.db.commit()

        runner_heartbeat(
            EmbeddingRunnerHeartbeat(
                runner_id="runner-local",
                name="本机 4070",
                hostname="workstation",
                status="online",
                gpu_name="NVIDIA GeForce RTX 4070 Laptop GPU",
                gpu_memory_mb=8192,
                gpu_free_memory_mb=4096,
                cuda_version="12.6",
                runner_version="0.1.0",
            ),
            self.db,
            None,
        )
        claim = claim_training_job(
            EmbeddingRunnerClaim(runner_id="runner-local"),
            self.db,
            None,
        )
        self.assertEqual(claim["job"]["id"], created["id"])
        self.assertEqual(claim["job"]["status"], "claimed")
        self.assertTrue(claim["job"]["dataset"])

        failed = fail_training_job(
            created["id"],
            EmbeddingRunnerFailure(
                runner_id="runner-local",
                error_message="CUDA out of memory",
                log_tail="CUDA out of memory",
                retryable=True,
            ),
            self.db,
            None,
        )
        self.assertEqual(failed["status"], "queued")
        self.assertIsNone(failed["job"]["runner_id"])
        runner = self.db.get(EmbeddingTrainingRunner, "runner-local")
        self.assertEqual(runner.status, "online")
        self.assertIsNone(runner.current_job_id)

        second_claim = claim_training_job(
            EmbeddingRunnerClaim(runner_id="runner-local"),
            self.db,
            None,
        )
        self.assertEqual(second_claim["job"]["id"], created["id"])
        completed = complete_training_job(
            created["id"],
            EmbeddingRunnerComplete(
                runner_id="runner-local",
                metrics={"quality_gate": {"status": "pass"}},
                artifact_uri="local-runner://runner-local/jobs/test/model",
                artifact_sha256="a" * 64,
                dimension=settings.EMBEDDING_DIMENSIONS,
                log_tail="done",
            ),
            self.db,
            None,
        )
        self.assertEqual(completed["status"], "completed")
        model = (
            self.db.query(EmbeddingModelVersion)
            .filter(EmbeddingModelVersion.training_job_id == created["id"])
            .one()
        )
        self.assertEqual(model.status, "candidate")
        self.assertEqual(model.model_name, "kb-test-candidate")
        self.assertEqual(model.dimension, settings.EMBEDDING_DIMENSIONS)

        approved = decide_model_version(
            model.id,
            EmbeddingModelDecision(
                action="approve",
                release_notes="仅批准候选，不上传或部署",
            ),
            self.db,
            self.user,
        )
        self.assertEqual(approved["status"], "approved")
        self.assertIsNone(approved["deployed_at"])
        self.assertTrue(approved["artifact_uri"].startswith("local-runner://"))

    def test_task_scoped_access_rotates_and_only_claims_bound_job(self):
        self._prepare_training_context()
        first = create_training_job(
            EmbeddingTrainingJobCreate(
                candidate_model_name="kb-task-first",
                train_type="lora",
            ),
            self.db,
            self.user,
        )
        second = create_training_job(
            EmbeddingTrainingJobCreate(
                candidate_model_name="kb-task-second",
                train_type="lora",
            ),
            self.db,
            self.user,
        )
        self.assertTrue(first["runner_access"]["url"].startswith("https://"))
        runner_heartbeat(
            EmbeddingRunnerHeartbeat(
                runner_id="legacy-runner",
                name="旧版 Runner",
                status="online",
            ),
            self.db,
            None,
        )
        legacy_claim = claim_training_job(
            EmbeddingRunnerClaim(runner_id="legacy-runner"),
            self.db,
            None,
        )
        self.assertIsNone(legacy_claim["job"])
        old_token = first["runner_access"]["token"]
        rotated = regenerate_training_job_runner_access(
            first["id"],
            self.db,
            self.user,
        )
        new_token = rotated["runner_access"]["token"]
        self.assertNotEqual(old_token, new_token)
        heartbeat = EmbeddingRunnerHeartbeat(
            runner_id="runner-other-pc",
            name="其他训练电脑",
            hostname="gpu-workstation",
            status="online",
            gpu_name="NVIDIA GeForce RTX 4070",
            gpu_memory_mb=8192,
            gpu_free_memory_mb=7000,
            cuda_version="12.6",
            runner_version="0.3.0",
        )
        with self.assertRaises(HTTPException) as invalid:
            probe_task_runner_access(
                first["id"],
                heartbeat,
                self.db,
                old_token,
            )
        self.assertEqual(invalid.exception.status_code, 401)
        with self.assertRaises(HTTPException) as cross_task:
            probe_task_runner_access(
                second["id"],
                heartbeat,
                self.db,
                new_token,
            )
        self.assertEqual(cross_task.exception.status_code, 401)

        claimed = claim_task_training_job(
            first["id"],
            heartbeat,
            self.db,
            new_token,
        )
        self.assertEqual(claimed["job"]["id"], first["id"])
        self.assertEqual(claimed["job"]["runner_id"], "runner-other-pc")
        self.assertTrue(claimed["job"]["dataset"])
        untouched = self.db.get(EmbeddingTrainingJob, second["id"])
        self.assertEqual(untouched.status, "queued")
        self.assertIsNone(untouched.runner_id)

        completion = EmbeddingRunnerComplete(
            runner_id="runner-other-pc",
            metrics={"quality_gate": {"status": "pass"}},
            artifact_uri="local-runner://runner-other-pc/jobs/first/model",
            artifact_sha256="b" * 64,
            dimension=settings.EMBEDDING_DIMENSIONS,
            log_tail="done",
        )
        with self.assertRaises(HTTPException) as legacy_update:
            complete_training_job(
                first["id"],
                completion,
                self.db,
                None,
            )
        self.assertEqual(legacy_update.exception.status_code, 409)
        with self.assertRaises(HTTPException) as rotate_after_claim:
            regenerate_training_job_runner_access(
                first["id"],
                self.db,
                self.user,
            )
        self.assertEqual(rotate_after_claim.exception.status_code, 409)

        completed = complete_task_training_job(
            first["id"],
            completion,
            self.db,
            new_token,
        )
        self.assertEqual(completed["status"], "completed")
        finished = self.db.get(EmbeddingTrainingJob, first["id"])
        self.assertIsNone(finished.runner_access_token_hash)
        self.assertFalse(completed["job"]["runner_access"]["configured"])

    def test_expired_lease_releases_stale_runner_before_reclaim(self):
        now = datetime.utcnow()
        old_runner = EmbeddingTrainingRunner(
            id="runner-old",
            name="旧 Runner",
            status="busy",
            current_job_id="etj-expired",
            last_seen_at=now - timedelta(hours=1),
        )
        new_runner = EmbeddingTrainingRunner(
            id="runner-new",
            name="新 Runner",
            status="online",
            last_seen_at=now,
        )
        expired_job = EmbeddingTrainingJob(
            id="etj-expired",
            status="running",
            stage="LoRA 训练",
            progress=45,
            base_model="Qwen/Qwen3-Embedding-0.6B",
            candidate_model_name="kb-expired-candidate",
            train_type="lora",
            training_config={},
            dataset_hash="b" * 64,
            dataset_payload=[{"split": "train"}],
            sample_count=1,
            train_count=1,
            validation_count=0,
            test_count=0,
            runner_id="runner-old",
            lease_expires_at=now - timedelta(minutes=5),
            requested_by="admin",
            created_at=now - timedelta(hours=2),
        )
        self.db.add_all([old_runner, new_runner, expired_job])
        self.db.commit()

        claim = claim_training_job(
            EmbeddingRunnerClaim(runner_id="runner-new"),
            self.db,
            None,
        )

        self.assertEqual(claim["job"]["id"], "etj-expired")
        self.assertEqual(claim["job"]["runner_id"], "runner-new")
        self.db.refresh(old_runner)
        self.assertEqual(old_runner.status, "offline")
        self.assertIsNone(old_runner.current_job_id)


if __name__ == "__main__":
    unittest.main()
