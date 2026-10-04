import argparse
import asyncio
import json
from pathlib import Path

from telethon import TelegramClient

from bot.config import settings


async def export_channels(
    output_dir: Path, limit: int | None, only_new: bool = False
) -> None:
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise SystemExit("TELEGRAM_API_ID и TELEGRAM_API_HASH не настроены.")

    output_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        settings.TELETHON_SESSION_PATH,
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Сначала выполните: python -m scripts.telethon_login")

        dialogs = [dialog for dialog in await client.get_dialogs() if dialog.is_channel]
        manifest_path = output_dir / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if only_new and manifest_path.exists()
            else []
        )
        known_ids = {item["id"] for item in manifest}
        if only_new:
            dialogs = [dialog for dialog in dialogs if dialog.id not in known_ids]
        for index, dialog in enumerate(dialogs, start=1):
            messages = []
            async for message in client.iter_messages(dialog.entity, limit=limit, reverse=True):
                text = message.message or ""
                if not text.strip():
                    continue
                messages.append(
                    {
                        "id": message.id,
                        "date": message.date.isoformat() if message.date else None,
                        "edit_date": (
                            message.edit_date.isoformat() if message.edit_date else None
                        ),
                        "text": text,
                        "views": message.views,
                        "forwards": message.forwards,
                    }
                )

            channel = {
                "id": dialog.id,
                "title": dialog.name,
                "username": getattr(dialog.entity, "username", None),
                "messages": messages,
            }
            filename = f"{abs(dialog.id)}.json"
            (output_dir / filename).write_text(
                json.dumps(channel, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            item = {
                "id": dialog.id,
                "title": dialog.name,
                "username": channel["username"],
                "message_count": len(messages),
                "file": filename,
            }
            manifest = [existing for existing in manifest if existing["id"] != dialog.id]
            manifest.append(item)
            print(f"[{index}/{len(dialogs)}] {dialog.name}: {len(messages)} текстовых")

        manifest_path.write_text(
            json.dumps(sorted(manifest, key=lambda item: item["title"]), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Telegram channel text history")
    parser.add_argument("--output", default="data/telethon/exports")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-new", action="store_true")
    args = parser.parse_args()
    asyncio.run(export_channels(Path(args.output), args.limit, args.only_new))


if __name__ == "__main__":
    main()
