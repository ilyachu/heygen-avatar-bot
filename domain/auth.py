from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite

from bot.migrations import configure_connection


class AuthenticationError(ValueError):
    pass


class AuthService:
    LOGIN_TTL = timedelta(minutes=10)

    def __init__(self, db_path: str, secret: str, session_days: int = 7):
        if len(secret) < 32:
            raise ValueError("WEB_SESSION_SECRET must contain at least 32 characters")
        self.db_path = db_path
        self.secret = secret.encode("utf-8")
        self.session_ttl = timedelta(days=session_days)

    async def create_login_token(self, user_id: int) -> str:
        raw_token = secrets.token_urlsafe(32)
        expires_at = _utc_now() + self.LOGIN_TTL
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            cursor = await db.execute(
                "SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)
            )
            if await cursor.fetchone() is None:
                raise AuthenticationError("User is not allowed to access the web panel")
            await db.execute(
                "INSERT INTO login_tokens(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (_hash(raw_token), user_id, expires_at.isoformat()),
            )
            await db.commit()
        return raw_token

    async def exchange_login_token(self, raw_token: str) -> str:
        if not raw_token or len(raw_token) > 256:
            raise AuthenticationError("Invalid or expired login link")
        now = _utc_now()
        session_raw = secrets.token_urlsafe(32)
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT id, user_id, expires_at FROM login_tokens
                    WHERE token_hash = ? AND used_at IS NULL
                    """,
                    (_hash(raw_token),),
                )
                token = await cursor.fetchone()
                if token is None or _parse_utc(token["expires_at"]) <= now:
                    raise AuthenticationError("Invalid or expired login link")
                cursor = await db.execute(
                    "UPDATE login_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
                    (now.isoformat(), token["id"]),
                )
                if cursor.rowcount != 1:
                    raise AuthenticationError("Invalid or expired login link")
                await db.execute(
                    "INSERT INTO web_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                    (
                        _hash(session_raw),
                        token["user_id"],
                        (now + self.session_ttl).isoformat(),
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return self._sign(session_raw)

    async def get_session_user(self, signed_token: str | None) -> int | None:
        raw_token = self._verify(signed_token)
        if raw_token is None:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            cursor = await db.execute(
                """
                SELECT s.user_id
                FROM web_sessions s
                JOIN whitelist w ON w.user_id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                """,
                (_hash(raw_token), _utc_now().isoformat()),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else None

    async def revoke_session(self, signed_token: str | None) -> None:
        raw_token = self._verify(signed_token)
        if raw_token is None:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await configure_connection(db)
            await db.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE token_hash = ?",
                (_utc_now().isoformat(), _hash(raw_token)),
            )
            await db.commit()

    def csrf_token(self, signed_token: str) -> str:
        return hmac.new(self.secret, f"csrf:{signed_token}".encode(), hashlib.sha256).hexdigest()

    def validate_csrf(self, signed_token: str | None, supplied_token: str | None) -> bool:
        if not signed_token or not supplied_token or self._verify(signed_token) is None:
            return False
        return hmac.compare_digest(self.csrf_token(signed_token), supplied_token)

    def _sign(self, raw_token: str) -> str:
        signature = hmac.new(self.secret, raw_token.encode(), hashlib.sha256).hexdigest()
        return f"{raw_token}.{signature}"

    def _verify(self, signed_token: str | None) -> str | None:
        if not signed_token or len(signed_token) > 512:
            return None
        try:
            raw_token, signature = signed_token.rsplit(".", 1)
        except ValueError:
            return None
        expected = hmac.new(self.secret, raw_token.encode(), hashlib.sha256).hexdigest()
        return raw_token if hmac.compare_digest(signature, expected) else None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
