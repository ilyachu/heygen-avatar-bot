import asyncio
import os
from pathlib import Path

from telethon import TelegramClient

from bot.config import settings


async def main() -> None:
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise SystemExit(
            "Добавьте TELEGRAM_API_ID и TELEGRAM_API_HASH в .env "
            "(my.telegram.org → API development tools)."
        )
    if not settings.TELEGRAM_PHONE:
        raise SystemExit("Добавьте TELEGRAM_PHONE в .env.")

    session_path = Path(settings.TELETHON_SESSION_PATH)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(session_path.parent, 0o700)

    client = TelegramClient(
        str(session_path), settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH
    )
    await client.start(phone=settings.TELEGRAM_PHONE)
    try:
        me = await client.get_me()
        dialogs = await client.get_dialogs(limit=None)
        channels = [dialog for dialog in dialogs if dialog.is_channel]
        print(f"Авторизация успешна: user_id={me.id}")
        print(f"Доступно каналов и супергрупп: {len(channels)}")
        print(f"Сессия сохранена: {session_path}.session")
    finally:
        await client.disconnect()
        session_file = session_path.with_suffix(".session")
        if session_file.exists():
            os.chmod(session_file, 0o600)


if __name__ == "__main__":
    asyncio.run(main())
