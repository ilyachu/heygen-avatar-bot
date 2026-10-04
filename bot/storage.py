import json
import logging
import aiosqlite
from typing import Optional, Dict, Any
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.fsm.state import State
from bot.migrations import configure_connection

logger = logging.getLogger(__name__)

class SqliteStorage(BaseStorage):
    """
    Persistent FSM storage backed by SQLite (bot.db).
    Ensures that user state and session data survive bot restarts, Docker rebuilds, and server reboots.
    """
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fsm_storage (
                    bot_id INTEGER,
                    chat_id INTEGER,
                    user_id INTEGER,
                    destiny TEXT,
                    state TEXT,
                    data TEXT DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (bot_id, chat_id, user_id, destiny)
                )
            """)
            await db.commit()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str = state.state if isinstance(state, State) else state
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("""
                INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data, updated_at)
                VALUES (?, ?, ?, ?, ?, '{}', CURRENT_TIMESTAMP)
                ON CONFLICT(bot_id, chat_id, user_id, destiny) DO UPDATE SET 
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
            """, (key.bot_id, key.chat_id, key.user_id, key.destiny or "default", state_str))
            await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            async with db.execute(
                "SELECT state FROM fsm_storage WHERE bot_id=? AND chat_id=? AND user_id=? AND destiny=?",
                (key.bot_id, key.chat_id, key.user_id, key.destiny or "default")
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        data_str = json.dumps(data, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute("""
                INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data, updated_at)
                VALUES (?, ?, ?, ?, NULL, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bot_id, chat_id, user_id, destiny) DO UPDATE SET 
                    data = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
            """, (key.bot_id, key.chat_id, key.user_id, key.destiny or "default", data_str))
            await db.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            async with db.execute(
                "SELECT data FROM fsm_storage WHERE bot_id=? AND chat_id=? AND user_id=? AND destiny=?",
                (key.bot_id, key.chat_id, key.user_id, key.destiny or "default")
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        return json.loads(row[0])
                    except Exception:
                        return {}
                return {}

    async def close(self) -> None:
        pass
