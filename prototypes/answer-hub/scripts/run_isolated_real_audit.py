"""Run the real workflow in a fresh local sandbox without website access."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from urllib.parse import urlparse

from answer_hub.automation import run_automation_pipeline
from answer_hub.mimo import MimoConfig


def model_profile(profile: str) -> tuple[str, str, str]:
    """Resolve exactly one credential source, without ambient key failover."""
    if profile == "personal":
        key = os.environ.get("DEEPSEEK_PERSONAL_API_KEY", "").strip()
        base = os.environ.get("DEEPSEEK_PERSONAL_BASE_URL", "https://api.deepseek.com/v1").strip()
        model = os.environ.get("DEEPSEEK_PERSONAL_MODEL", "").strip()
        if urlparse(base).scheme != "https" or urlparse(base).netloc != "api.deepseek.com":
            raise RuntimeError("Personal profile requires the official HTTPS DeepSeek endpoint")
        if not key or not model:
            raise RuntimeError(
                "Personal profile requires DEEPSEEK_PERSONAL_API_KEY and "
                "DEEPSEEK_PERSONAL_MODEL through environment variables"
            )
    elif profile == "group":
        key = os.environ.get("GROUP_LLM_API_KEY", "").strip()
        base = os.environ.get("GROUP_LLM_BASE_URL", "").strip()
        model = os.environ.get("GROUP_LLM_MODEL", "").strip()
        if not key or not base or not model:
            raise RuntimeError("Group profile requires its own key, base URL and model")
    else:
        raise ValueError("Unknown model profile")
    parsed = urlparse(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Model base URL must not contain credentials, query parameters or fragments")
    return key, base, model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--model-profile", choices=("personal", "group"), default="personal")
    parser.add_argument("--standards-snapshot", type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    root = args.output_root.resolve()
    if root.exists():
        raise RuntimeError("Output root must be new; refusing to reuse a prior run")
    key, base, model = model_profile(args.model_profile)
    standards = (
        args.standards_snapshot.resolve(strict=True)
        if args.standards_snapshot else None
    )
    if args.check_config:
        print(json.dumps({"profile": args.model_profile, "base_url": base, "model": model,
                          "credential_present": True, "network_called": False}))
        return
    root.mkdir(parents=True, exist_ok=False)
    for name in tuple(os.environ):
        if name.startswith(("KB_", "CZ_", "EMBEDDING_", "AUTO_REVIEW_", "MIMO_")):
            os.environ[name] = ""
    os.environ.update({
        "MIMO_API_KEY": key,
        "MIMO_BASE_URL": base,
        "MIMO_MODEL": model,
        "MIMO_MEDIA_MODEL": model,
        "MIMO_MAX_COMPLETION_TOKENS": "8192",
        "MIMO_MAX_RETRIES": "1",
        "MIMO_TIMEOUT_SECONDS": "60",
        "MIMO_RESPONSE_READ_TIMEOUT_SECONDS": "90",
        "MIMO_CLUSTER_MEDIA_POLICY": "never",
        "ANSWER_HUB_LOCAL_MODEL_CONFIG": str(root / "unused-local-model.json"),
        "ANSWER_HUB_DB_PATH": str(root / "isolated-audit.db"),
        "ANSWER_HUB_MIMO_MAX_WORKERS": "4",
        "ANSWER_HUB_DIRECT_CLUSTER_MAX_WORKERS": "4",
        "ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW": "false",
        "AUTO_REVIEW_ENABLED": "false",
        "AUTO_REVIEW_KILL_SWITCH": "true",
        "KB_BASE_URL": "",
        "KB_INTEGRATION_KEY": "",
        "KB_RETRIEVAL_API_KEY": "",
    })
    provenance = {
        "source_path": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "scope": "fresh_extraction_real_clustering_real_transcription_model_review",
        "model": os.environ["MIMO_MODEL"],
        "model_profile": args.model_profile,
        "base_url": base,
        "credential_source": "DEEPSEEK_PERSONAL_API_KEY" if args.model_profile == "personal" else "GROUP_LLM_API_KEY",
        "cluster_media_policy": "never",
        "old_progress_reused": False,
        "standard_retrieval": standards is not None,
        "standard_source": (
            {
                "path": str(standards),
                "sha256": hashlib.sha256(standards.read_bytes()).hexdigest(),
                "scope": "historical_local_snapshot",
                "current_headquarters_effectiveness_verified": False,
            }
            if standards else None
        ),
        "website_access_enabled": False,
        "audit_database": os.environ["ANSWER_HUB_DB_PATH"],
        "source_fingerprints": {
            name: hashlib.sha256(
                (Path(__file__).resolve().parents[1] / "src" / "answer_hub" / name).read_bytes()
            ).hexdigest()
            for name in (
                "workflow.py", "mimo.py", "answer_evidence.py",
                "knowledge_value.py", "automation.py", "cz_integration.py",
                "topic_transcription/service.py",
            )
        },
        "started_at_epoch": time.time(),
    }
    (root / "run_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    last_status = None
    last_print = 0.0

    def progress(manifest):
        nonlocal last_status, last_print
        active = [
            (s.get("id"), s.get("status"), s.get("detail"), s.get("metrics"))
            for s in manifest.get("stages", [])
            if s.get("status") not in {"pending"}
        ]
        snapshot = {"run_id": manifest["run_id"], "status": manifest["status"], "stages": active}
        (root / "latest_progress.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        status = (manifest["status"], tuple((s[0], s[1]) for s in active))
        if status != last_status or time.monotonic() - last_print >= 30:
            print(json.dumps({"run_id": manifest["run_id"], "status": manifest["status"],
                              "stages": [(s[0], s[1]) for s in active]}, ensure_ascii=False), flush=True)
            last_status = status
            last_print = time.monotonic()

    # Block ambient .env loading, not model calls or workflow gates.
    with ExitStack() as stack:
        for module in ("mimo", "audit", "auto_review", "cz_integration"):
            stack.enter_context(patch(f"answer_hub.{module}.load_dotenv"))
        config = MimoConfig.from_env()
        if config is None or config.model != provenance["model"] or config.base_url != base:
            raise RuntimeError("Isolated model configuration validation failed")
        manifest = run_automation_pipeline(
            source_path=source, standards_path=standards, output_root=root / "runs",
            use_mimo=True, clustering_mode="direct_mimo",
            cluster_only=False, continue_on_mimo_unavailable=False,
            direct_mimo_progress_path=None, cluster_media_policy="never",
            progress_callback=progress,
            use_standard_references=standards is not None,
        )
    (root / "final_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps({
        "run_id": manifest["run_id"], "status": manifest["status"],
        "run_dir": manifest["run_dir"], "error": manifest.get("error"),
    }, ensure_ascii=False), flush=True)
    if manifest["status"] not in {"review_pending", "completed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
