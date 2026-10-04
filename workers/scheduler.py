from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.migrations import configure_connection
from bot.services.publishing import PublishingService

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


class SchedulerService:
    def __init__(self, db_path: str, bot, approval_user_id: int, web_base_url: str):
        self.db_path = db_path
        self.bot = bot
        self.approval_user_id = approval_user_id
        self.web_base_url = web_base_url.rstrip("/")

    async def run_once(self, now: datetime | None = None) -> int:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        await self._mark_stale_publications(now)
        published = await self._publish_scheduled(now)
        post_ids = await self._claim_due(now)
        sent = 0
        for post_id in post_ids:
            post = await self._get_post(post_id)
            try:
                message = await self.bot.send_message(
                    chat_id=self.approval_user_id,
                    text=_notification_text(post),
                    parse_mode="HTML",
                    reply_markup=_approval_keyboard(post_id, self.web_base_url),
                )
                await self._mark_sent(post_id, message.chat.id, message.message_id)
                sent += 1
            except Exception as exc:
                logger.exception("Failed to notify for post %s", post_id)
                await self._mark_failed(post_id, type(exc).__name__)
        return published + sent

    async def _publish_scheduled(self, now: datetime) -> int:
        post_ids = await self._claim_scheduled(now)
        published = 0
        publisher = PublishingService(self.bot)
        for post_id in post_ids:
            post = await self._get_post(post_id)
            try:
                media_type = post.get("media_type") or "text"
                if media_type == "text":
                    message = await publisher.publish_text(
                        channel_id=post["telegram_channel_id"],
                        text=post["body"],
                    )
                else:
                    message = await publisher.publish_media(
                        channel_id=post["telegram_channel_id"],
                        media_type=media_type,
                        file_id=post.get("media_file_id") or "",
                        text=post.get("body") or None,
                    )
                await self._mark_published(
                    post_id, getattr(message, "message_id", None)
                )
                published += 1
                try:
                    await self.bot.send_message(
                        chat_id=self.approval_user_id,
                        text=_published_notification(post),
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception(
                        "Post %s was published but the summary notification failed",
                        post_id,
                    )
            except Exception as exc:
                logger.exception("Scheduled publication failed for post %s", post_id)
                await self._mark_publish_failed(post_id, type(exc).__name__)
        return published

    async def _claim_scheduled(self, now: datetime) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT p.id FROM posts p
                JOIN channels c ON c.id = p.channel_id
                WHERE p.status = 'scheduled' AND p.publish_at IS NOT NULL
                  AND p.publish_at <= ? AND p.body != '' AND c.is_active = 1
                ORDER BY p.publish_at LIMIT 20
                """,
                (now.isoformat(),),
            )
            rows = await cursor.fetchall()
            claimed: list[int] = []
            for row in rows:
                update = await db.execute(
                    "UPDATE posts SET status = 'publishing', published_by = 0, claim_until = ?, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND status = 'scheduled'",
                    ((now + timedelta(minutes=5)).isoformat(), row["id"]),
                )
                if update.rowcount != 1:
                    continue
                claimed.append(row["id"])
                await db.execute(
                    """
                    INSERT INTO post_events (
                        post_id, event_type, from_status, to_status,
                        actor_type, actor_id
                    ) VALUES (?, 'publish_claimed', 'scheduled', 'publishing',
                              'worker', 'scheduler')
                    """,
                    (row["id"],),
                )
            await db.commit()
            return claimed

    async def _mark_stale_publications(self, now: datetime) -> None:
        """Quarantine ambiguous sends after a worker crash; never retry blindly."""

        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT id FROM posts
                WHERE status = 'publishing' AND claim_until IS NOT NULL
                  AND claim_until <= ?
                """,
                (now.isoformat(),),
            )
            rows = await cursor.fetchall()
            for row in rows:
                await db.execute(
                    """
                    UPDATE posts SET status = 'publish_failed',
                        publish_error = 'ambiguous_delivery_after_worker_restart',
                        claim_until = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'publishing'
                    """,
                    (row["id"],),
                )
                await db.execute(
                    """
                    INSERT INTO post_events (
                        post_id, event_type, from_status, to_status,
                        actor_type, actor_id, details_json
                    ) VALUES (?, 'publish_failed', 'publishing', 'publish_failed',
                              'worker', 'scheduler',
                              '{"error":"ambiguous_delivery_after_worker_restart"}')
                    """,
                    (row["id"],),
                )
            await db.commit()

    async def run_forever(self, interval_seconds: int = 20) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler iteration failed")
            await asyncio.sleep(interval_seconds)

    async def _claim_due(self, now: datetime) -> list[int]:
        approval_horizon = now + timedelta(hours=24)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT id, status FROM posts
                WHERE status IN ('review', 'postponed') AND publish_at IS NOT NULL
                  AND publish_at <= ? AND body != ''
                ORDER BY publish_at LIMIT 20
                """,
                (approval_horizon.isoformat(),),
            )
            rows = await cursor.fetchall()
            claimed = []
            for row in rows:
                update = await db.execute(
                    "UPDATE posts SET status = 'notifying' WHERE id = ? AND status = ?",
                    (row["id"], row["status"]),
                )
                if update.rowcount == 1:
                    claimed.append(row["id"])
                    await db.execute(
                        """
                        INSERT INTO post_events (
                            post_id, event_type, from_status, to_status,
                            actor_type, actor_id
                        ) VALUES (?, 'notification_claimed', ?, 'notifying', 'worker', 'scheduler')
                        """,
                        (row["id"], row["status"]),
                    )
            await db.commit()
            return claimed

    async def _get_post(self, post_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT p.*, c.title AS channel_title,
                       c.channel_id AS telegram_channel_id,
                       w.title AS campaign_title
                FROM posts p JOIN channels c ON c.id = p.channel_id
                LEFT JOIN webinars w ON w.id = p.webinar_id WHERE p.id = ?
                """,
                (post_id,),
            )
            return dict(await cursor.fetchone())

    async def _mark_published(
        self, post_id: int, telegram_message_id: int | None
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE posts SET status = 'published', published_at = ?,
                    telegram_message_id = ?, publish_error = NULL,
                    claim_until = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'publishing'
                """,
                (datetime.now(timezone.utc).isoformat(), telegram_message_id, post_id),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """
                    INSERT INTO post_events (
                        post_id, event_type, from_status, to_status,
                        actor_type, actor_id
                    ) VALUES (?, 'published', 'publishing', 'published',
                              'worker', 'scheduler')
                    """,
                    (post_id,),
                )
            await db.commit()

    async def _mark_publish_failed(self, post_id: int, error_code: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE posts SET status = 'publish_failed', publish_error = ?,
                    claim_until = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'publishing'
                """,
                (error_code, post_id),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """
                    INSERT INTO post_events (
                        post_id, event_type, from_status, to_status,
                        actor_type, actor_id, details_json
                    ) VALUES (?, 'publish_failed', 'publishing', 'publish_failed',
                              'worker', 'scheduler', ?)
                    """,
                    (post_id, json.dumps({"error": error_code})),
                )
            await db.commit()

    async def _mark_sent(self, post_id: int, chat_id: int, message_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE posts SET status = 'notification_sent', notified_at = ?,
                    notification_chat_id = ?, notification_message_id = ?,
                    notification_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'notifying'
                """,
                (datetime.now(timezone.utc).isoformat(), str(chat_id), message_id, post_id),
            )
            await db.execute(
                """
                INSERT INTO post_events (
                    post_id, event_type, from_status, to_status,
                    actor_type, actor_id
                ) VALUES (?, 'notification_sent', 'notifying', 'notification_sent',
                          'worker', 'scheduler')
                """,
                (post_id,),
            )
            await db.commit()

    async def _mark_failed(self, post_id: int, error_code: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM post_events
                WHERE post_id = ? AND event_type = 'notification_failed'
                """,
                (post_id,),
            )
            previous_failures = (await cursor.fetchone())[0]
            target_status = "notification_failed" if previous_failures >= 2 else "review"
            retry_detail = (
                '{"retry":"stopped"}'
                if target_status == "notification_failed"
                else '{"retry":"scheduled"}'
            )
            await db.execute(
                """
                UPDATE posts SET status = ?, notification_error = ?,
                    publish_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'notifying'
                """,
                (
                    target_status,
                    error_code,
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    post_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO post_events (
                    post_id, event_type, from_status, to_status,
                    actor_type, actor_id, details_json
                ) VALUES (?, 'notification_failed', 'notifying', ?,
                          'worker', 'scheduler', ?)
                """,
                (post_id, target_status, retry_detail),
            )
            await db.commit()


def _notification_text(post: dict) -> str:
    preview = html.escape((post.get("body") or "")[:500])
    channel = html.escape(post.get("channel_title") or "")
    campaign = html.escape(post.get("campaign_title") or "Без запуска")
    topic = html.escape(post.get("topic") or post.get("post_type") or "Пост")
    scheduled = _display_time(post.get("publish_at"))
    post_type = {
        "useful": "полезный",
        "warming": "прогревающий",
        "selling": "продающий",
    }.get(post.get("post_type"), post.get("post_type") or "пост")
    return (
        f"<b>Нужен апрув поста для канала «{channel}»</b>\n"
        f"Запуск: {campaign}\n"
        f"Тип: {post_type}\n"
        f"Запланировано: {scheduled}\n"
        f"Тема: {topic}\n\n{preview}"
    )


def _published_notification(post: dict) -> str:
    channel = html.escape(post.get("channel_title") or "")
    topic = html.escape(post.get("topic") or post.get("post_type") or "Пост")
    return f"✅ <b>Опубликовано в «{channel}»</b>\n{topic}"


def _approval_keyboard(post_id: int, web_base_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Одобрить и запланировать", callback_data=f"content:approve:{post_id}")],
            [
                InlineKeyboardButton(text="Отложить на час", callback_data=f"content:hour:{post_id}"),
                InlineKeyboardButton(text="На завтра", callback_data=f"content:day:{post_id}"),
            ],
            [InlineKeyboardButton(text="Открыть в вебе", url=f"{web_base_url}/editor/posts/{post_id}")],
            [InlineKeyboardButton(text="Отменить", callback_data=f"content:cancel:{post_id}")],
        ]
    )


def _display_time(value: str | None) -> str:
    if not value:
        return "время не задано"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
