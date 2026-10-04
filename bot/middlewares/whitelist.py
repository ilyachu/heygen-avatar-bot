from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from bot.db import is_user_whitelisted

class WhitelistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)
            
        allowed = await is_user_whitelisted(user.id)
        if not allowed:
            if isinstance(event, Message):
                await event.answer(
                    f"⛔ <b>Доступ ограничен</b>\n\n"
                    f"Бот предназначен исключительно для сотрудников команды.\n"
                    f"Ваш Telegram ID: <code>{user.id}</code>\n\n"
                    f"Передайте этот ID администратору для открытия доступа.",
                    parse_mode="HTML"
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ У вас нет доступа к этому боту.", show_alert=True)
            return
            
        return await handler(event, data)
