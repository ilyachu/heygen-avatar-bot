import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.migrations import run_migrations
from domain.content_jobs import (
    ContentJobError,
    ContentJobQueue,
    JOB_WEEKLY_USEFUL,
    weekly_useful_job_spec,
)
from workers.content_jobs import ContentJobCancelled, ContentJobWorker


class ContentJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "jobs.db")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE,
                    title TEXT,
                    username TEXT,
                    added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        await run_migrations(self.db_path)
        self.queue = ContentJobQueue(self.db_path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_enqueue_replay_returns_one_job(self):
        first, second = await asyncio.gather(
            self.queue.enqueue(
                "test", {"value": 1}, idempotency_key="stable-key"
            ),
            self.queue.enqueue(
                "test", {"value": 1}, idempotency_key="stable-key"
            ),
        )
        self.assertEqual(first["id"], second["id"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM content_jobs").fetchone()[0])

    async def test_concurrent_claim_has_one_winner(self):
        job = await self.queue.enqueue("test", {}, idempotency_key="claim-once")
        claims = await asyncio.gather(
            self.queue.claim("worker-a"), self.queue.claim("worker-b")
        )
        winners = [item for item in claims if item is not None]
        self.assertEqual([job["id"]], [item["id"] for item in winners])
        self.assertEqual(1, winners[0]["attempt_count"])

    async def test_expired_lease_is_recovered_and_retry_is_bounded(self):
        start = datetime(2026, 9, 18, 9, tzinfo=timezone.utc)
        job = await self.queue.enqueue(
            "test", {}, idempotency_key="recover", max_attempts=2, available_at=start
        )
        first = await self.queue.claim("dead", now=start, lease_seconds=10)
        self.assertEqual(job["id"], first["id"])
        second = await self.queue.claim(
            "replacement", now=start + timedelta(seconds=11), lease_seconds=10
        )
        self.assertEqual(job["id"], second["id"])
        self.assertEqual(2, second["attempt_count"])
        self.assertEqual(1, await self.queue.recover_expired_leases(start + timedelta(seconds=22)))
        final = await self.queue.get(job["id"])
        self.assertEqual("failed", final["status"])
        self.assertEqual("lease_expired_max_attempts", final["last_error"])

    async def test_worker_reports_progress_and_completes(self):
        seen = []

        async def handler(job, context):
            seen.append(job["payload"]["week"])
            before = (await self.queue.get(job["id"]))["lease_expires_at"]
            await asyncio.sleep(0.01)
            self.assertTrue(await context.report_progress(total=3, completed=2, failed=1))
            after = (await self.queue.get(job["id"]))["lease_expires_at"]
            self.assertGreater(after, before)

        job = await self.queue.enqueue(
            "custom", {"week": "next"}, idempotency_key="worker"
        )
        worker = ContentJobWorker(
            self.queue, "worker", {"custom": handler}, lease_seconds=60
        )
        self.assertTrue(await worker.run_once())
        result = await self.queue.get(job["id"])
        self.assertEqual(["next"], seen)
        self.assertEqual("completed", result["status"])
        self.assertEqual((3, 2, 1), (
            result["total_items"], result["completed_items"], result["failed_items"]
        ))

    async def test_failure_retries_then_stops_and_cancel_blocks_completion(self):
        job = await self.queue.enqueue(
            "test", {}, idempotency_key="retry", max_attempts=2
        )
        first = await self.queue.claim("worker")
        self.assertEqual("queued", await self.queue.fail(
            first["id"], "worker", "temporary", retry_delay_seconds=0
        ))
        second = await self.queue.claim("worker")
        self.assertEqual("failed", await self.queue.fail(
            second["id"], "worker", "permanent", retry_delay_seconds=0
        ))
        self.assertIsNone(await self.queue.claim("worker"))

        cancelled = await self.queue.enqueue("test", {}, idempotency_key="cancel")
        claimed = await self.queue.claim("worker")
        self.assertEqual(cancelled["id"], claimed["id"])
        self.assertTrue(await self.queue.cancel(cancelled["id"]))
        self.assertFalse(await self.queue.complete(cancelled["id"], "worker"))

    async def test_weekend_policy_enqueues_next_week_useful_once(self):
        friday = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
        spec = weekly_useful_job_spec(friday)
        self.assertEqual(JOB_WEEKLY_USEFUL, spec.job_type)
        self.assertEqual("2026-09-21", spec.payload["week_start"])
        self.assertEqual(["useful"], spec.payload["post_types"])
        first = await self.queue.enqueue_weekly_useful(friday)
        second = await self.queue.enqueue_weekly_useful(friday + timedelta(days=2))
        self.assertEqual(first["id"], second["id"])
        self.assertIsNone(await self.queue.enqueue_weekly_useful(friday + timedelta(days=3)))

    async def test_saturday_and_sunday_catch_up_share_monday_key(self):
        saturday = datetime(2026, 9, 19, 10, tzinfo=timezone.utc)
        sunday = saturday + timedelta(days=1)
        self.assertEqual(
            weekly_useful_job_spec(saturday).idempotency_key,
            weekly_useful_job_spec(sunday).idempotency_key,
        )

    async def test_warming_and_selling_require_explicit_webinar_job(self):
        with self.assertRaises(ContentJobError):
            await self.queue.enqueue(
                "weekly", {"post_types": ["warming"]}, idempotency_key="wrong"
            )
        job = await self.queue.enqueue_webinar(42)
        self.assertEqual(["warming", "selling"], job["payload"]["post_types"])
        self.assertEqual(42, job["payload"]["webinar_id"])

    async def test_cancelled_handler_does_not_stop_worker(self):
        handled = []

        async def handler(job, context):
            if job["payload"].get("cancel"):
                await context.queue.cancel(job["id"])
                raise ContentJobCancelled
            handled.append(job["id"])

        cancelled = await self.queue.enqueue(
            "custom", {"cancel": True}, idempotency_key="cancel-handler"
        )
        next_job = await self.queue.enqueue(
            "custom", {"cancel": False}, idempotency_key="next-handler"
        )
        worker = ContentJobWorker(self.queue, "worker", {"custom": handler})

        self.assertTrue(await worker.run_once())
        self.assertTrue(await worker.run_once())

        self.assertEqual("cancelled", (await self.queue.get(cancelled["id"]))["status"])
        self.assertEqual([next_job["id"]], handled)

    async def test_failed_or_cancelled_job_can_be_retried_manually(self):
        failed = await self.queue.enqueue(
            "test", {}, idempotency_key="manual-failed", max_attempts=1
        )
        claim = await self.queue.claim("worker")
        await self.queue.fail(claim["id"], "worker", "failed", retry_delay_seconds=0)
        cancelled = await self.queue.enqueue(
            "test", {}, idempotency_key="manual-cancelled"
        )
        await self.queue.cancel(cancelled["id"])

        self.assertTrue(await self.queue.retry(failed["id"]))
        self.assertTrue(await self.queue.retry(cancelled["id"]))
        self.assertFalse(await self.queue.retry(cancelled["id"]))
        self.assertEqual("queued", (await self.queue.get(failed["id"]))["status"])
        self.assertEqual("queued", (await self.queue.get(cancelled["id"]))["status"])

    async def test_worker_loop_recovers_from_iteration_error(self):
        worker = ContentJobWorker(self.queue, "worker", {})
        with patch.object(
            worker,
            "run_once",
            new=AsyncMock(side_effect=[RuntimeError("temporary"), asyncio.CancelledError]),
        ) as run_once:
            with self.assertRaises(asyncio.CancelledError):
                await worker.run_forever(idle_seconds=0)

        self.assertEqual(2, run_once.await_count)


if __name__ == "__main__":
    unittest.main()
