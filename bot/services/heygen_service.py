import os
import json
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple
from bot.config import settings

logger = logging.getLogger(__name__)

def build_experts() -> Dict[str, Dict[str, Any]]:
    """Build the avatar catalog from environment settings (see docs/CONNECT_YOUR_AVATAR.md)."""
    avatar_id = settings.HEYGEN_AVATAR_ID.strip()
    if not avatar_id:
        return {}
    return {
        "expert": {
            "name": settings.HEYGEN_AVATAR_NAME,
            "gender": "",
            "looks": {
                "main": {
                    "id": avatar_id,
                    "group_id": settings.HEYGEN_AVATAR_GROUP_ID.strip(),
                    "name": settings.HEYGEN_AVATAR_LOOK_NAME,
                    "avatar_type": settings.HEYGEN_AVATAR_TYPE,
                    "category": settings.HEYGEN_AVATAR_CATEGORY,
                    "description": "",
                    "stories_fit": settings.HEYGEN_AVATAR_STORIES_FIT,
                }
            },
        }
    }


EXPERTS = build_experts()

VOICES: Dict[str, Any] = {}

class HeyGenClient:
    BASE_URL = "https://api.heygen.com"
    UPLOAD_URL = "https://upload.heygen.com"
    TOKEN_REFRESH_URL = "https://api2.heygen.com/v1/oauth/token"
    OAUTH_CLIENT_ID = "q2A2QRSke2LrFTPJhoDbHtXh"

    def __init__(self, api_key: str = settings.HEYGEN_API_KEY):
        self.api_key = api_key

    def _load_oauth_credentials(self) -> Optional[Dict[str, Any]]:
        candidates = [
            getattr(settings, "HEYGEN_OAUTH_PATH", "data/heygen_oauth.json"),
            os.path.expanduser("~/.heygen/credentials"),
            "/root/.heygen/credentials"
        ]
        for p in candidates:
            if p and os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if "oauth" in data and "access_token" in data["oauth"]:
                        return data
                except Exception as e:
                    logger.warning(f"Failed to read oauth credentials from {p}: {e}")
        return None

    def _save_oauth_credentials(self, data: Dict[str, Any]):
        save_path = getattr(settings, "HEYGEN_OAUTH_PATH", "data/heygen_oauth.json")
        try:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Updated OAuth credentials in {save_path}")
        except Exception as e:
            logger.error(f"Failed to save oauth credentials to {save_path}: {e}")

    async def _ensure_valid_token(self, force_refresh: bool = False) -> Optional[str]:
        creds = self._load_oauth_credentials()
        if not creds or "oauth" not in creds:
            return None

        oauth_data = creds["oauth"]
        token = oauth_data.get("access_token")
        refresh_token = oauth_data.get("refresh_token")
        expires_at_str = oauth_data.get("expires_at")

        is_expired = False
        if expires_at_str:
            try:
                exp_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                if (exp_dt - now_dt).total_seconds() < 300:
                    is_expired = True
            except Exception:
                pass

        should_refresh = force_refresh or is_expired

        if should_refresh and refresh_token:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        self.TOKEN_REFRESH_URL,
                        data={
                            "client_id": self.OAUTH_CLIENT_ID,
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token
                        }
                    )
                    if resp.status_code == 200:
                        new_data = resp.json()
                        oauth_data["access_token"] = new_data["access_token"]
                        if "refresh_token" in new_data:
                            oauth_data["refresh_token"] = new_data["refresh_token"]
                        exp_in = new_data.get("expires_in", 3600)
                        oauth_data["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=exp_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        creds["oauth"] = oauth_data
                        self._save_oauth_credentials(creds)
                        token = new_data["access_token"]
                        logger.info("Successfully refreshed HeyGen OAuth token!")
                        return token
                    else:
                        logger.error(f"HeyGen token refresh failed: {resp.status_code} {resp.text}")
                        if "invalid_grant" in resp.text:
                            logger.warning("HeyGen OAuth refresh token is invalid or revoked. Purging cached OAuth credentials.")
                            try:
                                save_path = getattr(settings, "HEYGEN_OAUTH_PATH", "data/heygen_oauth.json")
                                if os.path.exists(save_path):
                                    os.remove(save_path)
                            except Exception as del_err:
                                logger.error(f"Error removing invalid oauth file: {del_err}")
                        if is_expired or force_refresh:
                            return None
            except Exception as e:
                logger.error(f"Exception during HeyGen token refresh: {e}")
                if is_expired or force_refresh:
                    return None

        if is_expired:
            return None

        return token

    async def _get_headers(self, content_type: Optional[str] = "application/json", prefer_api_key: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": "application/json"
        }
        if content_type:
            headers["Content-Type"] = content_type

        if prefer_api_key and self.api_key:
            headers["X-Api-Key"] = self.api_key
            return headers

        token = await self._ensure_valid_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    async def get_balance_info(self) -> Dict[str, Any]:
        result = {"credits": 0, "wallet_usd": 0.0, "plan_credits": 0, "billing_type": "api", "plan": "unknown", "raw": {}}
        headers = await self._get_headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r1 = await client.get(f"{self.BASE_URL}/v2/user/remaining_quota", headers=headers)
                if r1.status_code == 401 and "Authorization" in headers and self.api_key:
                    headers = await self._get_headers(prefer_api_key=True)
                    r1 = await client.get(f"{self.BASE_URL}/v2/user/remaining_quota", headers=headers)
                if r1.status_code == 200:
                    data = r1.json().get("data", {})
                    result["credits"] = data.get("remaining_quota", 0)
                    result["plan_credits"] = data.get("details", {}).get("plan_credit", 0)
            except Exception as e:
                logger.error(f"Error fetching quota: {e}")

            try:
                r2 = await client.get(f"{self.BASE_URL}/v3/users/me", headers=headers)
                if r2.status_code == 401 and "Authorization" in headers and self.api_key:
                    headers = await self._get_headers(prefer_api_key=True)
                    r2 = await client.get(f"{self.BASE_URL}/v3/users/me", headers=headers)
                if r2.status_code == 200:
                    me_data = r2.json().get("data", {})
                    result["wallet_usd"] = me_data.get("wallet", {}).get("remaining_balance", 0.0)
                    result["email"] = me_data.get("email", "")
                    result["billing_type"] = me_data.get("billing_type", "api")
                    sub = me_data.get("subscription", {})
                    result["plan"] = sub.get("plan", "unknown")
                    rem = sub.get("credits", {}).get("premium_credits", {}).get("remaining")
                    if rem is not None:
                        result["plan_credits"] = rem
                        result["credits"] = rem
            except Exception as e:
                logger.error(f"Error fetching user info: {e}")

        return result

    async def upload_background_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[str]:
        headers = await self._get_headers(content_type=mime_type)
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                res = await client.post(
                    f"{self.UPLOAD_URL}/v1/asset",
                    headers=headers,
                    content=image_bytes
                )
                if res.status_code == 401 and "Authorization" in headers and self.api_key:
                    headers = await self._get_headers(content_type=mime_type, prefer_api_key=True)
                    res = await client.post(
                        f"{self.UPLOAD_URL}/v1/asset",
                        headers=headers,
                        content=image_bytes
                    )
                if res.status_code in [200, 201]:
                    data = res.json().get("data", {})
                    return data.get("url") or data.get("id")
                else:
                    logger.error(f"Failed to upload asset: {res.status_code} {res.text}")
            except Exception as e:
                logger.error(f"Exception uploading asset: {e}")
        return None

    async def upload_talking_photo(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[str]:
        headers = await self._get_headers(content_type=mime_type)
        async with httpx.AsyncClient(timeout=45.0) as client:
            try:
                res = await client.post(
                    f"{self.UPLOAD_URL}/v1/talking_photo",
                    headers=headers,
                    content=image_bytes
                )
                if res.status_code == 401 and "Authorization" in headers and self.api_key:
                    headers = await self._get_headers(content_type=mime_type, prefer_api_key=True)
                    res = await client.post(
                        f"{self.UPLOAD_URL}/v1/talking_photo",
                        headers=headers,
                        content=image_bytes
                    )
                if res.status_code in [200, 201]:
                    data = res.json().get("data", {})
                    talking_photo_id = data.get("talking_photo_id")
                    if talking_photo_id:
                        logger.info(f"HeyGen talking photo uploaded successfully: {talking_photo_id}")
                        return talking_photo_id
                logger.error(f"Failed to upload talking photo: {res.status_code} {res.text}")
            except Exception as e:
                logger.error(f"Exception uploading talking photo: {e}")
        return None

    async def create_avatar_video(
        self,
        avatar_id: str,
        voice_id: str,
        text: str,
        background: Dict[str, Any],
        avatar_type: str = "digital_twin",
        speed: float = 1.0,
        test_mode: bool = False,
        output_format: str = "circle",
    ) -> Tuple[bool, str]:
        experts = build_experts()
        # Allow passing a HeyGen "my avatars" group ID and resolve it to its look ID.
        for expert in experts.values():
            for look in expert["looks"].values():
                if avatar_id == look.get("group_id"):
                    avatar_id = look["id"]
                    avatar_type = look.get("avatar_type", avatar_type)

        is_portrait = any(
            look["id"] == avatar_id and look.get("stories_fit") == "best"
            for expert in experts.values() for look in expert["looks"].values()
        )
        if not any(look["id"] == avatar_id for expert in experts.values() for look in expert["looks"].values()):
            is_portrait = True

        if avatar_type == "photo_avatar":
            character_payload = {
                "type": "talking_photo",
                "talking_photo_id": avatar_id
            }
            # Always request the native source orientation from HeyGen:
            # - For landscape photos, request 16:9 (1280x720).
            # - For portrait photos, request 9:16 (720x1280).
            dimension = {"width": 720, "height": 1280} if is_portrait else {"width": 1280, "height": 720}
            aspect_ratio = "9:16" if is_portrait else "16:9"
            video_input = {
                "character": character_payload,
                "voice": {
                    "type": "text",
                    "input_text": text,
                    "voice_id": voice_id,
                    "speed": speed
                }
            }
        else:
            # Digital twin is natively landscape. For Stories we keep 16:9 from HeyGen
            # (asking for 9:16 only letterboxes/stretches) and compose a vertical frame in FFmpeg.
            # Keep "normal" (not closeUp): closeUp + cover-crop cuts forehead/chin.
            character_payload = {
                "type": "avatar",
                "avatar_id": avatar_id,
                "avatar_style": "normal",
            }
            dimension = {"width": 720, "height": 1280} if is_portrait else {"width": 1280, "height": 720}
            aspect_ratio = "9:16" if is_portrait else "16:9"
            bg_payload = {}
            if background.get("type") == "color":
                bg_payload = {"type": "color", "value": background.get("value", "#1E1E2E")}
            elif background.get("type") == "image":
                bg_payload = {"type": "image", "url": background.get("url")}
            else:
                bg_payload = {"type": "color", "value": "#1E1E2E"}
            video_input = {
                "character": character_payload,
                "voice": {
                    "type": "text",
                    "input_text": text,
                    "voice_id": voice_id,
                    "speed": speed
                },
                "background": bg_payload
            }

        payload = {
            "video_inputs": [video_input],
            "dimension": dimension,
            "aspect_ratio": aspect_ratio,
            "test": test_mode
        }

        headers = await self._get_headers()
        async with httpx.AsyncClient(timeout=25.0) as client:
            try:
                res = await client.post(
                    f"{self.BASE_URL}/v2/video/generate",
                    headers=headers,
                    json=payload
                )
                if res.status_code == 401:
                    new_token = await self._ensure_valid_token(force_refresh=True)
                    if new_token:
                        headers = await self._get_headers()
                    elif self.api_key:
                        headers = await self._get_headers(prefer_api_key=True)
                    res = await client.post(
                        f"{self.BASE_URL}/v2/video/generate",
                        headers=headers,
                        json=payload
                    )
                res_data = res.json()
                if res.status_code == 200 and res_data.get("data", {}).get("video_id"):
                    video_id = res_data["data"]["video_id"]
                    logger.info(f"HeyGen video created successfully: {video_id}")
                    return True, video_id
                else:
                    err_msg = res_data.get("error", {}).get("message") or res_data.get("message") or res.text
                    logger.error(f"HeyGen create video failed: {err_msg}")
                    return False, f"Ошибка HeyGen API: {err_msg}"
            except Exception as e:
                logger.error(f"Exception in create_avatar_video: {e}")
                return False, f"Сетевая ошибка: {str(e)}"

    async def poll_video_status(
        self,
        video_id: str,
        max_wait_sec: int = 600,
        progress_callback = None
    ) -> Tuple[str, Optional[str], Optional[str]]:
        start_time = asyncio.get_event_loop().time()
        poll_interval = 5
        last_progress_time = start_time
        headers = await self._get_headers()

        async with httpx.AsyncClient(timeout=15.0) as client:
            while (asyncio.get_event_loop().time() - start_time) < max_wait_sec:
                try:
                    res = await client.get(f"{self.BASE_URL}/v3/videos/{video_id}", headers=headers)
                    if res.status_code == 401 and "Authorization" in headers and self.api_key:
                        headers = await self._get_headers(prefer_api_key=True)
                        res = await client.get(f"{self.BASE_URL}/v3/videos/{video_id}", headers=headers)
                    if res.status_code == 200:
                        data = res.json().get("data", {})
                        status = data.get("status")
                        if status == "completed":
                            return "completed", data.get("video_url"), None
                        elif status == "failed":
                            err = data.get("error") or "Неизвестная ошибка рендера"
                            return "failed", None, str(err)
                    elif res.status_code == 404:
                        r_v1 = await client.get(f"{self.BASE_URL}/v1/video_status.get?video_id={video_id}", headers=headers)
                        if r_v1.status_code == 200:
                            v1_data = r_v1.json().get("data", {})
                            st = v1_data.get("status")
                            if st == "completed":
                                return "completed", v1_data.get("video_url"), None
                            elif st == "failed":
                                return "failed", None, v1_data.get("error")
                except Exception as e:
                    logger.warning(f"Polling transient error: {e}")

                now = asyncio.get_event_loop().time()
                if progress_callback and (now - last_progress_time) >= 10:
                    last_progress_time = now
                    elapsed_min = int((now - start_time) // 60)
                    elapsed_sec = int((now - start_time) % 60)
                    try:
                        await progress_callback(elapsed_min, elapsed_sec)
                    except Exception as e:
                        logger.debug(f"Progress callback error: {e}")

                await asyncio.sleep(poll_interval)

        logger.warning(f"Video {video_id} polling timed out after {max_wait_sec} seconds")
        return "timeout", None, f"Превышено время ожидания рендера ({max_wait_sec // 60} мин). Сервер HeyGen перегружен."

    async def generate_speech(self, voice_id: str, text: str, speed: float = 1.0) -> Tuple[bool, Optional[str], Optional[str]]:
        payload = {
            "voice_id": voice_id,
            "text": text,
            "speed": speed
        }
        headers = await self._get_headers()
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                res = await client.post(
                    f"{self.BASE_URL}/v3/voices/speech",
                    headers=headers,
                    json=payload
                )
                if res.status_code == 401:
                    logger.warning("generate_speech returned 401, attempting token refresh or fallback auth...")
                    new_token = await self._ensure_valid_token(force_refresh=True)
                    if new_token:
                        headers = await self._get_headers()
                    elif self.api_key:
                        headers = await self._get_headers(prefer_api_key=True)
                    res = await client.post(
                        f"{self.BASE_URL}/v3/voices/speech",
                        headers=headers,
                        json=payload
                    )
                elif res.status_code == 402 and "X-Api-Key" in headers:
                    # If using API key hit empty wallet, try OAuth subscription token if available
                    new_token = await self._ensure_valid_token()
                    if new_token:
                        headers = {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {new_token}"}
                        res = await client.post(
                            f"{self.BASE_URL}/v3/voices/speech",
                            headers=headers,
                            json=payload
                        )
                res_data = res.json()
                if res.status_code == 200 and res_data.get("data", {}).get("audio_url"):
                    audio_url = res_data["data"]["audio_url"]
                    return True, audio_url, None
                else:
                    err = res_data.get("error", {}).get("message") or res.text
                    return False, None, err
            except Exception as e:
                logger.error(f"Error generating speech in HeyGen: {e}")
                return False, None, str(e)

heygen_client = HeyGenClient()
