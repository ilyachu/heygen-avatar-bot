import os
from typing import List, Set
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

class Settings(BaseSettings):
    BOT_TOKEN: str
    BOT_MODE: str = 'content'
    CONTENT_BOT_ID: int = 0
    MEDIA_BOT_ID: int = 0
    ADMIN_IDS: str = ''
    
    HEYGEN_API_KEY: str
    HEYGEN_TEST_MODE: bool = False
    HEYGEN_OAUTH_PATH: str = 'data/heygen_oauth.json'
    HEYGEN_DEFAULT_VOICE_ID: str = ''
    # Your own HeyGen avatar. See docs/CONNECT_YOUR_AVATAR.md.
    HEYGEN_AVATAR_ID: str = ''
    HEYGEN_AVATAR_GROUP_ID: str = ''
    HEYGEN_AVATAR_TYPE: str = 'digital_twin'
    HEYGEN_AVATAR_NAME: str = 'Эксперт'
    HEYGEN_AVATAR_LOOK_NAME: str = 'Основной образ'
    HEYGEN_AVATAR_CATEGORY: str = 'live'
    HEYGEN_AVATAR_STORIES_FIT: str = 'best'
    
    MINIMAX_API_KEY: str = ''
    MINIMAX_GROUP_ID: str = ''
    
    MAX_TEXT_WORDS: int = 125
    MAX_STORIES_TEXT_WORDS: int = 200
    DB_PATH: str = 'data/bot.db'
    TEMP_DIR: str = 'temp'
    WEB_BASE_URL: str = 'http://localhost:8000'
    WEB_SESSION_SECRET: str = ''
    WEB_COOKIE_SECURE: bool = True
    WEB_SESSION_DAYS: int = 7
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ''
    TELEGRAM_PHONE: str = ''
    TELETHON_SESSION_PATH: str = 'data/telethon/content_account'
    CONTENT_LLM_BASE_URL: str = 'https://api.openai.com/v1'
    CONTENT_LLM_API_KEY: str = ''
    CONTENT_LLM_MODEL: str = ''
    APPROVAL_USER_ID: int = 0
    SCHEDULER_INTERVAL_SECONDS: int = 20
    CONTENT_JOB_IDLE_SECONDS: float = 2.0
    WEEKLY_ENQUEUE_INTERVAL_SECONDS: int = 900
    METRICS_INTERVAL_SECONDS: int = 900

    @field_validator('BOT_MODE')
    @classmethod
    def validate_bot_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {'content', 'media'}:
            raise ValueError('BOT_MODE must be content or media')
        return mode

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )

    @property
    def admin_id_list(self) -> Set[int]:
        ids = set()
        for item in self.ADMIN_IDS.split(','):
            item = item.strip()
            if item.isdigit():
                ids.add(int(item))
        return ids

    @property
    def approval_user_id(self) -> int:
        return self.APPROVAL_USER_ID or min(self.admin_id_list)

    @property
    def log_path(self) -> str:
        return f'data/{self.BOT_MODE}-bot.log'

    @property
    def expected_bot_id(self) -> int:
        return self.CONTENT_BOT_ID if self.BOT_MODE == 'content' else self.MEDIA_BOT_ID

settings = Settings()

# Ensure directories exist
os.makedirs(os.path.dirname(settings.DB_PATH) if os.path.dirname(settings.DB_PATH) else '.', exist_ok=True)
os.makedirs(settings.TEMP_DIR, exist_ok=True)
