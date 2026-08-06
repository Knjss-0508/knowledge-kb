import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

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
    activate_embedding_config,
    claim_training_job,
    complete_training_job,
    create_embedding_config,
    create_training_job,
    decide_model_version,
    fail_training_job,
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

    def test_training_job_claim_retry_and_complete(self):
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
