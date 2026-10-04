from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def configure_connection(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 5000")


async def run_migrations(db_path: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    applied: list[str] = []
    async with aiosqlite.connect(db_path) as db:
        await configure_connection(db)
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()

        for migration_path in sorted(migrations_dir.glob("*.sql")):
            version = migration_path.name
            script = migration_path.read_text(encoding="utf-8")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                )
                if await cursor.fetchone():
                    await db.commit()
                    continue
                for statement in _sql_statements(script):
                    await db.execute(statement)
                await db.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            applied.append(version)
            logger.info("Applied database migration %s", version)

    return applied


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise ValueError("Migration contains an incomplete SQL statement")
    return statements
