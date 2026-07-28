from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.core.config import settings


logger = logging.getLogger(__name__)
ProcessNextTask = Callable[[], bool]


async def run_knowledge_import_worker(
    stop_event: asyncio.Event,
    process_next_task: ProcessNextTask,
) -> None:
    """Run persisted Excel import tasks without tying work to a browser request."""

    poll_seconds = max(0.2, settings.KNOWLEDGE_IMPORT_POLL_SECONDS)
    while not stop_event.is_set():
        try:
            processed = await asyncio.to_thread(process_next_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Knowledge import worker iteration failed.")
            processed = False

        if processed:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
