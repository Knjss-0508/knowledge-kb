from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.knowledge import MediaDeletionTask, MediaUploadStaging
from app.services.media_storage import (
    MediaStorage,
    MediaStorageError,
    get_media_storage,
)


logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


def enqueue_media_deletion(
    db: Session,
    storage_key: str,
    filename: str,
    *,
    storage_backend: str | None = None,
) -> MediaDeletionTask:
    """在当前业务事务中登记对象删除任务。"""

    backend = storage_backend or get_media_storage().backend
    task = MediaDeletionTask(
        id=f"media-delete-{uuid.uuid4().hex}",
        storage_backend=backend,
        storage_key=storage_key,
        filename=filename,
        attempt_count=0,
        next_attempt_at=datetime.utcnow(),
        last_error="",
    )
    db.add(task)
    return task


def _retry_delay_seconds(attempt_count: int) -> int:
    base = max(1, settings.MEDIA_DELETION_RETRY_BASE_SECONDS)
    maximum = max(base, settings.MEDIA_DELETION_RETRY_MAX_SECONDS)
    exponent = min(max(attempt_count - 1, 0), 10)
    return min(maximum, base * (2**exponent))


def expire_media_upload_staging(
    *,
    session_factory: SessionFactory = SessionLocal,
    batch_size: int | None = None,
    now: datetime | None = None,
) -> int:
    """将过期 staging 在同一事务中转为对象删除 outbox。"""

    limit = max(1, batch_size or settings.MEDIA_DELETION_BATCH_SIZE)
    due_at = now or datetime.utcnow()
    db = session_factory()
    try:
        staged_uploads = (
            db.query(MediaUploadStaging)
            .filter(MediaUploadStaging.expires_at <= due_at)
            .order_by(
                MediaUploadStaging.expires_at,
                MediaUploadStaging.created_at,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
            .all()
        )
        for staged in staged_uploads:
            enqueue_media_deletion(
                db,
                staged.storage_key,
                staged.filename,
                storage_backend=staged.storage_backend,
            )
            db.delete(staged)
        db.commit()
        return len(staged_uploads)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def process_media_deletion_tasks(
    *,
    session_factory: SessionFactory = SessionLocal,
    storage: MediaStorage | None = None,
    batch_size: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """处理一批删除任务。

    对象删除本身是幂等的：若对象已被删除，但数据库提交失败，任务会在下次
    再次执行，最终只在删除调用成功且任务删除提交成功后离开 outbox。
    """

    active_storage = storage or get_media_storage()
    limit = max(1, batch_size or settings.MEDIA_DELETION_BATCH_SIZE)
    due_at = now or datetime.utcnow()
    deleted = 0
    failed = 0
    db = session_factory()
    try:
        tasks = (
            db.query(MediaDeletionTask)
            .filter(
                MediaDeletionTask.storage_backend == active_storage.backend,
                MediaDeletionTask.next_attempt_at <= due_at,
            )
            .order_by(
                MediaDeletionTask.next_attempt_at,
                MediaDeletionTask.created_at,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
            .all()
        )
        for task in tasks:
            try:
                active_storage.delete(task.storage_key, task.filename)
            except Exception as exc:
                task.attempt_count = (task.attempt_count or 0) + 1
                task.last_error = str(exc)[:1000] or type(exc).__name__
                task.next_attempt_at = due_at + timedelta(
                    seconds=_retry_delay_seconds(task.attempt_count)
                )
                task.updated_at = datetime.utcnow()
                failed += 1
                logger.warning(
                    "Media deletion task %s failed on attempt %s.",
                    task.id,
                    task.attempt_count,
                )
            else:
                db.delete(task)
                deleted += 1
        db.commit()
        return {
            "selected": len(tasks),
            "deleted": deleted,
            "failed": failed,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def delete_media_immediately_or_enqueue(
    storage_key: str,
    filename: str,
    *,
    storage: MediaStorage | None = None,
    session_factory: SessionFactory = SessionLocal,
) -> bool:
    """清理未提交事务产生的对象，失败时写入独立删除任务。"""

    active_storage = storage or get_media_storage()
    try:
        active_storage.delete(storage_key, filename)
        return True
    except MediaStorageError:
        logger.exception(
            "Immediate cleanup failed for media object %s; enqueueing retry.",
            filename,
        )

    db = session_factory()
    try:
        enqueue_media_deletion(
            db,
            storage_key,
            filename,
            storage_backend=active_storage.backend,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to persist deletion retry for media object %s.",
            filename,
        )
    finally:
        db.close()
    return False


async def run_media_deletion_worker(stop_event: asyncio.Event) -> None:
    """后台周期处理删除 outbox；多实例通过 SKIP LOCKED 安全并行。"""

    poll_seconds = max(0.5, settings.MEDIA_DELETION_POLL_SECONDS)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(expire_media_upload_staging)
            await asyncio.to_thread(process_media_deletion_tasks)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Media deletion worker iteration failed.")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
