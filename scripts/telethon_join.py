"""Check and optionally join Telegram invite/public channel URLs."""

import argparse
import asyncio
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready

from bot.config import settings


def _url_kind(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "t.me",
        "telegram.me",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return None
    value = parts[0]
    if value.startswith("+") and len(value) > 1:
        return "invite", value[1:]
    if value.startswith("joinchat/"):
        return None
    if value and not value.startswith("+") and value not in {"joinchat"}:
        return "public", value.lstrip("@").lower()
    return None


async def _process(client: TelegramClient, url: str, join: bool) -> str:
    parsed = _url_kind(url)
    if parsed is None:
        return "invalid"
    kind, value = parsed
    try:
        if kind == "invite":
            invite = await client(CheckChatInviteRequest(value))
            already = isinstance(invite, ChatInviteAlready)
            if already:
                return "already"
            if not join:
                return "requested"
            await client(ImportChatInviteRequest(value))
            return "joined"

        dialogs = await client.get_dialogs(limit=None)
        member = any(
            getattr(dialog.entity, "username", "").lower() == value
            for dialog in dialogs
            if getattr(dialog.entity, "username", None)
        )
        if member:
            return "already"
        if not join:
            return "requested"
        await client(JoinChannelRequest(value))
        return "joined"
    except FloodWaitError as exc:
        return f"error:FloodWaitError:{exc.seconds}s"
    except (InviteHashInvalidError, InviteHashExpiredError, ChannelInvalidError, ChannelPrivateError):
        return "invalid"
    except UserAlreadyParticipantError:
        return "already"
    except Exception as exc:
        return f"error:{type(exc).__name__}"


async def run(urls: list[str], join: bool) -> None:
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise SystemExit("TELEGRAM_API_ID и TELEGRAM_API_HASH не настроены.")
    client = TelegramClient(
        settings.TELETHON_SESSION_PATH,
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Сначала выполните: python -m scripts.telethon_login")
        for index, url in enumerate(urls):
            print(f"{url}: {await _process(client, url, join)}")
            if join and index < len(urls) - 1:
                await asyncio.sleep(1)
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or join Telegram channels")
    parser.add_argument("urls", nargs="+", help="Telegram invite or public URLs")
    parser.add_argument("--join", action="store_true", help="Join channels when needed")
    args = parser.parse_args()
    asyncio.run(run(args.urls, args.join))


if __name__ == "__main__":
    main()
