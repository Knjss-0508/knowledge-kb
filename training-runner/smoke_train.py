from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


os.environ.setdefault("TRAINING_CONTROL_BASE_URL", "http://127.0.0.1")
os.environ.setdefault(
    "TRAINING_RUNNER_TOKEN",
    "host-smoke-runner-token-with-32-characters",
)
os.environ.setdefault("TRAINING_RUNNER_ID", "host-smoke-runner")

import runner  # noqa: E402


def sample_rows() -> list[dict]:
    topics = [
        ("如何查看平板序列号", "打开设置、通用、关于本机，查看序列号。"),
        ("手机无法充电怎么办", "先检查充电器、数据线和充电接口是否损坏。"),
        ("如何退出账号", "进入设置中的账号与安全页面，选择退出登录。"),
        ("屏幕出现亮点是否正常", "固定亮点可能属于屏幕显示异常，需要进一步质检。"),
        ("设备进水后怎么处理", "立即关机并停止充电，避免继续通电造成损坏。"),
        ("如何恢复出厂设置", "备份数据后进入系统设置执行恢复出厂设置。"),
        ("忘记锁屏密码怎么办", "按品牌官方账号找回流程处理，必要时联系售后。"),
        ("摄像头无法对焦", "清洁镜头并重启相机，仍异常时检查摄像头模组。"),
    ]
    rows: list[dict] = []
    for split, count in (("train", 8), ("validation", 2), ("test", 2)):
        for index in range(count):
            question, answer = topics[index % len(topics)]
            negative = topics[(index + 3) % len(topics)][1]
            rows.append(
                {
                    "id": f"{split}-{index + 1}",
                    "task_type": "retrieval",
                    "split": split,
                    "messages": [{"role": "user", "content": question}],
                    "positive": [answer],
                    "negative": [negative],
                    "prompt": "检索能够准确回答用户问题的已发布知识",
                    "metadata": {"source": "host-smoke"},
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-root",
        required=True,
        help="项目目录外的训练运行目录",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen3-Embedding-0.6B",
    )
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve()
    smoke_root = runtime_root / "smoke-tests" / datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )
    smoke_root.mkdir(parents=True, exist_ok=False)
    dataset_paths = runner.write_dataset(
        {
            "dataset": sample_rows(),
            "training_config": {"max_negative_samples": 1},
        },
        smoke_root,
    )
    output_dir = smoke_root / "training-output"
    output_dir.mkdir(parents=True, exist_ok=False)
    job = {
        "base_model": args.model,
        "train_type": "lora",
        "training_config": {
            "low_memory_mode": True,
            "quant_method": "bnb",
            "quant_bits": 4,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_use_double_quant": True,
            "max_length": 64,
            "num_train_epochs": 1,
            "per_device_train_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "learning_rate": 0.0001,
            "lora_rank": 8,
            "lora_alpha": 16,
            "seed": 42,
            "max_steps": 1,
            "save_strategy": "steps",
            "save_steps": 1,
            "eval_strategy": "no",
            "save_total_limit": 1,
            "report_to": "none",
        },
    }
    command = runner.training_command(job, dataset_paths, output_dir)
    print("开始执行 1 step QLoRA 宿主机实训...", flush=True)
    completed = subprocess.run(
        command,
        cwd=str(smoke_root),
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"宿主机 QLoRA 实训失败，退出码：{completed.returncode}"
        )
    checkpoint = runner.latest_checkpoint(output_dir)
    expected_files = (
        "adapter_config.json",
        "adapter_model.safetensors",
    )
    missing = [
        name for name in expected_files if not (checkpoint / name).is_file()
    ]
    if missing:
        raise RuntimeError("训练未生成 LoRA 产物：" + "、".join(missing))
    print(
        json.dumps(
            {
                "status": "passed",
                "model": args.model,
                "steps": 1,
                "checkpoint": str(checkpoint),
                "files": list(expected_files),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
