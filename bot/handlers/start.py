from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from bot.config import settings
from bot.keyboards.all_keyboards import get_main_menu, get_user_settings_kb
from bot.services.heygen_service import heygen_client
from bot.db import get_user_settings, update_user_settings

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        f"👋 <b>Медиабот «{settings.HEYGEN_AVATAR_NAME}»</b>\n\n"
        "Здесь создаются видео и аудио:\n\n"
        "1️⃣ <b>Рилс (9:16)</b> — вертикальное видео для Reels.\n"
        "2️⃣ <b>Кружок</b> — нативное круглое видео Telegram.\n"
        "3️⃣ <b>Аудио</b> — озвучка голосом.\n\n"
        "<i>💡 Для максимальной безопасности баланса перед каждым рендером видео "
        "бот рассчитывает хронометраж и запрашивает подтверждение.</i>"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu(), parse_mode="HTML")


@router.message(F.text == "💳 Баланс")
async def show_balance(message: Message):
    wait_msg = await message.answer("⏳ Проверяю баланс HeyGen API...")
    info = await heygen_client.get_balance_info()
    credits = info.get("credits", 0)
    plan_credits = info.get("plan_credits", 0)
    wallet = info.get("wallet_usd", 0.0)
    email = info.get("email", "")

    text = (
        f"💳 <b>Состояние аккаунта HeyGen:</b>\n\n"
        f"• <b>API Кредиты:</b> <code>{credits}</code> шт.\n"
        f"• <b>План-кредиты:</b> <code>{plan_credits}</code> шт.\n"
        f"• <b>Баланс кошелька:</b> <code>${wallet:.2f}</code>\n"
        f"• <b>Аккаунт:</b> <code>{email}</code>\n\n"
        f"<i>1 кружок Telegram (до 60 сек) расходует ровно 1 кредит HeyGen.</i>"
    )
    await wait_msg.edit_text(text, parse_mode="HTML")


@router.message(F.text == "⚙️ Настройки по умолчанию")
async def show_user_settings(message: Message):
    pref = await get_user_settings(message.from_user.id)
    spd = pref.get("speed", 1.0)
    text = (
        "⚙️ <b>Настройки по умолчанию</b>\n\n"
        f"• <b>Спикер:</b> {settings.HEYGEN_AVATAR_NAME}\n"
        f"• <b>Скорость озвучки:</b> {spd}x\n\n"
        "<i>Нажмите на кнопку ниже, чтобы переключить параметр:</i>"
    )
    await message.answer(text, reply_markup=get_user_settings_kb(speed=spd), parse_mode="HTML")


@router.callback_query(F.data == "settings:toggle_speed")
async def toggle_default_speed(callback: CallbackQuery):
    pref = await get_user_settings(callback.from_user.id)
    cur_spd = pref.get("speed", 1.0)
    speeds = [0.8, 0.9, 1.0, 1.1, 1.2]
    try:
        idx = speeds.index(cur_spd)
        new_spd = speeds[(idx + 1) % len(speeds)]
    except ValueError:
        new_spd = 1.0

    await update_user_settings(callback.from_user.id, speed=new_spd)
    text = (
        "⚙️ <b>Настройки по умолчанию</b>\n\n"
        f"• <b>Спикер:</b> {settings.HEYGEN_AVATAR_NAME}\n"
        f"• <b>Скорость озвучки:</b> {new_spd}x\n\n"
        "<i>Нажмите на кнопку ниже, чтобы переключить параметр:</i>"
    )
    await callback.message.edit_text(text, reply_markup=get_user_settings_kb(speed=new_spd), parse_mode="HTML")
    await callback.answer(f"Скорость: {new_spd}x")


@router.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено.")
    await callback.answer("Отменено")
