import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.migrations import run_migrations
from domain.content_planning import (
    ContentPlanningError,
    ContentPlanningService,
    _generation_prompt,
    _revision_prompt,
)
from domain.content_quality import evaluate_post


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
    def __init__(self, fail_channel: str | None = None):
        self.fail_channel = fail_channel
        self.calls: list[tuple[str, dict]] = []

    async def generate(self, prompt, context):
        self.calls.append((prompt, context))
        channel = context["channel"]["title"]
        if channel == self.fail_channel:
            raise RuntimeError("provider down")
        if context["mode"] == "monthly_topics":
            return {
                "topics": [
                    {"post_id": slot["post_id"], "topic": f"Тема {slot['week_start']}"}
                    for slot in context["slots"]
                ]
            }
        post = context["post"]
        prefix = "Исправлено" if context["mode"] == "revision" else "Создано"
        return {
            "topic": post["topic"] or f"Тема {post['post_type']}",
            "body": f"{prefix} для {channel}: {post['post_type']}",
            "rationale": "Адаптировано под канал",
            "warnings": [],
        }


class ContentPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "planning.db")
        with sqlite3.connect(self.db_path) as db:
            db.executescript(BASE_SCHEMA)
            self.first_channel = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Первый')"
            ).lastrowid
            self.second_channel = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1002', 'Второй')"
            ).lastrowid
            self.inactive_channel = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1003', 'Неактивный')"
            ).lastrowid
        await run_migrations(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE channels SET is_active = 0 WHERE id = ?",
                (self.inactive_channel,),
            )
            db.executemany(
                """
                INSERT INTO channel_profiles (
                    channel_id, description, audience, tone_of_voice
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (self.first_channel, "Описание 1", "Аудитория 1", "Тон 1"),
                    (self.second_channel, "Описание 2", "Аудитория 2", "Тон 2"),
                ],
            )
        future = datetime.now(timezone.utc) + timedelta(days=90)
        self.month = future.strftime("%Y-%m")
        self.service = ContentPlanningService(self.db_path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _ensure_and_get_week(self, generator=None):
        service = ContentPlanningService(self.db_path, generator)
        result = await service.ensure_month(self.month, actor_id=42)
        return service, result["weeks"][0]

    async def _add_webinar(self, service, week, *, registration_url=""):
        starts_at = datetime.combine(
            date.fromisoformat(week) + timedelta(days=6),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) + timedelta(hours=18)
        fields = {
            "title": "Недельный вебинар",
            "starts_at": starts_at.isoformat(),
            "audience": "Общая аудитория",
            "problem": "Проблема недели",
            "promise": "Практический результат",
        }
        if registration_url:
            fields["registration_url"] = registration_url
        return await service.upsert_weekly_webinar(
            week,
            fields,
            [self.first_channel, self.second_channel],
            actor_id=42,
        )

    async def test_ensure_month_is_idempotent_and_uses_active_templates(self):
        self.assertEqual([], await run_migrations(self.db_path))
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO weekly_content_templates (
                    post_type, weekday_offset, publish_time, sort_order
                ) VALUES ('reminder', 6, '12:00:00', 40)
                """
            )
        first = await self.service.ensure_month(self.month, actor_id=7)
        second = await self.service.ensure_month(self.month, actor_id=7)
        plan = await self.service.list_plan(self.month, post_type="useful")

        expected_total = len(first["weeks"]) * 2 * 4
        self.assertEqual(expected_total, first["created"])
        self.assertEqual(0, second["created"])
        self.assertEqual(len(first["weeks"]) * 2, len(plan))
        self.assertEqual({self.first_channel, self.second_channel},
                         {row["channel_id"] for row in plan})
        self.assertEqual({"planned"}, {row["status"] for row in plan})
        with sqlite3.connect(self.db_path) as db:
            slots = db.execute(
                "SELECT COUNT(*) FROM monthly_useful_topic_slots WHERE plan_month = ?",
                (self.month,),
            ).fetchone()[0]
            planned_events = db.execute(
                "SELECT COUNT(*) FROM post_events WHERE event_type = 'planned'"
            ).fetchone()[0]
            reminder_count = db.execute(
                "SELECT COUNT(*) FROM posts WHERE plan_month = ? AND post_type = 'reminder'",
                (self.month,),
            ).fetchone()[0]
        self.assertEqual(len(plan), slots)
        self.assertEqual(len(first["weeks"]) * 2, reminder_count)
        self.assertEqual(expected_total, planned_events)
        first_publish_at = datetime.fromisoformat(plan[0]["publish_at"])
        self.assertEqual(10, first_publish_at.astimezone(ZoneInfo("Europe/Moscow")).hour)

    async def test_month_topics_are_planned_without_generating_future_bodies(self):
        generator = FakeGenerator()
        service, _week = await self._ensure_and_get_week(generator)

        result = await service.plan_month_topics(self.month, actor_id=42)
        useful = await service.list_plan(self.month, post_type="useful")

        self.assertEqual(len(useful), result["succeeded"])
        self.assertEqual(0, result["failed"])
        self.assertTrue(all(post["planned_topic"] for post in useful))
        self.assertTrue(all(post["body"] == "" for post in useful))
        self.assertEqual({"planned"}, {post["status"] for post in useful})

    async def test_week_generation_requires_webinar_and_schedule_updates_are_future(self):
        generator = FakeGenerator()
        service, week = await self._ensure_and_get_week(generator)
        with self.assertRaisesRegex(ContentPlanningError, "Add the webinar"):
            await service.generate_week(
                week, actor_id=42, post_types=["warming", "selling"]
            )

        progress = []
        useful_generation = await service.generate_week(
            week,
            actor_id=42,
            post_types=["useful"],
            progress_callback=lambda completed, total, failed: progress.append(
                (completed, total, failed)
            ),
        )
        self.assertEqual(2, useful_generation["succeeded"])
        self.assertEqual((2, 2, 0), progress[-1])

        # Mixed generate without webinar skips unlinked warming/selling and does not fail.
        mixed = await service.generate_week(week, actor_id=42)
        self.assertEqual(0, mixed["succeeded"])
        self.assertGreaterEqual(mixed["skipped"], 4)

        useful = (await service.list_plan(self.month, week_start=week, post_type="useful"))[0]
        future = datetime.now(timezone.utc) + timedelta(days=120)
        updated = await service.update_publish_times(
            {useful["id"]: future}, actor_id=42
        )
        self.assertEqual(1, updated["updated"])
        refreshed = await service.list_plan(self.month, week_start=week, post_type="useful")
        self.assertEqual(future.isoformat(), refreshed[0]["publish_at"])

    async def test_partial_webinar_channels_generate_only_linked_warming_selling(self):
        generator = FakeGenerator()
        service, week = await self._ensure_and_get_week(generator)
        starts_at = datetime.combine(
            date.fromisoformat(week) + timedelta(days=6),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) + timedelta(hours=18)
        await service.upsert_weekly_webinar(
            week,
            {
                "title": "Общий вебинар",
                "starts_at": starts_at.isoformat(),
                "problem": "Общая проблема",
                "promise": "Общий результат",
                "cta": "Запишитесь",
            },
            [self.first_channel],
            actor_id=42,
        )

        result = await service.generate_week(
            week, actor_id=42, post_types=["warming", "selling"]
        )
        self.assertEqual(2, result["succeeded"])
        self.assertGreaterEqual(result["skipped"], 2)

        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT c.title, p.post_type, p.status, p.webinar_id
                FROM posts p JOIN channels c ON c.id = p.channel_id
                WHERE p.week_start = ? AND p.post_type IN ('warming', 'selling')
                ORDER BY c.title, p.post_type
                """,
                (week,),
            ).fetchall()
        first = [row for row in rows if row[0] == "Первый"]
        second = [row for row in rows if row[0] == "Второй"]
        self.assertEqual({"review"}, {row[2] for row in first})
        self.assertTrue(all(row[3] is not None for row in first))
        self.assertEqual({"planned"}, {row[2] for row in second})
        self.assertTrue(all(row[3] is None for row in second))
        prompts = [call[0] for call in generator.calls]
        self.assertTrue(any("мост к общей теме вебинара" in prompt for prompt in prompts))

    async def test_one_time_is_applied_to_all_active_channels_for_type(self):
        service, week = await self._ensure_and_get_week()
        target = datetime.now(timezone.utc) + timedelta(days=120)

        result = await service.update_scope_publish_time(
            self.month, week, "useful", target, actor_id=42
        )

        self.assertEqual(2, result["updated"])
        plan = await service.list_plan(self.month, week_start=week)
        useful = [post for post in plan if post["post_type"] == "useful"]
        warming = [post for post in plan if post["post_type"] == "warming"]
        self.assertEqual({target.isoformat()}, {post["publish_at"] for post in useful})
        self.assertNotEqual({target.isoformat()}, {post["publish_at"] for post in warming})

    async def test_approval_can_be_revoked_before_publication(self):
        generator = FakeGenerator()
        service, week = await self._ensure_and_get_week(generator)
        await service.generate_week(week, actor_id=42, post_types=["useful"])
        useful = await service.list_plan(self.month, week_start=week, post_type="useful")
        original_times = {post["id"]: post["publish_at"] for post in useful}
        await service.approve_posts([post["id"] for post in useful], actor_id=42)

        result = await service.revoke_approvals(
            [post["id"] for post in useful], actor_id=77
        )

        self.assertEqual(2, result["revoked"])
        self.assertEqual({"review"}, {post["status"] for post in result["posts"]})
        self.assertTrue(all(post["approved_by"] is None for post in result["posts"]))
        self.assertEqual(
            original_times,
            {post["id"]: post["publish_at"] for post in result["posts"]},
        )
        with sqlite3.connect(self.db_path) as db:
            events = db.execute(
                "SELECT COUNT(*) FROM post_events WHERE event_type = 'approval_revoked'"
            ).fetchone()[0]
        self.assertEqual(2, events)

    async def test_blocking_or_stale_quality_review_prevents_approval(self):
        generator = FakeGenerator()
        service, week = await self._ensure_and_get_week(generator)
        await service.generate_week(week, actor_id=42, post_types=["useful"])
        useful = await service.list_plan(self.month, week_start=week, post_type="useful")
        post_id = useful[0]["id"]
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET body = 'Этот метод лечит и гарантирует 100% результат' WHERE id = ?",
                (post_id,),
            )
        await evaluate_post(self.db_path, post_id)
        with self.assertRaisesRegex(ContentPlanningError, "did not pass quality"):
            await service.approve_posts([post_id], actor_id=42)

        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET body = 'Исправленный безопасный текст.', version = version + 1 WHERE id = ?",
                (post_id,),
            )
        with self.assertRaisesRegex(ContentPlanningError, "fresh quality review"):
            await service.approve_posts([post_id], actor_id=42)

    async def test_weekly_webinar_is_upserted_once_and_generation_is_partial(self):
        generator = FakeGenerator(fail_channel="Второй")
        service, week = await self._ensure_and_get_week(generator)
        webinar = await self._add_webinar(service, week)
        updated = await service.upsert_weekly_webinar(
            week,
            {"offer": "Обновлённый оффер"},
            [self.first_channel, self.second_channel],
            actor_id=42,
        )
        result = await service.generate_week(week, actor_id=42)

        self.assertEqual(webinar["id"], updated["id"])
        self.assertEqual("", updated["registration_url"])
        self.assertEqual({"succeeded": 3, "failed": 3, "skipped": 0},
                         {key: result[key] for key in ("succeeded", "failed", "skipped")})
        self.assertEqual(6, len(generator.calls))
        self.assertTrue(all(call[1]["channel"]["tone_of_voice"] for call in generator.calls))
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT c.title, p.status, p.generation_error
                FROM posts p JOIN channels c ON c.id = p.channel_id
                WHERE p.week_start = ? ORDER BY c.title, p.post_type
                """,
                (week,),
            ).fetchall()
        self.assertEqual({"review"}, {row[1] for row in rows if row[0] == "Первый"})
        self.assertEqual({"planned"}, {row[1] for row in rows if row[0] == "Второй"})
        self.assertEqual({"RuntimeError"}, {row[2] for row in rows if row[0] == "Второй"})

    async def test_revision_preserves_channel_adaptation_linking_and_bulk_approval(self):
        generator = FakeGenerator()
        service, week = await self._ensure_and_get_week(generator)
        webinar = await self._add_webinar(service, week)
        generated = await service.generate_week(week, actor_id=42)
        self.assertEqual(6, generated["succeeded"])

        plan = await service.list_plan(self.month, week_start=week, post_type="warming")
        revised = await service.revise_posts(
            [row["id"] for row in plan], "Сделай короче", actor_id=77
        )
        self.assertEqual(2, revised["succeeded"])
        revision_calls = [context for _, context in generator.calls
                          if context["mode"] == "revision"]
        self.assertEqual({"Первый", "Второй"},
                         {context["channel"]["title"] for context in revision_calls})

        link = "https://example.test/register"
        linked = await service.set_webinar_link(webinar["id"], link, actor_id=77)
        self.assertEqual(4, linked["updated_posts"])
        week_plan = await service.list_plan(self.month, week_start=week)
        for post in week_plan:
            if post["include_webinar_link"]:
                self.assertIn(link, post["body"])
            else:
                self.assertNotIn(link, post["body"])

        notified_id = week_plan[0]["id"]
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET status = 'notification_sent', notified_at = CURRENT_TIMESTAMP, "
                "notification_chat_id = '42', notification_message_id = 7 WHERE id = ?",
                (notified_id,),
            )

        approved = await service.approve_posts(
            [post["id"] for post in week_plan], actor_id=88
        )
        repeated = await service.approve_posts(
            [post["id"] for post in week_plan], actor_id=88
        )
        self.assertEqual(6, approved["approved"])
        self.assertEqual(0, repeated["approved"])
        self.assertEqual(6, repeated["unchanged"])
        with sqlite3.connect(self.db_path) as db:
            notified = db.execute(
                "SELECT status, notified_at FROM posts WHERE id = ?", (notified_id,)
            ).fetchone()
        self.assertEqual(("scheduled", None), notified)

    async def test_approval_is_atomic_and_requires_future_time_and_link(self):
        generator = FakeGenerator()
        service, week = await self._ensure_and_get_week(generator)
        await self._add_webinar(service, week)
        await service.generate_week(week, actor_id=42)
        plan = await service.list_plan(self.month, week_start=week)
        selling = next(post for post in plan if post["post_type"] == "selling")
        useful = next(post for post in plan if post["post_type"] == "useful")

        with self.assertRaisesRegex(ContentPlanningError, "requires the webinar link"):
            await service.approve_posts([useful["id"], selling["id"]], actor_id=42)
        with sqlite3.connect(self.db_path) as db:
            statuses = db.execute(
                "SELECT status FROM posts WHERE id IN (?, ?) ORDER BY id",
                (useful["id"], selling["id"]),
            ).fetchall()
        self.assertEqual({"review"}, {row[0] for row in statuses})

        await service.set_webinar_link(
            selling["webinar_id"], "https://example.test/register", actor_id=42
        )
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET publish_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), useful["id"]),
            )
        with self.assertRaisesRegex(ContentPlanningError, "must be in the future"):
            await service.approve_posts([useful["id"], selling["id"]], actor_id=42)
        with sqlite3.connect(self.db_path) as db:
            statuses = db.execute(
                "SELECT status FROM posts WHERE id IN (?, ?)",
                (useful["id"], selling["id"]),
            ).fetchall()
        self.assertEqual({"review"}, {row[0] for row in statuses})


class UsefulPromptContractTests(unittest.TestCase):
    def _base_post(self, post_type="useful"):
        return {
            "post_type": post_type,
            "channel_title": "Тестовый канал",
            "channel_description": "описание",
            "channel_audience": "мужчины и женщины 45+",
            "tone_of_voice": "спокойный",
            "cta_rules": "мягкий шаг",
            "planned_topic": "Сон и давление",
            "topic": "Сон и давление",
            "body": "старый текст",
            "requires_link": 0,
            "include_webinar_link": 0,
            "webinar_title": "Секретный вебинар",
            "webinar_starts_at": "2026-09-20T18:00:00+03:00",
            "webinar_problem": "проблема",
            "webinar_promise": "результат",
            "webinar_offer": "оффер",
            "webinar_cta": "регистрируйтесь",
            "webinar_registration_url": "https://example.com/reg",
        }

    def test_useful_generation_prompt_excludes_webinar_fields(self):
        prompt = _generation_prompt(self._base_post("useful"))
        self.assertIn("типа useful", prompt)
        self.assertIn("самостоятельная польза без привязки к вебинару", prompt)
        self.assertIn("до 90 символов", prompt)
        for needle in (
            "Секретный вебинар",
            "2026-09-20",
            "https://example.com/reg",
            "регистрируйтесь",
            "Дата вебинара",
            "Ссылка регистрации",
        ):
            self.assertNotIn(needle, prompt)

    def test_warming_generation_prompt_includes_webinar_fields(self):
        prompt = _generation_prompt(self._base_post("warming"))
        self.assertIn("Секретный вебинар", prompt)
        self.assertIn("https://example.com/reg", prompt)
        self.assertIn("до 90 символов", prompt)
        self.assertIn("мост к общей теме вебинара", prompt)
        self.assertIn("проблема", prompt.lower())

    def test_useful_revision_prompt_keeps_anti_webinar_rules(self):
        prompt = _revision_prompt(self._base_post("useful"), "Сократи заголовок")
        self.assertIn("Сократи заголовок", prompt)
        self.assertIn("Запрещены ссылки", prompt)
        self.assertIn("до 90 символов", prompt)


if __name__ == "__main__":
    unittest.main()
