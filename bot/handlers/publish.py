import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.db import get_all_channels, add_channel
from bot.services.media_cache import get_media
from bot.services.publishing import PublishingService
from bot.keyboards.all_keyboards import (
    get_channels_select_kb, get_publish_options_kb
)

logger = logging.getLogger(__name__)
router = Router()

class PublishState(StatesGroup):
    entering_custom_channel = State()
    entering_post_text = State()

@router.callback_query(F.data.startswith("publish:"))
async def handle_publish_click(callback: CallbackQuery, state: FSMContext):
    short_id = callback.data.split(":", 1)[1]
    media = get_media(short_id)

    if not media:
        data = await state.get_data()
        media_type = data.get("pub_media_type", "video_note")
        file_id = data.get("pub_file_id", "")
    else:
        media_type = media["media_type"]
        file_id = media["file_id"]

    await state.update_data(pub_short_id=short_id, pub_media_type=media_type, pub_file_id=file_id)
    channels = await get_all_channels()

    if not channels:
        await state.set_state(PublishState.entering_custom_channel)
        await callback.message.answer(
            "📢 <b>Публикация в канал</b>\n\n"
            "В боте пока нет привязанных каналов.\n"
            "Пожалуйста, отправьте <b>@username канала</b> (например, <code>@my_channel</code>) "
            "или перешлите любой пост из него:\n\n"
            "<i>(Важно: бот должен быть добавлен в администраторы канала с правом публикации)</i>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    await callback.message.answer(
        "📢 <b>Выберите канал для публикации:</b>",
        reply_markup=get_channels_select_kb(channels, short_id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("pub_to:"))
async def handle_target_channel_selected(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    short_id = parts[1]
    channel_id = parts[2]

    media = get_media(short_id)
    if media:
        await state.update_data(
            pub_short_id=short_id,
            pub_channel_id=channel_id,
            pub_media_type=media["media_type"],
            pub_file_id=media["file_id"]
        )
    else:
        await state.update_data(pub_channel_id=channel_id)

    await callback.message.edit_text(
        "📢 <b>Параметры публикации:</b>\n\n"
        "Хотите отправить только медиа (кружок/рилс/голосовое) или добавить сопроводительный текст?",
        reply_markup=get_publish_options_kb(short_id, channel_id),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("pub_custom:"))
async def handle_pub_custom_request(callback: CallbackQuery, state: FSMContext):
    short_id = callback.data.split(":", 1)[1]
    media = get_media(short_id)
    if media:
        await state.update_data(
            pub_short_id=short_id,
            pub_media_type=media["media_type"],
            pub_file_id=media["file_id"]
        )
    await state.set_state(PublishState.entering_custom_channel)

    await callback.message.answer(
        "📢 <b>Ввод канала для публикации</b>\n\n"
        "Отправьте <b>@username канала</b> (например, <code>@my_channel</code>) "
        "или перешлите пост из канала:",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(PublishState.entering_custom_channel)
async def process_custom_channel_input(message: Message, state: FSMContext, bot: Bot):
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
                f"❌ Бот найден в канале <b>{chat.title}</b>, но еще не назначен администратором с правом отправки сообщений.\n\n"
                "Сделайте бота администратором в настройках канала и попробуйте снова.",
                parse_mode="HTML"
            )
            return

        await add_channel(
            channel_id=str(chat.id),
            title=chat.title or "",
            username=chat.username or "",
            added_by=message.from_user.id
        )

        data = await state.get_data()
        short_id = data.get("pub_short_id", "default")
        await state.update_data(pub_channel_id=str(chat.id))
        await message.answer(
            f"✅ Канал <b>{chat.title}</b> успешно привязан!\n\n"
            f"Хотите опубликовать сразу или добавить сопроводительный текст?",
            reply_markup=get_publish_options_kb(short_id, str(chat.id)),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error checking channel {channel_input}: {e}")
        await message.answer(
            f"❌ Не удалось получить доступ к каналу <code>{channel_input}</code>:\n\n"
            f"<i>Убедитесь, что бот добавлен в администраторы канала.</i>",
            parse_mode="HTML"
        )

@router.callback_query(F.data.startswith("do_pub_direct:"))
async def handle_pub_direct(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    short_id = parts[1]
    channel_id = parts[2]

    data = await state.get_data()
    media = get_media(short_id)
    file_id = media["file_id"] if media else data.get("pub_file_id")
    media_type = media["media_type"] if media else data.get("pub_media_type", "video_note")

    if not file_id:
        await callback.answer("Ошибка: медиафайл устарел. Создайте новый кружок.", show_alert=True)
        return

    try:
        if media_type not in {"video_note", "voice", "video"}:
            await callback.answer("Неизвестный тип медиа", show_alert=True)
            return
        await PublishingService(bot).publish_media(
            channel_id=channel_id,
            media_type=media_type,
            file_id=file_id,
        )

        chat = await bot.get_chat(channel_id)
        ch_name = chat.title or channel_id
        await callback.message.edit_text(
            f"🎉 <b>Успешно опубликовано в канал {ch_name}!</b>\n\n"
            f"Медиа отправлено подписчикам в ленту.",
            parse_mode="HTML"
        )
        await callback.answer("Опубликовано!")
    except Exception as e:
        logger.error(f"Error direct publishing: {e}")
        await callback.message.edit_text(f"❌ Ошибка публикации в канал:\n\n<code>{e}</code>", parse_mode="HTML")
    finally:
        await state.clear()

@router.callback_query(F.data.startswith("do_pub_text:"))
async def handle_pub_text_prompt(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    short_id = parts[1]
    channel_id = parts[2]

    data = await state.get_data()
    media = get_media(short_id)
    file_id = media["file_id"] if media else data.get("pub_file_id")
    media_type = media["media_type"] if media else data.get("pub_media_type", "video_note")

    await state.update_data(
        pub_short_id=short_id,
        pub_channel_id=channel_id,
        pub_media_type=media_type,
        pub_file_id=file_id
    )
    await state.set_state(PublishState.entering_post_text)

    await callback.message.edit_text(
        "✍️ <b>Введите сопроводительный текст для публикации:</b>\n\n"
        "<i>Отправьте текст сообщения, который выйдет вместе с кружком/голосовым:</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(PublishState.entering_post_text, F.text)
async def process_post_text_and_publish(message: Message, state: FSMContext, bot: Bot):
    caption_text = message.text.strip()
    data = await state.get_data()
    channel_id = data.get("pub_channel_id")
    media_type = data.get("pub_media_type")
    file_id = data.get("pub_file_id")

    if not channel_id or not file_id:
        await message.answer("❌ Ошибка параметров публикации. Попробуйте снова.")
        await state.clear()
        return

    wait_msg = await message.answer("⏳ Публикую в канал...")
    try:
        await PublishingService(bot).publish_media(
            channel_id=channel_id,
            media_type=media_type,
            file_id=file_id,
            text=caption_text,
        )

        chat = await bot.get_chat(channel_id)
        ch_name = chat.title or channel_id
        await wait_msg.edit_text(
            f"🎉 <b>Успешно опубликовано в канал {ch_name}!</b>\n\n"
            f"Пост и медиа вышли в ленту канала.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error publishing with text: {e}")
        await wait_msg.edit_text(f"❌ Ошибка публикации в канал:\n\n<code>{e}</code>", parse_mode="HTML")
    finally:
        await state.clear()

@router.callback_query(F.data.startswith("nav:create_more:"))
async def handle_create_more(callback: CallbackQuery, state: FSMContext):
    media_type = callback.data.split(":")[2]
    await state.clear()
    if media_type == "video_note":
        from bot.handlers.avatar import start_circle_flow
        await start_circle_flow(callback.message, state)
    elif media_type == "video":
        from bot.handlers.avatar import start_stories_flow
        await start_stories_flow(callback.message, state)
    else:
        from bot.handlers.audio import start_audio_flow
        await start_audio_flow(callback.message, state)
    await callback.answer()
