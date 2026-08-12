from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi.responses import JSONResponse


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key, X-Request-ID",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Request-ID, Retry-After",
}


def headers(content_type: str = "application/json", request_id: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    value = {"Content-Type": content_type, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", **CORS_HEADERS}
    value["X-Request-ID"] = request_id or secrets.token_urlsafe(12)
    if extra:
        value.update(extra)
    return value


def json_response(data: Any, status: int = 200, request_id: str | None = None, extra: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(content=data, status_code=status, headers=headers(request_id=request_id, extra=extra))


def error_response(message: str, status: int = 400, code: str | None = None, request_id: str | None = None, extra: dict[str, str] | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"detail": message}
    if code:
        payload["code"] = code
    return json_response(payload, status=status, request_id=request_id, extra=extra)


async def json_body(request: Any) -> dict[str, Any]:
    value = await request.json()
    if hasattr(value, "to_py"):
        value = value.to_py()
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def form_value(form: Any, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return str(value) if value is not None else default
