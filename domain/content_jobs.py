from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from bot.migrations import configure_connection


JOB_WEEKLY_USEFUL = "weekly_useful_generation"
JOB_WEBINAR = "webinar_generation"
JOB_MONTH_PLAN = "month_plan_generation"
JOB_REVISION = "post_revision"
JOB_CAMPAIGN = "campaign_generation"
JOB_QUALITY = "quality_review"
MOSCOW = ZoneInfo("Europe/Moscow")


class ContentJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobSpec:
    job_type: str
    idempotency_key: str
    payload: dict[str, Any]
    total_items: int = 0
    max_attempts: int = 3


def weekly_useful_job_spec(now: datetime) -> JobSpec | None:
    """Return the stable next-week job during the Moscow Fri-Sun catch-up window."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(MOSCOW)
    if local_now.weekday() not in (4, 5, 6):  # Friday through Sunday
        return None
    next_week = local_now.date() + timedelta(days=7 - local_now.weekday())
    return JobSpec(
        job_type=JOB_WEEKLY_USEFUL,
        idempotency_key=f"weekly-useful:{next_week.isoformat()}",
        payload={
            "week_start": next_week.isoformat(),
            "post_types": ["useful"],
            "trigger": "weekly_auto",
        },
    )


def webinar_job_spec(webinar_id: int, *, max_attempts: int = 3) -> JobSpec:
    """Build the only job type allowed to generate warming/selling posts."""

    if webinar_id <= 0:
        raise ValueError("webinar_id must be positive")
    return JobSpec(
        job_type=JOB_WEBINAR,
        idempotency_key=f"webinar:{webinar_id}:warming-selling",
        payload={
            "webinar_id": webinar_id,
            "post_types": ["warming", "selling"],
            "trigger": "explicit_webinar",
        },
        max_attempts=max_attempts,
    )


class ContentJobQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def enqueue_spec(self, spec: JobSpec) -> dict[str, Any]:
        return await self.enqueue(
            spec.job_type,
            spec.payload,
            idempotency_key=spec.idempotency_key,
            total_items=spec.total_items,
            max_attempts=spec.max_attempts,
        )

    async def enqueue_weekly_useful(self, now: datetime) -> dict[str, Any] | None:
        spec = weekly_useful_job_spec(now)
        return None if spec is None else await self.enqueue_spec(spec)

    async def enqueue_webinar(self, webinar_id: int, *, max_attempts: int = 3) -> dict[str, Any]:
        return await self.enqueue_spec(webinar_job_spec(webinar_id, max_attempts=max_attempts))

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        total_items: int = 0,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> dict[str, Any]:
        _validate_generation_job(job_type, payload)
        if not job_type.strip() or not idempotency_key.strip():
            raise ValueError("job_type and idempotency_key are required")
        if total_items < 0 or max_attempts < 1:
            raise ValueError("invalid progress or retry limits")
        now = datetime.now(timezone.utc)
        available = _utc(available_at or now)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT OR IGNORE INTO content_jobs (
                    job_type, idempotency_key, payload_json, total_items,
                    max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_type,
                    idempotency_key,
                    encoded,
                    total_items,
                    max_attempts,
                    available.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = await _fetch_one(
                db, "SELECT * FROM content_jobs WHERE idempotency_key = ?", (idempotency_key,)
            )
            await db.commit()
        return _job(row)

    async def get(self, job_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            row = await _fetch_one(db, "SELECT * FROM content_jobs WHERE id = ?", (job_id,))
        return None if row is None else _job(row)

    async def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM content_jobs ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [_job(row) for row in rows]

    async def claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 1:
            raise ValueError("worker_id and a positive lease are required")
        claimed_at = _utc(now or datetime.now(timezone.utc))
        lease_until = claimed_at + timedelta(seconds=lease_seconds)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await self._recover_in_transaction(db, claimed_at)
            row = await _fetch_one(
                db,
                """
                SELECT * FROM content_jobs
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY available_at, id LIMIT 1
                """,
                (claimed_at.isoformat(),),
            )
            if row is None:
                await db.commit()
                return None
            update = await db.execute(
                """
                UPDATE content_jobs SET status = 'running', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (
                    worker_id,
                    lease_until.isoformat(),
                    claimed_at.isoformat(),
                    claimed_at.isoformat(),
                    row["id"],
                ),
            )
            if update.rowcount != 1:
                await db.rollback()
                return None
            claimed = await _fetch_one(db, "SELECT * FROM content_jobs WHERE id = ?", (row["id"],))
            await db.commit()
        return _job(claimed)

    async def recover_expired_leases(self, now: datetime | None = None) -> int:
        recovered_at = _utc(now or datetime.now(timezone.utc))
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            count = await self._recover_in_transaction(db, recovered_at)
            await db.commit()
        return count

    async def _recover_in_transaction(self, db: aiosqlite.Connection, now: datetime) -> int:
        failed = await db.execute(
            """
            UPDATE content_jobs SET status = 'failed', lease_owner = NULL,
                lease_expires_at = NULL, last_error = 'lease_expired_max_attempts',
                completed_at = ?, updated_at = ?
            WHERE status = 'running' AND lease_expires_at <= ?
              AND attempt_count >= max_attempts
            """,
            (now.isoformat(), now.isoformat(), now.isoformat()),
        )
        queued = await db.execute(
            """
            UPDATE content_jobs SET status = 'queued', lease_owner = NULL,
                lease_expires_at = NULL, last_error = 'lease_expired',
                available_at = ?, updated_at = ?
            WHERE status = 'running' AND lease_expires_at <= ?
              AND attempt_count < max_attempts
            """,
            (now.isoformat(), now.isoformat(), now.isoformat()),
        )
        return failed.rowcount + queued.rowcount

    async def heartbeat(
        self, job_id: int, worker_id: str, *, lease_seconds: int = 300
    ) -> bool:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            result = await db.execute(
                """
                UPDATE content_jobs SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), job_id, worker_id),
            )
            await db.commit()
        return result.rowcount == 1

    async def set_progress(
        self,
        job_id: int,
        worker_id: str,
        *,
        total_items: int,
        completed_items: int,
        failed_items: int,
        lease_seconds: int = 300,
    ) -> bool:
        if min(total_items, completed_items, failed_items) < 0 or lease_seconds < 1:
            raise ValueError("progress cannot be negative")
        if completed_items + failed_items > total_items:
            raise ValueError("progress cannot exceed total_items")
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            result = await db.execute(
                """
                UPDATE content_jobs SET total_items = ?, completed_items = ?,
                    failed_items = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    total_items,
                    completed_items,
                    failed_items,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    job_id,
                    worker_id,
                ),
            )
            await db.commit()
        return result.rowcount == 1

    async def complete(self, job_id: int, worker_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            result = await db.execute(
                """
                UPDATE content_jobs SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (now, now, job_id, worker_id),
            )
            await db.commit()
        return result.rowcount == 1

    async def fail(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        *,
        retry_delay_seconds: int = 30,
    ) -> str | None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await _fetch_one(
                db,
                "SELECT attempt_count, max_attempts FROM content_jobs "
                "WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (job_id, worker_id),
            )
            if row is None:
                await db.rollback()
                return None
            retry = row["attempt_count"] < row["max_attempts"]
            status = "queued" if retry else "failed"
            await db.execute(
                """
                UPDATE content_jobs SET status = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?, available_at = ?,
                    completed_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    status,
                    error[:2000],
                    (now + timedelta(seconds=max(0, retry_delay_seconds))).isoformat(),
                    None if retry else now.isoformat(),
                    now.isoformat(),
                    job_id,
                ),
            )
            await db.commit()
        return status

    async def cancel(self, job_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            result = await db.execute(
                """
                UPDATE content_jobs SET status = 'cancelled', lease_owner = NULL,
                    lease_expires_at = NULL, cancelled_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (now, now, job_id),
            )
            await db.commit()
        return result.rowcount == 1

    async def retry(self, job_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            result = await db.execute(
                """
                UPDATE content_jobs SET status = 'queued', attempt_count = 0,
                    completed_items = 0, failed_items = 0,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = NULL, available_at = ?, completed_at = NULL,
                    cancelled_at = NULL, updated_at = ?
                WHERE id = ? AND status IN ('failed', 'cancelled')
                """,
                (now, now, job_id),
            )
            await db.commit()
        return result.rowcount == 1


async def _fetch_one(db: aiosqlite.Connection, query: str, params: tuple[Any, ...]):
    cursor = await db.execute(query, params)
    return await cursor.fetchone()


def _job(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_generation_job(job_type: str, payload: dict[str, Any]) -> None:
    post_types = set(payload.get("post_types") or [])
    if {"warming", "selling"} & post_types and job_type != JOB_WEBINAR:
        raise ContentJobError("warming/selling require an explicit webinar job")
    if job_type == JOB_WEBINAR:
        if not isinstance(payload.get("webinar_id"), int) or payload["webinar_id"] <= 0:
            raise ContentJobError("webinar jobs require webinar_id")
        if not post_types or not post_types <= {"warming", "selling"}:
            raise ContentJobError("webinar jobs may generate only warming/selling posts")
    if job_type == JOB_WEEKLY_USEFUL and post_types != {"useful"}:
        raise ContentJobError("weekly jobs may generate only useful posts")
