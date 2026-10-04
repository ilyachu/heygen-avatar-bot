import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.migrations import run_migrations
from domain.campaigns import (
    CampaignGenerationService,
    _validate_generated_posts,
)


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


class FakeGenerator:
    def __init__(self, *, error: Exception | None = None):
        self.error = error

    async def generate(self, campaign, channel):
        if self.error:
            raise self.error
        return [
            {
                "post_type": "useful",
                "topic": "Полезная тема",
                "body": "Полезный пост.",
                "rationale": "Даёт практическую пользу.",
                "warnings": [],
            },
            {
                "post_type": "warming",
                "topic": "Прогревающая тема",
                "body": "Прогревающий пост.",
                "rationale": "Помогает увидеть проблему.",
                "warnings": [],
            },
            {
                "post_type": "selling",
                "topic": "Продающая тема",
                "body": "Продающий пост.",
                "rationale": "Подводит к предложению.",
                "warnings": [],
            },
        ]


class CampaignGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "campaigns.db")
        with sqlite3.connect(self.db_path) as db:
            db.executescript(BASE_SCHEMA)
            channel_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Канал здоровья')"
            ).lastrowid
        await run_migrations(self.db_path)

        starts_at = datetime.now(timezone.utc) + timedelta(days=7)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO channel_profiles (
                    channel_id, description, audience, tone_of_voice
                ) VALUES (?, 'Практическое описание', 'Взрослые читатели', 'Просто и бережно')
                """,
                (channel_id,),
            )
            self.webinar_id = db.execute(
                """
                INSERT INTO webinars (
                    title, starts_at, audience, problem, promise, agenda,
                    offer, cta, registration_url
                ) VALUES (?, ?, 'Аудитория', 'Проблема', 'Результат', 'Программа',
                          'Предложение', 'Записаться', 'https://example.test/webinar')
                """,
                ("Вебинар о здоровье", starts_at.isoformat()),
            ).lastrowid
            db.execute(
                "INSERT INTO webinar_channels(webinar_id, channel_id) VALUES (?, ?)",
                (self.webinar_id, channel_id),
            )
        self.channel_id = channel_id

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_generate_creates_three_review_posts_and_events(self):
        result = await CampaignGenerationService(
            self.db_path, FakeGenerator()
        ).generate(self.webinar_id)

        self.assertEqual({"succeeded": 3, "failed": 0}, result)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            posts = db.execute(
                """
                SELECT post_type, topic, body, status, publish_at
                FROM posts WHERE webinar_id = ? ORDER BY post_type
                """,
                (self.webinar_id,),
            ).fetchall()
            events = db.execute(
                """
                SELECT event_type, from_status, to_status
                FROM post_events ORDER BY post_id
                """
            ).fetchall()
            campaign_status = db.execute(
                "SELECT status FROM webinars WHERE id = ?", (self.webinar_id,)
            ).fetchone()[0]

        self.assertEqual(["selling", "useful", "warming"], [row["post_type"] for row in posts])
        self.assertEqual({"review"}, {row["status"] for row in posts})
        self.assertEqual({"Полезная тема", "Прогревающая тема", "Продающая тема"},
                         {row["topic"] for row in posts})
        self.assertEqual([("generated", "generating", "review")] * 3,
                         [tuple(row) for row in events])
        self.assertEqual("generated", campaign_status)

        starts_at = datetime.now(timezone.utc) + timedelta(days=7)
        for row in posts:
            self.assertLess(datetime.fromisoformat(row["publish_at"]), starts_at)

    async def test_generation_failure_keeps_three_rows_and_marks_failure(self):
        with sqlite3.connect(self.db_path) as db:
            old_id = db.execute(
                """
                INSERT INTO posts(webinar_id, channel_id, post_type, body, status, publish_at)
                VALUES (?, ?, 'useful', 'Старый текст', 'review', ?)
                """,
                (self.webinar_id, self.channel_id,
                 (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()),
            ).lastrowid

        result = await CampaignGenerationService(
            self.db_path, FakeGenerator(error=RuntimeError("provider down"))
        ).generate(self.webinar_id)

        self.assertEqual({"succeeded": 0, "failed": 3}, result)
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT id, post_type, body, status, generation_error FROM posts WHERE webinar_id = ?",
                (self.webinar_id,),
            ).fetchall()
            event_count = db.execute(
                "SELECT COUNT(*) FROM post_events WHERE event_type = 'generation_failed'"
            ).fetchone()[0]
            campaign_status = db.execute(
                "SELECT status FROM webinars WHERE id = ?", (self.webinar_id,)
            ).fetchone()[0]

        self.assertEqual(3, len(rows))
        self.assertIn(old_id, {row[0] for row in rows})
        self.assertEqual({"generation_failed"}, {row[3] for row in rows})
        self.assertEqual({"RuntimeError"}, {row[4] for row in rows})
        self.assertEqual(3, event_count)
        self.assertEqual("generation_failed", campaign_status)

    def test_validation_rejects_duplicate_and_missing_type(self):
        duplicate = [
            {"post_type": "useful", "topic": "a", "body": "a"},
            {"post_type": "useful", "topic": "b", "body": "b"},
            {"post_type": "selling", "topic": "c", "body": "c"},
        ]
        missing = [
            {"post_type": "useful", "topic": "a", "body": "a"},
            {"post_type": "warming", "topic": "b", "body": "b"},
        ]
        with self.assertRaises(ValueError):
            _validate_generated_posts(duplicate)
        with self.assertRaises(ValueError):
            _validate_generated_posts(missing)


if __name__ == "__main__":
    unittest.main()
