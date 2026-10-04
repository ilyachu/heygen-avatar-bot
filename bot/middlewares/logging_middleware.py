import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

logger = logging.getLogger("user_activity")

class UserActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        user_info = f"ID:{user.id} (@{user.username or 'no_user'}) {user.full_name or ''}" if user else "Unknown"

        if isinstance(event, Message):
            content = event.text or event.caption or f"[{event.content_type}]"
            content_preview = content[:500].replace("\n", " ")
            logger.info(f"📩 [{user_info}] MESSAGE: {content_preview}")
        elif isinstance(event, CallbackQuery):
            logger.info(f"🔘 [{user_info}] CLICK: {event.data}")

        return await handler(event, data)
