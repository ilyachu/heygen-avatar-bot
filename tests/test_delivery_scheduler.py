import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.migrations import run_migrations
from domain.campaigns import CampaignGenerationService
from domain.delivery import DeliveryWorkflow
from workers.scheduler import SchedulerService


class DeliverySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE,
                    title TEXT,
                    username TEXT,
                    added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        await run_migrations(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            channel_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Test channel')"
            ).lastrowid
            self.now = datetime.now(timezone.utc).replace(microsecond=0)
            self.post_id = db.execute(
                """
                INSERT INTO posts(channel_id, post_type, topic, body, status, publish_at)
                VALUES (?, 'useful', 'A topic', 'A useful post', 'review', ?)
                """,
                (channel_id, (self.now - timedelta(minutes=1)).isoformat()),
            ).lastrowid

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _post(self):
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM posts WHERE id = ?", (self.post_id,)).fetchone())

    def _bot(self):
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(
            chat=SimpleNamespace(id=42), message_id=7
        )
        return bot

    async def test_due_review_post_is_notified_once_across_two_runs(self):
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=7)
        scheduler = SchedulerService(self.db_path, bot, approval_user_id=99, web_base_url="https://web")

        self.assertEqual(1, await scheduler.run_once(self.now))
        self.assertEqual(0, await scheduler.run_once(self.now + timedelta(seconds=1)))

        bot.send_message.assert_awaited_once()
        post = self._post()
        self.assertEqual("notification_sent", post["status"])
        self.assertEqual("42", post["notification_chat_id"])
        self.assertEqual(7, post["notification_message_id"])

    async def test_due_scheduled_post_is_published_automatically_once(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET status = 'scheduled' WHERE id = ?",
                (self.post_id,),
            )
        bot = self._bot()
        scheduler = SchedulerService(
            self.db_path, bot, approval_user_id=99, web_base_url="https://web"
        )

        self.assertEqual(1, await scheduler.run_once(self.now))
        self.assertEqual(0, await scheduler.run_once(self.now + timedelta(seconds=1)))

        publish_calls = [
            call for call in bot.send_message.await_args_list
            if call.kwargs.get("chat_id") == "-1001"
        ]
        self.assertEqual(1, len(publish_calls))
        self.assertEqual("A useful post", publish_calls[0].kwargs["text"])
        self.assertEqual("published", self._post()["status"])
        with sqlite3.connect(self.db_path) as db:
            events = [
                row[0]
                for row in db.execute(
                    "SELECT event_type FROM post_events WHERE post_id = ? ORDER BY id",
                    (self.post_id,),
                )
            ]
        self.assertEqual(["publish_claimed", "published"], events)

    async def test_inactive_channel_is_not_auto_published(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE posts SET status = 'scheduled' WHERE id = ?", (self.post_id,))
            db.execute("UPDATE channels SET is_active = 0")
        bot = self._bot()
        scheduler = SchedulerService(self.db_path, bot, 99, "https://web")

        self.assertEqual(0, await scheduler.run_once(self.now))
        self.assertEqual("scheduled", self._post()["status"])
        bot.send_message.assert_not_awaited()

    async def test_due_scheduled_photo_is_published_with_caption(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET status = 'scheduled', media_type = 'photo', "
                "media_file_id = 'photo-id' WHERE id = ?",
                (self.post_id,),
            )
        bot = self._bot()
        bot.send_photo.return_value = SimpleNamespace(message_id=55)
        scheduler = SchedulerService(self.db_path, bot, 99, "https://web")

        self.assertEqual(1, await scheduler.run_once(self.now))
        bot.send_photo.assert_awaited_once_with(
            chat_id="-1001", photo="photo-id", caption="A useful post"
        )
        self.assertEqual("published", self._post()["status"])

    async def test_stale_publication_is_quarantined_without_retry(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET status = 'publishing', claim_until = ? WHERE id = ?",
                ((self.now - timedelta(minutes=1)).isoformat(), self.post_id),
            )
        bot = self._bot()
        scheduler = SchedulerService(self.db_path, bot, 99, "https://web")

        self.assertEqual(0, await scheduler.run_once(self.now))
        post = self._post()
        self.assertEqual("publish_failed", post["status"])
        self.assertEqual("ambiguous_delivery_after_worker_restart", post["publish_error"])
        bot.send_message.assert_not_awaited()

    async def test_concurrent_claim_publish_allows_only_one_attempt(self):
        scheduler = SchedulerService(self.db_path, self._bot(), 99, "https://web")
        await scheduler.run_once(self.now)
        workflow = DeliveryWorkflow(self.db_path)

        first, second = await asyncio.gather(
            workflow.claim_publish(self.post_id, actor_id=1),
            workflow.claim_publish(self.post_id, actor_id=2),
        )

        self.assertEqual(1, sum(result.changed for result in (first, second)))
        self.assertEqual({"publishing", "publishing"}, {first.status, second.status})
        self.assertIn("Публикую", next(result.message for result in (first, second) if result.changed))
        self.assertEqual("publishing", self._post()["status"])

    async def test_mark_published_transitions_claimed_post(self):
        scheduler = SchedulerService(self.db_path, self._bot(), 99, "https://web")
        await scheduler.run_once(self.now)
        workflow = DeliveryWorkflow(self.db_path)
        await workflow.claim_publish(self.post_id, actor_id=1)

        result = await workflow.mark_published(self.post_id, telegram_message_id=123)

        self.assertTrue(result.changed)
        self.assertEqual("published", result.status)
        post = self._post()
        self.assertEqual("published", post["status"])
        self.assertEqual("123", str(post["telegram_message_id"]))
        self.assertIsNotNone(post["published_at"])

    async def test_postpone_clears_notification_fields(self):
        scheduler = SchedulerService(self.db_path, self._bot(), 99, "https://web")
        await scheduler.run_once(self.now)
        workflow = DeliveryWorkflow(self.db_path)

        result = await workflow.postpone(self.post_id, actor_id=1, hours=1)

        self.assertTrue(result.changed)
        self.assertEqual("postponed", result.status)
        post = self._post()
        self.assertEqual("postponed", post["status"])
        self.assertIsNone(post["notified_at"])
        self.assertIsNone(post["notification_chat_id"])
        self.assertIsNone(post["notification_message_id"])

    async def test_cancel_is_idempotent(self):
        scheduler = SchedulerService(self.db_path, self._bot(), 99, "https://web")
        await scheduler.run_once(self.now)
        workflow = DeliveryWorkflow(self.db_path)

        first = await workflow.cancel(self.post_id, actor_id=1)
        second = await workflow.cancel(self.post_id, actor_id=1)

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual("cancelled", first.status)
        self.assertEqual("cancelled", second.status)
        with sqlite3.connect(self.db_path) as db:
            event_count = db.execute(
                "SELECT COUNT(*) FROM post_events WHERE post_id = ? AND event_type = 'cancelled'",
                (self.post_id,),
            ).fetchone()[0]
        self.assertEqual(1, event_count)

    async def test_campaign_generation_to_publication_is_idempotent(self):
        with sqlite3.connect(self.db_path) as db:
            webinar_id = db.execute(
                """
                INSERT INTO webinars(title, starts_at, audience, problem, promise, offer, cta)
                VALUES ('Test campaign', ?, 'Readers', 'Problem', 'Promise', 'Offer', 'Join')
                """,
                ((self.now + timedelta(days=3)).isoformat(),),
            ).lastrowid
            channel_id = db.execute("SELECT id FROM channels LIMIT 1").fetchone()[0]
            db.execute(
                "INSERT INTO webinar_channels(webinar_id, channel_id) VALUES (?, ?)",
                (webinar_id, channel_id),
            )

        class FakeCampaignGenerator:
            async def generate(self, campaign, channel):
                return [
                    {"post_type": "useful", "topic": "Useful", "body": "Useful body"},
                    {"post_type": "warming", "topic": "Warming", "body": "Warming body"},
                    {"post_type": "selling", "topic": "Selling", "body": "Selling body"},
                ]

        generation = await CampaignGenerationService(
            self.db_path, FakeCampaignGenerator()
        ).generate(webinar_id)
        self.assertEqual({"succeeded": 3, "failed": 0}, generation)

        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            posts = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM posts WHERE webinar_id = ? ORDER BY post_type", (webinar_id,)
                )
            ]
            due = next(post for post in posts if post["post_type"] == "useful")
            db.execute(
                "UPDATE posts SET publish_at = ? WHERE id = ?",
                ((self.now - timedelta(minutes=1)).isoformat(), due["id"]),
            )
            db.execute(
                "UPDATE posts SET publish_at = ? WHERE id = ?",
                ((self.now + timedelta(days=2)).isoformat(), self.post_id),
            )
            self.assertEqual(3, len(posts))

        bot = self._bot()
        scheduler = SchedulerService(self.db_path, bot, 99, "https://web")
        self.assertEqual(2, await scheduler.run_once(self.now))
        self.assertEqual(2, bot.send_message.await_count)

        workflow = DeliveryWorkflow(self.db_path)
        claimed = await workflow.claim_publish(due["id"], actor_id=1)
        self.assertTrue(claimed.changed)
        fake_publish = AsyncMock(return_value=SimpleNamespace(message_id=123))
        telegram_message = await fake_publish(chat_id="-1001", text=due["body"])
        published = await workflow.mark_published(due["id"], telegram_message.message_id)
        repeated_claim = await workflow.claim_publish(due["id"], actor_id=1)

        self.assertTrue(fake_publish.await_count == 1)
        self.assertTrue(published.changed)
        self.assertEqual("published", published.status)
        self.assertFalse(repeated_claim.changed)
        self.assertEqual("published", self._post_by_id(due["id"])["status"])
        with sqlite3.connect(self.db_path) as db:
            event_types = [
                row[0]
                for row in db.execute(
                    "SELECT event_type FROM post_events WHERE post_id = ? ORDER BY id",
                    (due["id"],),
                )
            ]
        self.assertEqual(
            [
                "generated",
                "notification_claimed",
                "notification_sent",
                "publish_claimed",
                "published",
            ],
            event_types,
        )

    async def test_notification_retry_stops_after_three_failures(self):
        bot = AsyncMock()
        bot.send_message.side_effect = RuntimeError("Telegram unavailable")
        scheduler = SchedulerService(self.db_path, bot, 99, "https://web")

        await scheduler.run_once(self.now)
        await scheduler.run_once(self.now + timedelta(minutes=6))
        await scheduler.run_once(self.now + timedelta(minutes=12))

        self.assertEqual("notification_failed", self._post()["status"])
        self.assertEqual(3, bot.send_message.await_count)

        await scheduler.run_once(self.now + timedelta(minutes=18))

        self.assertEqual(3, bot.send_message.await_count)
        self.assertEqual("notification_failed", self._post()["status"])

    def _post_by_id(self, post_id):
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())


if __name__ == "__main__":
    unittest.main()
