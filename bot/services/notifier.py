import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from aiogram import Bot
from bot.config import settings

logger = logging.getLogger(__name__)

# MSK timezone (UTC+3)
MSK = timezone(timedelta(hours=3))

async def notify_admins_error(
    bot: Bot,
    title: str,
    error: Any,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    extra_info: Optional[Dict[str, Any]] = None
):
    """
    Sends an urgent push alert to all bot administrators when an error occurs.
    """
    now_str = datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S")
    err_str = str(error)
    if len(err_str) > 1800:
        err_str = err_str[:1800] + "... [сообщение обрезано]"
    safe_err = html.escape(err_str)

    user_line = ""
    if user_id:
        user_line = f"👤 <b>Пользователь:</b> <code>{user_id}</code>"
        if username:
            user_line += f" (@{html.escape(username)})"
        user_line += "\n"

    extra_lines = ""
    if extra_info:
        for k, v in extra_info.items():
            extra_lines += f"• <b>{html.escape(str(k))}:</b> <code>{html.escape(str(v))}</code>\n"

    text = (
        f"🚨 <b>ОШИБКА В БОТЕ!</b>\n\n"
        f"📍 <b>Контекст:</b> {html.escape(title)}\n"
        f"{user_line}"
        f"⏰ <b>Время:</b> {now_str} МСК\n"
    )
    if extra_lines:
        text += f"\n<b>Дополнительно:</b>\n{extra_lines}"

    text += f"\n⚠️ <b>Текст ошибки / детали:</b>\n<pre><code>{safe_err}</code></pre>"

    admin_ids = settings.admin_id_list
    if not admin_ids:
        logger.warning("No admins configured to send error alert!")
        return

    for aid in admin_ids:
        try:
            await bot.send_message(
                chat_id=aid,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Failed to send error alert to admin {aid}: {e}")
