from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from bot.migrations import configure_connection


class PostWorkflowError(ValueError):
    pass


class PostNotFoundError(PostWorkflowError):
    pass


class ConcurrentPostUpdateError(PostWorkflowError):
    pass


@dataclass(frozen=True)
class Actor:
    actor_type: str
    actor_id: str | int | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise PostWorkflowError("Datetime must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


class PostWorkflow:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def approve(
        self,
        post_id: int,
        actor: Actor,
        *,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        async with self._transaction() as db:
            post = await self._get_post(db, post_id)
            self._check_version(post, expected_version)
            if post["status"] != "review":
                raise PostWorkflowError("Only posts in review can be approved")
            if not post["body"].strip() or not post["channel_id"]:
                raise PostWorkflowError("Post requires body and channel")
            if not post["publish_at"]:
                raise PostWorkflowError("Post requires a publication time")
            publish_at = datetime.fromisoformat(post["publish_at"])
            if publish_at.tzinfo is None or publish_at.astimezone(timezone.utc) <= now:
                raise PostWorkflowError("Publication time must be in the future")

            await self._update(
                db,
                post,
                actor,
                "approved",
                "scheduled",
                {"approved_by": actor.actor_id},
            )
            return await self._get_post(db, post_id)

    async def edit(
        self,
        post_id: int,
        actor: Actor,
        *,
        body: str | None = None,
        channel_id: int | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return await self.edit_content(
            post_id,
            actor,
            body=body,
            channel_id=channel_id,
            expected_version=expected_version,
        )

    async def edit_content(
        self,
        post_id: int,
        actor: Actor,
        *,
        body: str | None = None,
        channel_id: int | None = None,
        topic: str | None = None,
        post_type: str | None = None,
        publish_at: datetime | None = None,
        include_webinar_link: bool | None = None,
        requires_link: bool | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if all(
            value is None
            for value in (
                body, channel_id, topic, post_type, publish_at,
                include_webinar_link, requires_link,
            )
        ):
            raise PostWorkflowError("No post changes supplied")
        async with self._transaction() as db:
            post = await self._get_post(db, post_id)
            self._check_version(post, expected_version)
            if post["status"] in {"published", "publishing", "cancelled"}:
                raise PostWorkflowError("Published, publishing or cancelled posts cannot be edited")
            values: dict[str, Any] = {}
            if body is not None:
                values["body"] = body
            if channel_id is not None:
                values["channel_id"] = channel_id
            if topic is not None:
                values["topic"] = topic
            if post_type is not None:
                values["post_type"] = post_type
            if publish_at is not None:
                values["publish_at"] = _as_utc_iso(publish_at)
            if include_webinar_link is not None:
                values["include_webinar_link"] = int(include_webinar_link)
            if requires_link is not None:
                if requires_link and include_webinar_link is False:
                    raise PostWorkflowError("A required link must be included")
                values["requires_link"] = int(requires_link)
            content_changed = any(
                value is not None and value != post[field]
                for field, value in (
                    ("body", body),
                    ("channel_id", channel_id),
                    ("topic", topic),
                    ("post_type", post_type),
                    ("include_webinar_link", None if include_webinar_link is None else int(include_webinar_link)),
                    ("requires_link", None if requires_link is None else int(requires_link)),
                )
            )
            date_changed = publish_at is not None and _as_utc_iso(publish_at) != post["publish_at"]
            target_status = post["status"]
            if content_changed and post["status"] in {
                "planned",
                "draft",
                "scheduled",
                "notification_sent",
                "generation_failed",
                "notification_failed",
                "publish_failed",
            }:
                target_status = "review"
            elif date_changed and post["status"] == "notification_sent":
                target_status = "postponed"
            if post["status"] == "notification_sent" and (content_changed or date_changed):
                values.update(
                    {
                        "notified_at": None,
                        "notification_chat_id": None,
                        "notification_message_id": None,
                    }
                )
            await self._update(db, post, actor, "edited", target_status, values)
            return await self._get_post(db, post_id)

    async def reschedule(
        self,
        post_id: int,
        publish_at: datetime,
        actor: Actor,
        *,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        if publish_at.tzinfo is None or publish_at.astimezone(timezone.utc) <= now:
            raise PostWorkflowError("Publication time must be in the future")
        async with self._transaction() as db:
            post = await self._get_post(db, post_id)
            self._check_version(post, expected_version)
            if post["status"] in {"published", "publishing", "cancelled"}:
                raise PostWorkflowError("Published, publishing or cancelled posts cannot be rescheduled")
            await self._update(
                db,
                post,
                actor,
                "rescheduled",
                post["status"],
                {"publish_at": _as_utc_iso(publish_at)},
            )
            return await self._get_post(db, post_id)

    async def cancel(
        self,
        post_id: int,
        actor: Actor,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        async with self._transaction() as db:
            post = await self._get_post(db, post_id)
            self._check_version(post, expected_version)
            if post["status"] == "published":
                raise PostWorkflowError("Published posts cannot be cancelled")
            if post["status"] == "cancelled":
                return post
            await self._update(db, post, actor, "cancelled", "cancelled", {})
            return await self._get_post(db, post_id)

    def _transaction(self):
        return _Transaction(self.db_path)

    @staticmethod
    async def _get_post(db: aiosqlite.Connection, post_id: int) -> dict[str, Any]:
        cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cursor.fetchone()
        if row is None:
            raise PostNotFoundError(f"Post {post_id} does not exist")
        return dict(row)

    @staticmethod
    def _check_version(post: dict[str, Any], expected_version: int | None) -> None:
        if expected_version is not None and post["version"] != expected_version:
            raise ConcurrentPostUpdateError("Post was changed by another request")

    @staticmethod
    async def _update(
        db: aiosqlite.Connection,
        post: dict[str, Any],
        actor: Actor,
        event_type: str,
        target_status: str,
        values: dict[str, Any],
    ) -> None:
        from_status = post["status"]
        updates = {**values, "status": target_status}
        if event_type == "approved":
            updates["approved_by"] = actor.actor_id
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = [*updates.values(), post["id"], post["version"]]
        cursor = await db.execute(
            f"UPDATE posts SET {assignments}, version = version + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND version = ?",
            params,
        )
        if cursor.rowcount != 1:
            raise ConcurrentPostUpdateError("Post was changed by another request")
        await db.execute(
            """
            INSERT INTO post_events (
                post_id, event_type, from_status, to_status,
                actor_type, actor_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post["id"],
                event_type,
                from_status,
                target_status,
                actor.actor_type,
                None if actor.actor_id is None else str(actor.actor_id),
                json.dumps(values, ensure_ascii=False, default=str),
            ),
        )


class _Transaction:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await configure_connection(self.db)
        await self.db.execute("BEGIN IMMEDIATE")
        return self.db

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        assert self.db is not None
        try:
            if exc_type is None:
                await self.db.commit()
            else:
                await self.db.rollback()
        finally:
            await self.db.close()
