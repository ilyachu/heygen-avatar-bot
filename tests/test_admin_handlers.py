import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.admin import (
    AdminUserState,
    cb_admin_add_user_hint,
    receive_admin_add_user,
)


class FakeState:
    def __init__(self):
        self.state = None
        self.cleared = False

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.state = None
        self.cleared = True


class AdminUserMenuTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, user_id=1000000):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            message=AsyncMock(),
            answer=AsyncMock(),
        )

    def message(self, text, user_id=1000000):
        return SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
        )

    @patch("bot.handlers.admin.is_admin", return_value=True)
    async def test_add_button_starts_guided_flow(self, _is_admin):
        callback = self.callback()
        state = FakeState()

        await cb_admin_add_user_hint(callback, state)

        self.assertEqual(AdminUserState.waiting_for_add_user.state, state.state)
        self.assertIn("ID и имя", callback.message.answer.await_args.args[0])
        callback.answer.assert_awaited_once()

    @patch("bot.handlers.admin.add_to_whitelist", new_callable=AsyncMock)
    @patch("bot.handlers.admin.is_admin", return_value=True)
    async def test_valid_user_is_added_from_menu(self, _is_admin, add_user):
        add_user.return_value = True
        message = self.message("123456789 Иван Маркетинг")
        state = FakeState()

        await receive_admin_add_user(message, state)

        add_user.assert_awaited_once_with(
            user_id=123456789,
            full_name="Иван Маркетинг",
            added_by=1000000,
        )
        self.assertTrue(state.cleared)
        self.assertIn("добавлен", message.answer.await_args.args[0])

    @patch("bot.handlers.admin.add_to_whitelist", new_callable=AsyncMock)
    @patch("bot.handlers.admin.is_admin", return_value=True)
    async def test_invalid_id_keeps_flow_open(self, _is_admin, add_user):
        message = self.message("не-id Иван")
        state = FakeState()
        state.state = AdminUserState.waiting_for_add_user.state

        await receive_admin_add_user(message, state)

        add_user.assert_not_awaited()
        self.assertFalse(state.cleared)
        self.assertIn("числовой Telegram ID", message.answer.await_args.args[0])

    @patch("bot.handlers.admin.add_to_whitelist", new_callable=AsyncMock)
    @patch("bot.handlers.admin.is_admin", return_value=False)
    async def test_non_admin_cannot_complete_flow(self, _is_admin, add_user):
        message = self.message("123456789 Иван", user_id=7)
        state = FakeState()

        await receive_admin_add_user(message, state)

        add_user.assert_not_awaited()
        self.assertTrue(state.cleared)
        self.assertIn("Недостаточно прав", message.answer.await_args.args[0])

