from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from typing import Any

from js import Object, Uint8Array, fetch
from pyodide.ffi import to_js

from credits import add_credits
from db import all_rows, first_row, run


GEMINI_FILES_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
GEMINI_FILES_API_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_language": {"type": "string"},
        "target_language": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "start_ms": {"type": "integer"},
                    "end_ms": {"type": "integer"},
                    "original_text": {"type": "string"},
                    "translated_text": {"type": "string"},
                    "tts_text": {"type": "string"},
                },
                "required": ["id", "start_ms", "end_ms", "original_text", "translated_text", "tts_text"],
            },
        },
    },
    "required": ["source_language", "target_language", "segments"],
}


def py(value: Any) -> Any:
    return value.to_py() if hasattr(value, "to_py") else value


async def response_json(response: Any) -> dict[str, Any]:
    value = await response.json()
    return py(value) or {}


async def response_text(response: Any) -> str:
    value = await response.text()
    return str(value)


async def request_json(url: str, method: str, *, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    options: dict[str, Any] = {"method": method, "headers": to_js(headers or {}, dict_converter=Object.fromEntries)}
    if body is not None:
        options["body"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        options["headers"] = to_js({**(headers or {}), "Content-Type": "application/json"}, dict_converter=Object.fromEntries)
    response = await fetch(url, to_js(options, dict_converter=Object.fromEntries))
    return int(response.status), await response_json(response)


async def upload_file(api_key: str, audio: bytes, mime_type: str, display_name: str) -> tuple[str, str]:
    start_options = {
        "method": "POST",
        "headers": to_js({"x-goog-api-key": api_key, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(len(audio)), "X-Goog-Upload-Header-Content-Type": mime_type, "Content-Type": "application/json"}, dict_converter=Object.fromEntries),
        "body": json.dumps({"file": {"display_name": display_name}}, separators=(",", ":")),
    }
    start = await fetch(GEMINI_FILES_UPLOAD_URL, to_js(start_options, dict_converter=Object.fromEntries))
    if int(start.status) not in {200, 201}:
        raise RuntimeError(f"gemini_upload_start:{int(start.status)}")
    upload_url = str(start.headers.get("x-goog-upload-url"))
    if not upload_url:
        raise RuntimeError("gemini_upload_url_missing")
    payload = Uint8Array.new(audio)
    upload_options = {
        "method": "POST",
        "headers": to_js({"x-goog-api-key": api_key, "Content-Length": str(len(audio)), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize", "Content-Type": mime_type}, dict_converter=Object.fromEntries),
        "body": payload,
    }
    uploaded = await fetch(upload_url, to_js(upload_options, dict_converter=Object.fromEntries))
    if int(uploaded.status) not in {200, 201}:
        raise RuntimeError(f"gemini_upload_finalize:{int(uploaded.status)}")
    data = await response_json(uploaded)
    file_data = data.get("file", data)
    return str(file_data["name"]), str(file_data["uri"])


async def file_uri(api_key: str, file_name: str, timeout_seconds: int = 180) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, data = await request_json(f"{GEMINI_FILES_API_URL}/{file_name}", "GET", headers={"x-goog-api-key": api_key})
        if status != 200:
            raise RuntimeError(f"gemini_file_status:{status}")
        state = str(data.get("state") or data.get("file", {}).get("state") or "").upper()
        if state == "ACTIVE":
            return str(data.get("uri") or data.get("file", {}).get("uri"))
        if state == "FAILED":
            raise RuntimeError("gemini_file_processing_failed")
        await asyncio.sleep(2)
    raise RuntimeError("gemini_file_processing_timeout")


async def delete_file(api_key: str, file_name: str) -> None:
    try:
        await request_json(f"{GEMINI_FILES_API_URL}/{file_name}", "DELETE", headers={"x-goog-api-key": api_key})
    except Exception:
        pass


async def gemini_transcribe(api_key: str, model: str, audio: bytes, target_language: str) -> dict[str, Any]:
    mime_type = "audio/wav"
    file_name, uploaded_uri = await upload_file(api_key, audio, mime_type, f"voicerecap-{uuid.uuid4()}.wav")
    try:
        active_uri = await file_uri(api_key, file_name)
        prompt = (
            "Transcribe every spoken segment in the attached audio with accurate integer start_ms and end_ms timestamps. "
            "Detect the source language. Translate every segment directly into target language "
            f"{target_language}. Do not summarize, merge, split, reorder, or omit speech. "
            "Return only structured JSON. original_text is the faithful transcription; translated_text is the faithful translation; "
            "tts_text is natural spoken text suitable for dubbing. If no speech exists, return an empty segments array."
        )
        status, data = await request_json(
            GEMINI_INTERACTIONS_URL,
            "POST",
            headers={"x-goog-api-key": api_key},
            body={"model": model, "input": [{"type": "text", "text": prompt}, {"type": "audio", "uri": active_uri or uploaded_uri,
 "mime_type": mime_type}], "response_format": {"type": "text", "mime_type": "application/json", "schema": RESPONSE_SCHEMA}, "store": False},
        )
        if status != 200:
            raise RuntimeError(f"gemini_interaction:{status}")
        text = str(data.get("output_text") or "")
        if not text:
            for key in ("outputs", "steps"):
                for item in data.get(key, []) if isinstance(data.get(key), list) else []:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        text += item["text"]
                    for part in item.get("content", []) if isinstance(item, dict) and isinstance(item.get("content"), list) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            text += part["text"]
        text = text.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(text)
    finally:
        await delete_file(api_key, file_name)


async def transcribe_direct(env: Any, audio: bytes, target_language: str, max_attempts: int = 3) -> tuple[dict[str, Any], dict[str, Any]]:
    """Transcribe request-scoped audio without writing it to R2 or a queue.

    The caller keeps the HTTP connection open while this function waits on Gemini.
    The audio byte string is released when this invocation returns or raises.
    """
    db = env.DB
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        slot = await choose_slot(db)
        if not slot:
            raise RuntimeError("no_gemini_slot_available")
        try:
            secret = getattr(env, slot["secret_name"], "")
            if not secret:
                raise RuntimeError("gemini_secret_missing")
            result = await gemini_transcribe(str(secret), slot["model"], audio, target_language)
            await release_slot(db, slot["id"])
            return result, slot
        except Exception as exc:
            last_error = exc
            text = str(exc)
            retryable = any(str(code) in text for code in RETRYABLE_CODES) or "timeout" in text.lower() or "temporarily" in text.lower()
            await release_slot(db, slot["id"], failed=True, cooldown_seconds=65 if retryable else 0)
            if not retryable or attempt + 1 >= max_attempts:
                raise
            await asyncio.sleep(min(2 ** attempt, 4))
    raise last_error or RuntimeError("gemini_transcription_failed")


async def extract_object(body: Any) -> bytes:
    if hasattr(body, "arrayBuffer"):
        raw = await body.arrayBuffer()
    elif hasattr(body, "array_buffer"):
        raw = await body.array_buffer()
    else:
        raw = body
    return bytes(py(raw))


async def media_bytes(env: Any, key: str) -> bytes:
    obj = await env.MEDIA.get(key)
    if obj is None:
        raise RuntimeError("media_not_found")
    body = getattr(obj, "body", obj)
    return await extract_object(body)


async def choose_slot(db: Any) -> dict[str, Any] | None:
    rows = await all_rows(db, "SELECT * FROM gemini_slots WHERE enabled=1 AND active_jobs<concurrency_limit AND (cooldown_until IS NULL OR cooldown_until<=datetime('now')) AND (window_started_at IS NULL OR window_started_at<=datetime('now','-60 seconds') OR window_used<rpm_limit) AND (daily_reset_at IS NULL OR daily_reset_at<=datetime('now') OR daily_used<daily_limit) ORDER BY priority,COALESCE(last_used_at,'1970-01-01'),active_jobs,id LIMIT 25")
    for row in rows:
        updated = await run(db, "UPDATE gemini_slots SET active_jobs=active_jobs+1,last_used_at=datetime('now'),window_started_at=CASE WHEN window_started_at IS NULL OR window_started_at<=datetime('now','-60 seconds') THEN datetime('now') ELSE window_started_at END,window_used=CASE WHEN window_started_at IS NULL OR window_started_at<=datetime('now','-60 seconds') THEN 1 ELSE window_used+1 END,daily_reset_at=CASE WHEN daily_reset_at IS NULL OR daily_reset_at<=datetime('now') THEN datetime('now','start of day','+1 day') ELSE daily_reset_at END,daily_used=CASE WHEN daily_reset_at IS NULL OR daily_reset_at<=datetime('now') THEN 1 ELSE daily_used+1 END,updated_at=datetime('now') WHERE id=? AND enabled=1 AND active_jobs<concurrency_limit", row["id"])
        meta = getattr(updated, "meta", None)
        if meta is None or int(getattr(meta, "changes", 1)) > 0:
            return row
    return None


async def release_slot(db: Any, slot_id: str, *, failed: bool = False, cooldown_seconds: int = 0) -> None:
    await run(db, "UPDATE gemini_slots SET active_jobs=MAX(0,active_jobs-1),fail_count=CASE WHEN ?=1 THEN fail_count+1 ELSE 0 END,cooldown_until=CASE WHEN ?=0 THEN NULL ELSE datetime('now',?) END,updated_at=datetime('now') WHERE id=?", int(failed), int(cooldown_seconds), f"+{cooldown_seconds} seconds", slot_id)


async def claim_job(db: Any, job_id: str) -> dict[str, Any] | None:
    row = await first_row(db, "SELECT * FROM processing_jobs WHERE id=?", job_id)
    if not row or row["status"] != "queued":
        return None
    await run(db, "UPDATE processing_jobs SET status='processing',queue_attempts=queue_attempts+1,started_at=datetime('now'),updated_at=datetime('now') WHERE id=? AND status='queued'", job_id)
    claimed = await first_row(db, "SELECT * FROM processing_jobs WHERE id=? AND status='processing'", job_id)
    return claimed


async def claim_next_fair_job(db: Any) -> dict[str, Any] | None:
    candidate = await first_row(db, "SELECT j.* FROM processing_jobs j WHERE j.status='queued' AND NOT EXISTS (SELECT 1 FROM processing_jobs active WHERE active.user_id=j.user_id AND active.status='processing') ORDER BY COALESCE((SELECT MAX(completed_at) FROM processing_jobs history WHERE history.user_id=j.user_id AND history.status='completed'),'1970-01-01'),j.created_at,j.id LIMIT 1")
    if not candidate:
        return None
    return await claim_job(db, candidate["id"])


async def process_job(env: Any, job_id: str | None = None) -> None:
    if job_id is None:
        job = await claim_next_fair_job(env.DB)
        if not job:
            return
    else:
        job = await claim_job(env.DB, job_id)
    if not job:
        return
    db = env.DB
    slot = await choose_slot(db)
    if not slot:
        await run(db, "UPDATE processing_jobs SET status='queued',updated_at=datetime('now') WHERE id=?", job["id"])
        raise RuntimeError("no_gemini_slot_available")
    try:
        secret = getattr(env, slot["secret_name"], "")
        if not secret:
            raise RuntimeError("gemini_secret_missing")
        audio = await media_bytes(env, job["audio_key"])
        payload = await gemini_transcribe(str(secret), slot["model"], audio, job["target_language"])
        await run(db, "UPDATE processing_jobs SET status='completed',result_json=?,provider_model=?,provider_slot=?,credits_committed=credits_reserved,completed_at=datetime('now'),updated_at=datetime('now') WHERE id=?", json.dumps(payload, ensure_ascii=False, separators=(",", ":")), slot["model"], slot["id"], job_id)
        await env.MEDIA.delete(job["audio_key"])
        await release_slot(db, slot["id"])
    except Exception as exc:
        text = str(exc)
        retryable = any(str(code) in text for code in RETRYABLE_CODES) or "timeout" in text or "no_gemini_slot" in text
        await release_slot(db, slot["id"], failed=True, cooldown_seconds=65 if retryable else 0)
        if retryable and int(job.get("queue_attempts") or 0) < 3:
            await run(db, "UPDATE processing_jobs SET status='queued',error_code='PROVIDER_RETRY',error_message=?,updated_at=datetime('now') WHERE id=?", text[:500], job_id)
            raise
        await run(db, "UPDATE processing_jobs SET status='failed',error_code='GEMINI_FAILED',error_message=?,updated_at=datetime('now'),completed_at=datetime('now') WHERE id=?", text[:500], job_id)
        await add_credits(db, job["user_id"], int(job.get("credits_reserved") or 0), "job_refund", reference_id=job_id, description="Refund for failed transcription", idempotency_key=f"refund:{job_id}")
        await env.MEDIA.delete(job["audio_key"])


async def consume_queue(batch: Any, env: Any) -> None:
    messages = getattr(batch, "messages", batch)
    for message in messages:
        try:
            await process_job(env)
            try:
                message.ack()
            except Exception:
                pass
        except Exception:
            try:
                message.retry()
            except Exception:
                pass
