"""在断网试点运行召回回流；只读数据库，只写隔离产物。"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.blind_label_answer_replay import AnswerReplayManifest
from app.services.blind_label_cycle import Diagnosis, run_learning_cycle
from app.services.blind_label_replay import ReplayManifest
from scripts._blind_replay_io import LOOPBACK_IPS, local_database, read_manifest, readonly_engine
from scripts.replay_blind_label_retrieval import _local_endpoints


def _local_ollama() -> tuple[str, str] | None:
    url = os.environ.get("BLIND_CYCLE_OLLAMA_URL", "")
    model = os.environ.get("BLIND_CYCLE_OLLAMA_MODEL", "")
    if not (url or model):
        return None
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in LOOPBACK_IPS or not parsed.port
            or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,79}", model)):
        raise ValueError("Invalid local Ollama configuration")
    return url.rstrip("/") + "/api/chat", model


def _diagnose(client: httpx.Client, endpoint: tuple[str, str], evidence: dict) -> Diagnosis | None:
    url, model = endpoint
    try:
        response = client.post(url, json={
            "model": model,
            "stream": False,
            "format": Diagnosis.model_json_schema(),
            "options": {"temperature": 0, "num_predict": 96},
            "messages": [
                {"role": "system", "content":
                    "根据结构化投票和原因计数推荐问题分类，不能确认事实。候选排序异常选 ranking，"
                    "已有知识但未召回选 retrieval_missed，有独立答复问题证据才选 answer_issue，"
                    "知识过时或条件不全选 knowledge_issue，已有明确缺知识证据选 knowledge_gap。"
                    "证据不足、矛盾或不能区分时选 insufficient_evidence。只输出 schema 指定的 JSON。"},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=True)},
            ],
        })
        response.raise_for_status()
        content = response.json()["message"]["content"]
        if not isinstance(content, str) or len(content) > 1024:
            return None
        return Diagnosis.model_validate_json(content)
    except (httpx.HTTPError, ValidationError, KeyError, TypeError, ValueError):
        # No model output or supplied data goes to console/errors.
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-local-sanitized", action="store_true")
    parser.add_argument("--retrieval-manifest", type=Path)
    parser.add_argument("--answer-manifest", type=Path)
    parser.add_argument("--scan-limit", type=int, choices=range(1, 101), default=50, metavar="1..100")
    parser.add_argument("--ai-limit", type=int, choices=range(0, 21), default=5, metavar="0..20")
    parser.add_argument("--cursor")
    parser.add_argument("--category-id")
    parser.add_argument("--out", type=Path, required=True, help="仓库外、受控且尚不存在的 JSON 文件")
    args = parser.parse_args()
    if not args.confirm_local_sanitized:
        parser.error("请确认是断网隔离副本（非生产隧道）且清单已脱敏，再传 --confirm-local-sanitized。")
    output = args.out.resolve()
    if (output.suffix.lower() != ".json" or output.exists()
            or any((ancestor / ".git").exists() for ancestor in output.parents)
            or not output.parent.is_dir()):
        parser.error("产物需写入仓库外、已存在的受控目录，且不得覆盖已有 JSON。")
    try:
        database = local_database()
        ollama = _local_ollama() if args.ai_limit else None
        if args.retrieval_manifest:
            _, settings.EMBEDDING_BASE_URL = _local_endpoints()
            settings.EMBEDDING_API_KEY = ""
    except (ArgumentError, ValueError):
        parser.error("数据库和模型均须显式配置有效的回环地址。")
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,::1"
    retrieval, answers, inputs = None, None, {}
    if args.retrieval_manifest:
        payload, retrieval = read_manifest(parser, args.retrieval_manifest, ReplayManifest)
        inputs["retrieval_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
        inputs["embedding_model"] = settings.EMBEDDING_MODEL
    if args.answer_manifest:
        payload, answers = read_manifest(parser, args.answer_manifest, AnswerReplayManifest)
        inputs["answer_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    if ollama:
        inputs["diagnosis_model"] = ollama[1]
    engine = readonly_engine(database)
    try:
        with engine.begin() as connection, httpx.Client(trust_env=False, timeout=60.0) as client:
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            # The online ranker closes its ORM session before embedding calls.
            # Keep the snapshot on this outer connection, not on that session.
            with Session(bind=connection, autoflush=False, join_transaction_mode="create_savepoint") as db:
                result = run_learning_cycle(
                    db, retrieval_manifest=retrieval, answer_manifest=answers,
                    scan_limit=args.scan_limit, cursor=args.cursor, category_id=args.category_id, ai_limit=args.ai_limit,
                    diagnose=(lambda evidence: _diagnose(client, ollama, evidence)) if ollama else None,
                )
    except SQLAlchemyError:
        parser.error("隔离数据库连接或查询失败；未输出 SQL 或输入。")
    finally:
        engine.dispose()
    try:
        # Exclusive creation with owner-only bits on POSIX. Windows relies on
        # the access control of the operator-supplied directory.
        descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({**result, "inputs": inputs}, handle, ensure_ascii=True, indent=2, allow_nan=False)
    except OSError:
        parser.error("无法写入受控产物；未输出样本内容。")
    discovery = result["discovery"]
    print(json.dumps({
        "scanned": discovery["scanned"], "preview_cases": len(discovery["cases"]),
        "ai_attempted": discovery["ai_attempted"], "ai_suggested": discovery["ai_suggested"],
        "retrieval": result["validation"]["retrieval"]["summary"],
        "answers_configured": answers is not None, "training": result["training"]["status"],
        "knowledge_drafts": len(result["knowledge_drafts"]), "published": False,
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
