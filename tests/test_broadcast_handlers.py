import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.broadcast import (
    MAX_TEXT_LENGTH,
    _cancel_scheduled_broadcast_batch,
    _claim_broadcast_operation,
    _create_broadcast_posts,
    _create_scheduled_broadcast_batch,
    _get_broadcast_batch,
    _list_future_scheduled_batches,
    _update_scheduled_batch_slot,
    choose_broadcast_batch_size,
    confirm_broadcast,
    receive_broadcast_batch_schedule,
    receive_broadcast_batch_text,
    receive_broadcast_text,
    start_broadcast,
    skip_broadcast_batch_media,
    toggle_broadcast_channel,
)
from bot.migrations import run_migrations
from bot.services.publishing import PublishingService


class FakeState:
    def __init__(self, data=None):
        self.data = data or {}
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.data.clear()
        self.state = None


def channel(channel_id, title):
    return {"id": channel_id, "channel_id": f"-100{channel_id}", "title": title, "username": ""}


class BroadcastHandlerTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, data, user_id=7):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=AsyncMock(),
            answer=AsyncMock(),
        )

    def message(self, text, user_id=7):
        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user_id), answer=AsyncMock())

    @patch("bot.handlers.broadcast._is_admin", return_value=True)
    @patch("bot.handlers.broadcast._active_channels", new_callable=AsyncMock)
    async def test_start_selects_all_connected_channels(self, get_all_channels, _is_admin):
        get_all_channels.return_value = [channel(1, "Первый"), channel(2, "Второй")]
        state = FakeState()
        callback = self.callback("broadcast:start")

        await start_broadcast(callback, state)

        self.assertEqual([1, 2], state.data["broadcast_channel_ids"])
        callback.message.answer.assert_awaited_once()
        self.assertIn("Пост на все каналы", callback.message.answer.await_args.args[0])

    @patch("bot.handlers.broadcast._is_admin", return_value=True)
    async def test_text_over_telegram_limit_is_rejected(self, _is_admin):
        state = FakeState()
        message = self.message("x" * (MAX_TEXT_LENGTH + 1))

        await receive_broadcast_text(message, state)

        self.assertNotIn("broadcast_text", state.data)
        self.assertIn("слишком длинный", message.answer.await_args.args[0])

    @patch("bot.handlers.broadcast._is_admin", return_value=True)
    @patch("bot.handlers.broadcast._active_channels", new_callable=AsyncMock)
    async def test_channel_toggle_excludes_then_includes_active_channel(self, get_all_channels, _is_admin):
        get_all_channels.return_value = [channel(1, "Первый"), channel(2, "Второй")]
        state = FakeState({"broadcast_channel_ids": [1, 2]})
        callback = self.callback("broadcast:toggle:2")

        await toggle_broadcast_channel(callback, state)
        self.assertEqual({1}, set(state.data["broadcast_channel_ids"]))

        await toggle_broadcast_channel(callback, state)
        self.assertEqual({1, 2}, set(state.data["broadcast_channel_ids"]))

    @patch("bot.handlers.broadcast._show_channel_selection", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._is_admin", return_value=True)
    async def test_two_post_guided_plan_collects_each_item(
        self, _is_admin, show_channels
    ):
        state = FakeState(
            {
                "broadcast_channel_ids": [1, 2],
                "broadcast_batch_key": "batch-guided",
                "broadcast_batch_items": [],
            }
        )
        choose = self.callback("broadcast:batch_size:2")
        await choose_broadcast_batch_size(choose, state)
        self.assertEqual(2, state.data["broadcast_batch_size"])

        first_text = self.message("Первый пост")
        await receive_broadcast_batch_text(first_text, state)
        first_skip = self.callback("broadcast:skip_photo")
        await skip_broadcast_batch_media(first_skip, state)
        first_time = self.message("01.10.2099 10:00")
        await receive_broadcast_batch_schedule(first_time, state)
        self.assertEqual(1, len(state.data["broadcast_batch_items"]))

        second_text = self.message("Второй пост")
        await receive_broadcast_batch_text(second_text, state)
        second_skip = self.callback("broadcast:skip_photo")
        await skip_broadcast_batch_media(second_skip, state)
        second_time = self.message("02.10.2099 11:00")
        await receive_broadcast_batch_schedule(second_time, state)

        self.assertEqual(2, len(state.data["broadcast_batch_items"]))
        show_channels.assert_awaited_once_with(second_time, state)

    @patch("bot.handlers.broadcast._record_broadcast_result", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._complete_broadcast_operation", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._claim_broadcast_operation", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._create_broadcast_posts", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast.PublishingService")
    @patch("bot.handlers.broadcast._active_channels", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._is_admin", return_value=True)
    async def test_immediate_confirmation_sends_once_and_records_each_channel(
        self, _is_admin, get_all_channels, publisher_cls, create_posts,
        claim_operation, complete_operation, record_result
    ):
        channels = [channel(1, "Первый"), channel(2, "Второй")]
        get_all_channels.return_value = channels
        create_posts.return_value = {1: 101, 2: 102}
        claim_operation.return_value = True
        publisher_cls.return_value.publish_text = AsyncMock(
            side_effect=[SimpleNamespace(message_id=11), SimpleNamespace(message_id=12)]
        )
        state = FakeState({
            "broadcast_channel_ids": [1, 2], "broadcast_text": "Текст",
            "broadcast_operation_key": "operation-1",
        })
        callback = self.callback("broadcast:confirm")

        await confirm_broadcast(callback, state, AsyncMock())

        create_posts.assert_awaited_once_with(
            channels, operation_key="operation-1", text="Текст",
            media_type="text", media_file_id=None,
            status="publishing", publish_at=None, created_by=7,
        )
        self.assertEqual(2, publisher_cls.return_value.publish_text.await_count)
        self.assertEqual(2, record_result.await_count)
        self.assertEqual({}, state.data)

    @patch("bot.handlers.broadcast._create_broadcast_posts", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._complete_broadcast_operation", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._claim_broadcast_operation", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._active_channels", new_callable=AsyncMock)
    @patch("bot.handlers.broadcast._is_admin", return_value=True)
    async def test_scheduled_photo_creates_one_scheduled_row_per_channel(
        self, _is_admin, get_all_channels, claim_operation,
        complete_operation, create_posts
    ):
        channels = [channel(1, "Первый"), channel(2, "Второй")]
        get_all_channels.return_value = channels
        create_posts.return_value = {1: 101, 2: 102}
        claim_operation.return_value = True
        state = FakeState({
            "broadcast_channel_ids": [1, 2],
            "broadcast_text": "Текст с фото",
            "broadcast_photo_file_id": "photo-file-id",
            "broadcast_publish_at": "2026-10-01T10:00:00+00:00",
            "broadcast_operation_key": "operation-2",
        })
        callback = self.callback("broadcast:confirm")

        await confirm_broadcast(callback, state, AsyncMock())

        create_posts.assert_awaited_once_with(
            channels, operation_key="operation-2", text="Текст с фото", media_type="photo",
            media_file_id="photo-file-id", status="scheduled",
            publish_at="2026-10-01T10:00:00+00:00", created_by=7,
        )
        self.assertEqual({}, state.data)


class PublishingServicePhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_is_sent_with_text_as_caption(self):
        bot = AsyncMock()
        await PublishingService(bot).publish_media(
            channel_id="-1001", media_type="photo", file_id="photo-id", text="Подпись"
        )
        bot.send_photo.assert_awaited_once_with(
            chat_id="-1001", photo="photo-id", caption="Подпись"
        )

    async def test_long_photo_caption_is_rejected_before_any_send(self):
        bot = AsyncMock()
        text = "x" * 1025
        with self.assertRaisesRegex(ValueError, "1024"):
            await PublishingService(bot).publish_media(
                channel_id="-1001", media_type="photo", file_id="photo-id", text=text
            )
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()

    async def test_video_and_voice_are_sent_with_validated_captions(self):
        bot = AsyncMock()
        service = PublishingService(bot)

        await service.publish_media(
            channel_id="-1001", media_type="video", file_id="video-id", text="Видео"
        )
        await service.publish_media(
            channel_id="-1001", media_type="voice", file_id="voice-id", text="Аудио"
        )

        bot.send_video.assert_awaited_once_with(
            chat_id="-1001", video="video-id", caption="Видео"
        )
        bot.send_voice.assert_awaited_once_with(
            chat_id="-1001", voice="voice-id", caption="Аудио", parse_mode="HTML"
        )


class BroadcastPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "broadcast.db")
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
            self.channel_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1001', 'Первый')"
            ).lastrowid
        await run_migrations(self.db_path)
        self.settings_patch = patch("bot.handlers.broadcast.settings.DB_PATH", self.db_path)
        self.settings_patch.start()

    async def asyncTearDown(self):
        self.settings_patch.stop()
        self.tmp.cleanup()

    async def test_operation_is_claimed_once_and_inactive_channel_is_rejected(self):
        self.assertTrue(await _claim_broadcast_operation("op-1", 7, None))
        self.assertFalse(await _claim_broadcast_operation("op-1", 7, None))
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE channels SET is_active = 0 WHERE id = ?", (self.channel_id,))
        with self.assertRaisesRegex(ValueError, "inactive"):
            await _create_broadcast_posts(
                [channel(self.channel_id, "Первый")],
                operation_key="op-1",
                text="Текст",
                media_type="text",
                media_file_id=None,
                status="publishing",
                publish_at=None,
                created_by=7,
            )

    async def test_two_post_batch_is_atomic_and_replay_safe(self):
        with sqlite3.connect(self.db_path) as db:
            second_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1002', 'Второй')"
            ).lastrowid
        channels = [channel(self.channel_id, "Первый"), channel(second_id, "Второй")]
        first_time = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        second_time = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        items = [
            {
                "text": "Первый пост",
                "media_type": None,
                "media_file_id": None,
                "publish_at": first_time,
            },
            {
                "text": "Второй пост",
                "media_type": "video",
                "media_file_id": "video-id",
                "publish_at": second_time,
            },
        ]

        created = await _create_scheduled_broadcast_batch(
            channels, batch_key="batch-1", items=items, created_by=7
        )
        replay = await _create_scheduled_broadcast_batch(
            channels, batch_key="batch-1", items=items, created_by=7
        )

        self.assertEqual(4, created)
        self.assertIsNone(replay)
        with sqlite3.connect(self.db_path) as db:
            posts = db.execute(
                "SELECT operation_key, media_type, status FROM posts ORDER BY id"
            ).fetchall()
            operations = db.execute(
                "SELECT operation_key, status FROM broadcast_operations ORDER BY operation_key"
            ).fetchall()
        self.assertEqual(
            [
                ("batch-1:1", "text", "scheduled"),
                ("batch-1:1", "text", "scheduled"),
                ("batch-1:2", "video", "scheduled"),
                ("batch-1:2", "video", "scheduled"),
            ],
            posts,
        )
        self.assertEqual(
            [("batch-1:1", "scheduled"), ("batch-1:2", "scheduled")],
            operations,
        )

    async def _seed_batch(self, batch_key="batch-edit"):
        with sqlite3.connect(self.db_path) as db:
            second_id = db.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('-1002', 'Второй')"
            ).lastrowid
        first_time = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        second_time = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        await _create_scheduled_broadcast_batch(
            [channel(self.channel_id, "Первый"), channel(second_id, "Второй")],
            batch_key=batch_key,
            items=[
                {"text": "Первый", "publish_at": first_time},
                {"text": "Второй", "publish_at": second_time},
            ],
            created_by=7,
        )
        return first_time, second_time

    async def test_confirmed_batches_can_be_listed_and_opened(self):
        await self._seed_batch()

        batches = await _list_future_scheduled_batches()
        batch = await _get_broadcast_batch("batch-edit")

        self.assertEqual(1, len(batches))
        self.assertEqual("batch-edit", batches[0]["batch_key"])
        self.assertEqual(2, batches[0]["slot_count"])
        self.assertEqual(2, batches[0]["channel_count"])
        self.assertEqual([1, 2], [slot["slot"] for slot in batch["slots"]])

    async def test_slot_text_edit_updates_every_channel_once_with_events(self):
        await self._seed_batch()

        changed = await _update_scheduled_batch_slot(
            "batch-edit", 1, actor_id=7, body="Новый первый"
        )
        replay = await _update_scheduled_batch_slot(
            "batch-edit", 1, actor_id=7, body="Новый первый"
        )

        self.assertEqual(2, changed)
        self.assertEqual(0, replay)
        with sqlite3.connect(self.db_path) as db:
            posts = db.execute(
                "SELECT body, version FROM posts WHERE operation_key = 'batch-edit:1'"
            ).fetchall()
            events = db.execute(
                """SELECT COUNT(*) FROM post_events
                   WHERE event_type = 'broadcast_batch_text_updated'"""
            ).fetchone()[0]
        self.assertEqual([("Новый первый", 2), ("Новый первый", 2)], posts)
        self.assertEqual(2, events)

    async def test_slot_schedule_edit_updates_posts_and_operation(self):
        await self._seed_batch()
        new_time = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()

        changed = await _update_scheduled_batch_slot(
            "batch-edit", 2, actor_id=7, publish_at=new_time
        )

        self.assertEqual(2, changed)
        with sqlite3.connect(self.db_path) as db:
            posts = db.execute(
                "SELECT publish_at, version FROM posts WHERE operation_key = 'batch-edit:2'"
            ).fetchall()
            operation = db.execute(
                "SELECT publish_at FROM broadcast_operations WHERE operation_key = 'batch-edit:2'"
            ).fetchone()[0]
            events = db.execute(
                """SELECT COUNT(*) FROM post_events
                   WHERE event_type = 'broadcast_batch_schedule_updated'"""
            ).fetchone()[0]
        self.assertEqual([(new_time, 2), (new_time, 2)], posts)
        self.assertEqual(new_time, operation)
        self.assertEqual(2, events)

    async def test_slot_edit_is_refused_after_any_delivery_started(self):
        await self._seed_batch()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """UPDATE posts SET status = 'publishing'
                   WHERE id = (SELECT MIN(id) FROM posts WHERE operation_key = 'batch-edit:1')"""
            )

        with self.assertRaisesRegex(ValueError, "уже началась"):
            await _update_scheduled_batch_slot(
                "batch-edit", 1, actor_id=7, body="Не должен примениться"
            )

        with sqlite3.connect(self.db_path) as db:
            bodies = db.execute(
                "SELECT DISTINCT body FROM posts WHERE operation_key = 'batch-edit:1'"
            ).fetchall()
        self.assertEqual([("Первый",)], bodies)

    async def test_batch_cancel_only_touches_scheduled_rows_and_replay_is_safe(self):
        await self._seed_batch()
        with sqlite3.connect(self.db_path) as db:
            publishing_id = db.execute(
                "SELECT MIN(id) FROM posts WHERE operation_key = 'batch-edit:1'"
            ).fetchone()[0]
            db.execute("UPDATE posts SET status = 'publishing' WHERE id = ?", (publishing_id,))

        result = await _cancel_scheduled_broadcast_batch("batch-edit", actor_id=7)
        replay = await _cancel_scheduled_broadcast_batch("batch-edit", actor_id=7)

        self.assertEqual({"cancelled": 3, "untouched": 1}, result)
        self.assertEqual({"cancelled": 0, "untouched": 4}, replay)
        with sqlite3.connect(self.db_path) as db:
            publishing = db.execute(
                "SELECT status, version FROM posts WHERE id = ?", (publishing_id,)
            ).fetchone()
            cancelled = db.execute(
                "SELECT status, version FROM posts WHERE id <> ? ORDER BY id", (publishing_id,)
            ).fetchall()
            events = db.execute(
                """SELECT COUNT(*) FROM post_events
                   WHERE event_type = 'broadcast_batch_cancelled'"""
            ).fetchone()[0]
            operations = db.execute(
                "SELECT operation_key, status FROM broadcast_operations ORDER BY operation_key"
            ).fetchall()
        self.assertEqual(("publishing", 1), publishing)
        self.assertEqual([("cancelled", 2)] * 3, cancelled)
        self.assertEqual(3, events)
        self.assertEqual(
            [("batch-edit:1", "scheduled"), ("batch-edit:2", "cancelled")],
            operations,
        )


if __name__ == "__main__":
    unittest.main()
