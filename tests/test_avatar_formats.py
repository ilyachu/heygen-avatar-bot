import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.config import settings
from bot.services.heygen_service import build_experts, VOICES, HeyGenClient
from bot.handlers.avatar import (
    auto_select_and_continue, handle_avatar_text, execute_render, AvatarState
)


AVATAR_ID = "test-avatar-id"
GROUP_ID = "test-group-id"


class AvatarCatalogTests(unittest.TestCase):
    def test_catalog_is_built_from_settings(self):
        with patch.object(settings, "HEYGEN_AVATAR_ID", AVATAR_ID), \
             patch.object(settings, "HEYGEN_AVATAR_GROUP_ID", GROUP_ID), \
             patch.object(settings, "HEYGEN_AVATAR_NAME", "Тестовый эксперт"), \
             patch.object(settings, "HEYGEN_AVATAR_TYPE", "digital_twin"), \
             patch.object(settings, "HEYGEN_AVATAR_LOOK_NAME", "Основной образ"), \
             patch.object(settings, "HEYGEN_AVATAR_CATEGORY", "live"), \
             patch.object(settings, "HEYGEN_AVATAR_STORIES_FIT", "best"):
            experts = build_experts()
        self.assertEqual({"expert"}, set(experts.keys()))
        expert = experts["expert"]
        self.assertEqual("Тестовый эксперт", expert["name"])
        self.assertEqual({"main"}, set(expert["looks"].keys()))
        look = expert["looks"]["main"]
        self.assertEqual(AVATAR_ID, look["id"])
        self.assertEqual(GROUP_ID, look["group_id"])
        self.assertEqual("Основной образ", look["name"])
        self.assertEqual("digital_twin", look["avatar_type"])
        self.assertEqual("live", look["category"])
        self.assertEqual("best", look["stories_fit"])

    def test_catalog_is_empty_without_avatar_id(self):
        with patch.object(settings, "HEYGEN_AVATAR_ID", ""):
            self.assertEqual({}, build_experts())

    def test_voices_catalog_is_empty(self):
        self.assertEqual({}, VOICES)


class AvatarFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_avatar_video_requests_portrait_for_configured_avatar(self):
        response = AsyncMock()
        response.status_code = 200
        response.json = Mock(return_value={"data": {"video_id": "test-video"}})
        http = AsyncMock()
        http.post.return_value = response
        http.__aenter__.return_value = http
        client = HeyGenClient(api_key="test")
        with patch.object(settings, "HEYGEN_AVATAR_ID", AVATAR_ID), \
             patch.object(settings, "HEYGEN_AVATAR_STORIES_FIT", "best"), \
             patch.object(client, "_get_headers", AsyncMock(return_value={"Authorization": "Bearer test"})), \
             patch("bot.services.heygen_service.httpx.AsyncClient", return_value=http):
            ok, _ = await client.create_avatar_video(
                AVATAR_ID, "voice", "text", {},
                avatar_type="digital_twin", output_format="stories"
            )
        self.assertTrue(ok)
        payload = http.post.await_args.kwargs["json"]
        self.assertEqual({"width": 720, "height": 1280}, payload["dimension"])
        character = payload["video_inputs"][0]["character"]
        self.assertEqual("avatar", character["type"])
        self.assertEqual(AVATAR_ID, character["avatar_id"])

    async def test_auto_select_blocks_when_avatar_not_configured(self):
        state = AsyncMock()
        target = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
        with patch.object(settings, "HEYGEN_AVATAR_ID", ""):
            await auto_select_and_continue(target, state)
        state.set_state.assert_not_awaited()
        self.assertIn("не настроен", target.message.edit_text.await_args.args[0])

    async def test_auto_select_prompts_voice_id_when_empty(self):
        state = AsyncMock()
        state.get_data.return_value = {
            "output_format": "circle",
            "expert_name": "Эксперт",
            "look_name": "Основной образ",
        }
        target = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
        with patch.object(settings, "HEYGEN_AVATAR_ID", AVATAR_ID), \
             patch.object(settings, "HEYGEN_DEFAULT_VOICE_ID", ""):
            await auto_select_and_continue(target, state)
        state.set_state.assert_awaited_with(AvatarState.entering_voice_id)
        sent_text = target.message.edit_text.await_args.args[0]
        self.assertIn("HEYGEN_DEFAULT_VOICE_ID", sent_text)

    async def test_auto_select_proceeds_to_text_with_voice_id(self):
        state = AsyncMock()
        state.get_data.return_value = {
            "output_format": "stories",
            "expert_name": "Эксперт",
            "look_name": "Основной образ",
        }
        target = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
        with patch.object(settings, "HEYGEN_AVATAR_ID", AVATAR_ID), \
             patch.object(settings, "HEYGEN_DEFAULT_VOICE_ID", "some-voice-id"):
            await auto_select_and_continue(target, state)
        state.set_state.assert_awaited_with(AvatarState.entering_text)
        state.update_data.assert_any_await(
            voice_id="some-voice-id", voice_name="Голос по умолчанию (HeyGen)"
        )

    async def test_text_over_limit_is_rejected(self):
        state = AsyncMock()
        state.get_data.return_value = {"output_format": "stories"}
        message = SimpleNamespace(text=" ".join(["слово"] * 201), answer=AsyncMock())
        with patch.object(settings, "MAX_STORIES_TEXT_WORDS", 200):
            await handle_avatar_text(message, state)
        state.update_data.assert_not_awaited()
        self.assertIn("слишком длинный", message.answer.await_args.args[0])

    async def test_execute_render_blocks_without_voice_id(self):
        state = AsyncMock()
        state.get_data.return_value = {
            "avatar_id": AVATAR_ID,
            "voice_id": "",
            "text": "привет",
            "output_format": "circle",
            "expert_name": "Эксперт",
            "look_name": "Основной образ",
        }
        callback = SimpleNamespace(
            data="confirm_render:prod",
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        with patch("bot.handlers.avatar.heygen_client.create_avatar_video", new_callable=AsyncMock) as generate:
            await execute_render(callback, state, AsyncMock())
            generate.assert_not_awaited()
        callback.message.edit_text.assert_awaited_once()
