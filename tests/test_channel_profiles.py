import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.migrations import run_migrations
from domain.channel_profiles import import_profiles, load_profile_files


class ChannelProfileImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "profiles.db")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT UNIQUE,
                    title TEXT, username TEXT, added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        await run_migrations(self.db_path)
        self.profile = {
            "channel_id": -1001,
            "title": "Печень",
            "description": "Практический канал о здоровье печени.",
            "audience": "Взрослые 40+",
            "purpose": "Тематический канал",
            "key_meanings": ["Системный подход"],
            "rubrics": ["Питание"],
            "tone_of_voice": "Объяснять просто.",
            "cta_rules": "Один CTA.",
            "forbidden_topics": [],
            "source_message_count": 100,
        }

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_import_upserts_channel_and_profile(self):
        self.assertEqual(1, await import_profiles(self.db_path, [self.profile]))
        changed = {**self.profile, "description": "Обновлённое описание"}
        await import_profiles(self.db_path, [changed])

        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                """
                SELECT c.channel_id, cp.description, cp.analysis_version,
                       cp.key_meanings_json
                FROM channels c JOIN channel_profiles cp ON cp.channel_id = c.id
                """
            ).fetchone()
        self.assertEqual("-1001", row[0])
        self.assertEqual("Обновлённое описание", row[1])
        self.assertEqual(2, row[2])
        self.assertEqual(["Системный подход"], json.loads(row[3]))

    async def test_loader_rejects_missing_fields(self):
        path = Path(self.tmp.name) / "invalid.json"
        path.write_text('[{"channel_id": 1}]', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_profile_files([path])
