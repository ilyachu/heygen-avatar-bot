from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from bot.migrations import configure_connection


@dataclass(frozen=True)
class DeliveryResult:
    changed: bool
    status: str
    message: str
    post: dict[str, Any] | None = None


class DeliveryWorkflow:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def claim_publish(self, post_id: int, actor_id: int) -> DeliveryResult:
        async with _transaction(self.db_path) as db:
            post = await _post_with_channel(db, post_id)
            if post is None:
                return DeliveryResult(False, "missing", "Пост не найден")
            if post["status"] != "notification_sent":
                return DeliveryResult(False, post["status"], _state_message(post["status"]), post)
            cursor = await db.execute(
                """
                UPDATE posts SET status = 'publishing', published_by = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'notification_sent'
                """,
                (actor_id, post_id),
            )
            if cursor.rowcount != 1:
                current = await _post_with_channel(db, post_id)
                return DeliveryResult(False, current["status"], _state_message(current["status"]), current)
            await _event(db, post_id, "publish_claimed", "notification_sent", "publishing", actor_id)
            post["status"] = "publishing"
            return DeliveryResult(True, "publishing", "Публикую…", post)

    async def mark_published(
        self, post_id: int, telegram_message_id: int | None
    ) -> DeliveryResult:
        async with _transaction(self.db_path) as db:
            post = await _post_with_channel(db, post_id)
            if post is None or post["status"] != "publishing":
                status = post["status"] if post else "missing"
                return DeliveryResult(False, status, _state_message(status), post)
            await db.execute(
                """
                UPDATE posts SET status = 'published', published_at = ?,
                    telegram_message_id = ?, publish_error = NULL,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'publishing'
                """,
                (_utc_now().isoformat(), telegram_message_id, post_id),
            )
            await _event(db, post_id, "published", "publishing", "published", post["published_by"])
            return DeliveryResult(True, "published", "Опубликовано", post)

    async def mark_publish_failed(self, post_id: int, error_code: str) -> DeliveryResult:
        async with _transaction(self.db_path) as db:
            post = await _post_with_channel(db, post_id)
            if post is None or post["status"] != "publishing":
                status = post["status"] if post else "missing"
                return DeliveryResult(False, status, _state_message(status), post)
            await db.execute(
                """
                UPDATE posts SET status = 'publish_failed', publish_error = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (error_code, post_id),
            )
            await _event(db, post_id, "publish_failed", "publishing", "publish_failed", post["published_by"])
            return DeliveryResult(True, "publish_failed", "Ошибка публикации", post)

    async def postpone(self, post_id: int, actor_id: int, *, days: int = 0, hours: int = 0) -> DeliveryResult:
        async with _transaction(self.db_path) as db:
            post = await _post_with_channel(db, post_id)
            if post is None:
                return DeliveryResult(False, "missing", "Пост не найден")
            if post["status"] != "notification_sent":
                return DeliveryResult(False, post["status"], _state_message(post["status"]), post)
            current = _parse_utc(post["publish_at"]) if post["publish_at"] else _utc_now()
            base = max(current, _utc_now())
            new_time = base + timedelta(days=days, hours=hours)
            await db.execute(
                """
                UPDATE posts SET status = 'postponed', publish_at = ?, notified_at = NULL,
                    notification_chat_id = NULL, notification_message_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'notification_sent'
                """,
                (new_time.isoformat(), post_id),
            )
            await _event(
                db,
                post_id,
                "postponed",
                "notification_sent",
                "postponed",
                actor_id,
                {"publish_at": new_time.isoformat()},
            )
            post["publish_at"] = new_time.isoformat()
            return DeliveryResult(True, "postponed", "Публикация отложена", post)

    async def cancel(self, post_id: int, actor_id: int) -> DeliveryResult:
        async with _transaction(self.db_path) as db:
            post = await _post_with_channel(db, post_id)
            if post is None:
                return DeliveryResult(False, "missing", "Пост не найден")
            if post["status"] == "cancelled":
                return DeliveryResult(False, "cancelled", "Публикация уже отменена", post)
            if post["status"] in {"published", "publishing"}:
                return DeliveryResult(False, post["status"], _state_message(post["status"]), post)
            from_status = post["status"]
            await db.execute(
                "UPDATE posts SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (post_id,),
            )
            await _event(db, post_id, "cancelled", from_status, "cancelled", actor_id)
            return DeliveryResult(True, "cancelled", "Публикация отменена", post)


async def _post_with_channel(db: aiosqlite.Connection, post_id: int):
    cursor = await db.execute(
        """
        SELECT p.*, c.channel_id AS telegram_channel_id, c.title AS channel_title,
               w.title AS campaign_title
        FROM posts p JOIN channels c ON c.id = p.channel_id
        LEFT JOIN webinars w ON w.id = p.webinar_id WHERE p.id = ?
        """,
        (post_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _event(
    db,
    post_id: int,
    event_type: str,
    from_status: str,
    to_status: str,
    actor_id: int | None,
    details: dict[str, Any] | None = None,
):
    await db.execute(
        """
        INSERT INTO post_events (
            post_id, event_type, from_status, to_status,
            actor_type, actor_id, details_json
        ) VALUES (?, ?, ?, ?, 'user', ?, ?)
        """,
        (
            post_id,
            event_type,
            from_status,
            to_status,
            None if actor_id is None else str(actor_id),
            json.dumps(details or {}, ensure_ascii=False),
        ),
    )


class _transaction:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def __aenter__(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await configure_connection(self.db)
        await self.db.execute("BEGIN IMMEDIATE")
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            await (self.db.commit() if exc_type is None else self.db.rollback())
        finally:
            await self.db.close()


def _state_message(status: str) -> str:
    return {
        "published": "Пост уже опубликован",
        "publishing": "Публикация уже выполняется",
        "postponed": "Пост уже отложен",
        "cancelled": "Публикация отменена",
        "publish_failed": "Предыдущая публикация завершилась ошибкой",
    }.get(status, f"Текущее состояние: {status}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
