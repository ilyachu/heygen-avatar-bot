from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

import aiosqlite

from bot.migrations import configure_connection


class MetricsCollectorError(ValueError):
    pass


class AsyncPostMetricsClient(Protocol):
    async def fetch_post_metrics(
        self, telegram_channel_id: str, telegram_message_id: int
    ) -> Mapping[str, Any]: ...


MetricsCallable = Callable[[str, int], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class MetricsCollectionResult:
    attempted: int
    collected: int
    skipped: int
    failed: int
    errors: tuple[tuple[int, str], ...]


DEFAULT_COLLECTION_WINDOWS = (
    ("1h", timedelta(hours=1)),
    ("24h", timedelta(hours=24)),
    ("72h", timedelta(hours=72)),
)


class TelethonMetricsClient:
    """Adapter for an already-authorized Telethon client.

    It never starts a Telegram session and therefore keeps credentials and
    connection lifecycle outside of the metrics domain.
    """

    def __init__(self, client: Any):
        self.client = client

    async def fetch_post_metrics(
        self, telegram_channel_id: str, telegram_message_id: int
    ) -> Mapping[str, Any]:
        try:
            entity: int | str = int(telegram_channel_id)
        except ValueError:
            entity = telegram_channel_id
        message = await self.client.get_messages(entity, ids=telegram_message_id)
        if message is None:
            raise MetricsCollectorError("Telegram message not found")
        reaction_details: dict[str, int] = {}
        reactions = getattr(message, "reactions", None)
        for result in getattr(reactions, "results", None) or ():
            reaction = getattr(result, "reaction", None)
            label = getattr(reaction, "emoticon", None)
            if label is None:
                document_id = getattr(reaction, "document_id", None)
                label = f"custom:{document_id}" if document_id is not None else "unknown"
            count = getattr(result, "count", 0)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                reaction_details[str(label)] = reaction_details.get(str(label), 0) + count
        return {
            "views": _safe_telegram_count(getattr(message, "views", 0)),
            "forwards": _safe_telegram_count(getattr(message, "forwards", 0)),
            "reactions": sum(reaction_details.values()),
            "reactions_detail": reaction_details,
            "provider": "telethon",
            "raw": {
                "message_id": telegram_message_id,
                "channel_id": telegram_channel_id,
            },
        }


class MetricsCollector:
    """Collect published-message metrics through an injected provider client.

    Telegram's Bot API is deliberately not assumed here. A Telethon, analytics,
    or other provider adapter can implement ``fetch_post_metrics``.
    """

    def __init__(
        self,
        db_path: str | Path,
        client: AsyncPostMetricsClient | MetricsCallable,
        *,
        provider: str,
    ):
        if not provider.strip():
            raise MetricsCollectorError("provider must not be empty")
        self.db_path = str(db_path)
        self.client = client
        self.provider = provider.strip()

    async def run_once(
        self,
        *,
        collected_at: datetime | None = None,
        collection_window: str = "",
        limit: int = 100,
        published_before: datetime | None = None,
    ) -> MetricsCollectionResult:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise MetricsCollectorError("limit must be a positive integer")
        now = (collected_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        timestamp = now.isoformat()
        cutoff = (
            published_before.astimezone(timezone.utc).isoformat()
            if published_before is not None
            else None
        )
        posts = await self._eligible_posts(
            collection_window, timestamp, limit, published_before=cutoff
        )
        collected = skipped = failed = 0
        errors: list[tuple[int, str]] = []
        for post in posts:
            try:
                payload = await self._fetch(post)
                metrics = _validate_metrics(payload)
                inserted = await self._insert_snapshot(
                    post["id"], timestamp, collection_window, metrics
                )
                if inserted:
                    collected += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                errors.append((post["id"], f"{type(exc).__name__}: {exc}"))
        return MetricsCollectionResult(
            attempted=len(posts),
            collected=collected,
            skipped=skipped,
            failed=failed,
            errors=tuple(errors),
        )

    async def run_due_windows(
        self,
        *,
        now: datetime | None = None,
        windows: tuple[tuple[str, timedelta], ...] = DEFAULT_COLLECTION_WINDOWS,
        limit_per_window: int = 100,
    ) -> dict[str, MetricsCollectionResult]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        results: dict[str, MetricsCollectionResult] = {}
        seen: set[str] = set()
        for name, age in windows:
            if not name or name in seen:
                raise MetricsCollectorError("collection window names must be unique and non-empty")
            if not isinstance(age, timedelta) or age <= timedelta(0):
                raise MetricsCollectorError("collection window age must be positive")
            seen.add(name)
            results[name] = await self.run_once(
                collected_at=current,
                collection_window=name,
                limit=limit_per_window,
                published_before=current - age,
            )
        return results

    async def _eligible_posts(
        self,
        collection_window: str,
        collected_at: str,
        limit: int,
        *,
        published_before: str | None,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            cutoff_sql = " AND p.published_at <= ?" if published_before else ""
            if collection_window:
                cursor = await db.execute(
                    f"""
                    SELECT p.id, p.telegram_message_id,
                           c.channel_id AS telegram_channel_id
                    FROM posts p JOIN channels c ON c.id = p.channel_id
                    WHERE p.status = 'published'
                      AND p.telegram_message_id IS NOT NULL
                      AND p.telegram_message_id != ''
                      {cutoff_sql}
                      AND NOT EXISTS (
                          SELECT 1 FROM post_metric_snapshots s
                          WHERE s.post_id = p.id AND s.collection_window = ?
                      )
                    ORDER BY p.published_at, p.id LIMIT ?
                    """,
                    (
                        *((published_before,) if published_before else ()),
                        collection_window,
                        limit,
                    ),
                )
            else:
                cursor = await db.execute(
                    f"""
                    SELECT p.id, p.telegram_message_id,
                           c.channel_id AS telegram_channel_id
                    FROM posts p JOIN channels c ON c.id = p.channel_id
                    WHERE p.status = 'published'
                      AND p.telegram_message_id IS NOT NULL
                      AND p.telegram_message_id != ''
                      {cutoff_sql}
                      AND NOT EXISTS (
                          SELECT 1 FROM post_metric_snapshots s
                          WHERE s.post_id = p.id AND s.collected_at = ?
                      )
                    ORDER BY p.published_at, p.id LIMIT ?
                    """,
                    (
                        *((published_before,) if published_before else ()),
                        collected_at,
                        limit,
                    ),
                )
            return [dict(row) for row in await cursor.fetchall()]

    async def _fetch(self, post: dict[str, Any]) -> Mapping[str, Any]:
        try:
            message_id = int(post["telegram_message_id"])
        except (TypeError, ValueError) as exc:
            raise MetricsCollectorError("telegram_message_id must be an integer") from exc
        method = (
            self.client.fetch_post_metrics
            if hasattr(self.client, "fetch_post_metrics")
            else self.client
        )
        result = method(str(post["telegram_channel_id"]), message_id)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _insert_snapshot(
        self,
        post_id: int,
        collected_at: str,
        collection_window: str,
        metrics: dict[str, Any],
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO post_metric_snapshots (
                    post_id, collected_at, collection_window, views, forwards,
                    reactions, reactions_json, provider, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    collected_at,
                    collection_window,
                    metrics["views"],
                    metrics["forwards"],
                    metrics["reactions"],
                    json.dumps(metrics["reactions_detail"], ensure_ascii=False, sort_keys=True),
                    metrics.get("provider") or self.provider,
                    json.dumps(metrics["raw"], ensure_ascii=False, sort_keys=True),
                ),
            )
            await db.commit()
            return cursor.rowcount == 1


def _validate_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MetricsCollectorError("metrics result must be an object")
    allowed = {"views", "forwards", "reactions", "reactions_detail", "provider", "raw"}
    required = {"views", "forwards", "reactions"}
    if not required <= set(value) or not set(value) <= allowed:
        raise MetricsCollectorError("metrics result has an invalid schema")
    result: dict[str, Any] = {}
    for field in ("views", "forwards", "reactions"):
        number = value[field]
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise MetricsCollectorError(f"{field} must be a non-negative integer")
        result[field] = number
    details = value.get("reactions_detail", {})
    raw = value.get("raw", {})
    if not isinstance(details, Mapping) or not isinstance(raw, Mapping):
        raise MetricsCollectorError("reactions_detail and raw must be objects")
    normalized_details: dict[str, int] = {}
    for key, count in details.items():
        if not isinstance(key, str) or not key:
            raise MetricsCollectorError("reaction labels must be non-empty strings")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise MetricsCollectorError("reaction counts must be non-negative integers")
        normalized_details[key] = count
    if normalized_details and sum(normalized_details.values()) != result["reactions"]:
        raise MetricsCollectorError("reactions must equal the reactions_detail total")
    provider = value.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise MetricsCollectorError("provider must be a non-empty string")
    result.update(
        reactions_detail=normalized_details,
        raw=dict(raw),
        provider=provider.strip() if provider else None,
    )
    return result


def _safe_telegram_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
