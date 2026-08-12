from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from db import first_row, run

PBKDF2_ITERATIONS = 160_000


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(_b64(digest), expected)
    except (ValueError, TypeError):
        return False


def _jwt_encode(payload: dict[str, Any], secret: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{signature}"


def _jwt_decode(token: str, secret: str) -> dict[str, Any] | None:
    try:
        header, body, signature = token.split(".", 2)
        expected = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body).decode())
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _secret(env: Any) -> str:
    value = getattr(env, "JWT_SECRET", "")
    if not value or len(value) < 32:
        raise ValueError("JWT_SECRET must be configured with at least 32 characters")
    return str(value)


async def issue_tokens(db: Any, env: Any, user: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    access_ttl = int(getattr(env, "JWT_ACCESS_TTL_SECONDS", "900"))
    refresh_ttl = int(getattr(env, "JWT_REFRESH_TTL_SECONDS", "2592000"))
    session_id = secrets.token_urlsafe(24)
    access = _jwt_encode({"sub": user["id"], "sid": session_id, "kind": "access", "iat": now, "exp": now + access_ttl}, _secret(env))
    refresh = secrets.token_urlsafe(48)
    refresh_hash = hashlib.sha256(refresh.encode()).hexdigest()
    await run(
        db,
        "INSERT INTO refresh_sessions(id,user_id,refresh_hash,expires_at) VALUES(?,?,?,datetime('now',?))",
        session_id,
        user["id"],
        refresh_hash,
        f"+{refresh_ttl} seconds",
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": public_user(user),
    }


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name"),
        "avatar_url": None,
        "is_admin": bool(user.get("is_admin")),
    }


async def current_user(request: Any, env: Any, db: Any) -> dict[str, Any]:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise PermissionError("Authentication required")
    payload = _jwt_decode(header.split(" ", 1)[1].strip(), _secret(env))
    if not payload or payload.get("kind") != "access":
        raise PermissionError("Invalid or expired token")
    row = await first_row(
        db,
        "SELECT u.* FROM users u JOIN refresh_sessions s ON s.user_id=u.id WHERE u.id=? AND s.id=? AND s.revoked_at IS NULL AND s.expires_at>datetime('now')",
        payload.get("sub"),
        payload.get("sid"),
    )
    if not row or not row.get("is_active") or row.get("is_banned"):
        raise PermissionError("Account unavailable")
    return row


def require_admin(user: dict[str, Any]) -> None:
    if not user.get("is_admin"):
        raise PermissionError("Admin access required")
