from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from domain.content_jobs import ContentJobQueue


logger = logging.getLogger(__name__)
JobHandler = Callable[[dict[str, Any], "JobContext"], Awaitable[None]]
JobFinishedHook = Callable[[dict[str, Any], str], Awaitable[None]]


class ContentJobCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class JobContext:
    queue: ContentJobQueue
    job_id: int
    worker_id: str
    lease_seconds: int

    async def report_progress(self, *, total: int, completed: int, failed: int = 0) -> bool:
        return await self.queue.set_progress(
            self.job_id,
            self.worker_id,
            total_items=total,
            completed_items=completed,
            failed_items=failed,
            lease_seconds=self.lease_seconds,
        )

    async def heartbeat(self, *, lease_seconds: int = 300) -> bool:
        return await self.queue.heartbeat(
            self.job_id, self.worker_id, lease_seconds=lease_seconds
        )

    async def is_cancelled(self) -> bool:
        job = await self.queue.get(self.job_id)
        return job is None or job["status"] == "cancelled"


class ContentJobWorker:
    def __init__(
        self,
        queue: ContentJobQueue,
        worker_id: str,
        handlers: Mapping[str, JobHandler],
        *,
        lease_seconds: int = 300,
        retry_delay_seconds: int = 30,
        on_finished: JobFinishedHook | None = None,
    ):
        self.queue = queue
        self.worker_id = worker_id
        self.handlers = dict(handlers)
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.on_finished = on_finished

    async def run_once(self) -> bool:
        job = await self.queue.claim(
            self.worker_id, lease_seconds=self.lease_seconds
        )
        if job is None:
            return False
        handler = self.handlers.get(job["job_type"])
        if handler is None:
            status = await self.queue.fail(
                job["id"],
                self.worker_id,
                f"no_handler:{job['job_type']}",
                retry_delay_seconds=self.retry_delay_seconds,
            )
            if status == "failed":
                await self._notify_finished(job, "failed")
            return True
        context = JobContext(
            self.queue, job["id"], self.worker_id, self.lease_seconds
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(context))
        try:
            result = handler(job, context)
            if not inspect.isawaitable(result):
                raise TypeError("content job handlers must be async")
            await result
            if await self.queue.complete(job["id"], self.worker_id):
                await self._notify_finished(job, "completed")
        except ContentJobCancelled:
            logger.info("Content job %s was cancelled", job["id"])
            await self._notify_finished(job, "cancelled")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Content job %s failed", job["id"])
            status = await self.queue.fail(
                job["id"],
                self.worker_id,
                f"{type(exc).__name__}: {exc}",
                retry_delay_seconds=self.retry_delay_seconds,
            )
            if status == "failed":
                await self._notify_finished(job, "failed")
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return True

    async def _heartbeat_loop(self, context: JobContext) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not await context.heartbeat(lease_seconds=self.lease_seconds):
                return

    async def _notify_finished(self, job: dict[str, Any], status: str) -> None:
        if self.on_finished is None:
            return
        try:
            await self.on_finished(job, status)
        except Exception:
            logger.exception("Content job %s notification failed", job["id"])

    async def run_forever(self, *, idle_seconds: float = 2.0) -> None:
        while True:
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Content job worker iteration failed")
                worked = False
            if not worked:
                await asyncio.sleep(idle_seconds)
