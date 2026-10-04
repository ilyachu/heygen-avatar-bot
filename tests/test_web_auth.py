import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bot.migrations import run_migrations
from domain.auth import AuthService, AuthenticationError
from domain.content_jobs import ContentJobQueue, JOB_QUALITY


SECRET = "test-secret-that-is-at-least-thirty-two-characters"


class WebAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "web.db")
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE whitelist (user_id INTEGER PRIMARY KEY);
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT UNIQUE,
                    title TEXT, username TEXT, added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO whitelist(user_id) VALUES (42);
                INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Test channel');
                """
            )
        await run_migrations(self.db_path)
        self.auth = AuthService(self.db_path, SECRET)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_login_token_is_one_time_and_session_is_signed(self):
        token = await self.auth.create_login_token(42)
        session = await self.auth.exchange_login_token(token)

        self.assertEqual(42, await self.auth.get_session_user(session))
        self.assertIsNone(await self.auth.get_session_user(session + "tampered"))
        with self.assertRaises(AuthenticationError):
            await self.auth.exchange_login_token(token)

    async def test_non_whitelisted_user_cannot_create_link(self):
        with self.assertRaises(AuthenticationError):
            await self.auth.create_login_token(999)

    async def test_revoke_invalidates_session(self):
        token = await self.auth.create_login_token(42)
        session = await self.auth.exchange_login_token(token)
        await self.auth.revoke_session(session)
        self.assertIsNone(await self.auth.get_session_user(session))

    async def test_csrf_is_bound_to_session(self):
        token = await self.auth.create_login_token(42)
        session = await self.auth.exchange_login_token(token)
        csrf = self.auth.csrf_token(session)
        self.assertTrue(self.auth.validate_csrf(session, csrf))
        self.assertFalse(self.auth.validate_csrf(session, "bad"))


class WebRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "web.db")
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE whitelist (user_id INTEGER PRIMARY KEY);
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT UNIQUE,
                    title TEXT, username TEXT, added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO whitelist(user_id) VALUES (42);
                INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Test channel');
                """
            )
        import asyncio
        asyncio.run(run_migrations(self.db_path))
        self.auth = AuthService(self.db_path, SECRET)
        self.settings_patch = patch.multiple(
            "web.app.settings",
            DB_PATH=self.db_path,
            WEB_COOKIE_SECURE=False,
            WEB_SESSION_DAYS=7,
        )
        self.settings_patch.start()
        from web.app import create_app
        self.client = TestClient(create_app(auth_service=self.auth, run_startup=False))

    def tearDown(self):
        self.client.close()
        self.settings_patch.stop()
        self.tmp.cleanup()

    def login(self):
        import asyncio

        token = asyncio.run(self.auth.create_login_token(42))
        self.client.get(f"/auth/{token}")
        session = self.client.cookies.get("content_session")
        return self.auth.csrf_token(session)

    def test_protected_pages_redirect_without_session(self):
        response = self.client.get("/channels", follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual("/login", response.headers["location"])

    def test_editor_shows_durable_job_progress(self):
        self.login()
        import asyncio

        queue = ContentJobQueue(self.db_path)
        job = asyncio.run(
            queue.enqueue(
                JOB_QUALITY,
                {"post_ids": [1]},
                idempotency_key="quality:test-progress",
                total_items=10,
            )
        )
        asyncio.run(queue.claim("web-test"))
        asyncio.run(
            queue.set_progress(
                job["id"],
                "web-test",
                total_items=10,
                completed_items=4,
                failed_items=0,
            )
        )

        page = self.client.get("/editor")

        self.assertEqual(200, page.status_code)
        self.assertIn("Очередь AI", page.text)
        self.assertIn('value="40"', page.text)

    def test_public_stylesheets_use_relative_urls(self):
        login = self.client.get("/login")
        self.assertIn('href="/static/app.css"', login.text)
        self.assertNotIn('href="http://', login.text)

        self.login()
        dashboard = self.client.get("/")
        self.assertIn('href="/static/app.css"', dashboard.text)
        self.assertIn('href="/static/editor.css"', dashboard.text)
        self.assertNotIn('href="http://', dashboard.text)

    def test_magic_link_opens_channels_and_cannot_be_reused(self):
        import asyncio
        token = asyncio.run(self.auth.create_login_token(42))
        response = self.client.get(f"/auth/{token}", follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertEqual("/editor", response.headers["location"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])

        channels = self.client.get("/channels")
        self.assertEqual(200, channels.status_code)
        self.assertIn("Test channel", channels.text)

        reused = self.client.get(f"/auth/{token}")
        self.assertEqual(401, reused.status_code)

    def test_content_plan_creates_month_slots_and_shows_all_channels(self):
        csrf = self.login()
        response = self.client.post(
            "/editor/month-plan",
            data={
                "csrf_token": csrf,
                "month": "2099-09",
                "week": "2099-09-07",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        page = self.client.get(
            "/editor?month=2099-09&week=2099-09-07&view=week&post_type=all"
        )
        self.assertEqual(200, page.status_code)
        self.assertIn("Все", page.text)
        self.assertIn("Полезные", page.text)
        self.assertIn("Прогревающие", page.text)
        self.assertIn("Продающие", page.text)
        self.assertIn("Test channel", page.text)
        self.assertNotIn('<details class="webinar-panel" open>', page.text)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                3,
                db.execute(
                    "SELECT COUNT(*) FROM posts WHERE week_start = '2099-09-07'"
                ).fetchone()[0],
            )

    def test_weekly_webinar_and_web_approval_flow(self):
        csrf = self.login()
        self.client.post(
            "/editor/month-plan",
            data={"csrf_token": csrf, "month": "2099-09", "week": "2099-09-07"},
        )
        webinar = self.client.post(
            "/editor/webinar",
            data={
                "csrf_token": csrf,
                "week": "2099-09-07",
                "title": "Вебинар недели",
                "starts_at": "2099-09-13T18:00",
                "audience": "Люди 50+",
                "problem": "Проблема",
                "promise": "Результат",
                "agenda": "Программа",
                "offer": "Оффер",
                "cta": "Записаться",
                "registration_url": "",
                "channel_ids": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, webinar.status_code)

        with sqlite3.connect(self.db_path) as db:
            post_id = db.execute(
                "SELECT id FROM posts WHERE week_start = '2099-09-07' AND post_type = 'useful'"
            ).fetchone()[0]
            db.execute(
                "UPDATE posts SET topic = 'Полезная тема', body = 'Готовый текст', status = 'review' WHERE id = ?",
                (post_id,),
            )
        approved = self.client.post(
            "/editor/posts/bulk-approve",
            data={
                "csrf_token": csrf,
                "month": "2099-09",
                "week": "2099-09-07",
                "post_ids": str(post_id),
            },
            follow_redirects=False,
        )
        self.assertEqual(303, approved.status_code)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                "scheduled",
                db.execute("SELECT status FROM posts WHERE id = ?", (post_id,)).fetchone()[0],
            )

    def test_bulk_schedule_updates_selected_post_time(self):
        csrf = self.login()
        self.client.post(
            "/editor/month-plan",
            data={"csrf_token": csrf, "month": "2099-09", "week": "2099-09-07"},
        )
        with sqlite3.connect(self.db_path) as db:
            post_id = db.execute(
                "SELECT id FROM posts WHERE week_start = '2099-09-07' ORDER BY id LIMIT 1"
            ).fetchone()[0]
        response = self.client.post(
            "/editor/posts/bulk-schedule",
            data={
                "csrf_token": csrf,
                "month": "2099-09",
                "week": "2099-09-07",
                "post_ids": str(post_id),
                f"publish_at_{post_id}": "2099-09-09T14:30",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        with sqlite3.connect(self.db_path) as db:
            stored = db.execute(
                "SELECT publish_at FROM posts WHERE id = ?", (post_id,)
            ).fetchone()[0]
        self.assertEqual("2099-09-09T11:30:00+00:00", stored)

    def test_one_date_can_be_set_for_all_useful_posts_in_week(self):
        csrf = self.login()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1002', 'Second channel')"
            )
        self.client.post(
            "/editor/month-plan",
            data={"csrf_token": csrf, "month": "2099-09", "week": "2099-09-07"},
        )

        response = self.client.post(
            "/editor/posts/type-schedule",
            data={
                "csrf_token": csrf,
                "month": "2099-09",
                "week": "2099-09-07",
                "view": "week",
                "post_type": "all",
                "scope_post_type": "useful",
                "bulk_publish_at": "2099-09-10T15:00",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertIn("post_type=useful", response.headers["location"])
        with sqlite3.connect(self.db_path) as db:
            useful = db.execute(
                "SELECT publish_at FROM posts WHERE week_start = ? AND post_type = ?",
                ("2099-09-07", "useful"),
            ).fetchall()
            warming = db.execute(
                "SELECT publish_at FROM posts WHERE week_start = ? AND post_type = ?",
                ("2099-09-07", "warming"),
            ).fetchall()
        self.assertEqual({("2099-09-10T12:00:00+00:00",)}, set(useful))
        self.assertNotEqual(set(useful), set(warming))

    def test_bulk_approval_can_be_revoked(self):
        csrf = self.login()
        self.client.post(
            "/editor/month-plan",
            data={"csrf_token": csrf, "month": "2099-09", "week": "2099-09-07"},
        )
        with sqlite3.connect(self.db_path) as db:
            post_id = db.execute(
                "SELECT id FROM posts WHERE week_start = ? AND post_type = 'useful'",
                ("2099-09-07",),
            ).fetchone()[0]
            db.execute(
                "UPDATE posts SET body = 'Готовый текст', status = 'review' WHERE id = ?",
                (post_id,),
            )
        approved = self.client.post(
            "/editor/posts/bulk-approve",
            data={
                "csrf_token": csrf,
                "month": "2099-09",
                "week": "2099-09-07",
                "post_ids": str(post_id),
            },
            follow_redirects=False,
        )
        self.assertEqual(303, approved.status_code)

        revoked = self.client.post(
            "/editor/posts/bulk-revoke",
            data={
                "csrf_token": csrf,
                "month": "2099-09",
                "week": "2099-09-07",
                "view": "week",
                "post_type": "useful",
                "post_ids": str(post_id),
            },
            follow_redirects=False,
        )

        self.assertEqual(303, revoked.status_code)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, approved_by, publish_at FROM posts WHERE id = ?",
                (post_id,),
            ).fetchone()
        self.assertEqual("review", row[0])
        self.assertIsNone(row[1])
        self.assertIsNotNone(row[2])

    def test_saved_webinar_exposes_and_enqueues_warming_selling_generation(self):
        csrf = self.login()
        self.client.post(
            "/editor/month-plan",
            data={"csrf_token": csrf, "month": "2099-09", "week": "2099-09-07"},
        )
        saved = self.client.post(
            "/editor/webinar",
            data={
                "csrf_token": csrf,
                "week": "2099-09-07",
                "title": "Вебинар недели",
                "starts_at": "2099-09-13T18:00",
                "channel_ids": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, saved.status_code)
        page = self.client.get(
            "/editor?month=2099-09&week=2099-09-07&view=week&post_type=all"
        )
        self.assertIn("Сгенерировать прогрев и продажи", page.text)

        import web.app as web_app_module

        with patch.object(web_app_module.settings, "CONTENT_LLM_API_KEY", "test"), patch.object(
            web_app_module.settings, "CONTENT_LLM_MODEL", "test-model"
        ):
            generated = self.client.post(
                "/editor/week/generate",
                data={
                    "csrf_token": csrf,
                    "month": "2099-09",
                    "week": "2099-09-07",
                    "generation_scope": "webinar",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, generated.status_code)
        self.assertIn("notice=webinar_posts_generating", generated.headers["location"])
        with sqlite3.connect(self.db_path) as db:
            statuses = dict(
                db.execute(
                    "SELECT post_type, status FROM posts WHERE week_start = ?",
                    ("2099-09-07",),
                ).fetchall()
            )
            job = db.execute(
                "SELECT job_type, payload_json, status FROM content_jobs"
            ).fetchone()
        self.assertEqual("planned", statuses["useful"])
        self.assertEqual("planned", statuses["warming"])
        self.assertEqual("planned", statuses["selling"])
        self.assertEqual("webinar_generation", job[0])
        self.assertEqual(["warming", "selling"], json.loads(job[1])["post_types"])
        self.assertEqual("queued", job[2])

    def test_logout_rejects_missing_csrf(self):
        import asyncio
        token = asyncio.run(self.auth.create_login_token(42))
        self.client.get(f"/auth/{token}")
        response = self.client.post("/logout", data={})
        self.assertEqual(422, response.status_code)

    def test_channel_profile_can_be_edited(self):
        csrf = self.login()
        response = self.client.post(
            "/channels/1",
            data={
                "csrf_token": csrf,
                "description": "Канал о здоровье печени",
                "audience": "Люди 40+",
                "purpose": "Тематический канал",
                "key_meanings": "Системный подход\nПрофилактика",
                "rubrics": "Питание\nПрактики",
                "tone_of_voice": "Спокойно и понятно",
                "cta_rules": "Один призыв",
                "forbidden_topics": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        page = self.client.get("/channels/1")
        self.assertIn("Канал о здоровье печени", page.text)
        self.assertIn("Системный подход", page.text)

    def test_month_editor_creates_and_displays_post(self):
        csrf = self.login()
        response = self.client.post(
            "/editor/posts",
            data={
                "csrf_token": csrf,
                "channel_id": 1,
                "post_type": "useful",
                "topic": "Тестовая тема",
                "body": "Текст поста",
                "publish_at": "2026-09-21T12:00",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertIn("month=2026-09", response.headers["location"])
        self.assertIn("week=2026-09-21", response.headers["location"])
        page = self.client.get(response.headers["location"])
        self.assertIn("Тестовая тема", page.text)
        self.assertIn("Полезный", page.text)

    def test_editor_rejects_unknown_post_type(self):
        csrf = self.login()
        response = self.client.post(
            "/editor/posts",
            data={
                "csrf_token": csrf,
                "channel_id": 1,
                "post_type": "unknown",
                "topic": "Тема",
            },
        )
        self.assertEqual(400, response.status_code)

    def test_campaign_can_be_created_for_selected_channel(self):
        csrf = self.login()
        response = self.client.post(
            "/campaigns",
            data={
                "csrf_token": csrf,
                "title": "Ревитология печени",
                "starts_at": "2099-09-27T18:00",
                "audience": "Люди 40+",
                "problem": "Скрытые сигналы",
                "promise": "Понять системный подход",
                "agenda": "Печень и обменные процессы",
                "offer": "Программа с куратором",
                "cta": "Узнать подробнее",
                "registration_url": "https://example.com/register",
                "channel_ids": "1",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        page = self.client.get(response.headers["location"])
        self.assertIn("Ревитология печени", page.text)
        self.assertIn("CONTENT_LLM_API_KEY", page.text)


if __name__ == "__main__":
    unittest.main()
