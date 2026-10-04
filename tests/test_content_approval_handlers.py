import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.content_approval import handle_content_action


class ContentApprovalHandlerTests(unittest.IsolatedAsyncioTestCase):
    @patch("bot.handlers.content_approval.ContentPlanningService")
    async def test_approve_callback_schedules_instead_of_publishing_immediately(
        self, service_class
    ):
        service_class.return_value.approve_posts = AsyncMock(
            return_value={"approved": 1, "unchanged": 0, "posts": []}
        )
        callback = SimpleNamespace(
            data="content:approve:7",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        bot = AsyncMock()

        await handle_content_action(callback, bot)

        service_class.return_value.approve_posts.assert_awaited_once_with([7], 42)
        bot.send_message.assert_not_awaited()
        callback.message.edit_text.assert_awaited_once()
        self.assertIn("Одобрено и запланировано", callback.message.edit_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
