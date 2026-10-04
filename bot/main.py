import os
import sys
import asyncio
import logging
import traceback
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher
from aiogram.types.error_event import ErrorEvent
from bot.config import settings
from bot.db import init_db
from bot.storage import SqliteStorage
from bot.middlewares.whitelist import WhitelistMiddleware
from bot.middlewares.logging_middleware import UserActivityMiddleware
from bot.handlers import (
    admin,
    audio,
    avatar,
    broadcast,
    content_approval,
    content_fallback,
    content_start,
    fallback,
    publish,
    start,
)
from bot.services.notifier import notify_admins_error
from domain.campaigns import OpenAICompatibleGenerator
from domain.content_jobs import ContentJobQueue
from domain.content_planning import OpenAIPlanningGenerator
from workers.content_automation import (
    ContentAutomation,
    enqueue_weekly_useful_forever,
)
from workers.content_jobs import ContentJobWorker
from workers.metrics_runtime import collect_metrics_forever
from workers.scheduler import SchedulerService

os.makedirs("data", exist_ok=True)

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(
    settings.log_path,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[console_handler, file_handler]
)
logger = logging.getLogger("bot")


def router_modules_for_mode(mode: str):
    if mode == "content":
        return (
            content_start,
            content_approval,
            broadcast,
            admin,
            content_fallback,
        )
    if mode == "media":
        return (start, avatar, audio, publish, fallback)
    raise ValueError(f"Unsupported bot mode: {mode}")


def scheduler_enabled_for_mode(mode: str) -> bool:
    return mode == "content"


def validate_bot_identity(mode: str, actual_id: int, expected_id: int) -> None:
    if expected_id and actual_id != expected_id:
        raise RuntimeError(
            f"Configured {mode} bot token has id {actual_id}, expected {expected_id}"
        )


async def main():
    logger.info("Initializing database...")
    await init_db()

    storage = SqliteStorage(settings.DB_PATH)
    await storage.init()

    bot = Bot(token=settings.BOT_TOKEN)
    me = await bot.get_me()
    try:
        validate_bot_identity(settings.BOT_MODE, me.id, settings.expected_bot_id)
    except Exception:
        await bot.session.close()
        raise
    logger.info("Verified %s bot identity: @%s (%s)", settings.BOT_MODE, me.username, me.id)
    dp = Dispatcher(storage=storage)

    # 1. Activity Logging Middleware (logs every message/callback with user info)
    dp.message.outer_middleware(UserActivityMiddleware())
    dp.callback_query.outer_middleware(UserActivityMiddleware())

    # 2. Whitelist Middleware
    dp.message.outer_middleware(WhitelistMiddleware())
    dp.callback_query.outer_middleware(WhitelistMiddleware())

    # 3. Global Unhandled Exception Handler
    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        exception = event.exception
        logger.exception(f"Unhandled exception in update {event.update.update_id}: {exception}")
        tb = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))

        user_id = None
        username = None
        context_desc = "Обновление Telegram"
        if event.update.message and event.update.message.from_user:
            user_id = event.update.message.from_user.id
            username = event.update.message.from_user.username
            context_desc = f"Сообщение: {event.update.message.text[:50] if event.update.message.text else 'медиа'}"
        elif event.update.callback_query and event.update.callback_query.from_user:
            user_id = event.update.callback_query.from_user.id
            username = event.update.callback_query.from_user.username
            context_desc = f"Callback: {event.update.callback_query.data}"

        await notify_admins_error(
            bot=bot,
            title=f"Неперехваченное исключение ({context_desc})",
            error=tb,
            user_id=user_id,
            username=username,
            extra_info={"Update ID": event.update.update_id}
        )

        try:
            if event.update.callback_query:
                await event.update.callback_query.answer(
                    "⚠️ Произошла ошибка. Администраторы уже уведомлены.",
                    show_alert=True
                )
            elif event.update.message:
                await event.update.message.answer(
                    "⚠️ <b>Произошла непредвиденная ошибка.</b>\n"
                    "Администраторы бота уже автоматически получили уведомление с деталями сбоя.",
                    parse_mode="HTML"
                )
        except Exception:
            pass

    # 4. Mode-specific handlers (fallback is always last)
    for module in router_modules_for_mode(settings.BOT_MODE):
        dp.include_router(module.router)

    logger.info("Starting %s bot polling (retaining pending updates)...", settings.BOT_MODE)
    await bot.delete_webhook(drop_pending_updates=False)

    runtime_tasks = []
    if scheduler_enabled_for_mode(settings.BOT_MODE):
        runtime_tasks.append(asyncio.create_task(
            SchedulerService(
                settings.DB_PATH, bot, settings.approval_user_id, settings.WEB_BASE_URL
            ).run_forever(settings.SCHEDULER_INTERVAL_SECONDS)
        ))
        queue = ContentJobQueue(settings.DB_PATH)
        automation = ContentAutomation(
            settings.DB_PATH,
            lambda: OpenAIPlanningGenerator(
                settings.CONTENT_LLM_BASE_URL,
                settings.CONTENT_LLM_API_KEY,
                settings.CONTENT_LLM_MODEL,
            ),
            lambda: OpenAICompatibleGenerator(
                settings.CONTENT_LLM_BASE_URL,
                settings.CONTENT_LLM_API_KEY,
                settings.CONTENT_LLM_MODEL,
            ),
            quality_model=settings.CONTENT_LLM_MODEL or "deterministic",
        )

        async def notify_content_job(job, status):
            if job["job_type"] == "quality_review":
                return
            labels = {
                "weekly_useful_generation": "полезные посты следующей недели",
                "webinar_generation": "прогрев и продажи",
                "month_plan_generation": "контент-план месяца",
                "post_revision": "редактура постов",
                "campaign_generation": "кампания",
                "quality_review": "проверка качества",
            }
            icon = "✅" if status == "completed" else "⚠️"
            await bot.send_message(
                settings.approval_user_id,
                f"{icon} AI-задача «{labels.get(job['job_type'], job['job_type'])}»: "
                f"{status}. Откройте /web для просмотра результата.",
            )

        content_worker = ContentJobWorker(
            queue,
            f"content-bot:{me.id}",
            automation.handlers(),
            on_finished=notify_content_job,
        )
        runtime_tasks.extend([
            asyncio.create_task(
                content_worker.run_forever(idle_seconds=settings.CONTENT_JOB_IDLE_SECONDS)
            ),
            asyncio.create_task(
                enqueue_weekly_useful_forever(
                    queue,
                    interval_seconds=settings.WEEKLY_ENQUEUE_INTERVAL_SECONDS,
                )
            ),
            asyncio.create_task(
                collect_metrics_forever(
                    settings.DB_PATH,
                    session_path=settings.TELETHON_SESSION_PATH,
                    api_id=settings.TELEGRAM_API_ID,
                    api_hash=settings.TELEGRAM_API_HASH,
                    interval_seconds=settings.METRICS_INTERVAL_SECONDS,
                )
            ),
        ])
    try:
        await dp.start_polling(bot)
    finally:
        for task in runtime_tasks:
            task.cancel()
        if runtime_tasks:
            await asyncio.gather(*runtime_tasks, return_exceptions=True)
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
