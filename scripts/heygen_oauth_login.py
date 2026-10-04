#!/usr/bin/env python3
"""
HeyGen OAuth 2.0 PKCE Authorization Helper

Runs a local callback server on 127.0.0.1 to capture the OAuth authorization code,
exchanges it for access and refresh tokens, checks the account and subscription,
and saves the credentials to data/heygen_oauth.json.
"""

import os
import sys
import json
import base64
import hashlib
import secrets
import argparse
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from datetime import datetime, timezone, timedelta

try:
    import httpx
except ImportError:
    import urllib.request
    import urllib.error
    httpx = None


CLIENT_ID = "q2A2QRSke2LrFTPJhoDbHtXh"
TOKEN_ENDPOINT = "https://api2.heygen.com/v1/oauth/token"
USER_ME_ENDPOINT = "https://api.heygen.com/v3/users/me"
AUTH_ENDPOINT = "https://app.heygen.com/oauth/authorize"


def generate_pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    
    if httpx:
        resp = httpx.post(TOKEN_ENDPOINT, data=payload, timeout=20.0)
        if resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")
        return resp.json()
    else:
        encoded_data = urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=encoded_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Token exchange failed ({e.code}): {err_body}")


def fetch_user_me(access_token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    if httpx:
        resp = httpx.get(USER_ME_ENDPOINT, headers=headers, timeout=15.0)
        if resp.status_code == 200:
            return resp.json().get("data", {})
        return {}
    else:
        req = urllib.request.Request(USER_ME_ENDPOINT, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", {})
        except Exception:
            return {}


def run_oauth_flow(output_path: str, port: int = 0, open_browser: bool = True, timeout: int = 300):
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)

    flow_state = {
        "done": False,
        "error": None,
        "result": None,
    }
    done_event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default server logs

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            qs = parse_qs(parsed.query)
            error_param = qs.get("error", [None])[0]
            if error_param:
                err_desc = qs.get("error_description", [error_param])[0]
                flow_state["error"] = f"OAuth Error: {err_desc}"
                self._send_html_response(False, "Ошибка авторизации", f"HeyGen вернул ошибку: {err_desc}")
                done_event.set()
                return

            req_state = qs.get("state", [None])[0]
            if req_state != state:
                flow_state["error"] = "Неверный параметр state (CSRF guard)."
                self._send_html_response(False, "Ошибка безопасности", "Параметр state не совпал. Повторите вход.")
                done_event.set()
                return

            code = qs.get("code", [None])[0]
            if not code:
                flow_state["error"] = "Параметр code отсутствует в ответе."
                self._send_html_response(False, "Ошибка", "Код авторизации не получен.")
                done_event.set()
                return

            try:
                redirect_uri = f"http://127.0.0.1:{server_port}/oauth/callback"
                token_data = exchange_code(code, verifier, redirect_uri)
                access_token = token_data.get("access_token")
                refresh_token = token_data.get("refresh_token")
                expires_in = token_data.get("expires_in", 86400)
                expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")

                user_info = fetch_user_me(access_token)
                email = user_info.get("email") or "не указан"
                username = user_info.get("username") or ""
                sub = user_info.get("subscription", {})
                plan = sub.get("plan", "Unknown")
                credits = sub.get("credits", {}).get("premium_credits", {}).get("remaining", "N/A")

                session_payload = {
                    "oauth": {
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "expires_at": expires_at,
                        "scope": token_data.get("scope", "openid profile email"),
                        "token_type": token_data.get("token_type", "Bearer")
                    },
                    "user": {
                        "email": email,
                        "username": username
                    }
                }

                out_abs = os.path.abspath(output_path)
                os.makedirs(os.path.dirname(out_abs), exist_ok=True)
                with open(out_abs, "w", encoding="utf-8") as f:
                    json.dump(session_payload, f, indent=2, ensure_ascii=False)
                os.chmod(out_abs, 0o600)

                flow_state["result"] = {
                    "email": email,
                    "plan": plan,
                    "credits": credits,
                    "output_path": out_abs
                }

                html_msg = f"""
                <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 520px; margin: 40px auto; padding: 24px; border-radius: 12px; background: #ffffff; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
                    <div style="font-size: 40px; margin-bottom: 12px;">✅</div>
                    <h2 style="color: #0f172a; margin: 0 0 12px 0;">Успешная авторизация в HeyGen!</h2>
                    <p style="color: #475569; font-size: 15px; line-height: 1.5;">Токен получен и сохранен для бота.</p>
                    <div style="background: #f8fafc; border-radius: 8px; padding: 12px 16px; margin: 16px 0; font-size: 14px; color: #334155;">
                        <p style="margin: 4px 0;"><strong>Аккаунт:</strong> {email}</p>
                        <p style="margin: 4px 0;"><strong>Тариф:</strong> {plan}</p>
                        <p style="margin: 4px 0;"><strong>Остаток кредитов подписки:</strong> {credits}</p>
                    </div>
                    <p style="color: #64748b; font-size: 13px; margin-top: 16px;">Эту страницу можно закрыть. Возвращайтесь в консоль/чат.</p>
                </div>
                """
                self._send_html_response(True, "Авторизация завершена", html_msg)
            except Exception as e:
                flow_state["error"] = str(e)
                self._send_html_response(False, "Ошибка обработки токена", f"<p style='color:red;'>{str(e)}</p>")
            finally:
                done_event.set()

        def _send_html_response(self, success: bool, title: str, body: str):
            status = 200 if success else 400
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
</head>
<body style="background: #f1f5f9; padding: 20px; text-align: center;">
    {body}
</body>
</html>"""
            self.wfile.write(full_html.encode("utf-8"))

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server_port = server.server_port

    redirect_uri = f"http://127.0.0.1:{server_port}/oauth/callback"
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile email",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri
    }
    auth_url = f"{AUTH_ENDPOINT}?{urlencode(params)}"

    print("=" * 70)
    print("🔑 HEYGEN OAUTH AUTHORIZATION")
    print("=" * 70)
    print(f"Файл назначения: {os.path.abspath(output_path)}")
    print(f"Локальный callback-сервер: {redirect_uri}")
    print("\n👉 Откройте следующую ссылку в браузере:")
    print(f"\n{auth_url}\n")
    print("=" * 70)
    print("Ожидание подтверждения входа... (нажмите Ctrl+C для отмены)")
    sys.stdout.flush()

    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    finished = done_event.wait(timeout=timeout)
    server.shutdown()
    server.server_close()

    if not finished:
        print("\n❌ Время ожидания истекло (timeout). Авторизация не была завершена.")
        return 1

    if flow_state["error"]:
        print(f"\n❌ Ошибка авторизации: {flow_state['error']}")
        return 1

    res = flow_state["result"]
    print("\n✅ АВТОРИЗАЦИЯ УСПЕШНО ЗАВЕРШЕНА!")
    print(f"📧 Email: {res['email']}")
    print(f"📦 Тариф: {res['plan']}")
    print(f"💳 Кредиты подписки: {res['credits']}")
    print(f"💾 Сессия сохранена в: {res['output_path']}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="HeyGen OAuth 2.0 PKCE Login")
    parser.add_argument(
        "--output", "-o",
        default="data/heygen_oauth.json",
        help="Путь для сохранения heygen_oauth.json (по умолчанию: data/heygen_oauth.json)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=0,
        help="Порт для callback-сервера (0 = случайный свободный порт)"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Не открывать браузер автоматически"
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=300,
        help="Таймаут ожидания авторизации в секундах (по умолчанию 300)"
    )
    args = parser.parse_args()

    exit_code = run_oauth_flow(
        output_path=args.output,
        port=args.port,
        open_browser=not args.no_browser,
        timeout=args.timeout
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
