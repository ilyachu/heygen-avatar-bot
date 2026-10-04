from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import aiosqlite
import httpx

from bot.migrations import configure_connection


POST_TYPES = ("useful", "warming", "selling")


class CampaignGenerationError(RuntimeError):
    pass


class ContentGenerator(Protocol):
    async def generate(self, campaign: dict[str, Any], channel: dict[str, Any]) -> list[dict[str, Any]]: ...


class OpenAICompatibleGenerator:
    def __init__(self, base_url: str, api_key: str, model: str):
        if not api_key or not model:
            raise CampaignGenerationError(
                "CONTENT_LLM_API_KEY and CONTENT_LLM_MODEL must be configured"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def generate(
        self, campaign: dict[str, Any], channel: dict[str, Any]
    ) -> list[dict[str, Any]]:
        prompt = _generation_prompt(campaign, channel)
        payload = {
            "model": self.model,
            "temperature": 0.7,
            "thinking": {"type": "disabled"},
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты редактор медицинского образовательного контента. "
                        "Не ставь диагнозы, не обещай лечение и гарантированный результат. "
                        "Верни только валидный JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            result = json.loads(content)
            return _validate_generated_posts(result.get("posts"))
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            raise CampaignGenerationError("LLM returned an invalid post set") from exc


class CampaignGenerationService:
    def __init__(self, db_path: str, generator: ContentGenerator):
        self.db_path = db_path
        self.generator = generator

    async def generate(self, webinar_id: int) -> dict[str, int]:
        campaign, channels = await self._load_campaign(webinar_id)
        if not channels:
            raise CampaignGenerationError("Campaign has no channels")
        await self._prepare_rows(campaign, channels)
        succeeded = 0
        failed = 0
        for channel in channels:
            try:
                posts = await self.generator.generate(campaign, channel)
                await self._save_success(campaign, channel, posts)
                succeeded += 3
            except Exception as exc:
                await self._save_failure(campaign, channel, type(exc).__name__)
                failed += 3
        await self._finish_campaign(webinar_id, failed)
        return {"succeeded": succeeded, "failed": failed}

    async def _load_campaign(
        self, webinar_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM webinars WHERE id = ?", (webinar_id,))
            row = await cursor.fetchone()
            if row is None:
                raise CampaignGenerationError("Campaign does not exist")
            campaign = dict(row)
            cursor = await db.execute(
                """
                SELECT c.id, c.title, cp.description, cp.audience AS profile_audience,
                       cp.key_meanings_json, cp.rubrics_json, cp.tone_of_voice,
                       cp.cta_rules, cp.forbidden_topics_json
                FROM webinar_channels wc JOIN channels c ON c.id = wc.channel_id
                LEFT JOIN channel_profiles cp ON cp.channel_id = c.id
                WHERE wc.webinar_id = ? ORDER BY c.title
                """,
                (webinar_id,),
            )
            channels = [dict(item) for item in await cursor.fetchall()]
        return campaign, channels

    async def _prepare_rows(
        self, campaign: dict[str, Any], channels: list[dict[str, Any]]
    ) -> None:
        starts_at = _parse_utc(campaign["starts_at"])
        dates = {
            "useful": starts_at - timedelta(days=3),
            "warming": starts_at - timedelta(days=2),
            "selling": starts_at - timedelta(days=1),
        }
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            for channel in channels:
                for post_type in POST_TYPES:
                    await db.execute(
                        """
                        INSERT INTO posts (
                            webinar_id, channel_id, post_type, status, publish_at
                        ) VALUES (?, ?, ?, 'generating', ?)
                        ON CONFLICT(webinar_id, channel_id, post_type) DO UPDATE SET
                            status = 'generating', generation_error = NULL,
                            publish_at = excluded.publish_at, updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            campaign["id"],
                            channel["id"],
                            post_type,
                            dates[post_type].isoformat(),
                        ),
                    )
            await db.commit()

    async def _save_success(
        self,
        campaign: dict[str, Any],
        channel: dict[str, Any],
        posts: list[dict[str, Any]],
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            for post in posts:
                cursor = await db.execute(
                    """
                    SELECT id, status FROM posts
                    WHERE webinar_id = ? AND channel_id = ? AND post_type = ?
                    """,
                    (campaign["id"], channel["id"], post["post_type"]),
                )
                row = await cursor.fetchone()
                await db.execute(
                    """
                    UPDATE posts SET topic = ?, body = ?, status = 'review',
                        generation_rationale = ?, generation_warnings_json = ?,
                        generation_error = NULL, version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        post["topic"],
                        post["body"],
                        post.get("rationale", ""),
                        json.dumps(post.get("warnings", []), ensure_ascii=False),
                        row[0],
                    ),
                )
                await _event(db, row[0], "generated", row[1], "review")
            await db.commit()

    async def _save_failure(
        self, campaign: dict[str, Any], channel: dict[str, Any], error: str
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT id, status FROM posts WHERE webinar_id = ? AND channel_id = ?
                """,
                (campaign["id"], channel["id"]),
            )
            for row in await cursor.fetchall():
                await db.execute(
                    """
                    UPDATE posts SET status = 'generation_failed', generation_error = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (error, row[0]),
                )
                await _event(db, row[0], "generation_failed", row[1], "generation_failed")
            await db.commit()

    async def _finish_campaign(self, webinar_id: int, failed: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute(
                "UPDATE webinars SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("generation_failed" if failed else "generated", webinar_id),
            )
            await db.commit()


def _generation_prompt(campaign: dict[str, Any], channel: dict[str, Any]) -> str:
    return f"""
Создай три самостоятельных Telegram-поста для канала «{channel['title']}»:
1) useful — расширяет понимание проблемы и даёт пользу без привязки к вебинару;
2) warming — помогает распознать проблему и формирует актуальность;
3) selling — логично подводит к предложению без давления.

Запуск: {campaign['title']}
Дата: {campaign['starts_at']}
Аудитория запуска: {campaign['audience']}
Проблема: {campaign['problem']}
Обещанный результат: {campaign['promise']}
Программа: {campaign['agenda']}
Предложение: {campaign['offer']}
CTA: {campaign['cta']}
Ссылка: {campaign['registration_url']}

Описание канала: {channel.get('description') or ''}
Аудитория канала: {channel.get('profile_audience') or ''}
Ключевые смыслы: {channel.get('key_meanings_json') or '[]'}
Рубрики: {channel.get('rubrics_json') or '[]'}
Tone of voice: {channel.get('tone_of_voice') or ''}
Правила CTA: {channel.get('cta_rules') or ''}
Запретные темы: {channel.get('forbidden_topics_json') or '[]'}

Общие правила заголовков: topic у каждого поста короткий, до 90 символов, один фокус.
Для useful отдельно:
- без ссылок, URL, дат вебинара и призывов на эфир/регистрацию;
- нейтральное обращение на «Вы»/«друзья», если аудитория канала явно не женская;
- бытовые примеры вместо абстракций; ориентир длины 900–2200 символов.
Для warming и selling допустимы ссылка и один CTA. Каждый текст — до 3500 символов,
короткие абзацы. Не ставь диагнозы, не обещай излечение и не выдумывай факты. Верни JSON:
{{"posts":[{{"post_type":"useful","topic":"...","body":"...","rationale":"...","warnings":[]}},
{{"post_type":"warming",...}},{{"post_type":"selling",...}}]}}
""".strip()


def _validate_generated_posts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Expected exactly three posts")
    by_type: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or item.get("post_type") not in POST_TYPES:
            raise ValueError("Unknown post type")
        topic = str(item.get("topic", "")).strip()
        body = str(item.get("body", "")).strip()
        if not topic or not body or len(topic) > 200 or len(body) > 4096:
            raise ValueError("Invalid generated post content")
        by_type[item["post_type"]] = {
            "post_type": item["post_type"],
            "topic": topic,
            "body": body,
            "rationale": str(item.get("rationale", ""))[:2000],
            "warnings": [str(warning)[:300] for warning in item.get("warnings", [])][:10],
        }
    if set(by_type) != set(POST_TYPES):
        raise ValueError("Missing post type")
    return [by_type[post_type] for post_type in POST_TYPES]


async def _event(db, post_id: int, event_type: str, from_status: str, to_status: str):
    await db.execute(
        """
        INSERT INTO post_events (
            post_id, event_type, from_status, to_status, actor_type, actor_id
        ) VALUES (?, ?, ?, ?, 'worker', 'generation')
        """,
        (post_id, event_type, from_status, to_status),
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
