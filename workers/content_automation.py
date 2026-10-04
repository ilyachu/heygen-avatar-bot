from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import aiosqlite

from bot.migrations import configure_connection
from domain.campaigns import CampaignGenerationService
from domain.content_jobs import (
    JOB_CAMPAIGN,
    JOB_MONTH_PLAN,
    JOB_QUALITY,
    JOB_REVISION,
    JOB_WEBINAR,
    JOB_WEEKLY_USEFUL,
    ContentJobQueue,
)
from domain.content_planning import ContentPlanningService
from domain.content_quality import evaluate_post
from workers.content_jobs import ContentJobCancelled, ContentJobWorker, JobContext

logger = logging.getLogger(__name__)


class ContentAutomation:
    def __init__(
        self,
        db_path: str,
        generator_factory: Callable[[], Any],
        campaign_generator_factory: Callable[[], Any],
        *,
        quality_model: str,
    ):
        self.db_path = db_path
        self.generator_factory = generator_factory
        self.campaign_generator_factory = campaign_generator_factory
        self.quality_model = quality_model

    def handlers(self):
        return {
            JOB_WEEKLY_USEFUL: self.weekly_useful,
            JOB_WEBINAR: self.webinar,
            JOB_MONTH_PLAN: self.month_plan,
            JOB_REVISION: self.revision,
            JOB_CAMPAIGN: self.campaign,
            JOB_QUALITY: self.quality,
        }

    async def weekly_useful(self, job: dict, context: JobContext) -> None:
        week = str(job["payload"]["week_start"])
        month = week[:7]
        service = ContentPlanningService(self.db_path, self.generator_factory())
        await service.ensure_month(month, actor_id=0)
        await self._generate_and_review(
            service,
            week,
            ["useful"],
            actor_id=0,
            context=context,
        )

    async def webinar(self, job: dict, context: JobContext) -> None:
        webinar_id = int(job["payload"]["webinar_id"])
        week = await self._webinar_week(webinar_id)
        service = ContentPlanningService(self.db_path, self.generator_factory())
        await self._generate_and_review(
            service,
            week,
            ["warming", "selling"],
            actor_id=job["payload"].get("actor_id", 0),
            context=context,
        )

    async def month_plan(self, job: dict, context: JobContext) -> None:
        payload = job["payload"]
        month = str(payload["month"])
        week = str(payload["week_start"])
        actor_id = payload.get("actor_id", 0)
        service = ContentPlanningService(self.db_path, self.generator_factory())
        await service.ensure_month(month, actor_id)
        month_posts = await service.list_plan(month, post_type="useful")
        week_posts = await service.list_plan(month, week_start=week, post_type="useful")
        topic_total = len(month_posts)
        week_total = len(
            [post for post in week_posts if post["status"] in {"planned", "draft"}]
        )

        async def topic_progress(completed: int, _total: int, failed: int) -> None:
            await self._progress_or_cancel(
                context,
                total=topic_total + week_total,
                completed=completed,
                failed=failed,
            )

        topic_result = await service.plan_month_topics(
            month, actor_id, progress_callback=topic_progress
        )
        topic_failed = int(topic_result["failed"])

        async def generation_progress(completed: int, _total: int, failed: int) -> None:
            await self._progress_or_cancel(
                context,
                total=topic_total + week_total,
                completed=topic_total - topic_failed + completed,
                failed=topic_failed + failed,
            )

        result = await service.generate_week(
            week,
            actor_id,
            post_types=["useful"],
            progress_callback=generation_progress,
        )
        await self._quality_stage(
            result,
            context,
            completed_before=topic_total + week_total - topic_failed - int(result["failed"]),
            failed_before=topic_failed + int(result["failed"]),
            total_before=topic_total + week_total,
        )

    async def revision(self, job: dict, context: JobContext) -> None:
        payload = job["payload"]
        post_ids = [int(value) for value in payload["post_ids"]]
        service = ContentPlanningService(self.db_path, self.generator_factory())

        async def progress(completed: int, total: int, failed: int) -> None:
            await self._progress_or_cancel(
                context, total=total, completed=completed, failed=failed
            )

        result = await service.revise_posts(
            post_ids,
            str(payload["instruction"]),
            payload.get("actor_id", 0),
            progress_callback=progress,
        )
        await self._quality_stage(
            result,
            context,
            completed_before=len(post_ids) - int(result["failed"]),
            failed_before=int(result["failed"]),
            total_before=len(post_ids),
        )

    async def campaign(self, job: dict, context: JobContext) -> None:
        campaign_id = int(job["payload"]["campaign_id"])
        generator = self.campaign_generator_factory()
        result = await CampaignGenerationService(self.db_path, generator).generate(
            campaign_id
        )
        post_ids = await self._campaign_post_ids(campaign_id)
        await context.report_progress(
            total=len(post_ids), completed=len(post_ids), failed=0
        )
        await self._quality_ids(
            post_ids,
            context,
            completed_before=len(post_ids),
            failed_before=0,
            total_before=len(post_ids),
            generator=self.generator_factory(),
        )
        if result.get("failed"):
            raise RuntimeError("Campaign generation completed with failures")

    async def quality(self, job: dict, context: JobContext) -> None:
        post_ids = [int(value) for value in job["payload"]["post_ids"]]
        try:
            generator = self.generator_factory()
        except Exception:
            generator = None
        await self._quality_ids(
            post_ids,
            context,
            completed_before=0,
            failed_before=0,
            total_before=0,
            generator=generator,
        )

    async def _generate_and_review(
        self,
        service: ContentPlanningService,
        week: str,
        post_types: list[str],
        *,
        actor_id: int,
        context: JobContext,
    ) -> None:
        target_posts = []
        for post_type in post_types:
            target_posts.extend(
                await service.list_plan(week[:7], week_start=week, post_type=post_type)
            )
        target_total = len(
            [post for post in target_posts if post["status"] in {"planned", "draft"}]
        )

        async def progress(completed: int, total: int, failed: int) -> None:
            await self._progress_or_cancel(
                context, total=total, completed=completed, failed=failed
            )

        result = await service.generate_week(
            week,
            actor_id,
            post_types=post_types,
            progress_callback=progress,
        )
        await self._quality_stage(
            result,
            context,
            completed_before=target_total - int(result["failed"]),
            failed_before=int(result["failed"]),
            total_before=target_total,
        )

    async def _quality_stage(
        self,
        generation_result: dict,
        context: JobContext,
        *,
        completed_before: int,
        failed_before: int,
        total_before: int,
    ) -> None:
        ids = [
            int(item["id"])
            for item in generation_result.get("posts", [])
            if item.get("status") == "review"
        ]
        await self._quality_ids(
            ids,
            context,
            completed_before=completed_before,
            failed_before=failed_before,
            total_before=total_before,
            generator=self.generator_factory(),
        )

    async def _quality_ids(
        self,
        post_ids: list[int],
        context: JobContext,
        *,
        completed_before: int,
        failed_before: int,
        total_before: int,
        generator: Any | None,
    ) -> None:
        total = total_before + len(post_ids)
        quality_completed = quality_failed = 0

        async def evaluator(prompt: str, quality_context: dict):
            return await generator.generate(prompt, quality_context)

        for post_id in post_ids:
            if await context.is_cancelled():
                raise ContentJobCancelled
            try:
                try:
                    if generator is None:
                        await evaluate_post(self.db_path, post_id)
                    else:
                        await evaluate_post(
                            self.db_path,
                            post_id,
                            evaluator=evaluator,
                            prompt_version="quality-v1",
                            model=self.quality_model,
                        )
                except Exception:
                    logger.exception(
                        "AI quality review failed for post %s; using deterministic review",
                        post_id,
                    )
                    await evaluate_post(self.db_path, post_id)
                quality_completed += 1
            except Exception:
                quality_failed += 1
                logger.exception("Quality review failed for post %s", post_id)
            await self._progress_or_cancel(
                context,
                total=total,
                completed=completed_before + quality_completed,
                failed=failed_before + quality_failed,
            )

    async def _progress_or_cancel(
        self,
        context: JobContext,
        *,
        total: int,
        completed: int,
        failed: int,
    ) -> None:
        if await context.is_cancelled():
            raise ContentJobCancelled
        await context.report_progress(
            total=max(total, completed + failed),
            completed=completed,
            failed=failed,
        )

    async def _webinar_week(self, webinar_id: int) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            cursor = await db.execute(
                "SELECT week_start FROM webinars WHERE id = ?", (webinar_id,)
            )
            row = await cursor.fetchone()
        if row is None or not row[0]:
            raise ValueError("Webinar has no week")
        return str(row[0])

    async def _campaign_post_ids(self, campaign_id: int) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            cursor = await db.execute(
                "SELECT id FROM posts WHERE webinar_id = ? AND status = 'review' ORDER BY id",
                (campaign_id,),
            )
            return [int(row[0]) for row in await cursor.fetchall()]


async def enqueue_weekly_useful_forever(
    queue: ContentJobQueue, *, interval_seconds: int = 900
) -> None:
    while True:
        try:
            await queue.enqueue_weekly_useful(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weekly useful job enqueue failed")
        await asyncio.sleep(interval_seconds)


def build_content_job_worker(
    db_path: str,
    worker_id: str,
    generator_factory: Callable[[], Any],
    campaign_generator_factory: Callable[[], Any],
    quality_model: str,
) -> ContentJobWorker:
    queue = ContentJobQueue(db_path)
    automation = ContentAutomation(
        db_path,
        generator_factory,
        campaign_generator_factory,
        quality_model=quality_model,
    )
    return ContentJobWorker(queue, worker_id, automation.handlers())
