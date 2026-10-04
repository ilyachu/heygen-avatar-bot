import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot.migrations import run_migrations
from domain.content_jobs import JOB_WEBINAR, JOB_WEEKLY_USEFUL, ContentJobQueue
from domain.content_planning import ContentPlanningService
from domain.content_quality import CRITERIA
from workers.content_automation import ContentAutomation
from workers.content_jobs import ContentJobWorker


class FakePlanningGenerator:
    async def generate(self, _prompt, context):
        if context.get("schema_version") == "quality-review-v1":
            return {
                "total_score": 82,
                "criteria": {criterion: 82 for criterion in CRITERIA},
                "is_blocking": False,
                "issues": [],
            }
        post = context["post"]
        return {
            "topic": post["topic"] or f"Новая тема {post['post_type']}",
            "body": f"Новый проверенный текст {post['post_type']}. Полезное действие.",
            "rationale": "Под канал",
            "warnings": [],
        }


class ContentAutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "automation.db")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE, title TEXT, username TEXT,
                    added_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self.channel_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Канал')"
            ).lastrowid
        await run_migrations(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO channel_profiles(channel_id, tone_of_voice) VALUES (?, 'Спокойно')",
                (self.channel_id,),
            )
        self.queue = ContentJobQueue(self.db_path)
        self.automation = ContentAutomation(
            self.db_path,
            FakePlanningGenerator,
            lambda: None,
            quality_model="fake-quality",
        )
        self.worker = ContentJobWorker(
            self.queue, "test-worker", self.automation.handlers()
        )
        self.week = (
            datetime.now(timezone.utc).date() + timedelta(days=90)
        )
        self.week = self.week - timedelta(days=self.week.weekday())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_weekly_job_generates_only_useful_and_reviews_quality(self):
        week = self.week.isoformat()
        job = await self.queue.enqueue(
            JOB_WEEKLY_USEFUL,
            {"week_start": week, "post_types": ["useful"]},
            idempotency_key=f"weekly-useful:{week}",
        )

        self.assertTrue(await self.worker.run_once())

        result = await self.queue.get(job["id"])
        with sqlite3.connect(self.db_path) as db:
            statuses = dict(
                db.execute(
                    "SELECT post_type, status FROM posts WHERE week_start = ?",
                    (week,),
                ).fetchall()
            )
            quality_count = db.execute(
                "SELECT COUNT(*) FROM quality_reviews"
            ).fetchone()[0]
        self.assertEqual("completed", result["status"])
        self.assertEqual("review", statuses["useful"])
        self.assertEqual("planned", statuses["warming"])
        self.assertEqual("planned", statuses["selling"])
        self.assertEqual(1, quality_count)

    async def test_webinar_job_generates_only_warming_and_selling(self):
        week = self.week.isoformat()
        service = ContentPlanningService(self.db_path)
        await service.ensure_month(week[:7], actor_id=42)
        starts_at = datetime.combine(
            date.fromisoformat(week) + timedelta(days=6),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) + timedelta(hours=18)
        webinar = await service.upsert_weekly_webinar(
            week,
            {"title": "Вебинар", "starts_at": starts_at.isoformat()},
            [self.channel_id],
            actor_id=42,
        )
        job = await self.queue.enqueue(
            JOB_WEBINAR,
            {
                "webinar_id": webinar["id"],
                "post_types": ["warming", "selling"],
                "actor_id": 42,
            },
            idempotency_key=f"webinar:{webinar['id']}:test",
        )

        self.assertTrue(await self.worker.run_once())

        result = await self.queue.get(job["id"])
        with sqlite3.connect(self.db_path) as db:
            statuses = dict(
                db.execute(
                    "SELECT post_type, status FROM posts WHERE week_start = ?",
                    (week,),
                ).fetchall()
            )
            quality_count = db.execute(
                "SELECT COUNT(*) FROM quality_reviews"
            ).fetchone()[0]
        self.assertEqual("completed", result["status"])
        self.assertEqual("planned", statuses["useful"])
        self.assertEqual("review", statuses["warming"])
        self.assertEqual("review", statuses["selling"])
        self.assertEqual(2, quality_count)
