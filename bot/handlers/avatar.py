import os
import uuid
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.config import settings
from bot.services.heygen_service import heygen_client, build_experts
from bot.services.video_processor import (
    download_file, convert_to_video_note, convert_to_stories_video,
    get_video_dimensions, safe_remove
)
from bot.keyboards.all_keyboards import (
    get_confirmation_kb, get_post_generation_kb
)
from bot.services.media_cache import store_media
from bot.db import log_generation, update_generation_status
from bot.services.notifier import notify_admins_error

logger = logging.getLogger(__name__)
router = Router()

class AvatarState(StatesGroup):
    entering_voice_id = State()
    entering_text = State()
    confirming = State()


def _is_stories(data: dict) -> bool:
    return data.get("output_format") == "stories"


def _product_label(data: dict) -> str:
    return "рилс" if _is_stories(data) else "кружок"


def _max_words(data: dict) -> int:
    return settings.MAX_STORIES_TEXT_WORDS if _is_stories(data) else settings.MAX_TEXT_WORDS


def _text_step_hint(data: dict) -> str:
    max_words = _max_words(data)
    if _is_stories(data):
        return (
            f"✍️ <b>Введите текст для рилса</b>\n\n"
            f"• Рекомендуемый объём: <b>до {max_words} слов</b> (~90 сек).\n"
            f"• Формат выдачи: вертикальное видео <b>9:16</b> для Reels.\n"
            f"• При превышении бот попросит сократить текст, чтобы контролировать расход кредитов.\n\n"
            f"<i>Отправьте готовый текст сообщением в чат:</i>"
        )
    return (
        f"✍️ <b>Введите текст для кружка</b>\n\n"
        f"• Рекомендуемый объём: <b>до {max_words} слов</b> (~50–55 сек).\n"
        f"• Telegram строго ограничивает кружки <b>60 секундами</b>.\n"
        f"• При превышении бот попросит сократить текст, чтобы не списывать лишние кредиты.\n\n"
        f"<i>Отправьте готовый текст сообщением в чат:</i>"
    )


def _default_expert_look():
    experts = build_experts()
    if not experts:
        raise RuntimeError(
            "HeyGen avatar is not configured. Set HEYGEN_AVATAR_ID in .env (see docs/CONNECT_YOUR_AVATAR.md)."
        )
    expert_key = next(iter(experts))
    exp = experts[expert_key]
    look_key = next(iter(exp["looks"]))
    look = exp["looks"][look_key]
    return expert_key, exp, look_key, look


async def _apply_default_expert_and_look(state: FSMContext):
    expert_key, exp, look_key, look = _default_expert_look()
    await state.update_data(
        expert_key=expert_key,
        expert_name=exp["name"],
        look_key=look_key,
        look_name=look["name"],
        avatar_id=look["id"],
        avatar_type=look.get("avatar_type", "photo_avatar"),
        look_category=look.get("category", "live"),
        is_custom_bg=False,
    )


async def _send(target, text: str, reply_markup=None):
    if isinstance(target, Message):
        return await target.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    return await target.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def _show_text_step(target, state: FSMContext):
    data = await state.get_data()
    await state.set_state(AvatarState.entering_text)
    await _send(
        target,
        f"👤 <b>Эксперт:</b> {data.get('expert_name')}\n"
        f"👔 <b>Образ:</b> {data.get('look_name')}\n"
        f"🎙 <b>Голос:</b> {data.get('voice_name')}\n\n"
        f"{_text_step_hint(data)}",
    )


async def _show_confirmation(target, state: FSMContext):
    data = await state.get_data()
    text = (data.get("text") or data.get("saved_prompt") or "").strip()
    words = text.split()
    word_count = len(words)
    est_duration = max(1.0, round(word_count / 2.2, 1))
    await state.update_data(text=text, word_count=word_count, est_duration=est_duration)
    await state.set_state(AvatarState.confirming)

    product = _product_label(data)
    preview_text = text if len(text) < 180 else text[:180] + "..."
    format_line = "• 📐 <b>Формат:</b> Рилс 9:16\n" if _is_stories(data) else ""

    await _send(
        target,
        f"📋 <b>Проверьте параметры перед генерацией:</b>\n\n"
        f"• 👤 <b>Эксперт:</b> {data.get('expert_name')}\n"
        f"• 👔 <b>Образ:</b> {data.get('look_name')}\n"
        f"• 🎙 <b>Голос:</b> {data.get('voice_name')}\n"
        f"{format_line}"
        f"• ⏱ <b>Хронометраж:</b> ~{est_duration:.0f} сек. ({word_count} слов)\n"
        f"• 💳 <b>Списание:</b> 1 кредит HeyGen\n\n"
        f"💬 <b>Текст:</b>\n<i>«{preview_text}»</i>\n\n"
        f"⚡ <b>ВНИМАНИЕ:</b> Чтобы запустить создание {product}, <b>обязательно нажмите кнопку ниже</b>:\n"
        f"<i>(Создание займёт 1–2 минуты, {product} придёт прямо сюда)</i>",
        reply_markup=get_confirmation_kb(data.get("output_format", "circle")),
    )


async def auto_select_and_continue(target, state: FSMContext):
    """Ставит единственного эксперта и образ, затем ведёт к голосу или тексту."""
    if not build_experts():
        await _send(
            target,
            "⚠️ <b>HeyGen-аватар не настроен</b>\n\n"
            "Задайте <code>HEYGEN_AVATAR_ID</code> и <code>HEYGEN_DEFAULT_VOICE_ID</code> в <code>.env</code>.\n"
            "Инструкция: <code>docs/CONNECT_YOUR_AVATAR.md</code>.",
        )
        return
    await _apply_default_expert_and_look(state)
    data = await state.get_data()
    voice_id = settings.HEYGEN_DEFAULT_VOICE_ID.strip()

    if not voice_id:
        product = _product_label(data)
        await state.set_state(AvatarState.entering_voice_id)
        await _send(
            target,
            f"👤 <b>Эксперт:</b> {data.get('expert_name')}\n"
            f"👔 <b>Образ:</b> {data.get('look_name')}\n\n"
            f"🎙 <b>Голос не настроен</b>\n\n"
            f"Голос по умолчанию ещё не задан (переменная <code>HEYGEN_DEFAULT_VOICE_ID</code> пуста).\n"
            f"Рендер {product} без voice id невозможен.\n\n"
            f"<i>Пришлите voice id HeyGen сообщением, чтобы продолжить:</i>",
        )
        return

    await state.update_data(voice_id=voice_id, voice_name="Голос по умолчанию (HeyGen)")
    if data.get("saved_prompt"):
        await _show_confirmation(target, state)
    else:
        await _show_text_step(target, state)


async def _begin_avatar_flow(message: Message, state: FSMContext, output_format: str):
    await state.clear()
    await state.update_data(output_format=output_format)
    await auto_select_and_continue(message, state)


@router.message(F.text == "🎥 Кружок")
async def start_circle_flow(message: Message, state: FSMContext):
    await _begin_avatar_flow(message, state, "circle")


@router.message(F.text == "🎬 Рилс (9:16)")
async def start_stories_flow(message: Message, state: FSMContext):
    await _begin_avatar_flow(message, state, "stories")


@router.message(AvatarState.entering_voice_id, F.text)
async def handle_voice_id_input(message: Message, state: FSMContext):
    voice_id = message.text.strip()
    await state.update_data(voice_id=voice_id, voice_name=f"Голос {voice_id}")
    data = await state.get_data()
    if data.get("saved_prompt"):
        await _show_confirmation(message, state)
    else:
        await _show_text_step(message, state)


@router.message(AvatarState.entering_text, F.text)
async def handle_avatar_text(message: Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()
    words = text.split()
    word_count = len(words)
    est_duration = max(1.0, round(word_count / 2.2, 1))
    max_words = _max_words(data)
    product = _product_label(data)

    if word_count > max_words:
        if _is_stories(data):
            limit_reason = (
                f"❌ Для рилса лимит <b>{max_words} слов</b>, чтобы контролировать расход кредитов HeyGen."
            )
        else:
            limit_reason = (
                "❌ Кружок Telegram не может длиться более 60 секунд! "
                "Кроме того, HeyGen спишет 2 кредита вместо 1."
            )
        await message.answer(
            f"⚠️ <b>Текст слишком длинный:</b> {word_count} слов (хронометраж ~{est_duration:.0f} сек.)\n\n"
            f"{limit_reason}\n\n"
            f"Пожалуйста, сократите текст до <b>{max_words} слов</b> и пришлите обновлённый вариант:",
            parse_mode="HTML"
        )
        return

    await state.update_data(text=text, word_count=word_count, est_duration=est_duration)
    await _show_confirmation(message, state)


@router.callback_query(F.data == "edit_prompt")
async def edit_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    product = _product_label(data)
    await state.set_state(AvatarState.entering_text)
    await callback.message.edit_text(
        f"✏️ Отправьте новый текст для {product} сообщением в чат:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_render:"))
async def execute_render(callback: CallbackQuery, state: FSMContext, bot: Bot):
    mode = callback.data.split(":", 1)[1]
    is_test = (mode == "test")

    data = await state.get_data()
    avatar_id = data.get("avatar_id")
    avatar_type = data.get("avatar_type", "photo_avatar")
    voice_id = data.get("voice_id")
    text = data.get("text")
    background = data.get("background", {"type": "color", "value": "#1E1E2E"})
    expert_name = data.get("expert_name", "Эксперт")
    look_name = data.get("look_name", "")
    output_format = data.get("output_format", "circle")
    product = _product_label(data)
    is_stories = output_format == "stories"

    if not avatar_id or not voice_id or not text:
        await callback.message.edit_text("❌ Ошибка параметров сессии. Начните заново с /start.")
        await state.clear()
        return

    mode_title = "🧪 ТЕСТОВЫЙ РЕЖИМ (0 кредитов)" if is_test else "🚀 БОЕВОЙ РЕЖИМ (-1 кредит)"

    status_msg = await callback.message.edit_text(
        f"⏳ <b>{mode_title}</b>\n\n"
        f"Отправляю задачу в HeyGen... Создание ИИ-аватара обычно занимает 1–2 минуты.\n"
        f"Пожалуйста, подождите — {product} придёт прямо сюда!",
        parse_mode="HTML"
    )
    await callback.answer()

    gen_id = await log_generation(
        user_id=callback.from_user.id,
        gen_type="video_stories" if is_stories else "video_circle",
        prompt=text,
        status="rendering",
        meta_json=(
            f'{{"test": {str(is_test).lower()}, "expert": "{expert_name}", '
            f'"look": "{look_name}", "output_format": "{output_format}"}}'
        )
    )

    success, result = await heygen_client.create_avatar_video(
        avatar_id=avatar_id,
        voice_id=voice_id,
        text=text,
        background=background,
        avatar_type=avatar_type,
        test_mode=is_test,
        output_format=output_format,
    )

    if not success:
        await update_generation_status(gen_id, status="failed")
        err_lower = str(result).lower()
        if "insufficient credits" in err_lower or "credit" in err_lower:
            await notify_admins_error(
                bot=bot,
                title="Закончились кредиты HeyGen",
                error=result,
                user_id=callback.from_user.id,
                username=callback.from_user.username,
                extra_info={"Режим": "Боевой" if not is_test else "Тест", "Эксперт": expert_name, "Образ": look_name}
            )
            await status_msg.edit_text(
                "💳 <b>Недостаточно кредитов на балансе HeyGen</b>\n\n"
                "На корпоративном аккаунте HeyGen временно закончились API-кредиты.\n"
                "Администраторы уже автоматически уведомлены о необходимости пополнить баланс.\n\n"
                "<i>💡 Вы можете сгенерировать этот же ролик прямо сейчас бесплатно (в тестовом режиме):</i>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🧪 Сгенерировать бесплатно (Тест)", callback_data="confirm_render:test")],
                    [InlineKeyboardButton(text="❌ Закрыть", callback_data="cancel_fsm")]
                ]),
                parse_mode="HTML"
            )
        else:
            await notify_admins_error(
                bot=bot,
                title="Ошибка запуска рендера HeyGen",
                error=result,
                user_id=callback.from_user.id,
                username=callback.from_user.username,
                extra_info={"Эксперт": expert_name, "Образ": look_name, "Текст": text[:100]}
            )
            await status_msg.edit_text(f"❌ Не удалось запустить рендер в HeyGen:\n\n<code>{result}</code>", parse_mode="HTML")
        await state.clear()
        return

    video_id = result
    logger.info(f"HeyGen rendering task {video_id} started for user {callback.from_user.id}")

    spinners = ["⏳", "⌛"]
    spin_idx = 0
    async def update_render_progress(elapsed_min: int, elapsed_sec: int):
        nonlocal spin_idx
        try:
            spin = spinners[spin_idx % len(spinners)]
            spin_idx += 1
            sec_str = f"{elapsed_sec:02d}"
            await status_msg.edit_text(
                f"{spin} <b>{mode_title}</b>\n\n"
                f"Идёт генерация в нейросети HeyGen... (прошло {elapsed_min}:{sec_str})\n"
                f"Создание фотореалистичного аватара обычно занимает 1–2 минуты.\n"
                f"Пожалуйста, подождите — {product} придёт прямо сюда!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    poll_status, video_url, error = await heygen_client.poll_video_status(
        video_id,
        max_wait_sec=600,
        progress_callback=update_render_progress
    )

    if poll_status != "completed" or not video_url:
        await update_generation_status(gen_id, status="failed")
        err_msg = error or f"Статус: {poll_status}"
        await notify_admins_error(
            bot=bot,
            title=f"Ошибка рендера HeyGen ({poll_status})",
            error=err_msg,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
            extra_info={"Video ID": video_id, "Эксперт": expert_name, "Образ": look_name}
        )
        if poll_status == "timeout":
            await status_msg.edit_text(
                f"⏳ <b>Сервер HeyGen сейчас сильно загружен</b>\n\n"
                f"Генерация длится дольше обычного (более 10 минут) и всё ещё находится в очереди рендера HeyGen.\n"
                f"ID задачи: <code>{video_id}</code>\n\n"
                f"Вы можете попробовать отправить текст повторно чуть позже.",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(f"❌ Ошибка рендера HeyGen:\n\n<code>{err_msg}</code>", parse_mode="HTML")
        await state.clear()
        return

    if is_stories:
        await status_msg.edit_text(
            "⚙️ <b>Видео готово! Монтирую вертикальный формат 9:16 (FFmpeg)...</b>",
            parse_mode="HTML",
        )
    else:
        await status_msg.edit_text(
            "⚙️ <b>Видео готово! Монтирую нативный кружок 1:1 (FFmpeg)...</b>",
            parse_mode="HTML",
        )

    raw_video_path = os.path.join(settings.TEMP_DIR, f"raw_{uuid.uuid4().hex[:8]}.mp4")
    downloaded = await download_file(video_url, raw_video_path)

    if not downloaded:
        await update_generation_status(gen_id, status="failed")
        await notify_admins_error(
            bot=bot,
            title="Ошибка скачивания видео HeyGen",
            error=f"Не удалось скачать видео по URL: {video_url}",
            user_id=callback.from_user.id,
            username=callback.from_user.username,
            extra_info={"Video ID": video_id}
        )
        await status_msg.edit_text("❌ Ошибка скачивания сгенерированного видео с сервера HeyGen.")
        await state.clear()
        return

    if is_stories:
        conv_success, out_path, conv_error = await convert_to_stories_video(raw_video_path)
        ffmpeg_title = "Ошибка FFmpeg (монтаж рилса 9:16)"
        ffmpeg_user_msg = "кадрировании рилса"
    else:
        conv_success, out_path, conv_error = await convert_to_video_note(raw_video_path)
        ffmpeg_title = "Ошибка FFmpeg (кадрирование кружка)"
        ffmpeg_user_msg = "кадрировании кружка"
    safe_remove(raw_video_path)

    if not conv_success or not out_path:
        await update_generation_status(gen_id, status="failed")
        await notify_admins_error(
            bot=bot,
            title=ffmpeg_title,
            error=conv_error,
            user_id=callback.from_user.id,
            username=callback.from_user.username,
            extra_info={"Video ID": video_id, "output_format": output_format}
        )
        await status_msg.edit_text(
            f"❌ Ошибка FFmpeg при {ffmpeg_user_msg}:\n\n<code>{conv_error}</code>",
            parse_mode="HTML",
        )
        await state.clear()
        return

    try:
        _, _, duration_flt = await get_video_dimensions(out_path)
        duration = int(duration_flt)
        media_file = FSInputFile(out_path)

        if is_stories:
            sent = await bot.send_video(
                chat_id=callback.message.chat.id,
                video=media_file,
                duration=duration,
                width=720,
                height=1280,
                supports_streaming=True,
            )
            file_id = sent.video.file_id if sent and sent.video else ""
            media_type = "video"
            done_title = "Рилс успешно готов и отправлен выше!"
            done_hint = "Вы можете переслать видео или сразу опубликовать в канал по кнопке ниже:"
        else:
            sent = await bot.send_video_note(
                chat_id=callback.message.chat.id,
                video_note=media_file,
                duration=duration,
            )
            file_id = sent.video_note.file_id if sent and sent.video_note else ""
            media_type = "video_note"
            done_title = "Кружок успешно готов и отправлен выше!"
            done_hint = "Вы можете переслать кружок или сразу опубликовать в канал по кнопке ниже:"

        short_id = store_media(media_type, file_id) if file_id else ""
        await update_generation_status(gen_id, status="completed")
        await status_msg.edit_text(
            f"🎉 <b>{done_title}</b>\n\n"
            f"• 👤 <b>Эксперт:</b> {expert_name} ({look_name})\n"
            f"• 📐 <b>Формат:</b> {'Рилс 9:16' if is_stories else 'Telegram-кружок'}\n"
            f"• ⏱ <b>Длительность:</b> {duration} сек.\n"
            f"• 💳 <b>Списание:</b> {'0 кредитов (Тест)' if is_test else '1 кредит HeyGen'}\n\n"
            f"<i>{done_hint}</i>",
            reply_markup=get_post_generation_kb(media_type, short_id),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to send {product}: {e}")
        await update_generation_status(gen_id, status="failed")
        await status_msg.edit_text(f"❌ Ошибка отправки {product} в Telegram: {e}")
    finally:
        safe_remove(out_path)
        await state.clear()
