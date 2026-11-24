"""Run a bounded retrieval baseline against a local restored database and model."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.blind_label_replay import ReplayManifest, run_retrieval_replay
from scripts._blind_replay_io import LOOPBACK_IPS, local_database, read_manifest, readonly_engine


def _local_endpoints():
    database = local_database()
    try:
        embedding = urlsplit(os.environ.get("BLIND_REPLAY_EMBEDDING_URL", ""))
        if (
            embedding.scheme not in {"http", "https"}
            or embedding.hostname not in LOOPBACK_IPS
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
    payload, manifest = read_manifest(parser, args.manifest, ReplayManifest)

    settings.EMBEDDING_BASE_URL = embedding_url
    # Do not send an inherited production key or route local requests via a proxy.
    settings.EMBEDDING_API_KEY = ""
    os.environ["NO_PROXY"] = "127.0.0.1,::1"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    engine = readonly_engine(database)
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
