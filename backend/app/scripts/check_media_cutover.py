"""Read-only preflight for switching the legacy local media host to gateway mode."""

from __future__ import annotations

import sys
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.knowledge import MediaDeletionTask, MediaUploadStaging


SessionFactory = Callable[[], Session]


def get_local_queue_counts(
    session_factory: SessionFactory | None = None,
) -> tuple[int, int]:
    """Return local deletion-task and staging-row counts without mutating data."""

    session_factory = session_factory or SessionLocal
    db = session_factory()
    try:
        deletion_tasks = (
            db.query(MediaDeletionTask)
            .filter(MediaDeletionTask.storage_backend == "local")
            .count()
        )
        staging_rows = (
            db.query(MediaUploadStaging)
            .filter(MediaUploadStaging.storage_backend == "local")
            .count()
        )
        return int(deletion_tasks), int(staging_rows)
    finally:
        db.close()


def main() -> int:
    try:
        deletion_tasks, staging_rows = get_local_queue_counts()
    except Exception as exc:
        print(
            f"媒体切换预检无法读取数据库：{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    print(f"local_deletion_tasks={deletion_tasks}")
    print(f"local_staging_rows={staging_rows}")
    if deletion_tasks or staging_rows:
        print(
            "媒体切换预检失败：local 队列必须全部清零后才能启用 MEDIA_GATEWAY_ONLY。",
            file=sys.stderr,
        )
        return 1

    print("媒体切换预检通过：local 队列为空。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
