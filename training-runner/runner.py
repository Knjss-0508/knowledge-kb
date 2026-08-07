from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

RUNNER_VERSION = "0.3.0"
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
TASK_ACCESS_PATH = re.compile(
    r"^/api/v1/embedding-model/runner/tasks/etj-[A-Za-z0-9._-]+$"
)
RUNNER_DIRECTORY = Path(__file__).resolve().parent
EVALUATOR_PATH = RUNNER_DIRECTORY / "evaluate_model.py"


class RetryableTrainingError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def validate_task_access_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
    ):
        raise RuntimeError("TRAINING_JOB_URL must be an absolute HTTP(S) URL")
    if (
        parsed.scheme != "https"
        and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RuntimeError("TRAINING_JOB_URL must use HTTPS outside localhost")
    if (
        parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not TASK_ACCESS_PATH.fullmatch(parsed.path)
    ):
        raise RuntimeError("TRAINING_JOB_URL is not an exact LoRA task access URL")
    return normalized


TASK_ACCESS_URL = os.getenv("TRAINING_JOB_URL", "").strip()
TASK_ACCESS_TOKEN = os.getenv("TRAINING_JOB_TOKEN", "").strip()
TASK_MODE = bool(TASK_ACCESS_URL or TASK_ACCESS_TOKEN)
if TASK_MODE:
    if not TASK_ACCESS_URL or not TASK_ACCESS_TOKEN:
        raise RuntimeError(
            "TRAINING_JOB_URL and TRAINING_JOB_TOKEN must be configured together"
        )
    TASK_ACCESS_URL = validate_task_access_url(TASK_ACCESS_URL)
    CONTROL_BASE_URL = ""
    RUNNER_TOKEN = TASK_ACCESS_TOKEN
    API_ROOT = TASK_ACCESS_URL
    TOKEN_HEADER = "X-Embedding-Task-Token"
else:
    CONTROL_BASE_URL = required_env("TRAINING_CONTROL_BASE_URL").rstrip("/")
    RUNNER_TOKEN = required_env("TRAINING_RUNNER_TOKEN")
    API_ROOT = f"{CONTROL_BASE_URL}/api/v1/embedding-model"
    TOKEN_HEADER = "X-Embedding-Runner-Token"
RUNNER_ID = required_env("TRAINING_RUNNER_ID")
RUNNER_NAME = os.getenv("TRAINING_RUNNER_NAME", RUNNER_ID).strip() or RUNNER_ID
POLL_SECONDS = max(2.0, float(os.getenv("TRAINING_POLL_SECONDS", "10")))
ARTIFACT_ROOT = Path(
    os.getenv("TRAINING_ARTIFACT_ROOT", str(RUNNER_DIRECTORY / "artifacts"))
).resolve()

if len(RUNNER_TOKEN) < 24:
    token_name = "TRAINING_JOB_TOKEN" if TASK_MODE else "TRAINING_RUNNER_TOKEN"
    raise RuntimeError(f"{token_name} must contain at least 24 characters")
if not SAFE_ID.fullmatch(RUNNER_ID):
    raise RuntimeError("TRAINING_RUNNER_ID contains unsupported characters")

ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


def swift_executable() -> str:
    executable_name = "swift.exe" if os.name == "nt" else "swift"
    alongside_python = Path(sys.executable).resolve().with_name(executable_name)
    if alongside_python.is_file():
        return str(alongside_python)
    discovered = shutil.which("swift")
    if discovered:
        return discovered
    raise RuntimeError("ms-swift CLI is not installed in the runner environment")


def api_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={TOKEN_HEADER: RUNNER_TOKEN},
    )


def request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    cancelled_on_conflict: bool = False,
) -> dict[str, Any]:
    with api_client() as client:
        response = client.request(method, f"{API_ROOT}{path}", json=payload)
        if response.status_code == 409 and cancelled_on_conflict:
            raise JobCancelled(response.text)
        response.raise_for_status()
        return response.json()


def gpu_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.free,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        first = result.stdout.strip().splitlines()[0]
        name, total, free, driver = [part.strip() for part in first.split(",", 3)]
        cuda_version = ""
        summary = subprocess.run(
            ["nvidia-smi"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        match = re.search(r"CUDA Version:\s*([0-9.]+)", summary)
        if match:
            cuda_version = match.group(1)
        return {
            "gpu_name": name,
            "gpu_memory_mb": int(float(total)),
            "gpu_free_memory_mb": int(float(free)),
            "cuda_version": cuda_version,
            "driver_version": driver,
        }
    except Exception as exc:
        return {
            "gpu_name": "",
            "gpu_memory_mb": 0,
            "gpu_free_memory_mb": 0,
            "cuda_version": "",
            "gpu_error": str(exc)[:300],
        }


def runner_heartbeat_payload(
    status: str = "online",
    current_job_id: str | None = None,
) -> dict[str, Any]:
    gpu = gpu_snapshot()
    return {
        "runner_id": RUNNER_ID,
        "name": RUNNER_NAME,
        "hostname": socket.gethostname(),
        "status": status,
        "gpu_name": gpu.get("gpu_name", ""),
        "gpu_memory_mb": gpu.get("gpu_memory_mb", 0),
        "gpu_free_memory_mb": gpu.get("gpu_free_memory_mb", 0),
        "cuda_version": gpu.get("cuda_version", ""),
        "runner_version": RUNNER_VERSION,
        "current_job_id": current_job_id,
        "metadata": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "driver_version": gpu.get("driver_version", ""),
            "gpu_error": gpu.get("gpu_error", ""),
        },
    }


def heartbeat(status: str = "online", current_job_id: str | None = None) -> dict:
    if TASK_MODE:
        path = "/heartbeat" if current_job_id else "/probe"
    else:
        path = "/runner/heartbeat"
    return request_json(
        "POST",
        path,
        payload=runner_heartbeat_payload(status, current_job_id),
        cancelled_on_conflict=bool(TASK_MODE and current_job_id),
    )


def claim_job() -> dict[str, Any] | None:
    if TASK_MODE:
        response = request_json(
            "POST",
            "/claim",
            payload=runner_heartbeat_payload(),
        )
        return response.get("job")
    response = request_json(
        "POST",
        "/runner/claim",
        payload={"runner_id": RUNNER_ID},
    )
    return response.get("job")


def job_directory(job_id: str) -> Path:
    if not SAFE_ID.fullmatch(job_id):
        raise RuntimeError("Unsafe job id")
    directory = (ARTIFACT_ROOT / "jobs" / job_id).resolve()
    if ARTIFACT_ROOT not in directory.parents:
        raise RuntimeError("Job directory escaped the artifact root")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_jsonl(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    max_negative_samples: int,
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            messages = [
                dict(message)
                for message in row.get("messages") or []
                if isinstance(message, dict)
            ]
            if not messages:
                raise RuntimeError(
                    f"Training row {row.get('id') or '<unknown>'} contains no messages"
                )
            instruction = str(row.get("prompt") or "").strip()
            if instruction:
                query_message = next(
                    (
                        message
                        for message in messages
                        if message.get("role") == "user"
                    ),
                    None,
                )
                if query_message is None:
                    raise RuntimeError(
                        f"Training row {row.get('id') or '<unknown>'} "
                        "contains no user query"
                    )
                query = str(query_message.get("content") or "").strip()
                query_message["content"] = f"Instruct: {instruction}\nQuery:{query}"

            positives = [
                str(value).strip()
                for value in row.get("positive") or []
                if str(value).strip()
            ]
            if not positives:
                raise RuntimeError(
                    f"Training row {row.get('id') or '<unknown>'} "
                    "contains no positive document"
                )
            negatives = [
                str(value).strip()
                for value in row.get("negative") or []
                if str(value).strip()
            ][:max_negative_samples]
            payload = {
                "messages": messages,
                "positive_messages": [
                    [{"role": "user", "content": value}]
                    for value in positives
                ],
                "negative_messages": [
                    [{"role": "user", "content": value}]
                    for value in negatives
                ],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_dataset(job: dict[str, Any], directory: Path) -> dict[str, Path]:
    dataset = job.get("dataset")
    if not isinstance(dataset, list) or not dataset:
        raise RuntimeError("Training job contains no dataset")
    config = job.get("training_config") or {}
    max_negative_samples = max(
        1,
        min(8, int(config.get("max_negative_samples", 2))),
    )
    paths: dict[str, Path] = {}
    for split in ("train", "validation", "test"):
        rows = [row for row in dataset if row.get("split") == split]
        if not rows:
            raise RuntimeError(f"Training job contains no {split} rows")
        path = directory / f"{split}.jsonl"
        write_jsonl(
            path,
            rows,
            max_negative_samples=max_negative_samples,
        )
        paths[split] = path
    full_path = directory / "dataset.snapshot.json"
    full_path.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["snapshot"] = full_path
    return paths


def report_progress(
    job_id: str,
    *,
    status: str,
    stage: str,
    progress: float,
    log_tail: str,
) -> None:
    request_json(
        "POST",
        "/progress" if TASK_MODE else f"/runner/jobs/{job_id}/progress",
        payload={
            "runner_id": RUNNER_ID,
            "status": status,
            "stage": stage,
            "progress": progress,
            "log_tail": log_tail[-20000:],
            "lease_seconds": 300,
        },
        cancelled_on_conflict=True,
    )


def run_command(
    job_id: str,
    command: list[str],
    *,
    stage: str,
    progress: float,
    cwd: Path,
) -> str:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    output_queue: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output_queue.put(line.rstrip())
        output_queue.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    lines: list[str] = []
    last_report = 0.0
    reader_done = False
    try:
        while process.poll() is None or not reader_done:
            try:
                line = output_queue.get(timeout=1.0)
                if line is None:
                    reader_done = True
                else:
                    lines.append(line)
                    if len(lines) > 500:
                        lines = lines[-500:]
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last_report >= 15:
                report_progress(
                    job_id,
                    status="running" if stage != "离线评估" else "evaluating",
                    stage=stage,
                    progress=progress,
                    log_tail="\n".join(lines[-200:]),
                )
                heartbeat("busy", job_id)
                last_report = now
    except JobCancelled:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    return_code = process.wait()
    log_tail = "\n".join(lines[-200:])
    if return_code != 0:
        raise RuntimeError(
            f"{stage} failed with exit code {return_code}\n{log_tail[-8000:]}"
        )
    return log_tail


def latest_checkpoint(output_dir: Path) -> Path:
    checkpoints = [
        path
        for path in output_dir.rglob("checkpoint-*")
        if path.is_dir()
    ]
    if not checkpoints:
        return output_dir

    def step(path: Path) -> int:
        match = re.search(r"checkpoint-(\d+)$", path.name)
        return int(match.group(1)) if match else 0

    return max(
        checkpoints,
        key=lambda path: (path.stat().st_mtime_ns, step(path)),
    )


def training_command(
    job: dict[str, Any],
    dataset_paths: dict[str, Path],
    output_dir: Path,
) -> list[str]:
    config = job.get("training_config") or {}
    train_type = job.get("train_type", "lora")
    command = [
        swift_executable(),
        "sft",
        "--model",
        str(job["base_model"]),
        "--model_type",
        "qwen3_emb",
        "--task_type",
        "embedding",
        "--tuner_type",
        train_type,
        "--dataset",
        str(dataset_paths["train"]),
        "--val_dataset",
        str(dataset_paths["validation"]),
        "--output_dir",
        str(output_dir),
        "--loss_type",
        "infonce",
        "--dataloader_drop_last",
        "true",
        "--label_names",
        "labels",
        "--remove_unused_columns",
        "false",
        "--gradient_checkpointing",
        "true",
        "--attn_impl",
        "sdpa",
        "--torch_dtype",
        "bfloat16",
        "--save_strategy",
        str(config.get("save_strategy", "epoch")),
        "--eval_strategy",
        str(config.get("eval_strategy", "epoch")),
        "--save_total_limit",
        str(int(config.get("save_total_limit", 2))),
        "--logging_steps",
        str(int(config.get("logging_steps", 1))),
        "--warmup_ratio",
        str(float(config.get("warmup_ratio", 0.05))),
        "--report_to",
        str(config.get("report_to", "tensorboard")),
        "--max_length",
        str(int(config.get("max_length", 256))),
        "--num_train_epochs",
        str(float(config.get("num_train_epochs", 2))),
        "--per_device_train_batch_size",
        str(int(config.get("per_device_train_batch_size", 1))),
        "--gradient_accumulation_steps",
        str(int(config.get("gradient_accumulation_steps", 16))),
        "--learning_rate",
        str(float(config.get("learning_rate", 0.0001))),
        "--seed",
        str(int(config.get("seed", 42))),
    ]
    max_steps = int(config.get("max_steps", -1))
    if max_steps > 0:
        command.extend(["--max_steps", str(max_steps)])
    save_steps = int(config.get("save_steps", 0))
    if save_steps > 0:
        command.extend(["--save_steps", str(save_steps)])
    if train_type == "lora":
        command.extend(
            [
                "--lora_rank",
                str(int(config.get("lora_rank", 8))),
                "--lora_alpha",
                str(int(config.get("lora_alpha", 16))),
                "--target_modules",
                "all-linear",
            ]
        )
        if bool(config.get("low_memory_mode", True)):
            command.extend(
                [
                    "--quant_method",
                    str(config.get("quant_method", "bnb")),
                    "--quant_bits",
                    str(int(config.get("quant_bits", 4))),
                    "--bnb_4bit_compute_dtype",
                    "bfloat16",
                    "--bnb_4bit_quant_type",
                    str(config.get("bnb_4bit_quant_type", "nf4")),
                    "--bnb_4bit_use_double_quant",
                    "true"
                    if bool(config.get("bnb_4bit_use_double_quant", True))
                    else "false",
                ]
            )
    return command


def merge_lora(
    job_id: str,
    checkpoint: Path,
    merged_dir: Path,
    working_dir: Path,
) -> str:
    return run_command(
        job_id,
        [
            swift_executable(),
            "export",
            "--adapters",
            str(checkpoint),
            "--merge_lora",
            "true",
            "--output_dir",
            str(merged_dir),
        ],
        stage="合并 LoRA 模型",
        progress=82,
        cwd=working_dir,
    )


def evaluate(
    job: dict[str, Any],
    dataset_path: Path,
    candidate_model: Path,
    working_dir: Path,
) -> tuple[dict[str, Any], int, str]:
    config = job.get("training_config") or {}
    metrics_path = working_dir / "metrics.json"
    log_tail = run_command(
        job["id"],
        [
            sys.executable,
            str(EVALUATOR_PATH),
            "--base-model",
            str(job["base_model"]),
            "--candidate-model",
            str(candidate_model),
            "--dataset",
            str(dataset_path),
            "--output",
            str(metrics_path),
            "--block-threshold",
            str(float(config.get("dedup_block_threshold", 0.96))),
            "--minimum-recall-at-10",
            str(float(config.get("minimum_recall_at_10", 0.8))),
            "--maximum-false-block-rate",
            str(float(config.get("maximum_false_block_rate", 0.01))),
            "--batch-size",
            str(int(config.get("evaluation_batch_size", 1))),
            "--max-length",
            str(int(config.get("max_length", 256))),
        ],
        stage="离线评估",
        progress=92,
        cwd=working_dir,
    )
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    dimension = int(metrics.get("candidate", {}).get("dimension") or 0)
    return metrics, dimension, log_tail


def directory_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(str(path.stat().st_size).encode("ascii"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def process_job(job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    directory = job_directory(job_id)
    dataset_paths = write_dataset(job, directory)
    config = job.get("training_config") or {}
    gpu = gpu_snapshot()
    minimum_free = int(config.get("min_free_gpu_memory_mb", 3200))
    free_memory = int(gpu.get("gpu_free_memory_mb", 0))
    if free_memory < minimum_free:
        raise RetryableTrainingError(
            f"GPU 可用显存只有 {free_memory} MB，训练至少需要 {minimum_free} MB；"
            "请关闭占用显存的软件后自动重试"
        )
    swift_executable()

    report_progress(
        job_id,
        status="running",
        stage="准备训练数据",
        progress=5,
        log_tail="训练数据快照已生成",
    )
    output_dir = directory / "training-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    train_log = run_command(
        job_id,
        training_command(job, dataset_paths, output_dir),
        stage="LoRA 训练" if job.get("train_type") == "lora" else "全参数训练",
        progress=45,
        cwd=directory,
    )
    checkpoint = latest_checkpoint(output_dir)
    if job.get("train_type") == "lora":
        candidate_dir = directory / "model"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        merge_log = merge_lora(job_id, checkpoint, candidate_dir, directory)
    else:
        candidate_dir = checkpoint
        merge_log = ""
    metrics, dimension, evaluation_log = evaluate(
        job,
        dataset_paths["snapshot"],
        candidate_dir,
        directory,
    )
    checksum = directory_sha256(candidate_dir)
    # 这里只回传本机产物引用和校验值，不传输模型文件。模型上传必须在用户
    # 单独授权后由独立流程执行，不能由训练完成事件隐式触发。
    artifact_uri = f"local-runner://{RUNNER_ID}/jobs/{job_id}/model"
    request_json(
        "POST",
        "/complete" if TASK_MODE else f"/runner/jobs/{job_id}/complete",
        payload={
            "runner_id": RUNNER_ID,
            "metrics": metrics,
            "artifact_uri": artifact_uri,
            "artifact_sha256": checksum,
            "dimension": dimension,
            "log_tail": "\n".join(
                part for part in (train_log, merge_log, evaluation_log) if part
            )[-20000:],
        },
        cancelled_on_conflict=True,
    )


def report_failure(job_id: str, error: Exception, *, retryable: bool) -> None:
    try:
        request_json(
            "POST",
            "/fail" if TASK_MODE else f"/runner/jobs/{job_id}/fail",
            payload={
                "runner_id": RUNNER_ID,
                "error_message": str(error)[:10000],
                "log_tail": str(error)[-20000:],
                "retryable": retryable,
            },
            cancelled_on_conflict=True,
        )
    except JobCancelled:
        return


def is_retryable_training_exception(error: Exception) -> bool:
    if isinstance(error, RetryableTrainingError):
        return True
    message = str(error).lower()
    return "out of memory" in message or "cuda error: memory allocation" in message


def main() -> None:
    if TASK_MODE:
        job = claim_job()
        if not job:
            raise RuntimeError("任务 URL 没有可领取的 LoRA 训练任务")
        try:
            process_job(job)
            print(
                f"training job {job['id']} completed",
                flush=True,
            )
        except JobCancelled:
            print(
                f"training job {job['id']} was cancelled",
                flush=True,
            )
        except RetryableTrainingError as exc:
            report_failure(str(job["id"]), exc, retryable=True)
            raise
        except Exception as exc:
            retryable = is_retryable_training_exception(exc)
            report_failure(str(job["id"]), exc, retryable=retryable)
            raise
        return
    while True:
        try:
            heartbeat()
            job = claim_job()
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            try:
                process_job(job)
            except JobCancelled:
                heartbeat()
            except RetryableTrainingError as exc:
                report_failure(str(job["id"]), exc, retryable=True)
                time.sleep(max(POLL_SECONDS, 30))
            except Exception as exc:
                retryable = is_retryable_training_exception(exc)
                report_failure(str(job["id"]), exc, retryable=retryable)
                heartbeat("online" if retryable else "error")
        except Exception as exc:
            print(f"runner loop error: {exc}", file=sys.stderr, flush=True)
            time.sleep(max(POLL_SECONDS, 15))


def run_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--heartbeat-once",
        action="store_true",
        help="发送一次 Runner 心跳并退出，用于连接检测",
    )
    args = parser.parse_args()
    if args.heartbeat_once:
        response = heartbeat()
        print(
            json.dumps(
                {
                    "status": "connected",
                    "runner": response.get("runner"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return
    main()


if __name__ == "__main__":
    run_cli()
