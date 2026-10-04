from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from bot.services.minimax_service import MINIMAX_VOICES, MINIMAX_EMOTIONS

def get_main_menu() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🎬 Рилс (9:16)")],
        [KeyboardButton(text="🎥 Кружок")],
        [KeyboardButton(text="🎙 Аудио")],
        [KeyboardButton(text="⚙️ Настройки по умолчанию"), KeyboardButton(text="💳 Баланс")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_content_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🗓 Контент-план")],
    ]
    if is_admin:
        buttons.extend([
            [KeyboardButton(text="📣 Пост на все каналы")],
            [KeyboardButton(text="🗂 План 2–3 постов")],
            [KeyboardButton(text="✏️ Подтверждённые планы")],
            [KeyboardButton(text="👑 Панель администратора")],
        ])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_confirmation_kb(output_format: str = "circle") -> InlineKeyboardMarkup:
    if output_format == "stories":
        render_label = "🚀 Сгенерировать рилс (-1 кредит)"
    else:
        render_label = "🚀 Сгенерировать кружок (-1 кредит)"
    buttons = [
        [InlineKeyboardButton(text=render_label, callback_data="confirm_render:prod")],
        [InlineKeyboardButton(text="🧪 Тест с водяным знаком (0 кредитов)", callback_data="confirm_render:test")],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="edit_prompt")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_fsm")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_audio_voices_kb() -> InlineKeyboardMarkup:
    rows = []
    for v_key, v_data in MINIMAX_VOICES.items():
        rows.append([InlineKeyboardButton(
            text=v_data["name"],
            callback_data=f"audio_voice:{v_key}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_user_settings_kb(speed: float = 1.0) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"⚡ Скорость озвучки: {speed}x", callback_data="settings:toggle_speed")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel_fsm")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_minimax_settings_kb(current_speed: float = 1.0, current_emotion: str = "neutral") -> InlineKeyboardMarkup:
    speed_buttons = []
    for sp in [0.9, 1.0, 1.1, 1.2]:
        mark = "• " if sp == current_speed else ""
        speed_buttons.append(InlineKeyboardButton(text=f"{mark}{sp}x", callback_data=f"mm_speed:{sp}"))
    
    emotion_rows = []
    for em_key, em_name in MINIMAX_EMOTIONS.items():
        mark = "✅ " if em_key == current_emotion else ""
        emotion_rows.append([InlineKeyboardButton(text=f"{mark}{em_name}", callback_data=f"mm_emotion:{em_key}")])

    keyboard = [
        speed_buttons,
        *emotion_rows,
        [InlineKeyboardButton(text="✍️ Перейти к вводу текста", callback_data="mm_start_input")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_post_generation_kb(media_type: str, short_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📢 Опубликовать в канал", callback_data=f"publish:{short_id}")],
        [InlineKeyboardButton(text="🔄 Создать ещё", callback_data=f"nav:create_more:{media_type}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_channels_select_kb(channels: list, short_id: str) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        ch_title = ch["title"] or ch["username"] or str(ch["channel_id"])
        buttons.append([InlineKeyboardButton(
            text=f"📢 {ch_title}",
            callback_data=f"pub_to:{short_id}:{ch['channel_id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Ввести username канала (@...)", callback_data=f"pub_custom:{short_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_publish_options_kb(short_id: str, channel_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🚀 Опубликовать сразу (без текста)", callback_data=f"do_pub_direct:{short_id}:{channel_id}")],
        [InlineKeyboardButton(text="✍️ Добавить сопроводительный текст", callback_data=f"do_pub_text:{short_id}:{channel_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="👥 Белый список ID", callback_data="admin:list_users")],
        [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="admin:add_user")],
        [InlineKeyboardButton(text="➖ Удалить сотрудника", callback_data="admin:del_user")],
        [InlineKeyboardButton(text="📢 Управление каналами", callback_data="admin:list_channels")],
        [InlineKeyboardButton(text="📣 Пост на все каналы", callback_data="broadcast:start")],
        [InlineKeyboardButton(text="🗂 План 2–3 постов", callback_data="broadcast:batch_start")],
        [InlineKeyboardButton(text="✏️ Подтверждённые планы", callback_data="broadcast:manage_batches")],
        [InlineKeyboardButton(text="📋 Последние логи сервера", callback_data="admin:view_logs")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_broadcast_photo_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Без медиа", callback_data="broadcast:skip_photo")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])


def get_broadcast_batch_size_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="2 поста", callback_data="broadcast:batch_size:2")],
        [InlineKeyboardButton(text="3 поста", callback_data="broadcast:batch_size:3")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])


def get_broadcast_batches_kb(batches: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for batch in batches:
        when = batch["first_publish_at"]
        buttons.append([InlineKeyboardButton(
            text=f"🗂 {when} · {batch['slot_count']} поста",
            callback_data=f"broadcast:manage_batch:{batch['batch_key']}",
        )])
    buttons.append([InlineKeyboardButton(text="🔙 В админ-меню", callback_data="admin:back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_broadcast_batch_manage_kb(batch_key: str, slots: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for slot in slots:
        if not slot.get("editable", False):
            continue
        slot_number = slot["slot"]
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️ Текст #{slot_number}",
                callback_data=f"broadcast:edit_text:{batch_key}:{slot_number}",
            ),
            InlineKeyboardButton(
                text=f"🕓 Время #{slot_number}",
                callback_data=f"broadcast:edit_time:{batch_key}:{slot_number}",
            ),
        ])
    buttons.extend([
        [InlineKeyboardButton(
            text="🗑 Отменить весь план",
            callback_data=f"broadcast:cancel_batch_prompt:{batch_key}",
        )],
        [InlineKeyboardButton(text="🔙 К списку планов", callback_data="broadcast:manage_batches")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_broadcast_batch_cancel_kb(batch_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗑 Да, отменить план",
            callback_data=f"broadcast:cancel_batch:{batch_key}",
        )],
        [InlineKeyboardButton(
            text="🔙 Не отменять",
            callback_data=f"broadcast:manage_batch:{batch_key}",
        )],
    ])


def get_broadcast_channels_kb(channels: list, selected_ids: set[int]) -> InlineKeyboardMarkup:
    buttons = []
    for channel in channels:
        title = channel["title"] or channel["username"] or str(channel["channel_id"])
        mark = "✅ " if channel["id"] in selected_ids else "⬜️ "
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark}{title[:45]}",
                callback_data=f"broadcast:toggle:{channel['id']}",
            )
        ])
    buttons.extend([
        [InlineKeyboardButton(text="➡️ Продолжить", callback_data="broadcast:channels_done")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_broadcast_timing_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="broadcast:now")],
        [InlineKeyboardButton(text="🗓 Запланировать", callback_data="broadcast:schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])


def get_broadcast_confirm_kb(
    callback_data: str = "broadcast:confirm",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=callback_data)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])

def get_direct_text_action_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎬 Создать рилс (9:16)", callback_data="direct_action:stories")],
        [InlineKeyboardButton(text="🎥 Создать кружок", callback_data="direct_action:circle")],
        [InlineKeyboardButton(text="🎙 Озвучить голосом (Аудио)", callback_data="direct_action:audio")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
