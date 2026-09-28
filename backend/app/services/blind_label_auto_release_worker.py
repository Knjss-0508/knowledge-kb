"""后台自动回收超时盲标任务。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.blind_labeling import release_expired_assignments


logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


def reclaim_expired_blind_label_assignments(
    *,
    session_factory: SessionFactory = SessionLocal,
) -> int:
    """在独立数据库事务中回收一批超时任务。"""

    db = session_factory()
    try:
        reclaimed = release_expired_assignments(db)
        db.commit()
        return reclaimed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_blind_label_auto_release_worker(stop_event: asyncio.Event) -> None:
    """按配置周期扫描超时盲标预约，进程停止时立即退出。"""

    poll_seconds = max(1.0, settings.BLIND_LABEL_RELEASE_POLL_SECONDS)
    while not stop_event.is_set():
        try:
            reclaimed = await asyncio.to_thread(reclaim_expired_blind_label_assignments)
            if reclaimed:
                logger.info("已自动回收 %s 条超时盲标任务。", reclaimed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("盲标超时自动回收任务执行失败。")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
