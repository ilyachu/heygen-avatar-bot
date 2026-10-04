import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.publish import handle_pub_direct, process_post_text_and_publish


class PublishHandlersCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def make_callback(self, data: str):
        return SimpleNamespace(data=data, message=AsyncMock(), answer=AsyncMock())

    def make_state(self, data: dict):
        state = AsyncMock()
        state.get_data.return_value = data
        return state

    @patch("bot.handlers.publish.get_media")
    async def test_direct_video_note_is_published_and_state_is_cleared(self, get_media):
        get_media.return_value = {
            "media_type": "video_note",
            "file_id": "video-file-id",
        }
        callback = self.make_callback("do_pub_direct:short-1:-10001")
        state = self.make_state({})
        bot = AsyncMock()
        bot.get_chat.return_value = SimpleNamespace(title="Test channel")

        await handle_pub_direct(callback, state, bot)

        bot.send_video_note.assert_awaited_once_with(
            chat_id="-10001", video_note="video-file-id"
        )
        bot.send_voice.assert_not_awaited()
        bot.get_chat.assert_awaited_once_with("-10001")
        callback.message.edit_text.assert_awaited_once()
        self.assertIn(
            "Успешно опубликовано в канал Test channel",
            callback.message.edit_text.await_args.args[0],
        )
        callback.answer.assert_awaited_once_with("Опубликовано!")
        state.clear.assert_awaited_once_with()

    @patch("bot.handlers.publish.get_media")
    async def test_direct_voice_is_published_and_state_is_cleared(self, get_media):
        get_media.return_value = {"media_type": "voice", "file_id": "voice-file-id"}
        callback = self.make_callback("do_pub_direct:short-2:@channel")
        state = self.make_state({})
        bot = AsyncMock()
        bot.get_chat.return_value = SimpleNamespace(title=None)

        await handle_pub_direct(callback, state, bot)

        bot.send_voice.assert_awaited_once_with(
            chat_id="@channel", voice="voice-file-id"
        )
        bot.send_video_note.assert_not_awaited()
        self.assertIn(
            "Успешно опубликовано в канал @channel",
            callback.message.edit_text.await_args.args[0],
        )
        callback.answer.assert_awaited_once_with("Опубликовано!")
        state.clear.assert_awaited_once_with()

    @patch("bot.handlers.publish.get_media", return_value=None)
    async def test_direct_publish_uses_media_from_state_when_cache_misses(
        self, _get_media
    ):
        callback = self.make_callback("do_pub_direct:expired:-10002")
        state = self.make_state(
            {"pub_media_type": "voice", "pub_file_id": "state-voice-id"}
        )
        bot = AsyncMock()
        bot.get_chat.return_value = SimpleNamespace(title="Fallback channel")

        await handle_pub_direct(callback, state, bot)

        bot.send_voice.assert_awaited_once_with(
            chat_id="-10002", voice="state-voice-id"
        )
        state.clear.assert_awaited_once_with()

    @patch("bot.handlers.publish.get_media", return_value=None)
    async def test_direct_publish_with_missing_file_shows_alert_and_keeps_state(
        self, _get_media
    ):
        callback = self.make_callback("do_pub_direct:expired:-10003")
        state = self.make_state({})
        bot = AsyncMock()

        await handle_pub_direct(callback, state, bot)

        callback.answer.assert_awaited_once_with(
            "Ошибка: медиафайл устарел. Создайте новый кружок.", show_alert=True
        )
        bot.send_video_note.assert_not_awaited()
        bot.send_voice.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()
        state.clear.assert_not_awaited()

    @patch("bot.handlers.publish.get_media")
    async def test_direct_publish_with_unknown_media_shows_alert_and_clears_state(
        self, get_media
    ):
        get_media.return_value = {"media_type": "document", "file_id": "doc-id"}
        callback = self.make_callback("do_pub_direct:short-3:-10004")
        state = self.make_state({})
        bot = AsyncMock()

        await handle_pub_direct(callback, state, bot)

        callback.answer.assert_awaited_once_with(
            "Неизвестный тип медиа", show_alert=True
        )
        bot.send_video_note.assert_not_awaited()
        bot.send_voice.assert_not_awaited()
        bot.get_chat.assert_not_awaited()
        state.clear.assert_awaited_once_with()

    async def test_text_and_video_note_are_sent_in_that_order_and_state_is_cleared(
        self,
    ):
        events = []
        message = AsyncMock()
        wait_msg = AsyncMock()
        message.text = "  Intro text  "
        message.answer.return_value = wait_msg
        state = self.make_state(
            {
                "pub_channel_id": "-10005",
                "pub_media_type": "video_note",
                "pub_file_id": "video-id",
            }
        )
        bot = AsyncMock()
        bot.send_message.side_effect = lambda **_kwargs: events.append("text")
        bot.send_video_note.side_effect = lambda **_kwargs: events.append("video_note")
        bot.get_chat.return_value = SimpleNamespace(title="Video channel")

        await process_post_text_and_publish(message, state, bot)

        self.assertEqual(events, ["text", "video_note"])
        bot.send_message.assert_awaited_once_with(
            chat_id="-10005", text="Intro text", parse_mode="HTML"
        )
        bot.send_video_note.assert_awaited_once_with(
            chat_id="-10005", video_note="video-id"
        )
        wait_msg.edit_text.assert_awaited_once()
        self.assertIn(
            "Успешно опубликовано в канал Video channel",
            wait_msg.edit_text.await_args.args[0],
        )
        state.clear.assert_awaited_once_with()

    async def test_text_and_voice_are_sent_as_one_captioned_voice_message(self):
        message = AsyncMock()
        wait_msg = AsyncMock()
        message.text = "  Voice caption  "
        message.answer.return_value = wait_msg
        state = self.make_state(
            {
                "pub_channel_id": "@voice_channel",
                "pub_media_type": "voice",
                "pub_file_id": "voice-id",
            }
        )
        bot = AsyncMock()
        bot.get_chat.return_value = SimpleNamespace(title="Voice channel")

        await process_post_text_and_publish(message, state, bot)

        bot.send_voice.assert_awaited_once_with(
            chat_id="@voice_channel",
            voice="voice-id",
            caption="Voice caption",
            parse_mode="HTML",
        )
        bot.send_message.assert_not_awaited()
        bot.send_video_note.assert_not_awaited()
        wait_msg.edit_text.assert_awaited_once()
        state.clear.assert_awaited_once_with()

    async def test_text_publish_with_missing_channel_rejects_and_clears_state(self):
        message = AsyncMock()
        message.text = "Caption"
        state = self.make_state(
            {"pub_media_type": "voice", "pub_file_id": "voice-id"}
        )
        bot = AsyncMock()

        await process_post_text_and_publish(message, state, bot)

        message.answer.assert_awaited_once_with(
            "❌ Ошибка параметров публикации. Попробуйте снова."
        )
        bot.send_voice.assert_not_awaited()
        bot.send_video_note.assert_not_awaited()
        state.clear.assert_awaited_once_with()

    async def test_text_publish_with_missing_file_rejects_and_clears_state(self):
        message = AsyncMock()
        message.text = "Caption"
        state = self.make_state(
            {"pub_channel_id": "-10006", "pub_media_type": "video_note"}
        )
        bot = AsyncMock()

        await process_post_text_and_publish(message, state, bot)

        message.answer.assert_awaited_once_with(
            "❌ Ошибка параметров публикации. Попробуйте снова."
        )
        bot.send_voice.assert_not_awaited()
        bot.send_video_note.assert_not_awaited()
        state.clear.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
