#!/usr/bin/env python3
"""
Securely save and verify HeyGen OAuth credentials.
Does NOT print secret tokens to stdout or logs.
Validates the token against GET https://api.heygen.com/v3/users/me.
"""

import os
import sys
import json
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx

HEYGEN_BASE_URL = "https://api.heygen.com"


async def verify_and_save(access_token: str, refresh_token: str = ""):
    access_token = access_token.strip().strip('"').strip("'")
    refresh_token = refresh_token.strip().strip('"').strip("'")

    if not access_token:
        print("❌ Ошибка: access_token не может быть пустым.")
        return False

    print("⏳ Проверяю токен в HeyGen API (GET /v3/users/me)...")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"{HEYGEN_BASE_URL}/v3/users/me", headers=headers)
        except Exception as e:
            print(f"❌ Ошибка сетевого запроса к HeyGen: {e}")
            return False

        if resp.status_code != 200:
            print(f"❌ Неверный или истекший токен: HTTP {resp.status_code}")
            try:
                err_json = resp.json()
                print(f"   Детали: {err_json.get('message') or err_json}")
            except Exception:
                print(f"   Ответ сервера: {resp.text[:200]}")
            return False

        user_data = resp.json().get("data", {})
        email = user_data.get("email", "unknown")
        username = user_data.get("username", user_data.get("id", ""))
        sub_info = user_data.get("subscription", {})
        plan_name = sub_info.get("plan", "Unknown")

        quota_resp = await client.get(f"{HEYGEN_BASE_URL}/v2/user/remaining_quota", headers=headers)
        credits_left = "н/д"
        if quota_resp.status_code == 200:
            credits_left = quota_resp.json().get("data", {}).get("remaining_quota", 0)

    expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    oauth_payload = {
        "oauth": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": "openid profile email",
            "token_type": "Bearer"
        },
        "user": {
            "email": email,
            "username": username
        }
    }

    local_target = Path(__file__).resolve().parent.parent / "data" / "heygen_oauth.json"
    local_target.parent.mkdir(parents=True, exist_ok=True)
    with open(local_target, "w", encoding="utf-8") as f:
        json.dump(oauth_payload, f, indent=2)
    os.chmod(local_target, 0o600)

    print("\n✅ УСПЕШНО! HeyGen OAuth сессия сохранена и проверена:")
    print(f"   👤 Аккаунт: {email}")
    print(f"   📋 Подписка: {plan_name}")
    print(f"   💳 Доступные кредиты плана: {credits_left}")
    print(f"   📁 Файл сохранён: {local_target}")

    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    access_token = ""
    refresh_token = ""

    if len(args) >= 1:
        raw = args[0]
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                if "oauth" in parsed:
                    access_token = parsed["oauth"].get("access_token", "")
                    refresh_token = parsed["oauth"].get("refresh_token", "")
                else:
                    access_token = parsed.get("access_token", "")
                    refresh_token = parsed.get("refresh_token", "")
        except Exception:
            access_token = raw
            if len(args) >= 2:
                refresh_token = args[1]

    if not access_token:
        print("=== Настройка HeyGen OAuth Сессии ===")
        print("Вставьте access_token (или JSON сессии):")
        try:
            line = input().strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                if "oauth" in parsed:
                    access_token = parsed["oauth"].get("access_token", "")
                    refresh_token = parsed["oauth"].get("refresh_token", "")
                else:
                    access_token = parsed.get("access_token", "")
                    refresh_token = parsed.get("refresh_token", "")
            else:
                access_token = line
                print("Вставьте refresh_token (если есть, иначе нажмите Enter):")
                refresh_token = input().strip()
        except (KeyboardInterrupt, EOFError):
            print("\nОтменено.")
            return

    asyncio.run(verify_and_save(access_token, refresh_token))


if __name__ == "__main__":
    main()
