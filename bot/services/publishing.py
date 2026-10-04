from __future__ import annotations

from typing import Protocol


class TelegramPublisher(Protocol):
    async def send_message(self, **kwargs): ...

    async def send_photo(self, **kwargs): ...

    async def send_video(self, **kwargs): ...

    async def send_video_note(self, **kwargs): ...

    async def send_voice(self, **kwargs): ...


class PublishingService:
    """Telegram delivery shared by immediate and scheduled publication flows."""

    def __init__(self, bot: TelegramPublisher):
        self.bot = bot

    async def publish_text(
        self,
        *,
        channel_id: str,
        text: str,
        parse_mode: str | None = "HTML",
    ):
        if not text.strip():
            raise ValueError("Post text is empty")
        return await self.bot.send_message(
            chat_id=channel_id, text=text, parse_mode=parse_mode
        )

    async def publish_media(
        self,
        *,
        channel_id: str,
        media_type: str,
        file_id: str,
        text: str | None = None,
    ) -> None:
        if media_type == "video_note":
            if text:
                await self.bot.send_message(
                    chat_id=channel_id, text=text, parse_mode="HTML"
                )
            await self.bot.send_video_note(chat_id=channel_id, video_note=file_id)
            return
        if media_type == "voice":
            if text and len(text) > 1024:
                raise ValueError("Voice caption exceeds Telegram's 1024 character limit")
            kwargs = {"chat_id": channel_id, "voice": file_id}
            if text:
                kwargs.update({"caption": text, "parse_mode": "HTML"})
            await self.bot.send_voice(**kwargs)
            return
        if media_type == "photo":
            if text and len(text) > 1024:
                raise ValueError("Photo caption exceeds Telegram's 1024 character limit")
            kwargs = {"chat_id": channel_id, "photo": file_id}
            if text:
                kwargs["caption"] = text
            return await self.bot.send_photo(**kwargs)
        if media_type == "video":
            if text and len(text) > 1024:
                raise ValueError("Video caption exceeds Telegram's 1024 character limit")
            kwargs = {"chat_id": channel_id, "video": file_id}
            if text:
                kwargs["caption"] = text
            return await self.bot.send_video(**kwargs)
        raise ValueError(f"Unsupported media type: {media_type}")
