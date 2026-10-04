#!/usr/bin/env python3
"""Render a 9:16 reel for the configured HeyGen avatar and send it to a Telegram chat."""
import argparse
import asyncio
import logging

from aiogram import Bot
from aiogram.types import FSInputFile

from bot.config import settings
from bot.services.heygen_service import heygen_client
from bot.services.video_processor import download_file, convert_to_stories_video, safe_remove

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("render_and_send")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Render and send a HeyGen reel (9:16)")
    parser.add_argument("--text", required=True, help="Script text for the video")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat/user id to send the video to")
    parser.add_argument("--test", action="store_true", help="Use HeyGen test mode (0 credits, watermark)")
    args = parser.parse_args()

    avatar_id = settings.HEYGEN_AVATAR_ID.strip()
    voice_id = settings.HEYGEN_DEFAULT_VOICE_ID.strip()
    if not avatar_id or not voice_id:
        logger.error("Set HEYGEN_AVATAR_ID and HEYGEN_DEFAULT_VOICE_ID in .env first.")
        return 1

    success, video_id = await heygen_client.create_avatar_video(
        avatar_id=avatar_id,
        voice_id=voice_id,
        text=args.text,
        background={"type": "color", "value": "#1E1E2E"},
        avatar_type=settings.HEYGEN_AVATAR_TYPE,
        test_mode=args.test,
        output_format="stories",
    )
    if not success or not video_id:
        logger.error("Failed to create video in HeyGen: %s", video_id)
        return 1

    logger.info("Video task created in HeyGen: %s. Waiting for rendering...", video_id)
    status, video_url, error = await heygen_client.poll_video_status(video_id, max_wait_sec=600)
    if status != "completed" or not video_url:
        logger.error("HeyGen rendering failed: status=%s error=%s", status, error)
        return 1

    raw_path = f"{settings.TEMP_DIR}/raw_{video_id[:8]}.mp4"
    if not await download_file(video_url, raw_path):
        logger.error("Failed to download rendered video")
        return 1

    ok, processed_path, err = await convert_to_stories_video(raw_path)
    if not ok or not processed_path:
        logger.error("FFmpeg processing failed: %s", err)
        safe_remove(raw_path)
        return 1

    bot = Bot(token=settings.BOT_TOKEN)
    try:
        await bot.send_video(
            chat_id=args.chat_id,
            video=FSInputFile(processed_path),
            caption="🎬 Готовый рилс 9:16.",
        )
        logger.info("Video delivered to chat %s", args.chat_id)
    finally:
        await bot.session.close()
        safe_remove(raw_path)
        safe_remove(processed_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
