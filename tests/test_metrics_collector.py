import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from bot.migrations import run_migrations
from workers.metrics_collector import MetricsCollector, TelethonMetricsClient


class MetricsCollectorTests(unittest.IsolatedAsyncioTestCase):
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
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Test')"
            ).lastrowid
            self.post_id = db.execute(
                """
                INSERT INTO posts(
                    channel_id, body, status, published_at, telegram_message_id
                ) VALUES (?, 'Published', 'published', ?, '123')
                """,
                (channel_id, datetime.now(timezone.utc).isoformat()),
            ).lastrowid
            db.execute(
                """
                INSERT INTO posts(channel_id, body, status, telegram_message_id)
                VALUES (?, 'Draft', 'review', '124')
                """,
                (channel_id,),
            )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_collects_published_message_and_persists_provider_payload(self):
        calls = []

        async def client(channel_id, message_id):
            calls.append((channel_id, message_id))
            return {
                "views": 120,
                "forwards": 4,
                "reactions": 9,
                "reactions_detail": {"👍": 7, "❤": 2},
                "raw": {"source": "fixture"},
            }

        result = await MetricsCollector(
            self.db_path, client, provider="telethon"
        ).run_once(collection_window="24h")

        self.assertEqual(("-1001", 123), calls[0])
        self.assertEqual((1, 1, 0), (result.attempted, result.collected, result.failed))
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                """
                SELECT views, forwards, reactions, provider, collection_window
                FROM post_metric_snapshots
                """
            ).fetchone()
        self.assertEqual((120, 4, 9, "telethon", "24h"), row)

    async def test_collection_window_is_idempotent(self):
        calls = 0

        async def client(channel_id, message_id):
            nonlocal calls
            calls += 1
            return {"views": 1, "forwards": 0, "reactions": 0}

        collector = MetricsCollector(self.db_path, client, provider="fixture")
        first = await collector.run_once(collection_window="72h")
        second = await collector.run_once(collection_window="72h")

        self.assertEqual(1, first.collected)
        self.assertEqual(0, second.attempted)
        self.assertEqual(1, calls)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM post_metric_snapshots").fetchone()[0])

    async def test_same_timestamp_is_idempotent_without_window(self):
        async def client(channel_id, message_id):
            return {"views": 1, "forwards": 0, "reactions": 0}

        collector = MetricsCollector(self.db_path, client, provider="fixture")
        now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        self.assertEqual(1, (await collector.run_once(collected_at=now)).collected)
        self.assertEqual(0, (await collector.run_once(collected_at=now)).attempted)

    async def test_provider_failure_is_isolated_and_not_persisted(self):
        async def client(channel_id, message_id):
            raise RuntimeError("provider unavailable")

        result = await MetricsCollector(
            self.db_path, client, provider="fixture"
        ).run_once(collection_window="1h")

        self.assertEqual(1, result.failed)
        self.assertIn("provider unavailable", result.errors[0][1])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM post_metric_snapshots").fetchone()[0])

    async def test_invalid_metrics_are_rejected_per_post(self):
        async def client(channel_id, message_id):
            return {"views": -1, "forwards": 0, "reactions": 0}

        result = await MetricsCollector(
            self.db_path, client, provider="fixture"
        ).run_once(collection_window="1h")

        self.assertEqual(1, result.failed)
        self.assertIn("views must be a non-negative integer", result.errors[0][1])

    async def test_due_windows_respect_publish_age_and_are_idempotent(self):
        now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET published_at = ? WHERE id = ?",
                ((now - timedelta(hours=25)).isoformat(), self.post_id),
            )
        calls = []

        async def client(channel_id, message_id):
            calls.append((channel_id, message_id))
            return {"views": 10, "forwards": 1, "reactions": 2}

        collector = MetricsCollector(self.db_path, client, provider="fixture")
        first = await collector.run_due_windows(now=now)
        second = await collector.run_due_windows(now=now)

        self.assertEqual(1, first["1h"].collected)
        self.assertEqual(1, first["24h"].collected)
        self.assertEqual(0, first["72h"].attempted)
        self.assertEqual(0, second["1h"].attempted)
        self.assertEqual(2, len(calls))

    async def test_telethon_adapter_safely_aggregates_reactions(self):
        message = SimpleNamespace(
            views=50,
            forwards=None,
            reactions=SimpleNamespace(
                results=[
                    SimpleNamespace(
                        reaction=SimpleNamespace(emoticon="👍"), count=3
                    ),
                    SimpleNamespace(
                        reaction=SimpleNamespace(document_id=42), count=2
                    ),
                    SimpleNamespace(
                        reaction=SimpleNamespace(emoticon="bad"), count=-1
                    ),
                ]
            ),
        )

        class FakeTelethon:
            async def get_messages(self, entity, ids):
                self.call = (entity, ids)
                return message

        fake = FakeTelethon()
        payload = await TelethonMetricsClient(fake).fetch_post_metrics("-1001", 123)

        self.assertEqual((-1001, 123), fake.call)
        self.assertEqual(50, payload["views"])
        self.assertEqual(0, payload["forwards"])
        self.assertEqual(5, payload["reactions"])
        self.assertEqual({"👍": 3, "custom:42": 2}, payload["reactions_detail"])
