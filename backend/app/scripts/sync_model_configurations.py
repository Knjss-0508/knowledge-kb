from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.database import SessionLocal
from app.services.model_configuration import (
    ModelConfigurationSyncError,
    parse_model_configuration_payload,
    sync_model_configurations,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="同步飞书机型配置信息到知识库中台。",
    )
    parser.add_argument(
        "input",
        help=(
            "由 scripts/export-model-configurations-from-lark.ps1 生成的 JSON 文件；"
            "传 - 时从标准输入读取。"
        ),
    )
    parser.add_argument(
        "--actor",
        default="model-configuration-sync",
        help="写入 created_by/updated_by/change_logs 的操作者标识。",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload_text = (
        sys.stdin.read()
        if args.input == "-"
        else Path(args.input).read_text(encoding="utf-8-sig")
    )
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_JSON_INVALID",
            f"机型配置同步文件不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列。",
        ) from exc
    if not isinstance(payload, dict):
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_PAYLOAD_INVALID",
            "机型配置同步文件根节点必须是对象。",
        )
    records = parse_model_configuration_payload(payload)
    actor = args.actor.strip() or "model-configuration-sync"
    if len(actor) > 128:
        raise ModelConfigurationSyncError(
            "MODEL_CONFIGURATION_ACTOR_TOO_LONG",
            "同步操作者标识不能超过 128 个字符。",
        )
    db = SessionLocal()
    try:
        result = sync_model_configurations(
            db,
            records,
            actor=actor,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    print(
        json.dumps(
            {
                "status": "success",
                "total": result.total,
                "created": result.created,
                "updated": result.updated,
                "unchanged": result.unchanged,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelConfigurationSyncError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": exc.code,
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
