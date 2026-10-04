import asyncio

import aiosqlite
from aiogram import Bot

from bot.config import settings
from bot.migrations import configure_connection


async def main() -> int:
    bot = Bot(settings.BOT_TOKEN)
    try:
        me = await bot.get_me()
        async with aiosqlite.connect(settings.DB_PATH) as db:
            await configure_connection(db)
            cursor = await db.execute(
                "SELECT channel_id, title FROM channels WHERE is_active = 1 ORDER BY title"
            )
            channels = await cursor.fetchall()

        missing: list[tuple[str, str]] = []
        for channel_id, title in channels:
            try:
                member = await bot.get_chat_member(channel_id, me.id)
                if member.status not in {"administrator", "creator"}:
                    missing.append((title or channel_id, member.status))
            except Exception as exc:
                missing.append((title or channel_id, type(exc).__name__))

        print(
            f"{settings.BOT_MODE} @{me.username}: "
            f"{len(channels) - len(missing)}/{len(channels)} channels ready"
        )
        for title, reason in missing:
            print(f"MISSING {title}: {reason}")
        return 1 if missing else 0
    finally:
        await bot.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
