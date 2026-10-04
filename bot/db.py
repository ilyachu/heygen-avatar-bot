
import aiosqlite
import logging
from typing import Set, Optional, Dict, Any
from bot.config import settings
from bot.migrations import configure_connection, run_migrations

logger = logging.getLogger(__name__)

CREATE_WHITELIST = '\nCREATE TABLE IF NOT EXISTS whitelist (\n    user_id INTEGER PRIMARY KEY,\n    username TEXT,\n    full_name TEXT,\n    added_by INTEGER,\n    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n)\n'

CREATE_SETTINGS = "\nCREATE TABLE IF NOT EXISTS user_settings (\n    user_id INTEGER PRIMARY KEY,\n    expert_id TEXT DEFAULT '',\n    voice_id TEXT DEFAULT '',\n    background_type TEXT DEFAULT 'preset',\n    background_value TEXT DEFAULT 'studio',\n    speed REAL DEFAULT 1.0,\n    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n)\n"

CREATE_GENS = '\nCREATE TABLE IF NOT EXISTS generations (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    user_id INTEGER,\n    gen_type TEXT,\n    prompt TEXT,\n    meta_json TEXT,\n    status TEXT,\n    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n)\n'

CREATE_CHANNELS = '\nCREATE TABLE IF NOT EXISTS channels (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    channel_id TEXT UNIQUE,\n    title TEXT,\n    username TEXT,\n    added_by INTEGER,\n    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n)\n'

async def init_db():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute('PRAGMA journal_mode = WAL')
        await db.execute(CREATE_WHITELIST)
        await db.execute(CREATE_SETTINGS)
        await db.execute(CREATE_GENS)
        await db.execute(CREATE_CHANNELS)
        for admin_id in settings.admin_id_list:
            await db.execute(
                'INSERT OR IGNORE INTO whitelist (user_id, username, full_name, added_by) VALUES (?, ?, ?, ?)',
                (admin_id, 'admin', 'Root Administrator', 0)
            )
        await db.commit()
    await run_migrations(settings.DB_PATH)
    logger.info('Database initialized.')

async def is_user_whitelisted(user_id: int) -> bool:
    if user_id in settings.admin_id_list:
        return True
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        async with db.execute('SELECT 1 FROM whitelist WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def add_to_whitelist(user_id: int, username: str = '', full_name: str = '', added_by: int = 0) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        try:
            await db.execute(
                'INSERT OR REPLACE INTO whitelist (user_id, username, full_name, added_by) VALUES (?, ?, ?, ?)',
                (user_id, username, full_name, added_by)
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error(f'Error adding {user_id} to whitelist: {e}')
            return False

async def remove_from_whitelist(user_id: int) -> bool:
    if user_id in settings.admin_id_list:
        return False
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute('DELETE FROM whitelist WHERE user_id = ?', (user_id,))
        await db.commit()
        return True

async def get_all_whitelisted():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM whitelist ORDER BY created_at DESC') as cursor:
            return await cursor.fetchall()

async def add_channel(channel_id: str, title: str = '', username: str = '', added_by: int = 0) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        try:
            await db.execute(
                'INSERT OR REPLACE INTO channels (channel_id, title, username, added_by) VALUES (?, ?, ?, ?)',
                (str(channel_id), title, username, added_by)
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error(f'Error adding channel {channel_id}: {e}')
            return False

async def remove_channel(channel_id: str) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute('DELETE FROM channels WHERE channel_id = ?', (str(channel_id),))
        await db.commit()
        return True

async def get_all_channels():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM channels ORDER BY created_at DESC') as cursor:
            return await cursor.fetchall()

async def get_user_settings(user_id: int) -> Dict[str, Any]:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM user_settings WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {
                'user_id': user_id,
                'expert_id': '',
                'voice_id': '',
                'background_type': 'preset',
                'background_value': 'studio',
                'speed': 1.0
            }

async def update_user_settings(user_id: int, **kwargs):
    if not kwargs:
        return
    set_clause = ', '.join([f'{k} = ?' for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        await db.execute('INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)', (user_id,))
        await db.execute(f'UPDATE user_settings SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', values)
        await db.commit()

async def log_generation(user_id: int, gen_type: str, prompt: str, status: str = 'pending', meta_json: str = '') -> int:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        cursor = await db.execute(
            'INSERT INTO generations (user_id, gen_type, prompt, status, meta_json) VALUES (?, ?, ?, ?, ?)',
            (user_id, gen_type, prompt, status, meta_json)
        )
        await db.commit()
        return cursor.lastrowid

async def update_generation_status(gen_id: int, status: str, meta_json: Optional[str] = None):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        if meta_json:
            await db.execute(
                'UPDATE generations SET status = ?, meta_json = ? WHERE id = ?',
                (status, meta_json, gen_id)
            )
        else:
            await db.execute(
                'UPDATE generations SET status = ? WHERE id = ?',
                (status, gen_id)
            )
        await db.commit()

async def get_recent_generations(limit: int = 10):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM generations ORDER BY created_at DESC LIMIT ?', (limit,)) as cursor:
            return await cursor.fetchall()
