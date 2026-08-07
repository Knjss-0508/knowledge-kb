import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "training-runner"
    / "runner.py"
)
os.environ.setdefault("TRAINING_CONTROL_BASE_URL", "http://127.0.0.1:8000")
os.environ.setdefault("TRAINING_RUNNER_TOKEN", "test-runner-token-with-32-characters")
os.environ.setdefault("TRAINING_RUNNER_ID", "test-runner")
os.environ.setdefault(
    "TRAINING_ARTIFACT_ROOT",
    str(Path(tempfile.gettempdir()) / "knowledge-kb-test-runner"),
)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "knowledge_kb_training_runner",
    RUNNER_PATH,
)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


def _argument_value(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class EmbeddingTrainingRunnerTests(unittest.TestCase):
    @staticmethod
    def _dataset_rows():
        rows = []
        for split in ("train", "validation", "test"):
            rows.append(
                {
                    "id": f"{split}-sample",
                    "task_type": "retrieval",
                    "split": split,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"{split} 问题",
                        }
                    ],
                    "positive": [f"{split} 正确知识"],
                    "negative": [
                        f"{split} 负样本 1",
                        f"{split} 负样本 2",
                        f"{split} 负样本 3",
                    ],
                    "prompt": "检索能够准确回答用户问题的已发布知识",
                    "metadata": {"source_id": split},
                }
            )
        return rows

    def test_write_dataset_uses_ms_swift_embedding_message_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = RUNNER.write_dataset(
                {
                    "dataset": self._dataset_rows(),
                    "training_config": {"max_negative_samples": 2},
                },
                Path(directory),
            )
            payload = json.loads(
                paths["train"].read_text(encoding="utf-8").splitlines()[0]
            )

        self.assertEqual(
            payload["messages"][0]["content"],
            "Instruct: 检索能够准确回答用户问题的已发布知识\nQuery:train 问题",
        )
        self.assertEqual(
            payload["positive_messages"],
            [[{"role": "user", "content": "train 正确知识"}]],
        )
        self.assertEqual(len(payload["negative_messages"]), 2)
        self.assertNotIn("positive", payload)
        self.assertNotIn("negative", payload)
        self.assertNotIn("prompt", payload)

    def test_training_command_uses_low_memory_qlora_arguments_for_swift_4(self):
        with patch.object(RUNNER, "swift_executable", return_value="swift"):
            command = RUNNER.training_command(
                {
                    "base_model": "Qwen/Qwen3-Embedding-0.6B",
                    "train_type": "lora",
                    "training_config": {
                        "low_memory_mode": True,
                        "quant_method": "bnb",
                        "quant_bits": 4,
                        "max_length": 256,
                        "per_device_train_batch_size": 1,
                        "gradient_accumulation_steps": 16,
                        "lora_rank": 8,
                        "lora_alpha": 16,
                        "max_steps": 1,
                        "save_strategy": "steps",
                        "save_steps": 1,
                        "eval_strategy": "no",
                        "report_to": "none",
                    },
                },
                {
                    "train": Path("train.jsonl"),
                    "validation": Path("validation.jsonl"),
                },
                Path("output"),
            )

        self.assertIn("--tuner_type", command)
        self.assertNotIn("--train_type", command)
        self.assertEqual(_argument_value(command, "--tuner_type"), "lora")
        self.assertEqual(_argument_value(command, "--quant_method"), "bnb")
        self.assertEqual(_argument_value(command, "--quant_bits"), "4")
        self.assertEqual(_argument_value(command, "--max_length"), "256")
        self.assertEqual(
            _argument_value(command, "--per_device_train_batch_size"),
            "1",
        )
        self.assertEqual(
            _argument_value(command, "--gradient_accumulation_steps"),
            "16",
        )
        self.assertEqual(_argument_value(command, "--lora_rank"), "8")
        self.assertEqual(_argument_value(command, "--lora_alpha"), "16")
        self.assertEqual(_argument_value(command, "--max_steps"), "1")
        self.assertEqual(_argument_value(command, "--save_strategy"), "steps")
        self.assertEqual(_argument_value(command, "--save_steps"), "1")
        self.assertEqual(_argument_value(command, "--eval_strategy"), "no")
        self.assertEqual(_argument_value(command, "--report_to"), "none")

    def test_evaluation_inherits_low_memory_batch_and_length(self):
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            captured = {}

            def fake_run_command(_job_id, command, **_kwargs):
                captured["command"] = command
                (working_dir / "metrics.json").write_text(
                    json.dumps({"candidate": {"dimension": 1024}}),
                    encoding="utf-8",
                )
                return "evaluation complete"

            with patch.object(RUNNER, "run_command", side_effect=fake_run_command):
                metrics, dimension, log_tail = RUNNER.evaluate(
                    {
                        "id": "etj-test",
                        "base_model": "Qwen/Qwen3-Embedding-0.6B",
                        "training_config": {
                            "evaluation_batch_size": 1,
                            "max_length": 256,
                        },
                    },
                    working_dir / "dataset.snapshot.json",
                    working_dir / "model",
                    working_dir,
                )

        self.assertEqual(dimension, 1024)
        self.assertEqual(metrics["candidate"]["dimension"], 1024)
        self.assertEqual(log_tail, "evaluation complete")
        self.assertEqual(_argument_value(captured["command"], "--batch-size"), "1")
        self.assertEqual(_argument_value(captured["command"], "--max-length"), "256")
        self.assertEqual(Path(captured["command"][1]), RUNNER.EVALUATOR_PATH)

    def test_cuda_oom_is_retryable(self):
        self.assertTrue(
            RUNNER.is_retryable_training_exception(
                RuntimeError("CUDA out of memory while allocating tensor")
            )
        )
        self.assertTrue(
            RUNNER.is_retryable_training_exception(
                RUNNER.RetryableTrainingError("GPU 可用显存不足")
            )
        )
        self.assertFalse(
            RUNNER.is_retryable_training_exception(
                RuntimeError("dataset schema is invalid")
            )
        )

    def test_latest_checkpoint_finds_ms_swift_versioned_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "training-output"
            checkpoint = (
                output_dir
                / "v0-20260806-150532"
                / "checkpoint-1"
            )
            checkpoint.mkdir(parents=True)

            discovered = RUNNER.latest_checkpoint(output_dir)

        self.assertEqual(discovered, checkpoint)

    def test_heartbeat_once_cli_exits_without_entering_poll_loop(self):
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["runner.py", "--heartbeat-once"],
            ),
            patch.object(
                RUNNER,
                "heartbeat",
                return_value={"runner": {"id": "runner-local"}},
            ),
            patch.object(RUNNER, "main") as poll_loop,
            redirect_stdout(output),
        ):
            RUNNER.run_cli()

        poll_loop.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "connected")
        self.assertEqual(payload["runner"]["id"], "runner-local")

    def test_task_access_url_requires_exact_job_path_and_https(self):
        self.assertEqual(
            RUNNER.validate_task_access_url(
                "https://kb.example.test/api/v1/embedding-model/"
                "runner/tasks/etj-bound/"
            ),
            "https://kb.example.test/api/v1/embedding-model/"
            "runner/tasks/etj-bound",
        )
        self.assertTrue(
            RUNNER.validate_task_access_url(
                "http://127.0.0.1/api/v1/embedding-model/"
                "runner/tasks/etj-local"
            ).startswith("http://127.0.0.1/")
        )

        for invalid_url in (
            (
                "http://kb.example.test/api/v1/embedding-model/"
                "runner/tasks/etj-bound"
            ),
            (
                "https://kb.example.test/api/v1/embedding-model/"
                "runner/tasks/etj-bound/claim"
            ),
            (
                "https://kb.example.test/api/v1/embedding-model/"
                "runner/tasks/etj-bound?source=console"
            ),
            (
                "https://user:pass@kb.example.test/api/v1/embedding-model/"
                "runner/tasks/etj-bound"
            ),
        ):
            with (
                self.subTest(invalid_url=invalid_url),
                self.assertRaises(RuntimeError),
            ):
                RUNNER.validate_task_access_url(invalid_url)

    def test_task_mode_claims_only_bound_job_endpoint(self):
        with (
            patch.object(RUNNER, "TASK_MODE", True),
            patch.object(
                RUNNER,
                "runner_heartbeat_payload",
                return_value={"runner_id": "runner-local"},
            ),
            patch.object(
                RUNNER,
                "request_json",
                return_value={"job": {"id": "etj-bound"}},
            ) as request_json,
        ):
            job = RUNNER.claim_job()

        self.assertEqual(job["id"], "etj-bound")
        request_json.assert_called_once_with(
            "POST",
            "/claim",
            payload={"runner_id": "runner-local"},
        )

    def test_task_mode_reports_progress_to_task_relative_endpoint(self):
        with (
            patch.object(RUNNER, "TASK_MODE", True),
            patch.object(RUNNER, "request_json") as request_json,
        ):
            RUNNER.report_progress(
                "etj-bound",
                status="running",
                stage="LoRA 训练",
                progress=45,
                log_tail="training",
            )

        request_json.assert_called_once()
        args, kwargs = request_json.call_args
        self.assertEqual(args, ("POST", "/progress"))
        self.assertEqual(kwargs["payload"]["runner_id"], "test-runner")
        self.assertTrue(kwargs["cancelled_on_conflict"])

    def test_task_mode_runs_one_job_and_exits(self):
        with (
            patch.object(RUNNER, "TASK_MODE", True),
            patch.object(
                RUNNER,
                "claim_job",
                return_value={"id": "etj-bound"},
            ) as claim_job,
            patch.object(RUNNER, "process_job") as process_job,
        ):
            RUNNER.main()

        claim_job.assert_called_once_with()
        process_job.assert_called_once_with({"id": "etj-bound"})


if __name__ == "__main__":
    unittest.main()
