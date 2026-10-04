import os
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.config import settings
from bot.db import (
    get_all_whitelisted, add_to_whitelist, remove_from_whitelist,
    get_all_channels, add_channel, remove_channel, get_recent_generations
)
from bot.services.heygen_service import heygen_client
from bot.keyboards.all_keyboards import get_admin_menu_kb

logger = logging.getLogger(__name__)
router = Router()

class AdminChannelState(StatesGroup):
    waiting_for_channel = State()

class AdminUserState(StatesGroup):
    waiting_for_add_user = State()

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_list

@router.message(Command("whitelist"))
async def cmd_whitelist(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    users = await get_all_whitelisted()
    if not users:
        await message.answer("Белый список пуст.")
        return

    lines = ["👥 <b>Белый список пользователей:</b>\n"]
    for u in users:
        admin_tag = "👑 [Админ]" if u["user_id"] in settings.admin_id_list else "👤 [Сотрудник]"
        lines.append(f"• <code>{u['user_id']}</code> | {admin_tag} {u['full_name']} (@{u['username']})")

    lines.append("\n<i>Добавить: <code>/add_user &lt;id&gt; [Имя]</code>\nУдалить: <code>/del_user &lt;id&gt;</code></i>")
    await message.answer("\n".join(lines), parse_mode="HTML")

@router.message(Command("add_user"))
async def cmd_add_user(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("⚠️ Использование: <code>/add_user &lt;user_id&gt; [Имя сотрудника]</code>", parse_mode="HTML")
        return

    new_id = int(parts[1])
    name = parts[2] if len(parts) > 2 else "Сотрудник"

    success = await add_to_whitelist(
        user_id=new_id,
        full_name=name,
        added_by=message.from_user.id
    )

    if success:
        await message.answer(f"✅ Пользователь <code>{new_id}</code> ({name}) успешно добавлен в белый список!", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка при добавлении в базу данных.")

@router.message(Command("del_user"))
async def cmd_del_user(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("⚠️ Использование: <code>/del_user &lt;user_id&gt;</code>", parse_mode="HTML")
        return

    target_id = int(parts[1])
    if target_id in settings.admin_id_list:
        await message.answer("❌ Нельзя удалить корневого администратора.")
        return

    success = await remove_from_whitelist(target_id)
    if success:
        await message.answer(f"✅ Пользователь <code>{target_id}</code> удален из белого списка.", parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось удалить пользователя.")

@router.callback_query(F.data == "admin:list_users")
async def cb_admin_list_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return

    users = await get_all_whitelisted()
    lines = ["👥 <b>Белый список пользователей:</b>\n"]
    for u in users:
        admin_tag = "👑 [Админ]" if u["user_id"] in settings.admin_id_list else "👤 [Сотрудник]"
        lines.append(f"• <code>{u['user_id']}</code> | {admin_tag} {u['full_name']} (@{u['username']})")

    lines.append("\n<i>Команды:\n<code>/add_user &lt;id&gt; [Имя]</code>\n<code>/del_user &lt;id&gt;</code></i>")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin:add_user")
async def cb_admin_add_user_hint(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminUserState.waiting_for_add_user)
    await callback.message.answer(
        "➕ <b>Добавление сотрудника</b>\n\n"
        "Отправьте Telegram ID и имя одним сообщением:\n"
        "<code>123456789 Иван Маркетинг</code>\n\n"
        "Пользователь увидит свой ID, если напишет боту до получения доступа.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AdminUserState.waiting_for_add_user)
async def receive_admin_add_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Недостаточно прав.")
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer(
            "⚠️ Сначала укажите числовой Telegram ID, затем имя.\n"
            "Пример: <code>123456789 Иван Маркетинг</code>",
            parse_mode="HTML",
        )
        return

    new_id = int(parts[0])
    if new_id <= 0 or new_id > 9_223_372_036_854_775_807:
        await message.answer("⚠️ Telegram ID указан неверно.")
        return

    name = parts[1].strip()[:100] if len(parts) > 1 else "Сотрудник"
    if not name:
        name = "Сотрудник"
    success = await add_to_whitelist(
        user_id=new_id,
        full_name=name,
        added_by=message.from_user.id,
    )
    if not success:
        await message.answer("❌ Ошибка при добавлении в базу данных.")
        return

    await state.clear()
    await message.answer(
        f"✅ <b>{name}</b> добавлен.\n"
        f"Telegram ID: <code>{new_id}</code>\n\n"
        "Теперь сотрудник может отправить боту /start и затем /web.",
        reply_markup=get_admin_menu_kb(),
        parse_mode="HTML",
    )

@router.callback_query(F.data == "admin:del_user")
async def cb_admin_del_user_hint(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await callback.message.answer(
        "➖ <b>Удаление сотрудника</b>\n\n"
        "Отправьте команду:\n"
        "<code>/del_user &lt;user_id&gt;</code>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "admin:back_to_menu")
async def cb_admin_back_to_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=get_admin_menu_kb(),
        parse_mode="HTML"
    )
    await callback.answer()

# ---- Channels Management ----

async def _build_channels_view():
    channels = await get_all_channels()
    if not channels:
        text = (
            "📢 <b>Привязанные каналы для публикации</b>\n\n"
            "Список пуст.\n\n"
            "Чтобы подключить канал:\n"
            "1. Добавьте бота в администраторы канала с правом публикации сообщений.\n"
            "2. Нажмите <b>«➕ Привязать канал»</b> ниже или отправьте <code>/add_channel @username</code>.\n"
            "<i>(Также канал автоматически сохранится при первой публикации из кружка или голосового)</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Привязать канал", callback_data="admin:add_channel_prompt")],
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")]
        ])
        return text, kb

    lines = ["📢 <b>Привязанные каналы для публикаций:</b>\n"]
    buttons = []
    for ch in channels:
        title = ch["title"] or ch["username"] or str(ch["channel_id"])
        uname = f" (@{ch['username']})" if ch["username"] else ""
        lines.append(f"• <b>{title}</b>{uname} | <code>{ch['channel_id']}</code>")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Удалить: {title[:20]}",
            callback_data=f"admin:del_channel:{ch['channel_id']}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Привязать канал", callback_data="admin:add_channel_prompt")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(lines), kb

@router.callback_query(F.data == "admin:list_channels")
async def cb_admin_list_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return

    text, kb = await _build_channels_view()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.message(Command("channels"))
async def cmd_channels(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    text, kb = await _build_channels_view()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("admin:del_channel:"))
async def cb_admin_del_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return

    channel_id = callback.data.split(":")[2]
    await remove_channel(channel_id)
    await callback.answer("Канал удален", show_alert=False)

    text, kb = await _build_channels_view()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "admin:add_channel_prompt")
async def cb_admin_add_channel_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return

    await state.set_state(AdminChannelState.waiting_for_channel)
    await callback.message.answer(
        "📢 <b>Привязка нового канала</b>\n\n"
        "1. Убедитесь, что бот добавлен в администраторы канала с правом публикации.\n"
        "2. Отправьте ответным сообщением <b>@username канала</b> (например, <code>@my_channel</code>) "
        "или перешлите любой пост из канала:",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(AdminChannelState.waiting_for_channel)
async def process_admin_add_channel(message: Message, state: FSMContext, bot: Bot):
    channel_input = message.text.strip() if message.text else ""
    if message.forward_from_chat:
        channel_input = str(message.forward_from_chat.id)

    if not channel_input:
        await message.answer("⚠️ Не удалось распознать канал. Пожалуйста, отправьте @username канала.")
        return

    try:
        chat = await bot.get_chat(channel_input)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await message.answer(
                f"❌ Бот найден в канале <b>{chat.title}</b>, но не является администратором.\n"
                "Выдайте боту права администратора и попробуйте снова.",
                parse_mode="HTML"
            )
            return

        await add_channel(
            channel_id=str(chat.id),
            title=chat.title or "",
            username=chat.username or "",
            added_by=message.from_user.id
        )
        await state.clear()
        await message.answer(
            f"✅ Канал <b>{chat.title}</b> успешно привязан!\n\n"
            f"Теперь любой созданный кружок или аудиозапись можно опубликовать в него в 1 клик.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error adding channel in admin: {e}")
        await message.answer(
            f"❌ Не удалось привязать канал <code>{channel_input}</code>:\n\n"
            f"<i>{e}</i>\n\n"
            "Убедитесь, что канал существует и бот назначен администратором.",
            parse_mode="HTML"
        )

@router.message(Command("add_channel"))
async def cmd_add_channel(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ Использование: <code>/add_channel @username_канала</code>", parse_mode="HTML")
        return

    ch_input = parts[1].strip()
    try:
        chat = await bot.get_chat(ch_input)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await message.answer(f"❌ Бот не администратор в <b>{chat.title}</b>.", parse_mode="HTML")
            return

        await add_channel(str(chat.id), chat.title or "", chat.username or "", message.from_user.id)
        await message.answer(f"✅ Канал <b>{chat.title}</b> успешно добавлен в список!", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка добавления канала: {e}")

@router.message(Command("del_channel"))
async def cmd_del_channel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ Использование: <code>/del_channel &lt;channel_id&gt;</code>", parse_mode="HTML")
        return

    ch_id = parts[1].strip()
    await remove_channel(ch_id)
    await message.answer(f"✅ Канал <code>{ch_id}</code> удален.")

@router.callback_query(F.data == "admin:check_balance")
async def cb_admin_check_balance(callback: CallbackQuery):
    info = await heygen_client.get_balance_info()
    credits = info.get("credits", 0)
    plan_credits = info.get("plan_credits", 0)
    wallet = info.get("wallet_usd", 0.0)

    text = (
        f"💳 <b>Текущий баланс HeyGen:</b>\n\n"
        f"• Доступно API кредитов: <code>{credits}</code>\n"
        f"• План-кредитов: <code>{plan_credits}</code>\n"
        f"• Баланс кошелька: <code>${wallet:.2f}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.message(Command("logs"))
async def cmd_logs(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return

    log_path = settings.log_path
    if not os.path.exists(log_path):
        await message.answer(f"📋 Файл логов {log_path} пока не создан.")
        return

    parts = message.text.strip().split()
    if len(parts) > 1 and parts[1].lower() in ["full", "file", "all"]:
        doc = FSInputFile(log_path, filename="bot.log")
        await message.answer_document(doc, caption="📋 Полный файл логов сервера bot.log")
        return

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last_lines = "".join(lines[-35:]) if lines else "Лог пуст."
        if len(last_lines) > 3500:
            last_lines = last_lines[-3500:]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать полный файл логов", callback_data="admin:dl_log")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:view_logs")]
        ])
        await message.answer(
            f"📋 <b>Последние 35 строк логов сервера:</b>\n\n<pre><code>{last_lines}</code></pre>",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения логов: {e}")

@router.callback_query(F.data == "admin:dl_log")
async def cb_admin_dl_log(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    log_path = settings.log_path
    if os.path.exists(log_path):
        doc = FSInputFile(log_path, filename="bot.log")
        await callback.message.answer_document(doc, caption="📋 Полный файл логов сервера bot.log")
        await callback.answer()
    else:
        await callback.answer("Файл логов пока не создан", show_alert=True)

@router.callback_query(F.data == "admin:view_logs")
async def cb_admin_view_logs(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    log_path = settings.log_path
    if not os.path.exists(log_path):
        await callback.answer("Файл логов пока не создан", show_alert=True)
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last_lines = "".join(lines[-30:]) if lines else "Лог пуст."
        if len(last_lines) > 3500:
            last_lines = last_lines[-3500:]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать полный файл", callback_data="admin:dl_log")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:view_logs")],
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")]
        ])
        await callback.message.edit_text(
            f"📋 <b>Последние строки логов:</b>\n\n<pre><code>{last_lines}</code></pre>",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)

@router.callback_query(F.data == "admin:recent_gens")
async def cb_admin_recent_gens(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    gens = await get_recent_generations(limit=10)
    if not gens:
        await callback.message.edit_text(
            "📊 История генераций пуста.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")]
            ])
        )
        await callback.answer()
        return

    lines = ["📊 <b>Последние 10 генераций:</b>\n"]
    for g in gens:
        st_icon = "✅" if g["status"] == "completed" else ("❌" if g["status"] == "failed" else "⏳")
        prompt_snippet = (g["prompt"][:40] + "...") if g["prompt"] and len(g["prompt"]) > 40 else (g["prompt"] or "")
        created = str(g["created_at"])[:16]
        lines.append(f"• {st_icon} <b>#{g['id']}</b> ({g['gen_type']}) | User: <code>{g['user_id']}</code> | {created}\n  <i>«{prompt_snippet}»</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:recent_gens")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin:back_to_menu")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()
