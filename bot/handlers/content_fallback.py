import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import settings
from bot.keyboards.all_keyboards import get_content_main_menu

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.text)
async def fallback_text_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        logger.warning(
            "Unhandled content-bot text from %s in state %s",
            message.from_user.id,
            current_state,
        )
        await message.answer(
            "⚠️ Ожидается другое действие. Следуйте подсказке выше или "
            "отправьте /start для возврата в меню."
        )
        return
    await message.answer(
        "Используйте кнопку «🗓 Контент-план» или отправьте /web.",
        reply_markup=get_content_main_menu(
            is_admin=message.from_user.id in settings.admin_id_list
        ),
    )


@router.message()
async def fallback_any_handler(message: Message) -> None:
    await message.answer("Для возврата в контент-меню отправьте /start.")
