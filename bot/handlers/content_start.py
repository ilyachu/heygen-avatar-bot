from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.keyboards.all_keyboards import get_admin_menu_kb, get_content_main_menu
from domain.auth import AuthService, AuthenticationError

router = Router()


async def _send_web_panel_link(message: Message) -> None:
    if len(settings.WEB_SESSION_SECRET) < 32:
        await message.answer("Веб-панель ещё не настроена администратором.")
        return
    try:
        token = await AuthService(
            settings.DB_PATH, settings.WEB_SESSION_SECRET, settings.WEB_SESSION_DAYS
        ).create_login_token(message.from_user.id)
    except AuthenticationError:
        await message.answer("⛔ У вас нет доступа к веб-панели.")
        return
    url = f"{settings.WEB_BASE_URL.rstrip('/')}/auth/{token}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть веб-панель", url=url)]]
    )
    await message.answer(
        "Ссылка действует 10 минут и только один раз.", reply_markup=keyboard
    )


@router.message(Command("web"))
async def open_web_panel(message: Message) -> None:
    await _send_web_panel_link(message)


@router.message(F.text == "🗓 Контент-план")
async def open_web_panel_button(message: Message) -> None:
    await _send_web_panel_link(message)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    is_admin = message.from_user.id in settings.admin_id_list
    await message.answer(
        "👋 <b>Контент-машина</b>\n\n"
        "Здесь находятся контент-план, согласование, расписание и публикация "
        "постов. Создание кружков и аудио вынесено в отдельный медиабот "
        "(запускается с BOT_MODE=media).",
        reply_markup=get_content_main_menu(is_admin=is_admin),
        parse_mode="HTML",
    )


@router.message(F.text == "👑 Панель администратора")
async def admin_panel(message: Message) -> None:
    if message.from_user.id not in settings.admin_id_list:
        await message.answer("⛔ У вас нет прав администратора.")
        return
    await message.answer(
        "👑 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=get_admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено.")
    await callback.answer("Отменено")
