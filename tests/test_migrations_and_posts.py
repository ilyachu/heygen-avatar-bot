import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.migrations import run_migrations
from domain.posts import Actor, ConcurrentPostUpdateError, PostWorkflow, PostWorkflowError


BASE_SCHEMA = """
CREATE TABLE channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT UNIQUE,
    title TEXT,
    username TEXT,
    added_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        with sqlite3.connect(self.db_path) as db:
            db.executescript(BASE_SCHEMA)

    async def asyncTearDown(self):
        self.tmp.cleanup()


class ContentPlatformMigrationTests(DatabaseTestCase):
    async def test_concurrent_migration_runners_apply_each_version_once(self):
        results = await asyncio.gather(
            run_migrations(self.db_path),
            run_migrations(self.db_path),
        )

        self.assertEqual(7, sum(len(result) for result in results))
        with sqlite3.connect(self.db_path) as db:
            versions = db.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(7, len(versions))

    async def test_migrations_are_idempotent_and_enable_wal(self):
        first = await run_migrations(self.db_path)
        second = await run_migrations(self.db_path)

        self.assertEqual(
            [
                "001_content_platform.sql",
                "002_channel_profile_description.sql",
                "003_campaign_delivery.sql",
                "004_weekly_content_plan.sql",
                "005_broadcast_operations.sql",
                "006_content_jobs.sql",
                "007_quality_metrics.sql",
            ],
            first,
        )
        self.assertEqual([], second)
        with sqlite3.connect(self.db_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertTrue(
            {
                "posts",
                "post_events",
                "webinars",
                "login_tokens",
                "content_jobs",
                "quality_reviews",
                "post_metric_snapshots",
            }
            <= tables
        )
        self.assertEqual("wal", journal_mode)


class PostWorkflowTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await run_migrations(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            channel_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Test')"
            ).lastrowid
            self.publish_at = datetime.now(timezone.utc) + timedelta(hours=2)
            self.post_id = db.execute(
                """
                INSERT INTO posts(channel_id, body, status, publish_at)
                VALUES (?, 'Ready body', 'review', ?)
                """,
                (channel_id, self.publish_at.isoformat()),
            ).lastrowid
        self.workflow = PostWorkflow(self.db_path)
        self.actor = Actor("user", 42)

    async def test_approve_schedules_post_and_writes_event(self):
        post = await self.workflow.approve(self.post_id, self.actor, expected_version=1)

        self.assertEqual("scheduled", post["status"])
        self.assertEqual(2, post["version"])
        self.assertEqual(42, post["approved_by"])
        with sqlite3.connect(self.db_path) as db:
            event = db.execute(
                "SELECT event_type, from_status, to_status, actor_id FROM post_events"
            ).fetchone()
        self.assertEqual(("approved", "review", "scheduled", "42"), event)

    async def test_approve_rejects_missing_body_or_past_date(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE posts SET body = '' WHERE id = ?", (self.post_id,))
        with self.assertRaises(PostWorkflowError):
            await self.workflow.approve(self.post_id, self.actor)

        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET body = 'Ready', publish_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), self.post_id),
            )
        with self.assertRaises(PostWorkflowError):
            await self.workflow.approve(self.post_id, self.actor)

    async def test_edit_scheduled_post_returns_it_to_review(self):
        await self.workflow.approve(self.post_id, self.actor)
        post = await self.workflow.edit(self.post_id, self.actor, body="Changed")

        self.assertEqual("review", post["status"])
        self.assertEqual("Changed", post["body"])

    async def test_editing_planned_post_moves_to_review_and_updates_link_rules(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET status = 'planned', body = '' WHERE id = ?",
                (self.post_id,),
            )
        post = await self.workflow.edit_content(
            self.post_id,
            self.actor,
            topic="Тема",
            body="Готовый текст",
            include_webinar_link=True,
            requires_link=True,
        )

        self.assertEqual("review", post["status"])
        self.assertEqual(1, post["include_webinar_link"])
        self.assertEqual(1, post["requires_link"])

    async def test_future_reschedule_keeps_scheduled_status(self):
        await self.workflow.approve(self.post_id, self.actor)
        new_time = self.publish_at + timedelta(days=1)
        post = await self.workflow.reschedule(self.post_id, new_time, self.actor)

        self.assertEqual("scheduled", post["status"])
        self.assertEqual(new_time.isoformat(), post["publish_at"])

    async def test_editing_topic_returns_scheduled_post_to_review(self):
        await self.workflow.approve(self.post_id, self.actor)
        post = await self.workflow.edit_content(
            self.post_id, self.actor, topic="Новая тема"
        )

        self.assertEqual("review", post["status"])
        self.assertEqual("Новая тема", post["topic"])

    async def test_editing_notified_post_returns_to_review_and_clears_notification(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                UPDATE posts SET status = 'notification_sent', notified_at = ?,
                    notification_chat_id = '42', notification_message_id = 7
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), self.post_id),
            )
        post = await self.workflow.edit_content(
            self.post_id, self.actor, body="Обновлённый текст"
        )

        self.assertEqual("review", post["status"])
        self.assertIsNone(post["notified_at"])
        self.assertIsNone(post["notification_message_id"])

    async def test_optimistic_lock_rejects_stale_version(self):
        with self.assertRaises(ConcurrentPostUpdateError):
            await self.workflow.edit(
                self.post_id, self.actor, body="Changed", expected_version=999
            )

    async def test_published_post_cannot_be_edited(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET status = 'published' WHERE id = ?", (self.post_id,)
            )
        with self.assertRaises(PostWorkflowError):
            await self.workflow.edit_content(
                self.post_id, self.actor, topic="Нельзя изменить"
            )

    async def test_cancel_is_idempotent(self):
        first = await self.workflow.cancel(self.post_id, self.actor)
        second = await self.workflow.cancel(self.post_id, self.actor)

        self.assertEqual("cancelled", first["status"])
        self.assertEqual(first["version"], second["version"])
        with sqlite3.connect(self.db_path) as db:
            event_count = db.execute("SELECT COUNT(*) FROM post_events").fetchone()[0]
        self.assertEqual(1, event_count)


if __name__ == "__main__":
    unittest.main()
