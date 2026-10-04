"""Admin-only, confirmed broadcasts to the currently connected channels."""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.keyboards.all_keyboards import (
    get_broadcast_channels_kb,
    get_broadcast_batch_cancel_kb,
    get_broadcast_batch_manage_kb,
    get_broadcast_batches_kb,
    get_broadcast_batch_size_kb,
    get_broadcast_confirm_kb,
    get_broadcast_photo_kb,
    get_broadcast_timing_kb,
)
from bot.migrations import configure_connection
from bot.services.publishing import PublishingService

logger = logging.getLogger(__name__)
router = Router()
MOSCOW = ZoneInfo("Europe/Moscow")
MAX_TEXT_LENGTH = 4096
MAX_PHOTO_CAPTION_LENGTH = 1024


class BroadcastState(StatesGroup):
    entering_text = State()
    entering_photo = State()
    selecting_channels = State()
    selecting_timing = State()
    entering_schedule = State()
    confirming = State()
    choosing_batch_size = State()
    entering_batch_text = State()
    entering_batch_media = State()
    entering_batch_schedule = State()
    confirming_batch = State()
    editing_batch_text = State()
    editing_batch_schedule = State()


# Keep the longest callback (``broadcast:cancel_batch_prompt:``) under Telegram's
# 64-byte callback_data limit. Generated keys are currently 22 ASCII characters.
BATCH_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_list


def _channel_title(channel) -> str:
    return channel["title"] or channel["username"] or str(channel["channel_id"])


def _message_media(message: Message) -> tuple[str, str] | None:
    if getattr(message, "photo", None):
        return "photo", message.photo[-1].file_id
    if getattr(message, "video", None):
        return "video", message.video.file_id
    if getattr(message, "voice", None):
        return "voice", message.voice.file_id
    return None


async def _active_channels():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM channels WHERE is_active = 1 ORDER BY created_at DESC"
        )
        return await cursor.fetchall()


async def _selected_channels(state: FSMContext):
    selected_ids = {int(value) for value in (await state.get_data()).get("broadcast_channel_ids", [])}
    return [channel for channel in await _active_channels() if channel["id"] in selected_ids]


async def _show_channel_selection(message: Message, state: FSMContext) -> None:
    channels = await _active_channels()
    if not channels:
        await message.answer("⚠️ Нет привязанных каналов для рассылки.")
        await state.clear()
        return
    data = await state.get_data()
    selected_ids = set(data.get("broadcast_channel_ids", []))
    await state.set_state(BroadcastState.selecting_channels)
    await message.answer(
        "📣 <b>Каналы рассылки</b>\n\n"
        "По умолчанию выбраны все активные каналы. Нажмите на канал, чтобы исключить его.",
        reply_markup=get_broadcast_channels_kb(channels, selected_ids),
        parse_mode="HTML",
    )


async def _create_broadcast_posts(
    channels,
    *,
    operation_key: str,
    text: str,
    media_type: str,
    media_file_id: str | None,
    status: str,
    publish_at: str | None,
    created_by: int,
) -> dict[int, int]:
    """Create one durable delivery record per selected channel."""
    post_ids = {}
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute("BEGIN IMMEDIATE")
        channel_ids = [int(channel["id"]) for channel in channels]
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = await db.execute(
            f"SELECT id FROM channels WHERE is_active = 1 AND id IN ({placeholders})",
            channel_ids,
        )
        if {row[0] for row in await cursor.fetchall()} != set(channel_ids):
            await db.rollback()
            raise ValueError("Broadcast contains an inactive channel")
        for channel in channels:
            cursor = await db.execute(
                """
                INSERT INTO posts (
                    channel_id, body, media_type, media_file_id, status, publish_at,
                    created_by, operation_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel["id"], text, media_type, media_file_id, status,
                    publish_at, created_by, operation_key,
                ),
            )
            post_id = cursor.lastrowid
            post_ids[channel["id"]] = post_id
            await db.execute(
                """
                INSERT INTO post_events (
                    post_id, event_type, from_status, to_status, actor_type, actor_id
                ) VALUES (?, 'broadcast_created', NULL, ?, 'user', ?)
                """,
                (post_id, status, str(created_by)),
            )
        await db.commit()
    return post_ids


async def _create_scheduled_broadcast_batch(
    channels,
    *,
    batch_key: str,
    items: list[dict],
    created_by: int,
) -> int | None:
    """Create a 2–3 item scheduled plan atomically; None means replay."""
    if len(items) not in {2, 3}:
        raise ValueError("Broadcast batch must contain 2 or 3 posts")
    if not batch_key or len(batch_key) > 128:
        raise ValueError("Broadcast batch key is invalid")
    now = datetime.now(timezone.utc)
    for item in items:
        text = str(item.get("text") or "").strip()
        media_type = item.get("media_type") or "text"
        if not text or len(text) > MAX_TEXT_LENGTH:
            raise ValueError("Broadcast batch text is invalid")
        if media_type not in {"text", "photo", "video", "voice"}:
            raise ValueError("Broadcast batch media type is invalid")
        if media_type != "text" and not item.get("media_file_id"):
            raise ValueError("Broadcast batch media file is missing")
        if media_type != "text" and len(text) > MAX_PHOTO_CAPTION_LENGTH:
            raise ValueError("Broadcast batch media caption is too long")
        try:
            publish_at = datetime.fromisoformat(str(item.get("publish_at") or ""))
        except ValueError as exc:
            raise ValueError("Broadcast batch schedule is invalid") from exc
        if publish_at.tzinfo is None or publish_at.astimezone(timezone.utc) <= now:
            raise ValueError("Broadcast batch schedule must be in the future")
    channel_ids = [int(channel["id"]) for channel in channels]
    if not channel_ids:
        raise ValueError("Broadcast batch requires active channels")
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = await db.execute(
            f"SELECT id FROM channels WHERE is_active = 1 AND id IN ({placeholders})",
            channel_ids,
        )
        if {row[0] for row in await cursor.fetchall()} != set(channel_ids):
            await db.rollback()
            raise ValueError("Broadcast batch contains an inactive channel")

        created = 0
        for index, item in enumerate(items, start=1):
            operation_key = f"{batch_key}:{index}"
            claim = await db.execute(
                """
                INSERT INTO broadcast_operations(
                    operation_key, created_by, status, publish_at, completed_at
                ) VALUES (?, ?, 'scheduled', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(operation_key) DO NOTHING
                """,
                (operation_key, created_by, item["publish_at"]),
            )
            if claim.rowcount != 1:
                await db.rollback()
                return None
            for channel in channels:
                cursor = await db.execute(
                    """
                    INSERT INTO posts (
                        channel_id, body, media_type, media_file_id, status,
                        publish_at, created_by, operation_key
                    ) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?)
                    """,
                    (
                        channel["id"],
                        item["text"],
                        item.get("media_type") or "text",
                        item.get("media_file_id"),
                        item["publish_at"],
                        created_by,
                        operation_key,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO post_events (
                        post_id, event_type, from_status, to_status,
                        actor_type, actor_id, details_json
                    ) VALUES (?, 'broadcast_batch_created', NULL, 'scheduled',
                              'user', ?, ?)
                    """,
                    (
                        cursor.lastrowid,
                        str(created_by),
                        f'{{"batch_key":"{batch_key}","slot":{index}}}',
                    ),
                )
                created += 1
        await db.commit()
    return created


def _parse_batch_operation_key(operation_key: str) -> tuple[str, int] | None:
    try:
        batch_key, raw_slot = operation_key.rsplit(":", 1)
        slot = int(raw_slot)
    except (AttributeError, ValueError):
        return None
    if not BATCH_KEY_RE.fullmatch(batch_key) or slot not in {1, 2, 3}:
        return None
    return batch_key, slot


async def _list_future_scheduled_batches() -> list[dict]:
    """List confirmed batches that still have future scheduled deliveries."""
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT operation_key, publish_at, channel_id, status
            FROM posts
            WHERE operation_key IS NOT NULL
            ORDER BY publish_at, id
            """
        )
        rows = await cursor.fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        parsed = _parse_batch_operation_key(row["operation_key"])
        if parsed is None:
            continue
        batch_key, slot = parsed
        batch = grouped.setdefault(
            batch_key,
            {
                "batch_key": batch_key,
                "slots": set(),
                "channels": set(),
                "future_publish_at": [],
            },
        )
        batch["slots"].add(slot)
        batch["channels"].add(int(row["channel_id"]))
        if (
            row["status"] == "scheduled"
            and row["publish_at"]
            and datetime.fromisoformat(row["publish_at"]).astimezone(timezone.utc) > now
        ):
            batch["future_publish_at"].append(row["publish_at"])
    result = []
    for batch in grouped.values():
        if len(batch["slots"]) not in {2, 3} or not batch["future_publish_at"]:
            continue
        first = min(batch["future_publish_at"])
        result.append({
            "batch_key": batch["batch_key"],
            "slot_count": len(batch["slots"]),
            "channel_count": len(batch["channels"]),
            "first_publish_at_iso": first,
            "first_publish_at": datetime.fromisoformat(first).astimezone(MOSCOW).strftime(
                "%d.%m %H:%M"
            ),
        })
    return sorted(result, key=lambda item: item["first_publish_at_iso"])


async def _get_broadcast_batch(batch_key: str) -> dict | None:
    if not BATCH_KEY_RE.fullmatch(batch_key):
        return None
    operation_keys = [f"{batch_key}:{slot}" for slot in (1, 2, 3)]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, operation_key, body, media_type, status, publish_at, version
            FROM posts WHERE operation_key IN (?, ?, ?) ORDER BY operation_key, id
            """,
            operation_keys,
        )
        rows = await cursor.fetchall()
    if not rows:
        return None
    slots = []
    for slot in (1, 2, 3):
        slot_rows = [row for row in rows if row["operation_key"] == f"{batch_key}:{slot}"]
        if not slot_rows:
            continue
        scheduled = [row for row in slot_rows if row["status"] == "scheduled"]
        exemplar = scheduled[0] if scheduled else slot_rows[0]
        slots.append({
            "slot": slot,
            "body": exemplar["body"],
            "publish_at": exemplar["publish_at"],
            "scheduled_count": len(scheduled),
            "total_count": len(slot_rows),
            "editable": len(scheduled) == len(slot_rows),
        })
    return {"batch_key": batch_key, "slots": slots}


async def _update_scheduled_batch_slot(
    batch_key: str,
    slot: int,
    *,
    actor_id: int,
    body: str | None = None,
    publish_at: str | None = None,
) -> int:
    """Atomically edit every channel copy in one not-yet-started batch slot."""
    if not BATCH_KEY_RE.fullmatch(batch_key) or slot not in {1, 2, 3}:
        raise ValueError("Некорректный план")
    if (body is None) == (publish_at is None):
        raise ValueError("Нужно изменить только одно поле")
    operation_key = f"{batch_key}:{slot}"
    if body is not None:
        body = body.strip()
        if not body or len(body) > MAX_TEXT_LENGTH:
            raise ValueError(f"Текст должен содержать 1–{MAX_TEXT_LENGTH} символов")
    if publish_at is not None:
        try:
            parsed_publish_at = datetime.fromisoformat(publish_at)
        except ValueError as exc:
            raise ValueError("Некорректная дата") from exc
        if parsed_publish_at.tzinfo is None or parsed_publish_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Дата должна быть в будущем")

    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """SELECT id, body, media_type, status, publish_at, version
               FROM posts WHERE operation_key = ? ORDER BY id""",
            (operation_key,),
        )
        rows = await cursor.fetchall()
        if not rows:
            await db.rollback()
            raise ValueError("План не найден")
        if any(row["status"] != "scheduled" for row in rows):
            await db.rollback()
            raise ValueError("Публикация уже началась; слот больше нельзя менять")
        now = datetime.now(timezone.utc)
        if any(not row["publish_at"] or datetime.fromisoformat(row["publish_at"]).astimezone(timezone.utc) <= now for row in rows):
            await db.rollback()
            raise ValueError("Время публикации уже наступило; слот больше нельзя менять")
        if body is not None and any(row["media_type"] != "text" for row in rows) and len(body) > MAX_PHOTO_CAPTION_LENGTH:
            await db.rollback()
            raise ValueError(f"Текст поста с медиа должен быть не длиннее {MAX_PHOTO_CAPTION_LENGTH} символов")

        changed_rows = [
            row for row in rows
            if (body is not None and row["body"] != body)
            or (publish_at is not None and row["publish_at"] != publish_at)
        ]
        if not changed_rows:
            await db.rollback()
            return 0
        event_type = "broadcast_batch_text_updated" if body is not None else "broadcast_batch_schedule_updated"
        for row in changed_rows:
            details = (
                {"batch_key": batch_key, "slot": slot, "old_body": row["body"], "new_body": body}
                if body is not None
                else {"batch_key": batch_key, "slot": slot, "old_publish_at": row["publish_at"], "new_publish_at": publish_at}
            )
            if body is not None:
                await db.execute(
                    """UPDATE posts SET body = ?, version = version + 1,
                              updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'scheduled' AND version = ?""",
                    (body, row["id"], row["version"]),
                )
            else:
                await db.execute(
                    """UPDATE posts SET publish_at = ?, version = version + 1,
                              updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'scheduled' AND version = ?""",
                    (publish_at, row["id"], row["version"]),
                )
            await db.execute(
                """INSERT INTO post_events(
                       post_id, event_type, from_status, to_status, actor_type, actor_id, details_json
                   ) VALUES (?, ?, 'scheduled', 'scheduled', 'user', ?, ?)""",
                (row["id"], event_type, str(actor_id), json.dumps(details, ensure_ascii=False)),
            )
        if publish_at is not None:
            await db.execute(
                """UPDATE broadcast_operations SET publish_at = ?
                   WHERE operation_key = ? AND status = 'scheduled' AND publish_at <> ?""",
                (publish_at, operation_key, publish_at),
            )
        await db.commit()
        return len(changed_rows)


async def _cancel_scheduled_broadcast_batch(batch_key: str, *, actor_id: int) -> dict:
    """Cancel only deliveries the scheduler has not claimed; replay is a no-op."""
    if not BATCH_KEY_RE.fullmatch(batch_key):
        raise ValueError("Некорректный план")
    operation_keys = [f"{batch_key}:{slot}" for slot in (1, 2, 3)]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """SELECT id, operation_key, status, version FROM posts
               WHERE operation_key IN (?, ?, ?) ORDER BY id""",
            operation_keys,
        )
        rows = await cursor.fetchall()
        if not rows:
            await db.rollback()
            raise ValueError("План не найден")
        scheduled = [row for row in rows if row["status"] == "scheduled"]
        for row in scheduled:
            cursor = await db.execute(
                """UPDATE posts SET status = 'cancelled', version = version + 1,
                          updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'scheduled' AND version = ?""",
                (row["id"], row["version"]),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError("План изменился одновременно; повторите действие")
            await db.execute(
                """INSERT INTO post_events(
                       post_id, event_type, from_status, to_status, actor_type, actor_id, details_json
                   ) VALUES (?, 'broadcast_batch_cancelled', 'scheduled', 'cancelled', 'user', ?, ?)""",
                (
                    row["id"], str(actor_id),
                    json.dumps({"batch_key": batch_key}, ensure_ascii=False),
                ),
            )
        for operation_key in operation_keys:
            await db.execute(
                """UPDATE broadcast_operations SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
                   WHERE operation_key = ? AND status = 'scheduled'
                     AND NOT EXISTS (
                         SELECT 1 FROM posts WHERE operation_key = ? AND status <> 'cancelled'
                     )""",
                (operation_key, operation_key),
            )
        await db.commit()
    return {"cancelled": len(scheduled), "untouched": len(rows) - len(scheduled)}


async def _claim_broadcast_operation(
    operation_key: str, actor_id: int, publish_at: str | None
) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        cursor = await db.execute(
            """
            INSERT INTO broadcast_operations(operation_key, created_by, publish_at)
            VALUES (?, ?, ?) ON CONFLICT(operation_key) DO NOTHING
            """,
            (operation_key, actor_id, publish_at),
        )
        await db.commit()
        return cursor.rowcount == 1


async def _complete_broadcast_operation(operation_key: str, status: str) -> None:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute(
            """
            UPDATE broadcast_operations SET status = ?, completed_at = CURRENT_TIMESTAMP
            WHERE operation_key = ? AND status = 'processing'
            """,
            (status, operation_key),
        )
        await db.commit()


async def _record_broadcast_result(
    post_id: int,
    *,
    status: str,
    message_id: int | None = None,
    error: str | None = None,
    actor_id: int,
) -> None:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute("BEGIN IMMEDIATE")
        if status == "published":
            await db.execute(
                """
                UPDATE posts SET status = 'published', published_at = ?,
                    telegram_message_id = ?, publish_error = NULL,
                    published_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'publishing'
                """,
                (datetime.now(timezone.utc).isoformat(), message_id, actor_id, post_id),
            )
        else:
            await db.execute(
                """
                UPDATE posts SET status = 'publish_failed', publish_error = ?,
                    published_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'publishing'
                """,
                (error or "UnknownError", actor_id, post_id),
            )
        await db.execute(
            """
            INSERT INTO post_events (
                post_id, event_type, from_status, to_status, actor_type, actor_id
            ) VALUES (?, ?, 'publishing', ?, 'user', ?)
            """,
            (post_id, status, status, str(actor_id)),
        )
        await db.commit()


async def _show_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    channels = await _selected_channels(state)
    if not channels:
        await message.answer("⚠️ Выберите хотя бы один активный канал.")
        await state.set_state(BroadcastState.selecting_channels)
        return
    scheduled_at = data.get("broadcast_publish_at")
    timing = (
        f"запланировано на {datetime.fromisoformat(scheduled_at).astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')}"
        if scheduled_at else "будет опубликовано сейчас"
    )
    media_type = data.get("broadcast_media_type")
    if not media_type and data.get("broadcast_photo_file_id"):
        media_type = "photo"
    media_label = {
        "photo": "с фото",
        "video": "с видео",
        "voice": "с голосовым",
    }.get(media_type, "без медиа")
    await state.set_state(BroadcastState.confirming)
    await message.answer(
        f"📣 <b>Предпросмотр рассылки</b>\n\n"
        f"Каналов: <b>{len(channels)}</b>\n"
        f"Формат: {media_label}\n"
        f"Время: {timing}\n\n"
        f"<b>Текст:</b>\n{html.escape(data['broadcast_text'][:500])}"
        f"{'…' if len(data['broadcast_text']) > 500 else ''}\n\n"
        "Отправка начнётся только после подтверждения.",
        reply_markup=get_broadcast_confirm_kb(),
        parse_mode="HTML",
    )


async def _begin_broadcast(message: Message, state: FSMContext) -> bool:
    channels = await _active_channels()
    if not channels:
        await message.answer("⚠️ Нет привязанных активных каналов.")
        return False
    await state.clear()
    await state.update_data(
        broadcast_channel_ids=[channel["id"] for channel in channels],
        broadcast_operation_key=secrets.token_urlsafe(16),
    )
    await state.set_state(BroadcastState.entering_text)
    await message.answer(
        "📣 <b>Пост на все каналы</b>\n\n"
        "Отправьте текст поста одним сообщением (до 4096 символов).",
        parse_mode="HTML",
    )
    return True


async def _begin_broadcast_batch(message: Message, state: FSMContext) -> bool:
    channels = await _active_channels()
    if not channels:
        await message.answer("⚠️ Нет привязанных активных каналов.")
        return False
    await state.clear()
    await state.update_data(
        broadcast_channel_ids=[channel["id"] for channel in channels],
        broadcast_batch_key=secrets.token_urlsafe(16),
        broadcast_batch_items=[],
    )
    await state.set_state(BroadcastState.choosing_batch_size)
    await message.answer(
        "🗂 <b>План одинаковых постов для каналов</b>\n\n"
        "Выберите количество публикаций. Для каждой зададим свой текст, "
        "необязательное медиа и время.",
        reply_markup=get_broadcast_batch_size_kb(),
        parse_mode="HTML",
    )
    return True


async def _prompt_batch_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    index = len(data.get("broadcast_batch_items", [])) + 1
    total = int(data["broadcast_batch_size"])
    await state.set_state(BroadcastState.entering_batch_text)
    await message.answer(
        f"✍️ <b>Пост {index} из {total}</b>\n\n"
        "Отправьте текст одним сообщением (до 4096 символов).",
        parse_mode="HTML",
    )


async def _prompt_batch_schedule(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    index = len(data.get("broadcast_batch_items", [])) + 1
    await state.set_state(BroadcastState.entering_batch_schedule)
    await message.answer(
        f"🕓 Укажите время публикации поста {index} в формате "
        "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (МСК).",
        parse_mode="HTML",
    )


async def _show_batch_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    channels = await _selected_channels(state)
    items = data.get("broadcast_batch_items", [])
    if not channels or len(items) not in {2, 3}:
        await message.answer("⚠️ План неполный. Начните заново.")
        await state.clear()
        return
    lines = [
        "🗂 <b>Предпросмотр плана</b>",
        f"Публикаций: <b>{len(items)}</b>",
        f"Каналов: <b>{len(channels)}</b>",
        f"Будет создано заданий: <b>{len(items) * len(channels)}</b>",
    ]
    for index, item in enumerate(items, start=1):
        when = datetime.fromisoformat(item["publish_at"]).astimezone(MOSCOW)
        media = {
            "photo": "фото",
            "video": "видео",
            "voice": "голосовое",
        }.get(item.get("media_type"), "без медиа")
        preview = html.escape(item["text"][:120])
        suffix = "…" if len(item["text"]) > 120 else ""
        lines.append(
            f"\n<b>{index}. {when.strftime('%d.%m.%Y %H:%M МСК')}</b> · {media}\n"
            f"{preview}{suffix}"
        )
    lines.append("\nПлан будет создан только после подтверждения.")
    await state.set_state(BroadcastState.confirming_batch)
    await message.answer(
        "\n".join(lines),
        reply_markup=get_broadcast_confirm_kb("broadcast:batch_confirm"),
        parse_mode="HTML",
    )


async def _show_scheduled_batches(message: Message) -> None:
    batches = await _list_future_scheduled_batches()
    if not batches:
        await message.answer("📭 Нет будущих подтверждённых планов 2–3 постов.")
        return
    await message.answer(
        "✏️ <b>Подтверждённые планы</b>\n\n"
        "Выберите план, чтобы изменить текст или время отдельного поста либо отменить весь план.",
        reply_markup=get_broadcast_batches_kb(batches),
        parse_mode="HTML",
    )


async def _show_scheduled_batch(message: Message, batch_key: str) -> bool:
    batch = await _get_broadcast_batch(batch_key)
    if batch is None:
        await message.answer("⚠️ План не найден или уже удалён.")
        return False
    lines = ["🗂 <b>Подтверждённый план</b>"]
    for item in batch["slots"]:
        when = (
            datetime.fromisoformat(item["publish_at"]).astimezone(MOSCOW).strftime(
                "%d.%m.%Y %H:%M МСК"
            )
            if item["publish_at"] else "время не задано"
        )
        preview = html.escape(item["body"][:160])
        suffix = "…" if len(item["body"]) > 160 else ""
        state_label = (
            f"готово для {item['scheduled_count']} каналов"
            if item["editable"]
            else f"отправка началась; осталось {item['scheduled_count']} из {item['total_count']}"
        )
        lines.append(
            f"\n<b>#{item['slot']} · {when}</b>\n{preview}{suffix}\n<i>{state_label}</i>"
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=get_broadcast_batch_manage_kb(batch_key, batch["slots"]),
        parse_mode="HTML",
    )
    return True


@router.message(F.text == "📣 Пост на все каналы")
async def start_broadcast_from_menu(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return
    await _begin_broadcast(message, state)


@router.callback_query(F.data == "broadcast:start")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await _begin_broadcast(callback.message, state)
    await callback.answer()


@router.message(F.text == "🗂 План 2–3 постов")
async def start_broadcast_batch_from_menu(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return
    await _begin_broadcast_batch(message, state)


@router.callback_query(F.data == "broadcast:batch_start")
async def start_broadcast_batch(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await _begin_broadcast_batch(callback.message, state)
    await callback.answer()


@router.message(F.text == "✏️ Подтверждённые планы")
async def list_broadcast_batches_from_menu(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Недостаточно прав.")
        return
    await state.clear()
    await _show_scheduled_batches(message)


@router.callback_query(F.data == "broadcast:manage_batches")
async def list_broadcast_batches(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await _show_scheduled_batches(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast:manage_batch:"))
async def open_broadcast_batch(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    batch_key = callback.data.removeprefix("broadcast:manage_batch:")
    if not BATCH_KEY_RE.fullmatch(batch_key):
        await callback.answer("Некорректный план.", show_alert=True)
        return
    await state.clear()
    await _show_scheduled_batch(callback.message, batch_key)
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast:edit_text:"))
async def prompt_edit_broadcast_batch_text(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    try:
        batch_key, raw_slot = callback.data.removeprefix("broadcast:edit_text:").rsplit(":", 1)
        slot = int(raw_slot)
    except ValueError:
        await callback.answer("Некорректный пост.", show_alert=True)
        return
    if not BATCH_KEY_RE.fullmatch(batch_key) or slot not in {1, 2, 3}:
        await callback.answer("Некорректный пост.", show_alert=True)
        return
    await state.clear()
    await state.update_data(manage_batch_key=batch_key, manage_batch_slot=slot)
    await state.set_state(BroadcastState.editing_batch_text)
    await callback.message.answer(
        f"✏️ Отправьте новый текст для поста #{slot}.\n"
        "Он изменится во всех каналах, где отправка ещё не началась."
    )
    await callback.answer()


@router.message(BroadcastState.editing_batch_text, F.text)
async def receive_broadcast_batch_edit_text(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    try:
        changed = await _update_scheduled_batch_slot(
            data.get("manage_batch_key", ""),
            int(data.get("manage_batch_slot", 0)),
            actor_id=message.from_user.id,
            body=message.text,
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    batch_key = data["manage_batch_key"]
    await state.clear()
    if changed:
        await message.answer(f"✅ Текст изменён в <b>{changed}</b> заданиях.", parse_mode="HTML")
    else:
        await message.answer("ℹ️ Текст не изменился.")
    await _show_scheduled_batch(message, batch_key)


@router.message(BroadcastState.editing_batch_text)
async def reject_broadcast_batch_edit_text(message: Message):
    await message.answer("⚠️ Отправьте новый текст одним сообщением.")


@router.callback_query(F.data.startswith("broadcast:edit_time:"))
async def prompt_edit_broadcast_batch_schedule(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    try:
        batch_key, raw_slot = callback.data.removeprefix("broadcast:edit_time:").rsplit(":", 1)
        slot = int(raw_slot)
    except ValueError:
        await callback.answer("Некорректный пост.", show_alert=True)
        return
    if not BATCH_KEY_RE.fullmatch(batch_key) or slot not in {1, 2, 3}:
        await callback.answer("Некорректный пост.", show_alert=True)
        return
    await state.clear()
    await state.update_data(manage_batch_key=batch_key, manage_batch_slot=slot)
    await state.set_state(BroadcastState.editing_batch_schedule)
    await callback.message.answer(
        f"🕓 Введите новое время поста #{slot}: "
        "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (МСК).",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BroadcastState.editing_batch_schedule, F.text)
async def receive_broadcast_batch_edit_schedule(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        scheduled = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(
            tzinfo=MOSCOW
        )
    except ValueError:
        await message.answer(
            "⚠️ Формат даты: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (МСК).",
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    try:
        changed = await _update_scheduled_batch_slot(
            data.get("manage_batch_key", ""),
            int(data.get("manage_batch_slot", 0)),
            actor_id=message.from_user.id,
            publish_at=scheduled.astimezone(timezone.utc).isoformat(),
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    batch_key = data["manage_batch_key"]
    await state.clear()
    if changed:
        await message.answer(f"✅ Время изменено в <b>{changed}</b> заданиях.", parse_mode="HTML")
    else:
        await message.answer("ℹ️ Время не изменилось.")
    await _show_scheduled_batch(message, batch_key)


@router.callback_query(F.data.startswith("broadcast:cancel_batch_prompt:"))
async def prompt_cancel_broadcast_batch(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    batch_key = callback.data.removeprefix("broadcast:cancel_batch_prompt:")
    if not BATCH_KEY_RE.fullmatch(batch_key):
        await callback.answer("Некорректный план.", show_alert=True)
        return
    await callback.message.answer(
        "⚠️ <b>Отменить весь план?</b>\n\n"
        "Будут отменены только публикации, отправка которых ещё не началась.",
        reply_markup=get_broadcast_batch_cancel_kb(batch_key),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast:cancel_batch:"))
async def cancel_scheduled_broadcast_batch(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    batch_key = callback.data.removeprefix("broadcast:cancel_batch:")
    try:
        result = await _cancel_scheduled_broadcast_batch(
            batch_key, actor_id=callback.from_user.id
        )
    except (ValueError, RuntimeError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    if result["cancelled"]:
        text = f"✅ Отменено заданий: <b>{result['cancelled']}</b>."
        if result["untouched"]:
            text += f"\nУже начались или завершились: <b>{result['untouched']}</b>."
    else:
        text = "ℹ️ В этом плане уже нет ожидающих публикаций."
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer("План обработан")


@router.callback_query(
    BroadcastState.choosing_batch_size, F.data.startswith("broadcast:batch_size:")
)
async def choose_broadcast_batch_size(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    try:
        size = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        size = 0
    if size not in {2, 3}:
        await callback.answer("Можно выбрать только 2 или 3 поста.", show_alert=True)
        return
    await state.update_data(broadcast_batch_size=size)
    await _prompt_batch_text(callback.message, state)
    await callback.answer()


@router.message(BroadcastState.entering_batch_text, F.text)
async def receive_broadcast_batch_text(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        await message.answer(f"⚠️ Текст должен содержать 1–{MAX_TEXT_LENGTH} символов.")
        return
    await state.update_data(broadcast_current_text=text)
    await state.set_state(BroadcastState.entering_batch_media)
    await message.answer(
        "📎 Пришлите фото, видео или голосовое для этого поста либо продолжите без медиа.",
        reply_markup=get_broadcast_photo_kb(),
    )


@router.message(BroadcastState.entering_batch_text)
async def reject_nontext_broadcast_batch_content(message: Message):
    await message.answer("⚠️ Нужен текст поста одним сообщением.")


@router.message(BroadcastState.entering_batch_media, F.photo | F.video | F.voice)
async def receive_broadcast_batch_media(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    if len(data.get("broadcast_current_text", "")) > MAX_PHOTO_CAPTION_LENGTH:
        await state.set_state(BroadcastState.entering_batch_text)
        await message.answer(
            "⚠️ Текст поста с медиа должен быть не длиннее 1024 символов. "
            "Отправьте сокращённый текст заново."
        )
        return
    media = _message_media(message)
    if media is None:
        await message.answer("⚠️ Не удалось распознать медиа.")
        return
    await state.update_data(
        broadcast_current_media_type=media[0],
        broadcast_current_media_file_id=media[1],
    )
    await _prompt_batch_schedule(message, state)


@router.callback_query(
    BroadcastState.entering_batch_media, F.data == "broadcast:skip_photo"
)
async def skip_broadcast_batch_media(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await state.update_data(
        broadcast_current_media_type=None,
        broadcast_current_media_file_id=None,
    )
    await _prompt_batch_schedule(callback.message, state)
    await callback.answer()


@router.message(BroadcastState.entering_batch_media)
async def reject_broadcast_batch_media(message: Message):
    await message.answer("⚠️ Пришлите фото, видео, голосовое или нажмите «Без медиа».")


@router.message(BroadcastState.entering_batch_schedule, F.text)
async def receive_broadcast_batch_schedule(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        scheduled = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(
            tzinfo=MOSCOW
        )
    except ValueError:
        await message.answer(
            "⚠️ Формат даты: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (МСК).",
            parse_mode="HTML",
        )
        return
    publish_at = scheduled.astimezone(timezone.utc)
    if publish_at <= datetime.now(timezone.utc):
        await message.answer("⚠️ Укажите дату и время в будущем.")
        return
    data = await state.get_data()
    items = list(data.get("broadcast_batch_items", []))
    items.append(
        {
            "text": data["broadcast_current_text"],
            "media_type": data.get("broadcast_current_media_type"),
            "media_file_id": data.get("broadcast_current_media_file_id"),
            "publish_at": publish_at.isoformat(),
        }
    )
    await state.update_data(broadcast_batch_items=items)
    if len(items) < int(data["broadcast_batch_size"]):
        await _prompt_batch_text(message, state)
        return
    await _show_channel_selection(message, state)


@router.message(BroadcastState.entering_text, F.text)
async def receive_broadcast_text(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if not text:
        await message.answer("⚠️ Текст не должен быть пустым.")
        return
    if len(text) > MAX_TEXT_LENGTH:
        await message.answer(f"⚠️ Текст слишком длинный: максимум {MAX_TEXT_LENGTH} символов.")
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(BroadcastState.entering_photo)
    await message.answer(
        "📎 Пришлите фото, видео или голосовое сообщение либо продолжите без медиа.",
        reply_markup=get_broadcast_photo_kb(),
    )


@router.message(BroadcastState.entering_text)
async def reject_nontext_broadcast_content(message: Message):
    await message.answer("⚠️ Нужен текст рассылки одним сообщением.")


@router.message(BroadcastState.entering_photo, F.photo | F.video | F.voice)
async def receive_broadcast_photo(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    if len(data.get("broadcast_text", "")) > MAX_PHOTO_CAPTION_LENGTH:
        await state.set_state(BroadcastState.entering_text)
        await message.answer(
            "⚠️ Для поста с медиа текст должен быть не длиннее 1024 символов. "
            "Отправьте сокращённый текст заново."
        )
        return
    media = _message_media(message)
    if media is None:
        await message.answer("⚠️ Не удалось распознать медиа.")
        return
    await state.update_data(
        broadcast_media_type=media[0],
        broadcast_media_file_id=media[1],
    )
    await _show_channel_selection(message, state)


@router.callback_query(BroadcastState.entering_photo, F.data == "broadcast:skip_photo")
async def skip_broadcast_photo(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await _show_channel_selection(callback.message, state)
    await callback.answer()


@router.message(BroadcastState.entering_photo)
async def reject_nonphoto_broadcast_media(message: Message):
    await message.answer("⚠️ Пришлите фото, видео, голосовое или нажмите «Без медиа».")


@router.callback_query(BroadcastState.selecting_channels, F.data.startswith("broadcast:toggle:"))
async def toggle_broadcast_channel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    try:
        channel_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный канал.", show_alert=True)
        return
    channels = await _active_channels()
    active_ids = {channel["id"] for channel in channels}
    if channel_id not in active_ids:
        await callback.answer("Канал больше не активен.", show_alert=True)
        return
    data = await state.get_data()
    selected_ids = set(data.get("broadcast_channel_ids", []))
    if channel_id in selected_ids:
        selected_ids.remove(channel_id)
    else:
        selected_ids.add(channel_id)
    await state.update_data(broadcast_channel_ids=list(selected_ids))
    await callback.message.edit_reply_markup(
        reply_markup=get_broadcast_channels_kb(channels, selected_ids)
    )
    await callback.answer()


@router.callback_query(BroadcastState.selecting_channels, F.data == "broadcast:channels_done")
async def finish_broadcast_channels(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    if not await _selected_channels(state):
        await callback.answer("Выберите хотя бы один канал.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("broadcast_batch_items"):
        await _show_batch_preview(callback.message, state)
        await callback.answer()
        return
    await state.set_state(BroadcastState.selecting_timing)
    await callback.message.answer(
        "🕓 Опубликовать рассылку сейчас или запланировать дату и время?",
        reply_markup=get_broadcast_timing_kb(),
    )
    await callback.answer()


@router.callback_query(BroadcastState.selecting_timing, F.data == "broadcast:now")
async def broadcast_now_preview(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await state.update_data(broadcast_publish_at=None)
    await _show_preview(callback.message, state)
    await callback.answer()


@router.callback_query(BroadcastState.selecting_timing, F.data == "broadcast:schedule")
async def prompt_broadcast_schedule(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await state.set_state(BroadcastState.entering_schedule)
    await callback.message.answer(
        "Введите дату и время в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (МСК).\n"
        "Время должно быть в будущем.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BroadcastState.entering_schedule, F.text)
async def receive_broadcast_schedule(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        scheduled = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW)
    except ValueError:
        await message.answer("⚠️ Формат даты: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (МСК).", parse_mode="HTML")
        return
    now = datetime.now(timezone.utc)
    if scheduled.astimezone(timezone.utc) <= now:
        await message.answer("⚠️ Укажите дату и время в будущем.")
        return
    await state.update_data(broadcast_publish_at=scheduled.astimezone(timezone.utc).isoformat())
    await _show_preview(message, state)


@router.callback_query(BroadcastState.confirming, F.data == "broadcast:confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    channels = await _selected_channels(state)
    text = data.get("broadcast_text", "")
    operation_key = data.get("broadcast_operation_key", "")
    if not channels or not text or not operation_key:
        await callback.answer("Данные рассылки устарели. Начните заново.", show_alert=True)
        await state.clear()
        return

    media_file_id = data.get("broadcast_media_file_id") or data.get("broadcast_photo_file_id")
    media_type = data.get("broadcast_media_type") or ("photo" if media_file_id else "text")
    publish_at = data.get("broadcast_publish_at")
    if not await _claim_broadcast_operation(
        operation_key, callback.from_user.id, publish_at
    ):
        await callback.answer("Рассылка уже подтверждена.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    status = "scheduled" if publish_at else "publishing"
    post_ids = await _create_broadcast_posts(
        channels,
        operation_key=operation_key,
        text=text,
        media_type=media_type,
        media_file_id=media_file_id,
        status=status,
        publish_at=publish_at,
        created_by=callback.from_user.id,
    )

    if publish_at:
        await _complete_broadcast_operation(operation_key, "scheduled")
        when = datetime.fromisoformat(publish_at).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M МСК")
        await callback.message.answer(
            f"✅ Запланировано: <b>{len(channels)}</b> пост(ов) на {when}.",
            parse_mode="HTML",
        )
        await callback.answer("Запланировано")
        return

    publisher = PublishingService(bot)
    successful, failed = [], []
    for channel in channels:
        try:
            if media_file_id:
                telegram_message = await publisher.publish_media(
                    channel_id=channel["channel_id"], media_type=media_type,
                    file_id=media_file_id, text=text,
                )
            else:
                telegram_message = await publisher.publish_text(
                    channel_id=channel["channel_id"], text=text,
                )
            await _record_broadcast_result(
                post_ids[channel["id"]], status="published",
                message_id=getattr(telegram_message, "message_id", None),
                actor_id=callback.from_user.id,
            )
            successful.append(_channel_title(channel))
        except Exception as exc:
            logger.exception("Broadcast failed for channel %s", channel["channel_id"])
            await _record_broadcast_result(
                post_ids[channel["id"]], status="publish_failed",
                error=type(exc).__name__, actor_id=callback.from_user.id,
            )
            failed.append(_channel_title(channel))

    lines = [f"📣 <b>Рассылка завершена</b>", f"Успешно: {len(successful)}, ошибок: {len(failed)}."]
    if successful:
        lines.append("\n✅ " + ", ".join(html.escape(name) for name in successful[:20]))
    if failed:
        lines.append("\n❌ " + ", ".join(html.escape(name) for name in failed[:20]))
    await _complete_broadcast_operation(
        operation_key, "partial" if failed else "published"
    )
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer("Рассылка завершена")


@router.callback_query(
    BroadcastState.confirming_batch, F.data == "broadcast:batch_confirm"
)
async def confirm_broadcast_batch(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    channels = await _selected_channels(state)
    items = data.get("broadcast_batch_items", [])
    batch_key = data.get("broadcast_batch_key", "")
    if not channels or len(items) not in {2, 3} or not batch_key:
        await callback.answer("Данные плана устарели. Начните заново.", show_alert=True)
        await state.clear()
        return
    created = await _create_scheduled_broadcast_batch(
        channels,
        batch_key=batch_key,
        items=items,
        created_by=callback.from_user.id,
    )
    if created is None:
        await callback.answer("Этот план уже подтверждён.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    await callback.message.answer(
        f"✅ План создан: <b>{len(items)}</b> публикации × "
        f"<b>{len(channels)}</b> каналов = <b>{created}</b> заданий.\n"
        "Каждый пост выйдет в своё указанное время.",
        parse_mode="HTML",
    )
    await callback.answer("План создан")


@router.callback_query(F.data == "broadcast:cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer("Рассылка отменена.")
    await callback.answer()
