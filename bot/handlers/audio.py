import os
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.services.minimax_service import minimax_client, MINIMAX_VOICES, MINIMAX_EMOTIONS
from bot.services.video_processor import safe_remove, convert_to_voice_note
from bot.keyboards.all_keyboards import (
    get_audio_voices_kb, get_minimax_settings_kb, get_post_generation_kb
)
from bot.services.media_cache import store_media
from bot.db import log_generation
from bot.services.notifier import notify_admins_error

logger = logging.getLogger(__name__)
router = Router()

class AudioState(StatesGroup):
    choosing_voice = State()
    adjusting_settings = State()
    entering_text = State()


def _emotion_name(emotion: str) -> str:
    return MINIMAX_EMOTIONS.get(emotion, "😐 Нейтральная")


def _settings_text(data: dict) -> str:
    return (
        f"🎙 <b>Выбран спикер:</b> {data.get('audio_voice_name', '')}\n\n"
        f"Шаг 2 из 3: Настройте параметры генерации:\n"
        f"• Скорость речи: <b>{data.get('audio_speed', 1.0)}x</b>\n"
        f"• Эмоция: <b>{_emotion_name(data.get('audio_emotion', 'neutral'))}</b>"
    )


@router.message(F.text == "🎙 Аудио")
async def start_audio_flow(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AudioState.choosing_voice)
    await message.answer(
        "🎙 <b>Генерация голосового сообщения</b>\n\n"
        "Шаг 1 из 3: Выберите спикера для озвучки текста:\n"
        "<i>(Синтез через MiniMax)</i>",
        reply_markup=get_audio_voices_kb(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "nav:back_to_audio_voices")
async def back_to_audio_voices(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AudioState.choosing_voice)
    await callback.message.edit_text(
        "🎙 <b>Генерация голосового сообщения</b>\n\n"
        "Шаг 1 из 3: Выберите спикера для озвучки текста:",
        reply_markup=get_audio_voices_kb(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("audio_voice:"))
async def audio_voice_selected(callback: CallbackQuery, state: FSMContext):
    voice_key = callback.data.split(":", 1)[1]
    voice_data = MINIMAX_VOICES.get(voice_key)
    if not voice_data:
        await callback.answer("Голос не найден", show_alert=True)
        return

    await state.update_data(
        audio_voice_key=voice_key,
        audio_voice_id=voice_data["voice_id"],
        audio_voice_name=voice_data["name"],
        audio_speed=1.0,
        audio_emotion="neutral"
    )
    await state.set_state(AudioState.adjusting_settings)

    data = await state.get_data()
    await callback.message.edit_text(
        _settings_text(data),
        reply_markup=get_minimax_settings_kb(current_speed=1.0, current_emotion="neutral"),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mm_speed:"))
async def audio_speed_changed(callback: CallbackQuery, state: FSMContext):
    speed = float(callback.data.split(":", 1)[1])
    await state.update_data(audio_speed=speed)
    data = await state.get_data()
    await callback.message.edit_text(
        _settings_text(data),
        reply_markup=get_minimax_settings_kb(
            current_speed=speed,
            current_emotion=data.get("audio_emotion", "neutral")
        ),
        parse_mode="HTML"
    )
    await callback.answer(f"Скорость: {speed}x")


@router.callback_query(F.data.startswith("mm_emotion:"))
async def audio_emotion_changed(callback: CallbackQuery, state: FSMContext):
    emotion = callback.data.split(":", 1)[1]
    await state.update_data(audio_emotion=emotion)
    data = await state.get_data()
    await callback.message.edit_text(
        _settings_text(data),
        reply_markup=get_minimax_settings_kb(
            current_speed=data.get("audio_speed", 1.0),
            current_emotion=emotion
        ),
        parse_mode="HTML"
    )
    await callback.answer(f"Эмоция: {_emotion_name(emotion)}")


@router.callback_query(F.data == "mm_start_input")
async def audio_start_input(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AudioState.entering_text)
    data = await state.get_data()
    await callback.message.edit_text(
        f"✍️ <b>Шаг 3 из 3: Введите текст для голосового</b>\n\n"
        f"• Спикер: <b>{data.get('audio_voice_name', '')}</b>\n"
        f"• Скорость: <b>{data.get('audio_speed', 1.0)}x</b>\n"
        f"• Эмоция: <b>{_emotion_name(data.get('audio_emotion', 'neutral'))}</b>\n\n"
        f"<i>Отправьте готовый текст ответным сообщением:</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AudioState.entering_text, F.text)
async def audio_process_text(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    data = await state.get_data()
    voice_id = data.get("audio_voice_id", "")
    voice_name = data.get("audio_voice_name", "Эксперт")
    speed = data.get("audio_speed", 1.0)
    emotion = data.get("audio_emotion", "neutral")

    wait_msg = await message.answer("⏳ <b>Синтезирую голос в MiniMax...</b>", parse_mode="HTML")

    success, mp3_path, err = await minimax_client.generate_speech(
        text=text,
        voice_id=voice_id,
        speed=speed,
        emotion=emotion
    )

    if not success or not mp3_path:
        await notify_admins_error(
            bot=bot,
            title="Ошибка синтеза речи MiniMax (Audio)",
            error=err,
            user_id=message.from_user.id,
            username=message.from_user.username,
            extra_info={"Спикер": voice_name, "Скорость": f"{speed}x", "Текст": text[:100]}
        )
        await wait_msg.edit_text(f"❌ Ошибка синтеза речи MiniMax:\n\n<code>{err}</code>", parse_mode="HTML")
        await state.clear()
        return

    conv_ok, ogg_path, conv_err = await convert_to_voice_note(mp3_path, loudnorm=True, pitch="normal")
    safe_remove(mp3_path)

    if not conv_ok or not ogg_path:
        await notify_admins_error(
            bot=bot,
            title="Ошибка конвертации голосового (FFmpeg)",
            error=conv_err,
            user_id=message.from_user.id,
            username=message.from_user.username
        )
        await wait_msg.edit_text(f"❌ Ошибка конвертации голосового: {conv_err}")
        await state.clear()
        return

    try:
        audio_file = FSInputFile(ogg_path)
        sent_voice = await bot.send_voice(
            chat_id=message.chat.id,
            voice=audio_file,
            caption=f"🎙 <b>Озвучка:</b> {voice_name} ({speed}x)"
        )
        file_id = sent_voice.voice.file_id if sent_voice and sent_voice.voice else ""
        short_id = store_media("voice", file_id) if file_id else ""
        await wait_msg.delete()
        await message.answer(
            f"🎉 <b>Голосовое сообщение успешно готово!</b>\n\n"
            f"• 🎙 <b>Спикер:</b> {voice_name}\n"
            f"• ⚡ <b>Темп:</b> {speed}x\n\n"
            f"<i>Вы можете переслать голосовое или сразу опубликовать в канал по кнопке ниже:</i>",
            reply_markup=get_post_generation_kb("voice", short_id),
            parse_mode="HTML"
        )
        await log_generation(
            user_id=message.from_user.id,
            gen_type="voice_note",
            prompt=text,
            status="completed",
            meta_json=f"{{\"speaker\": \"{voice_name}\", \"speed\": {speed}}}"
        )
    except Exception as e:
        logger.error(f"Error sending voice note: {e}")
        await notify_admins_error(
            bot=bot,
            title="Ошибка отправки голосового сообщения в чат",
            error=str(e),
            user_id=message.from_user.id,
            username=message.from_user.username
        )
        await wait_msg.edit_text(f"❌ Ошибка отправки голосового в чат: {e}")
    finally:
        safe_remove(ogg_path)
        await state.clear()
