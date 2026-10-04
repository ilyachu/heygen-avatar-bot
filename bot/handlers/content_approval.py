import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot.services.publishing import PublishingService
from bot.config import settings
from domain.content_planning import ContentPlanningError, ContentPlanningService
from domain.delivery import DeliveryWorkflow

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("content:"))
async def handle_content_action(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректный пост", show_alert=True)
        return
    _, action, raw_post_id = parts
    post_id = int(raw_post_id)
    actor_id = callback.from_user.id
    workflow = DeliveryWorkflow(settings.DB_PATH)

    if action == "approve":
        try:
            approved = await ContentPlanningService(settings.DB_PATH).approve_posts(
                [post_id], actor_id
            )
            message_text = (
                "Одобрено и запланировано"
                if approved["approved"]
                else "Пост уже запланирован"
            )
        except ContentPlanningError:
            message_text = "Не удалось одобрить: проверьте время и обязательную ссылку в вебе"
        await callback.message.edit_text(
            f"Пост №{post_id}: <b>{message_text}</b>", parse_mode="HTML"
        )
        await callback.answer(message_text, show_alert="Не удалось" in message_text)
        return
    if action == "publish":
        result = await workflow.claim_publish(post_id, actor_id)
        if result.changed:
            try:
                message = await PublishingService(bot).publish_text(
                    channel_id=result.post["telegram_channel_id"],
                    text=result.post["body"],
                )
                result = await workflow.mark_published(post_id, message.message_id)
            except Exception as exc:
                logger.exception("Content publication failed for post %s", post_id)
                result = await workflow.mark_publish_failed(post_id, type(exc).__name__)
    elif action == "hour":
        result = await workflow.postpone(post_id, actor_id, hours=1)
    elif action == "day":
        result = await workflow.postpone(post_id, actor_id, days=1)
    elif action == "cancel":
        result = await workflow.cancel(post_id, actor_id)
    else:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    await callback.message.edit_text(f"Пост №{post_id}: <b>{result.message}</b>", parse_mode="HTML")
    await callback.answer(result.message)
