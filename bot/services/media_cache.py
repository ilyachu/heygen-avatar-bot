import uuid
from typing import Dict, Any, Optional

_CACHE: Dict[str, Dict[str, Any]] = {}

def store_media(media_type: str, file_id: str) -> str:
    key = uuid.uuid4().hex[:8]
    _CACHE[key] = {
        "media_type": media_type,
        "file_id": file_id
    }
    if len(_CACHE) > 500:
        oldest_keys = list(_CACHE.keys())[:100]
        for k in oldest_keys:
            _CACHE.pop(k, None)
    return key

def get_media(key: str) -> Optional[Dict[str, Any]]:
    return _CACHE.get(key)
