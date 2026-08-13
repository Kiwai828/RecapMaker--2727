from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from js import Object, fetch
from pyodide.ffi import to_js

from db import all_rows, first_row, run
from credentials import resolve_credential

OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=transcription"
ZEN_CHAT_URL = "https://opencode.ai/zen/v1/chat/completions"
ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS_URL = GEMINI_API_ROOT + "/models"

STT_PROVIDER = "openrouter_stt"
TRANSLATION_PROVIDER = "opencode_zen"
GEMINI_PROVIDER = "gemini"

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "translated_text": {"type": "string"},
                    "tts_text": {"type": "string"},
                },
                "required": ["id", "translated_text", "tts_text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


class AIProviderError(RuntimeError):
    def __init__(self, provider: str, stage: str, status: int, message: str = ""):
        self.provider = provider
        self.stage = stage
        self.status = int(status)
        self.provider_message = str(message or "")[:500]
        self.code = f"{provider.upper()}_{stage.upper()}_{self.status}"
        suffix = f":{self.provider_message}" if self.provider_message else ""
        super().__init__(f"{self.code}{suffix}")


class AIProviderCapacityError(RuntimeError):
    code = "AI_PROVIDER_CAPACITY"

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(f"No enabled {capability} provider model is currently available")


class AIProviderConfigurationError(RuntimeError):
    code = "AI_PROVIDER_CONFIGURATION"


class AIProviderResponseError(ValueError):
    code = "AI_PROVIDER_MALFORMED_RESPONSE"

    def __init__(self, provider: str, message: str = "Provider returned incomplete or invalid translation JSON"):
        self.provider = provider
        self.stage = "TRANSLATION"
        self.status = 502
        self.provider_message = message
        super().__init__(message)


def py(value: Any) -> Any:
    return value.to_py() if hasattr(value, "to_py") else value


async def response_json(response: Any) -> dict[str, Any]:
    try:
        value = await response.json()
        data = py(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            text = await response.text()
            return {"message": str(text)[:500]}
        except Exception:
            return {}


def provider_message(data: dict[str, Any]) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")
    if isinstance(error, str):
        return error
    return str(data.get("message") or "") if isinstance(data, dict) else ""


async def request_json(url: str, method: str = "GET", *, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    request_headers = dict(headers or {})
    options: dict[str, Any] = {"method": method, "headers": to_js(request_headers, dict_converter=Object.fromEntries)}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        options["headers"] = to_js(request_headers, dict_converter=Object.fromEntries)
        options["body"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    response = await fetch(url, to_js(options, dict_converter=Object.fromEntries))
    return int(response.status), await response_json(response)


def _secret(env: Any, secret_name: str) -> str:
    if not secret_name:
        return ""
    return str(getattr(env, secret_name, "") or "")


def _auth_headers(api_key: str, credential: dict[str, Any] | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if not api_key:
        return headers
    row = credential or {}
    auth_type = str(row.get("auth_type") or "bearer")
    auth_header = str(row.get("auth_header") or "Authorization")
    if auth_type == "x_api_key":
        headers[auth_header] = api_key
    elif auth_type == "none":
        pass
    else:
        headers[auth_header] = f"Bearer {api_key}"
    return headers


async def _model_secret(env: Any, model_row: dict[str, Any]) -> str:
    value, _ = await resolve_credential(
        env,
        env.DB,
        credential_id=str(model_row.get("credential_id") or "") or None,
        legacy_secret_name=str(model_row.get("secret_name") or "") or None,
    )
    return value


async def _model_credential(env: Any, model_row: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    return await resolve_credential(
        env,
        env.DB,
        credential_id=str(model_row.get("credential_id") or "") or None,
        legacy_secret_name=str(model_row.get("secret_name") or "") or None,
    )


def _credential_url(row: dict[str, Any] | None, suffix: str) -> str:
    base = str((row or {}).get("base_url") or "").rstrip("/")
    if not base:
        return ""
    if base.endswith(suffix):
        return base
    return base + suffix


def _gemini_catalog_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    raw_name = str(item.get("name") or item.get("id") or "").strip()
    normalized["id"] = raw_name.removeprefix("models/")
    normalized["name"] = str(item.get("displayName") or normalized["id"])
    normalized["pricing"] = {"prompt": "unknown", "completion": "unknown", "request": "unknown"}
    normalized["context_length"] = item.get("inputTokenLimit")
    normalized["supported_parameters"] = item.get("supportedGenerationMethods") or []
    return _catalog_item(GEMINI_PROVIDER, normalized)


def _gemini_generate_url(credential: dict[str, Any] | None, model_id: str) -> str:
    base = str((credential or {}).get("base_url") or GEMINI_API_ROOT).rstrip("/")
    if base.endswith("/models"):
        return f"{base}/{model_id}:generateContent"
    return f"{base}/models/{model_id}:generateContent"


def _catalog_item(provider: str, item: dict[str, Any]) -> dict[str, Any]:
    architecture = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    prompt_price = str(pricing.get("prompt") or "0")
    completion_price = str(pricing.get("completion") or "0")
    request_price = str(pricing.get("request") or "0")
    return {
        "provider": provider,
        "model_id": str(item.get("id") or item.get("model") or "").strip(),
        "display_name": str(item.get("name") or item.get("id") or item.get("model") or "").strip(),
        "description": str(item.get("description") or "")[:1000],
        "input_modalities": architecture.get("input_modalities") or item.get("input_modalities") or [],
        "output_modalities": architecture.get("output_modalities") or item.get("output_modalities") or [],
        "supported_parameters": item.get("supported_parameters") or [],
        "pricing": {"prompt": prompt_price, "completion": completion_price, "request": request_price},
        "is_free": prompt_price == "0" and completion_price == "0" and request_price == "0",
        "context_length": item.get("context_length"),
        "canonical_slug": item.get("canonical_slug"),
        "expiration_date": item.get("expiration_date"),
    }


async def fetch_catalog(env: Any, provider: str, secret_name: str = "", credential_id: str = "") -> list[dict[str, Any]]:
    api_key, credential = await resolve_credential(env, env.DB, credential_id=credential_id or None, legacy_secret_name=secret_name or None)
    if provider == STT_PROVIDER:
        url = str((credential or {}).get("models_url") or OPENROUTER_MODELS_URL)
    elif provider == TRANSLATION_PROVIDER:
        url = str((credential or {}).get("models_url") or ZEN_MODELS_URL)
    elif provider == GEMINI_PROVIDER:
        url = str((credential or {}).get("models_url") or GEMINI_MODELS_URL)
    elif provider == "custom":
        url = str((credential or {}).get("models_url") or _credential_url(credential, "/models"))
        if not url:
            raise AIProviderConfigurationError("Custom provider requires a model catalog URL or base URL")
    else:
        raise AIProviderConfigurationError(f"Unsupported provider: {provider}")
    if provider in {TRANSLATION_PROVIDER, GEMINI_PROVIDER} and not api_key:
        label = "OpenCode Zen" if provider == TRANSLATION_PROVIDER else "Gemini"
        raise AIProviderConfigurationError(f"A {label} credential or legacy secret binding is required to fetch its catalog")
    headers = _auth_headers(api_key, credential)
    if provider == GEMINI_PROVIDER:
        headers.pop("Authorization", None)
        headers["x-goog-api-key"] = api_key
    status, data = await request_json(url, headers=headers)
    if status != 200:
        raise AIProviderError(provider, "CATALOG", status, provider_message(data))
    raw = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
    if not isinstance(raw, list):
        raw = []
    result = [(_gemini_catalog_item(item) if provider == GEMINI_PROVIDER else _catalog_item(provider, item)) for item in raw if isinstance(item, dict)]
    return [item for item in result if item.get("model_id")]


async def claim_model(db: Any, capability: str) -> dict[str, Any] | None:
    rows = await all_rows(
        db,
        "SELECT * FROM ai_provider_models WHERE enabled=1 AND capability=? AND active_requests<concurrency_limit "
        "AND (cooldown_until IS NULL OR cooldown_until<=datetime('now')) "
        "AND (window_started_at IS NULL OR window_started_at<=datetime('now','-60 seconds') OR window_used<rpm_limit) "
        "AND (daily_reset_at IS NULL OR daily_reset_at<=datetime('now') OR daily_used<daily_limit) "
        "ORDER BY priority,COALESCE(last_used_at,'1970-01-01'),active_requests,id LIMIT 100",
        capability,
    )
    for row in rows:
        updated = await run(
            db,
            "UPDATE ai_provider_models SET active_requests=active_requests+1,last_used_at=datetime('now'),"
            "window_started_at=CASE WHEN window_started_at IS NULL OR window_started_at<=datetime('now','-60 seconds') THEN datetime('now') ELSE window_started_at END,"
            "window_used=CASE WHEN window_started_at IS NULL OR window_started_at<=datetime('now','-60 seconds') THEN 1 ELSE window_used+1 END,"
            "daily_reset_at=CASE WHEN daily_reset_at IS NULL OR daily_reset_at<=datetime('now') THEN datetime('now','start of day','+1 day') ELSE daily_reset_at END,"
            "daily_used=CASE WHEN daily_reset_at IS NULL OR daily_reset_at<=datetime('now') THEN 1 ELSE daily_used+1 END,"
            "updated_at=datetime('now') WHERE id=? AND enabled=1 AND active_requests<concurrency_limit",
            row["id"],
        )
        meta = getattr(updated, "meta", None)
        if meta is None or int(getattr(meta, "changes", 1)) > 0:
            return row
    return None


async def release_model(db: Any, model_id: str, *, failed: bool = False, cooldown_seconds: int = 0) -> None:
    await run(
        db,
        "UPDATE ai_provider_models SET active_requests=MAX(0,active_requests-1),"
        "fail_count=CASE WHEN ?=1 THEN fail_count+1 ELSE 0 END,"
        "cooldown_until=CASE WHEN ?=0 THEN NULL ELSE datetime('now',?) END,updated_at=datetime('now') WHERE id=?",
        int(failed), int(cooldown_seconds), f"+{cooldown_seconds} seconds", model_id,
    )


def _stt_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = data.get("segments") if isinstance(data.get("segments"), list) else []
    output: list[dict[str, Any]] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = float(item.get("start") or 0) * 1000
        end = float(item.get("end") or 0) * 1000
        if end <= start:
            end = start + 1000
        output.append({"id": f"seg-{index + 1}", "start_ms": int(start), "end_ms": int(end), "original_text": text})
    if output:
        return output
    text = str(data.get("text") or "").strip()
    if not text:
        return []
    duration = data.get("duration")
    if duration is None and isinstance(data.get("usage"), dict):
        duration = data["usage"].get("seconds")
    end_ms = max(1000, int(float(duration or 1) * 1000))
    return [{"id": "seg-1", "start_ms": 0, "end_ms": end_ms, "original_text": text}]


async def openrouter_transcribe(env: Any, audio: bytes, model_row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = await _model_secret(env, model_row)
    if not api_key:
        raise AIProviderConfigurationError(f"Missing configured secret binding: {model_row.get('secret_name')}")
    payload = {
        "model": str(model_row["model_id"]),
        "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "wav"},
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment"],
    }
    status, data = await request_json(OPENROUTER_STT_URL, "POST", headers=_auth_headers(api_key), body=payload)
    if status != 200:
        raise AIProviderError(STT_PROVIDER, "TRANSCRIPTION", status, provider_message(data))
    segments = _stt_segments(data)
    return segments, {"provider": STT_PROVIDER, "model": model_row["model_id"], "row_id": model_row["id"], "source_language": str(data.get("language") or ""), "usage": data.get("usage") or {}}


def _json_from_content(content: Any, provider: str = TRANSLATION_PROVIDER) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    decoder = json.JSONDecoder()
    # Providers occasionally add a short preamble before the JSON object. Use
    # raw_decode from each opening brace so a valid object can still be parsed,
    # but never fabricate a result from an incomplete JSON string.
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AIProviderResponseError(provider)


async def custom_transcribe(env: Any, audio: bytes, model_row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key, credential = await _model_credential(env, model_row)
    if not api_key or not credential:
        raise AIProviderConfigurationError("Custom provider credential is not configured")
    url = _credential_url(credential, "/audio/transcriptions")
    if not url:
        raise AIProviderConfigurationError("Custom transcription provider requires a base URL")
    payload = {"model": str(model_row["model_id"]), "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "wav"}, "response_format": "verbose_json", "timestamp_granularities": ["segment"]}
    status, data = await request_json(url, "POST", headers=_auth_headers(api_key, credential), body=payload)
    if status != 200:
        raise AIProviderError("custom", "TRANSCRIPTION", status, provider_message(data))
    return _stt_segments(data), {"provider": "custom", "model": model_row["model_id"], "row_id": model_row["id"], "source_language": str(data.get("language") or ""), "usage": data.get("usage") or {}}


async def zen_translate(env: Any, segments: list[dict[str, Any]], target_language: str, model_row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = await _model_secret(env, model_row)
    if not api_key:
        raise AIProviderConfigurationError(f"Missing configured secret binding: {model_row.get('secret_name')}")
    source = [{"id": s["id"], "original_text": s["original_text"]} for s in segments]
    system = (
        "You are a professional subtitle translator and dubbing script editor. "
        "Translate every segment faithfully into the requested target language. "
        "Do not summarize, merge, split, reorder, add, or omit segments. "
        "Return ONLY valid JSON with this exact shape: "
        '{"segments":[{"id":"same id","translated_text":"translation","tts_text":"natural spoken dubbing text"}]}.'
    )
    user = json.dumps({"target_language": target_language, "segments": source}, ensure_ascii=False, separators=(",", ":"))
    payload = {
        "model": str(model_row["model_id"]),
        "temperature": 0.1,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max(512, min(12000, len(source) * 180)),
    }
    status, data = await request_json(ZEN_CHAT_URL, "POST", headers=_auth_headers(api_key), body=payload)
    if status != 200:
        raise AIProviderError(TRANSLATION_PROVIDER, "TRANSLATION", status, provider_message(data))
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("translation_response_choices_missing")
    choice = choices[0]
    if str(choice.get("finish_reason") or "").lower() in {"length", "max_tokens"}:
        raise AIProviderResponseError(TRANSLATION_PROVIDER, "OpenCode translation response was truncated at the token limit")
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    parsed = _json_from_content(message.get("content"), TRANSLATION_PROVIDER)
    translated = parsed.get("segments")
    if not isinstance(translated, list):
        raise ValueError("translation_segments_missing")
    by_id = {str(item.get("id")): item for item in translated if isinstance(item, dict) and item.get("id")}
    if any(s["id"] not in by_id for s in segments):
        raise ValueError("translation_segment_count_mismatch")
    merged = []
    for segment in segments:
        item = by_id[segment["id"]]
        translated_text = str(item.get("translated_text") or "").strip()
        tts_text = str(item.get("tts_text") or translated_text).strip()
        if not translated_text or not tts_text:
            raise ValueError("translation_segment_empty")
        merged.append({**segment, "translated_text": translated_text, "tts_text": tts_text})
    return merged, {"provider": TRANSLATION_PROVIDER, "model": model_row["model_id"], "row_id": model_row["id"], "usage": data.get("usage") or {}}


async def gemini_translate(env: Any, segments: list[dict[str, Any]], target_language: str, model_row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key, credential = await _model_credential(env, model_row)
    if not api_key:
        raise AIProviderConfigurationError("Gemini credential is not configured")
    source = [{"id": s["id"], "original_text": s["original_text"]} for s in segments]
    prompt = (
        "You are a professional subtitle translator and dubbing script editor. "
        "Translate every segment faithfully into the requested target language. "
        "Do not summarize, merge, split, reorder, add, or omit segments. "
        "Return ONLY valid JSON with this exact shape: "
        '{"segments":[{"id":"same id","translated_text":"translation","tts_text":"natural spoken dubbing text"}]}.'
        "\n" + json.dumps({"target_language": target_language, "segments": source}, ensure_ascii=False, separators=(",", ":"))
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max(512, min(12000, len(source) * 180))},
    }
    url = _gemini_generate_url(credential, str(model_row["model_id"]))
    status, data = await request_json(url, "POST", headers={"Accept": "application/json", "x-goog-api-key": api_key}, body=payload)
    if status != 200:
        raise AIProviderError(GEMINI_PROVIDER, "TRANSLATION", status, provider_message(data))
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    if str(candidate.get("finishReason") or "").upper() in {"MAX_TOKENS", "SAFETY", "RECITATION"}:
        raise AIProviderResponseError(GEMINI_PROVIDER, f"Gemini translation response ended early: {candidate.get('finishReason')}")
    parts = (candidate.get("content") or {}).get("parts", []) if isinstance(candidate.get("content"), dict) else []
    content = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    parsed = _json_from_content(content, GEMINI_PROVIDER)
    translated = parsed.get("segments")
    if not isinstance(translated, list):
        raise ValueError("translation_segments_missing")
    by_id = {str(item.get("id")): item for item in translated if isinstance(item, dict) and item.get("id")}
    if any(s["id"] not in by_id for s in segments):
        raise ValueError("translation_segment_count_mismatch")
    merged = []
    for segment in segments:
        item = by_id[segment["id"]]
        translated_text = str(item.get("translated_text") or "").strip()
        tts_text = str(item.get("tts_text") or translated_text).strip()
        if not translated_text or not tts_text:
            raise ValueError("translation_segment_empty")
        merged.append({**segment, "translated_text": translated_text, "tts_text": tts_text})
    return merged, {"provider": GEMINI_PROVIDER, "model": model_row["model_id"], "row_id": model_row["id"], "usage": data.get("usageMetadata") or {}}


async def custom_translate(env: Any, segments: list[dict[str, Any]], target_language: str, model_row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key, credential = await _model_credential(env, model_row)
    if not api_key or not credential:
        raise AIProviderConfigurationError("Custom provider credential is not configured")
    url = _credential_url(credential, "/chat/completions")
    if not url:
        raise AIProviderConfigurationError("Custom translation provider requires a base URL")
    source = [{"id": s["id"], "original_text": s["original_text"]} for s in segments]
    system = 'Translate every segment faithfully. Return ONLY JSON with this shape: {"segments":[{"id":"same id","translated_text":"translation","tts_text":"spoken dubbing text"}]}'
    payload = {"model": str(model_row["model_id"]), "temperature": 0.1, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"target_language": target_language, "segments": source}, ensure_ascii=False)}], "max_tokens": max(512, min(12000, len(source) * 180))}
    status, data = await request_json(url, "POST", headers=_auth_headers(api_key, credential), body=payload)
    if status != 200:
        raise AIProviderError("custom", "TRANSLATION", status, provider_message(data))
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict) else {}
    parsed = _json_from_content(message.get("content"), "custom")
    translated = parsed.get("segments") if isinstance(parsed.get("segments"), list) else []
    by_id = {str(item.get("id")): item for item in translated if isinstance(item, dict) and item.get("id")}
    if any(s["id"] not in by_id for s in segments):
        raise ValueError("translation_segment_count_mismatch")
    merged = []
    for segment in segments:
        item = by_id[segment["id"]]
        translated_text = str(item.get("translated_text") or "").strip()
        tts_text = str(item.get("tts_text") or translated_text).strip()
        if not translated_text or not tts_text:
            raise ValueError("translation_segment_empty")
        merged.append({**segment, "translated_text": translated_text, "tts_text": tts_text})
    return merged, {"provider": "custom", "model": model_row["model_id"], "row_id": model_row["id"], "usage": data.get("usage") or {}}


async def _run_with_fallback(env: Any, capability: str, fn: Any) -> tuple[Any, dict[str, Any]]:
    db = env.DB
    attempted: set[str] = set()
    last_error: Exception | None = None
    for _ in range(12):
        row = await claim_model(db, capability)
        if not row or row["id"] in attempted:
            break
        attempted.add(row["id"])
        try:
            result, metadata = await fn(row)
            await release_model(db, row["id"])
            return result, metadata
        except Exception as exc:
            last_error = exc
            status = getattr(exc, "status", None)
            retryable = status in RETRYABLE_STATUS or isinstance(exc, (ValueError, json.JSONDecodeError))
            cooldown_seconds = 65 if retryable else 0
            if isinstance(exc, AIProviderError):
                exc.model_id = str(row.get("model_id") or "")
                exc.cooldown_seconds = cooldown_seconds
            await release_model(db, row["id"], failed=True, cooldown_seconds=cooldown_seconds)
            if not retryable:
                continue
            await asyncio.sleep(1)
    if last_error:
        raise last_error
    raise AIProviderCapacityError(capability)


async def transcribe_and_translate(env: Any, audio: bytes, target_language: str) -> tuple[dict[str, Any], dict[str, Any]]:
    async def run_stt(row: dict[str, Any]):
        return await (custom_transcribe(env, audio, row) if row.get("provider") == "custom" else openrouter_transcribe(env, audio, row))
    async def run_translation(row: dict[str, Any]):
        if row.get("provider") == "custom":
            return await custom_translate(env, segments, target_language, row)
        if row.get("provider") == GEMINI_PROVIDER:
            return await gemini_translate(env, segments, target_language, row)
        return await zen_translate(env, segments, target_language, row)
    segments, stt_meta = await _run_with_fallback(env, "stt", run_stt)
    translated, translation_meta = await _run_with_fallback(env, "translation", run_translation)
    result = {
        "source_language": str(stt_meta.get("source_language") or ""),
        "target_language": target_language,
        "segments": translated,
        "stt": {k: v for k, v in stt_meta.items() if k != "row_id"},
        "translation": {k: v for k, v in translation_meta.items() if k != "row_id"},
    }
    return result, {"stt": stt_meta, "translation": translation_meta}
