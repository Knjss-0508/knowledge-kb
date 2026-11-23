"""Run a bounded retrieval baseline against a local restored database and model."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.blind_label_replay import ReplayManifest, run_retrieval_replay


_LOOPBACK_IPS = {"127.0.0.1", "::1"}
_MAX_MANIFEST_BYTES = 1_000_000


def _local_endpoints():
    database = make_url(os.environ.get("BLIND_REPLAY_DATABASE_URL", ""))
    if (
        database.drivername not in {"postgresql", "postgresql+psycopg2"}
        or database.host not in _LOOPBACK_IPS
        or not database.database
        or database.query
    ):
        raise ValueError("需要显式配置不带连接参数的本机 PostgreSQL 副本地址。")
    try:
        embedding = urlsplit(os.environ.get("BLIND_REPLAY_EMBEDDING_URL", ""))
        if (
            embedding.scheme not in {"http", "https"}
            or embedding.hostname not in _LOOPBACK_IPS
            or not embedding.port
            or embedding.username is not None
            or embedding.query
            or embedding.fragment
        ):
            raise ValueError
    except ValueError:
        raise ValueError("需要显式配置带端口的本机 Embedding 地址。") from None
    return database, embedding.geturl()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="本地脱敏回放清单 JSON")
    parser.add_argument("--confirm-local-sanitized", action="store_true",
                        help="确认本机不是生产隧道，清单已脱敏，数据库是隔离副本")
    args = parser.parse_args()
    if not args.confirm_local_sanitized:
        parser.error("请先确认隔离环境和清单脱敏，再传 --confirm-local-sanitized")
    try:
        database, embedding_url = _local_endpoints()
    except (ArgumentError, ValueError):
        parser.error("请配置有效的 BLIND_REPLAY_DATABASE_URL 与 BLIND_REPLAY_EMBEDDING_URL（仅接受本机 IP）。")
    try:
        with args.manifest.open("rb") as source:
            payload = source.read(_MAX_MANIFEST_BYTES + 1)
    except OSError:
        parser.error("无法读取回放清单。")
    if len(payload) > _MAX_MANIFEST_BYTES:
        parser.error("回放清单超过大小限制。")
    try:
        manifest = ReplayManifest.model_validate_json(payload)
    except ValidationError as exc:
        parser.error(f"回放清单格式错误（{exc.error_count()} 处）；未输出原始输入。")

    settings.EMBEDDING_BASE_URL = embedding_url
    # Do not send an inherited production key or route local requests via a proxy.
    settings.EMBEDDING_API_KEY = ""
    os.environ["NO_PROXY"] = "127.0.0.1,::1"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    engine = create_engine(
        database, hide_parameters=True,
        connect_args={
            "hostaddr": database.host,
            "connect_timeout": 10,
            "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
        },
    )
    try:
        with Session(engine, autoflush=False) as db:
            report = run_retrieval_replay(db, manifest)
    except SQLAlchemyError:
        parser.error("隔离数据库连接或查询失败，未输出 SQL 或输入内容。")
    finally:
        engine.dispose()

    report["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    report["embedding_model"] = settings.EMBEDDING_MODEL
    report["knowledge_api_version"] = settings.VERSION
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
