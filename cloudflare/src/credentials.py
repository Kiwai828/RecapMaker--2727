"""Encrypted provider credential vault helpers for Cloudflare Python Workers.

The administrator may manage credentials from the Admin UI, but the key material is
never returned to the browser and never stored as plaintext in D1. A Worker secret
named PROVIDER_CREDENTIAL_MASTER_KEY protects the AES-GCM envelope.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

try:
    import js
except ImportError:  # local unit-test fallback; WebCrypto is only used in Workers
    js = None
try:
    from pyodide.ffi import to_js
except ImportError:  # local unit-test fallback
    def to_js(value: Any, **kwargs: Any) -> Any:
        return value

from db import first_row

PREFIX = "enc:v1:"
MASTER_BINDING = "PROVIDER_CREDENTIAL_MASTER_KEY"


def _u8(data: bytes) -> Any:
    if js is None:
        raise RuntimeError("WebCrypto is available only in the Cloudflare Worker runtime")
    array_type = getattr(js, "Uint8Array")
    return getattr(array_type, "from")(to_js(list(data)))


def _bytes(value: Any) -> bytes:
    converted = value.to_py() if hasattr(value, "to_py") else value
    return bytes(int(item) & 255 for item in converted)


async def _key(env: Any) -> Any:
    master = str(getattr(env, MASTER_BINDING, "") or "")
    if len(master) < 32:
        raise ValueError(f"{MASTER_BINDING} must be configured with at least 32 characters")
    digest = hashlib.sha256(master.encode("utf-8")).digest()
    if js is None:
        raise RuntimeError("WebCrypto is available only in the Cloudflare Worker runtime")
    webcrypto = getattr(js, "crypto")
    return await webcrypto.subtle.importKey(
        "raw",
        _u8(digest),
        {"name": "AES-GCM"},
        False,
        ["encrypt", "decrypt"],
    )


async def encrypt_secret(env: Any, plaintext: str) -> str:
    if not plaintext:
        raise ValueError("Provider credential cannot be empty")
    key = await _key(env)
    iv_bytes = secrets.token_bytes(12)
    if js is None:
        raise RuntimeError("WebCrypto is available only in the Cloudflare Worker runtime")
    webcrypto = getattr(js, "crypto")
    encrypted = await webcrypto.subtle.encrypt(
        {"name": "AES-GCM", "iv": _u8(iv_bytes)},
        key,
        _u8(plaintext.encode("utf-8")),
    )
    ciphertext = _bytes(encrypted)
    return PREFIX + base64.urlsafe_b64encode(iv_bytes).decode("ascii") + ":" + base64.urlsafe_b64encode(ciphertext).decode("ascii")


async def decrypt_secret(env: Any, ciphertext: str) -> str:
    if not ciphertext or not ciphertext.startswith(PREFIX):
        raise ValueError("Invalid encrypted provider credential")
    parts = ciphertext.split(":", 3)
    if len(parts) != 4 or parts[0] != "enc" or parts[1] != "v1":
        raise ValueError("Unsupported provider credential envelope")
    iv = base64.urlsafe_b64decode(parts[2].encode("ascii"))
    encrypted = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    key = await _key(env)
    if js is None:
        raise RuntimeError("WebCrypto is available only in the Cloudflare Worker runtime")
    webcrypto = getattr(js, "crypto")
    plaintext = await webcrypto.subtle.decrypt(
        {"name": "AES-GCM", "iv": _u8(iv)},
        key,
        _u8(encrypted),
    )
    return _bytes(plaintext).decode("utf-8")


async def credential_row(db: Any, credential_id: str | None) -> dict[str, Any] | None:
    if not credential_id:
        return None
    return await first_row(db, "SELECT * FROM ai_provider_credentials WHERE id=? AND enabled=1", credential_id)


async def resolve_credential(env: Any, db: Any, *, credential_id: str | None = None, legacy_secret_name: str | None = None) -> tuple[str, dict[str, Any] | None]:
    row = await credential_row(db, credential_id)
    if row:
        cipher = str(row.get("credential_ciphertext") or "")
        if cipher.startswith(PREFIX):
            return await decrypt_secret(env, cipher), row
    if legacy_secret_name:
        value = str(getattr(env, legacy_secret_name, "") or "")
        if value:
            return value, row
    return "", row


def masked_last4(value: str) -> str:
    return value[-4:] if len(value) >= 4 else ""


def public_credential(row: dict[str, Any], *, secret_configured: bool = False) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "provider_type": row.get("provider_type"),
        "base_url": row.get("base_url"),
        "models_url": row.get("models_url"),
        "api_format": row.get("api_format"),
        "auth_type": row.get("auth_type"),
        "auth_header": row.get("auth_header"),
        "auth_query_name": row.get("auth_query_name"),
        "credential_last4": row.get("credential_last4") or "",
        "secret_configured": bool(secret_configured or str(row.get("credential_ciphertext") or "").startswith(PREFIX)),
        "enabled": bool(row.get("enabled")),
        "last_tested_at": row.get("last_tested_at"),
        "last_test_status": row.get("last_test_status"),
        "last_test_message": row.get("last_test_message"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
