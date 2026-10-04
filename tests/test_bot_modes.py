import tempfile
import unittest
from pathlib import Path

from aiogram.fsm.storage.base import StorageKey

from bot.keyboards.all_keyboards import get_content_main_menu, get_main_menu
from bot.main import (
    router_modules_for_mode,
    scheduler_enabled_for_mode,
    validate_bot_identity,
)
from bot.storage import SqliteStorage


def button_texts(keyboard) -> set[str]:
    return {button.text for row in keyboard.keyboard for button in row}


class BotModeTests(unittest.TestCase):
    def test_content_mode_has_only_content_routers_and_scheduler(self):
        names = {module.__name__.rsplit(".", 1)[-1] for module in router_modules_for_mode("content")}

        self.assertEqual(
            {"content_start", "content_approval", "broadcast", "admin", "content_fallback"},
            names,
        )
        self.assertTrue(scheduler_enabled_for_mode("content"))

    def test_media_mode_has_only_media_routers_without_scheduler(self):
        names = {module.__name__.rsplit(".", 1)[-1] for module in router_modules_for_mode("media")}

        self.assertEqual({"start", "avatar", "audio", "publish", "fallback"}, names)
        self.assertFalse(scheduler_enabled_for_mode("media"))

    def test_menus_do_not_mix_content_and_media_actions(self):
        content = button_texts(get_content_main_menu(is_admin=True))
        media = button_texts(get_main_menu())

        self.assertIn("🗓 Контент-план", content)
        self.assertIn("🗂 План 2–3 постов", content)
        self.assertIn("👑 Панель администратора", content)
        self.assertNotIn("🎥 Кружок", content)
        self.assertIn("🎥 Кружок", media)
        self.assertIn("🎬 Рилс (9:16)", media)
        self.assertIn("🎙 Аудио", media)
        self.assertNotIn("🗓 Контент-план", media)
        self.assertNotIn("👑 Панель администратора", media)

    def test_unknown_mode_fails_closed(self):
        with self.assertRaises(ValueError):
            router_modules_for_mode("combined")

    def test_swapped_bot_token_fails_closed(self):
        validate_bot_identity("content", actual_id=111111111, expected_id=111111111)
        with self.assertRaises(RuntimeError):
            validate_bot_identity("content", actual_id=222222222, expected_id=111111111)


class SplitBotStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_fsm_state_and_data_are_isolated_by_bot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = SqliteStorage(str(Path(tmp) / "fsm.db"))
            await storage.init()
            content_key = StorageKey(bot_id=111111111, chat_id=42, user_id=42)
            media_key = StorageKey(bot_id=222222222, chat_id=42, user_id=42)

            await storage.set_state(content_key, "content:review")
            await storage.set_data(content_key, {"post_id": 7})
            await storage.set_state(media_key, "media:avatar")
            await storage.set_data(media_key, {"voice": "default"})

            self.assertEqual("content:review", await storage.get_state(content_key))
            self.assertEqual({"post_id": 7}, await storage.get_data(content_key))
            self.assertEqual("media:avatar", await storage.get_state(media_key))
            self.assertEqual({"voice": "default"}, await storage.get_data(media_key))
