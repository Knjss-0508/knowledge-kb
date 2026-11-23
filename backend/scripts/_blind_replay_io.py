"""Local-only input and read-only connection for the blind-label replay CLIs."""

import os

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


LOOPBACK_IPS = {"127.0.0.1", "::1"}
_MAX_MANIFEST_BYTES = 1_000_000


def local_database():
    database = make_url(os.environ.get("BLIND_REPLAY_DATABASE_URL", ""))
    if (
        database.drivername not in {"postgresql", "postgresql+psycopg2"}
        or database.host not in LOOPBACK_IPS
        or not database.database
        or database.query
    ):
        raise ValueError("需要显式配置不带连接参数的本机 PostgreSQL 副本地址。")
    return database


def readonly_engine(database):
    return create_engine(
        database, hide_parameters=True,
        connect_args={
            "hostaddr": database.host,
            "connect_timeout": 10,
            "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
        },
    )


def read_manifest(parser, path, manifest_type):
    try:
        with path.open("rb") as source:
            payload = source.read(_MAX_MANIFEST_BYTES + 1)
    except OSError:
        parser.error("无法读取回放清单。")
    if len(payload) > _MAX_MANIFEST_BYTES:
        parser.error("回放清单超过大小限制。")
    try:
        return payload, manifest_type.model_validate_json(payload)
    except ValidationError as exc:
        parser.error(f"回放清单格式错误（{exc.error_count()} 处）；未输出原始输入。")
