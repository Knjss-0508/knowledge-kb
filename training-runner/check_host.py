from __future__ import annotations

import importlib.metadata
import json
import shutil
import sys
from pathlib import Path


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def main() -> None:
    import bitsandbytes as bnb
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch 未识别到 CUDA GPU")

    device = torch.device("cuda:0")
    probe = torch.randn(128, 128, device=device, dtype=torch.bfloat16)
    quantized, state = bnb.functional.quantize_4bit(
        probe,
        quant_type="nf4",
        compress_statistics=True,
    )
    restored = bnb.functional.dequantize_4bit(quantized, state)
    if restored.shape != probe.shape:
        raise RuntimeError("bitsandbytes NF4 校验结果尺寸异常")

    swift_cli = shutil.which("swift")
    if not swift_cli:
        candidate = (
            "swift.exe" if sys.platform == "win32" else "swift"
        )
        alongside_python = Path(sys.executable).resolve().with_name(candidate)
        if alongside_python.is_file():
            swift_cli = str(alongside_python)
    if not swift_cli:
        raise RuntimeError("未找到 ms-swift CLI")

    from swift.pipelines import sft_main  # noqa: F401

    payload = {
        "status": "ready",
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "cuda_runtime": torch.version.cuda or "",
        "gpu": torch.cuda.get_device_name(0),
        "gpu_memory_mb": round(
            torch.cuda.get_device_properties(0).total_memory / 1024 / 1024
        ),
        "bitsandbytes": package_version("bitsandbytes"),
        "ms_swift": package_version("ms-swift"),
        "sentence_transformers": package_version("sentence-transformers"),
        "nf4_probe": "passed",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
