import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from bot.handlers.avatar import auto_select_and_continue
from bot.handlers.audio import AudioState
from bot.keyboards.all_keyboards import (
    get_main_menu, get_direct_text_action_kb, get_audio_voices_kb
)

logger = logging.getLogger(__name__)
router = Router()

@router.callback_query(F.data == "direct_action:circle")
async def direct_action_circle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("saved_prompt")
    if not text:
        await callback.message.edit_text("⚠️ Текст не найден. Пожалуйста, отправьте текст снова или нажмите /start.")
        await state.clear()
        return

    await state.update_data(output_format="circle")
    await auto_select_and_continue(callback, state)
    await callback.answer()


@router.callback_query(F.data == "direct_action:stories")
async def direct_action_stories(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("saved_prompt")
    if not text:
        await callback.message.edit_text("⚠️ Текст не найден. Пожалуйста, отправьте текст снова или нажмите /start.")
        await state.clear()
        return

    await state.update_data(output_format="stories")
    await auto_select_and_continue(callback, state)
    await callback.answer()


@router.callback_query(F.data == "direct_action:audio")
async def direct_action_audio(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("saved_prompt")
    if not text:
        await callback.message.edit_text("⚠️ Текст не найден. Пожалуйста, отправьте текст снова или нажмите /start.")
        await state.clear()
        return

    await state.set_state(AudioState.choosing_voice)
    await callback.message.edit_text(
        "🎙 <b>Генерация голосового сообщения</b>\n\n"
        "Шаг 1 из 2: Выберите спикера для озвучки сохранённого текста:",
        reply_markup=get_audio_voices_kb(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(F.text)
async def fallback_text_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        logger.warning(f"Unhandled text from {message.from_user.id} in state {current_state}: {message.text[:50]}")
        await message.answer(
            "⚠️ <b>Ожидается другое действие</b>\n\n"
            "Пожалуйста, следуйте кнопкам в сообщении выше или нажмите /start для возврата в главное меню.",
            parse_mode="HTML"
        )
        return

    text = message.text.strip()
    words = text.split()

    if len(words) >= 3:
        logger.info(f"User {message.from_user.id} provided direct prompt ({len(words)} words)")
        await state.update_data(saved_prompt=text)
        preview_text = text if len(text) < 180 else text[:180] + "..."
        await message.answer(
            f"📝 <b>Вы отправили текст ({len(words)} слов):</b>\n\n"
            f"<i>«{preview_text}»</i>\n\n"
            f"Выберите, что вы хотите с ним сделать:",
            reply_markup=get_direct_text_action_kb(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "👋 <b>Главное меню</b>\n\n"
            "Чтобы создать рилс, кружок или озвучить текст, воспользуйтесь кнопками меню ниже или просто пришлите готовый текст в чат:",
            reply_markup=get_main_menu(),
            parse_mode="HTML"
        )

@router.message()
async def fallback_any_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    logger.info(f"Unhandled non-text message from {message.from_user.id}, content_type={message.content_type}, state={current_state}")
    await message.answer(
        "ℹ️ Для работы с ботом отправьте текст или воспользуйтесь кнопками меню.\n"
        "Для сброса и возврата в главное меню нажмите /start."
    )
