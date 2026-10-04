import os
import uuid
import logging
import httpx
from typing import Optional, Tuple, Dict, Any
from bot.config import settings

logger = logging.getLogger(__name__)

# Available voice templates / emotions in MiniMax
MINIMAX_EMOTIONS = {
    'neutral': '😐 Нейтральная',
    'happy': '😊 Радостная / Энергичная',
    'serious': '💼 Серьёзная / Деловая',
    'curious': '🤔 Интригующая',
    'warm': '☕ Тёплая / Спокойная'
}

MINIMAX_VOICES = {
    'male_expert_1': {
        'name': '👨‍⚕️ Эксперт 1 (Клон голоса)',
        'voice_id': 'clone_expert_1',
        'gender': 'male'
    },
    'male_expert_2': {
        'name': '👨‍💼 Эксперт 2 (Клон голоса)',
        'voice_id': 'clone_expert_2',
        'gender': 'male'
    },
    'female_announcer': {
        'name': '👩 Диктор (Женский)',
        'voice_id': 'female-shaonv',
        'gender': 'female'
    },
    'male_announcer': {
        'name': '👨 Диктор (Мужской)',
        'voice_id': 'male-qn-qingse',
        'gender': 'male'
    }
}

class MiniMaxClient:
    BASE_URL = 'https://api.minimax.chat/v1/t2a_v2'

    def __init__(self, api_key: str = settings.MINIMAX_API_KEY, group_id: str = settings.MINIMAX_GROUP_ID):
        self.api_key = api_key
        self.group_id = group_id

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    async def generate_speech(
        self,
        text: str,
        voice_id: str = 'male-qn-qingse',
        speed: float = 1.0,
        emotion: str = 'neutral',
        pitch: int = 0
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        if not self.is_configured:
            return False, None, 'Ключ MiniMax API ещё не настроен в файле .env. Добавьте MINIMAX_API_KEY для генерации речи.'

        url = f'{self.BASE_URL}?GroupId={self.group_id}' if self.group_id else self.BASE_URL
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

        payload = {
            'model': 'speech-01-turbo',
            'text': text,
            'stream': False,
            'voice_setting': {
                'voice_id': voice_id,
                'speed': speed,
                'vol': 1.0,
                'pitch': pitch,
                'emotion': emotion
            },
            'audio_setting': {
                'sample_rate': 32000,
                'bitrate': 128000,
                'format': 'mp3',
                'channel': 1
            }
        }

        output_path = os.path.join(settings.TEMP_DIR, f'audio_{uuid.uuid4().hex[:8]}.mp3')

        async with httpx.AsyncClient(timeout=45.0) as client:
            try:
                res = await client.post(url, headers=headers, json=payload)
                data = res.json()

                if res.status_code == 200 and data.get('base_resp', {}).get('status_code') == 0:
                    # Audio data is returned in hex or extra info, or direct bytes
                    audio_hex = data.get('data', {}).get('audio')
                    if audio_hex:
                        with open(output_path, 'wb') as f:
                            f.write(bytes.fromhex(audio_hex))
                        return True, output_path, None
                    else:
                        return False, None, 'MiniMax вернул пустую аудиодорожку'
                else:
                    msg = data.get('base_resp', {}).get('status_msg') or res.text
                    logger.error(f'MiniMax error: {msg}')
                    return False, None, f'Ошибка MiniMax: {msg}'
            except Exception as e:
                logger.error(f'Exception in MiniMaxClient: {e}')
                return False, None, str(e)

minimax_client = MiniMaxClient()
