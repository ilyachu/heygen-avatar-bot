from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from bot.migrations import configure_connection


REQUIRED_FIELDS = {
    "channel_id",
    "title",
    "description",
    "audience",
    "purpose",
    "key_meanings",
    "rubrics",
    "tone_of_voice",
    "cta_rules",
    "forbidden_topics",
    "source_message_count",
}


def load_profile_files(paths: Iterable[Path]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    seen: set[int] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON array")
        for profile in payload:
            missing = REQUIRED_FIELDS - profile.keys()
            if missing:
                raise ValueError(f"{path}: missing fields {sorted(missing)}")
            channel_id = int(profile["channel_id"])
            if channel_id in seen:
                raise ValueError(f"Duplicate channel_id: {channel_id}")
            seen.add(channel_id)
            profiles.append(profile)
    return profiles


async def import_profiles(db_path: str, profiles: list[dict[str, Any]]) -> int:
    async with aiosqlite.connect(db_path) as db:
        await configure_connection(db)
        await db.execute("BEGIN IMMEDIATE")
        try:
            for profile in profiles:
                external_id = str(profile["channel_id"])
                await db.execute(
                    """
                    INSERT INTO channels(channel_id, title, username, added_by)
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        title = excluded.title,
                        username = COALESCE(excluded.username, channels.username)
                    """,
                    (external_id, profile["title"].strip(), profile.get("username")),
                )
                cursor = await db.execute(
                    "SELECT id FROM channels WHERE channel_id = ?", (external_id,)
                )
                internal_id = (await cursor.fetchone())[0]
                await db.execute(
                    """
                    INSERT INTO channel_profiles (
                        channel_id, description, audience, purpose,
                        key_meanings_json, rubrics_json, tone_of_voice,
                        cta_rules, forbidden_topics_json, analysis_status,
                        analysis_version, source_message_count, analyzed_at, updated_at,
                        channel_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 1, ?, CURRENT_TIMESTAMP,
                              CURRENT_TIMESTAMP, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        description = excluded.description,
                        audience = excluded.audience,
                        purpose = excluded.purpose,
                        key_meanings_json = excluded.key_meanings_json,
                        rubrics_json = excluded.rubrics_json,
                        tone_of_voice = excluded.tone_of_voice,
                        cta_rules = excluded.cta_rules,
                        forbidden_topics_json = excluded.forbidden_topics_json,
                        analysis_status = 'ready',
                        analysis_version = channel_profiles.analysis_version + 1,
                        source_message_count = excluded.source_message_count,
                        analyzed_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP,
                        channel_kind = excluded.channel_kind
                    """,
                    (
                        internal_id,
                        profile["description"].strip(),
                        profile["audience"].strip(),
                        profile["purpose"].strip(),
                        json.dumps(profile["key_meanings"], ensure_ascii=False),
                        json.dumps(profile["rubrics"], ensure_ascii=False),
                        profile["tone_of_voice"].strip(),
                        profile["cta_rules"].strip(),
                        json.dumps(profile["forbidden_topics"], ensure_ascii=False),
                        int(profile["source_message_count"]),
                        profile.get("channel_kind", "thematic"),
                    ),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return len(profiles)
