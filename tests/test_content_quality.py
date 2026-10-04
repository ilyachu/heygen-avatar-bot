import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.migrations import run_migrations
from domain.content_quality import (
    CRITERIA,
    ContentQualityError,
    check_approval_quality,
    evaluate_post,
    get_latest_review,
)


class ContentQualityTests(unittest.IsolatedAsyncioTestCase):
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
                INSERT INTO posts(channel_id, post_type, topic, body, status)
                VALUES (?, 'useful', 'Сон', ?, 'review')
                """,
                (
                    channel_id,
                    "Качественный сон поддерживает самочувствие. "
                    "Соблюдайте режим и обсудите тревожные симптомы с врачом.",
                ),
            ).lastrowid

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _valid_payload(self, score=84):
        return {
            "total_score": score,
            "criteria": {name: score for name in CRITERIA},
            "is_blocking": False,
            "issues": [],
        }

    async def test_evaluator_review_is_append_only_and_latest_is_returned(self):
        calls = []

        async def evaluator(prompt, context):
            calls.append((prompt, context))
            return self._valid_payload(82 + len(calls))

        first = await evaluate_post(
            self.db_path,
            self.post_id,
            evaluator=evaluator,
            prompt_version="prompt-1",
            model="model-a",
        )
        second = await evaluate_post(
            self.db_path,
            self.post_id,
            evaluator=evaluator,
            prompt_version="prompt-2",
            model="model-b",
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual("prompt-2", second.prompt_version)
        self.assertEqual("model-b", second.model)
        self.assertEqual(second, await get_latest_review(self.db_path, self.post_id))
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                2,
                db.execute(
                    "SELECT COUNT(*) FROM quality_reviews WHERE post_id = ?",
                    (self.post_id,),
                ).fetchone()[0],
            )

    async def test_deterministic_medical_and_guarantee_checks_always_block(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET body = ? WHERE id = ?",
                (
                    "Этот метод лечит диабет и гарантирует 100% результат.",
                    self.post_id,
                ),
            )

        async def permissive_evaluator(prompt, context):
            return self._valid_payload(99)

        review = await evaluate_post(
            self.db_path, self.post_id, evaluator=permissive_evaluator, model="test"
        )

        self.assertTrue(review.is_blocking)
        self.assertLess(review.total_score, 50)
        codes = {issue.code for issue in review.issues}
        self.assertTrue({"guaranteed_outcome", "medical_claim"} <= codes)
        check = await check_approval_quality(self.db_path, self.post_id)
        self.assertFalse(check.allowed)
        self.assertEqual("quality_review_blocking", check.reason)

    async def test_strict_evaluator_schema_rejects_bad_values_without_write(self):
        async def invalid_evaluator(prompt, context):
            payload = self._valid_payload()
            payload["criteria"]["style"] = "excellent"
            return payload

        with self.assertRaises(ContentQualityError):
            await evaluate_post(
                self.db_path, self.post_id, evaluator=invalid_evaluator, model="test"
            )
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM quality_reviews").fetchone()[0])

    async def test_approval_helper_requires_review_and_minimum_score(self):
        missing = await check_approval_quality(self.db_path, self.post_id)
        self.assertFalse(missing.allowed)
        self.assertEqual("quality_review_missing", missing.reason)

        async def evaluator(prompt, context):
            return self._valid_payload(59)

        await evaluate_post(self.db_path, self.post_id, evaluator=evaluator, model="test")
        low = await check_approval_quality(self.db_path, self.post_id, minimum_score=60)
        self.assertFalse(low.allowed)
        self.assertEqual("quality_score_too_low", low.reason)
        self.assertTrue(
            (await check_approval_quality(self.db_path, self.post_id, minimum_score=55)).allowed
        )

    async def test_approval_helper_rejects_review_for_old_post_version(self):
        await evaluate_post(self.db_path, self.post_id)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET body = 'Changed after review', version = version + 1 WHERE id = ?",
                (self.post_id,),
            )

        check = await check_approval_quality(self.db_path, self.post_id)

        self.assertFalse(check.allowed)
        self.assertEqual("quality_review_stale", check.reason)
        self.assertEqual(1, check.review.post_version)

    async def test_schedule_only_version_change_keeps_review_fresh(self):
        await evaluate_post(self.db_path, self.post_id)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET publish_at = '2099-01-01T10:00:00+00:00', "
                "version = version + 1 WHERE id = ?",
                (self.post_id,),
            )

        check = await check_approval_quality(self.db_path, self.post_id)

        self.assertTrue(check.allowed)

    async def test_persisted_json_is_valid(self):
        await evaluate_post(self.db_path, self.post_id)
        with sqlite3.connect(self.db_path) as db:
            criteria_json, issues_json = db.execute(
                "SELECT criteria_json, issues_json FROM quality_reviews"
            ).fetchone()
        self.assertEqual(set(CRITERIA), set(json.loads(criteria_json)))
        self.assertIsInstance(json.loads(issues_json), list)

    async def test_useful_post_blocks_link_and_webinar_cta(self):
        long_enough = (
            "Дорогие друзья, давайте разберём простую привычку. "
            "Утром выпейте стакан тёплой воды, а вечером сделайте лёгкую прогулку "
            "10–15 минут. Белковый ужин — это творог, рыба или яйца. "
            "Так организм успевает восстановиться без резких ограничений. "
            "Попробуйте уже сегодня и отметьте самочувствие. "
            "Если симптомы тревожат, обсудите их с врачом. "
        )
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET topic = ?, body = ? WHERE id = ?",
                (
                    "Короткий заголовок про воду",
                    long_enough + " Приходите на вебинар 15.02: https://example.com/webinar",
                    self.post_id,
                ),
            )

        review = await evaluate_post(self.db_path, self.post_id)
        codes = {issue.code for issue in review.issues}
        self.assertTrue(review.is_blocking)
        self.assertIn("useful_has_link", codes)
        self.assertIn("useful_webinar_cta", codes)
        check = await check_approval_quality(self.db_path, self.post_id)
        self.assertFalse(check.allowed)
        self.assertEqual("quality_review_blocking", check.reason)

    async def test_long_title_blocks_approval(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET topic = ?, body = ? WHERE id = ?",
                (
                    "Очень длинный и сложный заголовок, который читателю трудно понять с первого взгляда и который нужно сократить",
                    "Дорогие друзья, качественный сон поддерживает самочувствие. "
                    "Соблюдайте режим и обсудите тревожные симптомы с врачом. "
                    "Вечером уберите телефон за час до сна и проветрите комнату. "
                    "Белковый ужин — творог, рыба или яйца. Этого достаточно для мягкого старта.",
                    self.post_id,
                ),
            )

        review = await evaluate_post(self.db_path, self.post_id)
        self.assertTrue(review.is_blocking)
        self.assertIn("title_too_long", {issue.code for issue in review.issues})

    async def test_female_only_address_is_soft_for_mixed_audience(self):
        body = (
            "Дорогие читательницы, давайте разберём простую привычку вместе. "
            "Утром выпейте стакан тёплой воды, а вечером прогуляйтесь 10–15 минут. "
            "Белковый ужин — это творог, рыба или яйца. "
            "Так организм мягко восстанавливается без резких ограничений. "
            "Попробуйте уже сегодня и отметьте самочувствие. "
            "Если симптомы тревожат, обсудите их с врачом."
        )
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE posts SET topic = ?, body = ? WHERE id = ?",
                ("Привычка на утро", body, self.post_id),
            )

        review = await evaluate_post(self.db_path, self.post_id)
        codes = {issue.code: issue.blocking for issue in review.issues}
        self.assertIn("female_only_address", codes)
        self.assertFalse(codes["female_only_address"])
        self.assertFalse(review.is_blocking)
