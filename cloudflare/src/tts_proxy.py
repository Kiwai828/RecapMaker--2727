from __future__ import annotations

import base64
import io
import json
import struct
import uuid
import wave
from typing import Any

from js import Object, Uint8Array, fetch
from pyodide.ffi import to_js

from credits import CreditError, add_credits, tts_cost


class TtsProxyError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.status = status


def py(value: Any) -> Any:
    return value.to_py() if hasattr(value, "to_py") else value


async def _response_bytes(response: Any) -> bytes:
    raw = await response.arrayBuffer()
    return bytes(py(raw))


def _audio_info(raw: bytes) -> tuple[int, int, int]:
    if len(raw) > 32 * 1024 * 1024 or not raw.startswith(b"RIFF") or b"WAVE" not in raw[:16]:
        raise TtsProxyError("INVALID_AUDIO", "Modal returned a non-WAV or oversized response.")
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            channels, width, rate, frames = reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()
    except (wave.Error, EOFError) as exc:
        raise TtsProxyError("INVALID_AUDIO", "Modal returned an unreadable WAV response.") from exc
    if channels not in (1, 2) or width != 2 or rate < 8000 or rate > 48000:
        raise TtsProxyError("INVALID_AUDIO_FORMAT", "Modal WAV must be 16-bit mono/stereo audio at 8–48 kHz.")
    return rate, channels, int(frames / rate * 1000)


def _mono_pcm(raw: bytes) -> tuple[int, list[int]]:
    with wave.open(io.BytesIO(raw), "rb") as reader:
        rate, channels = reader.getframerate(), reader.getnchannels()
        frames = reader.readframes(reader.getnframes())
    values = list(struct.unpack("<%dh" % (len(frames) // 2), frames))
    if channels == 1:
        return rate, values
    return rate, [(values[i] + values[i + 1]) // 2 for i in range(0, len(values) - 1, 2)]


def _wav(sample_rate: int, samples: list[int]) -> bytes:
    values = [max(-32768, min(32767, int(sample))) for sample in samples]
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack("<%dh" % len(values), *values))
    return out.getvalue()


def _fit(samples: list[int], rate: int, target_ms: int) -> tuple[list[int], int, int, float, bool]:
    actual = int(len(samples) / rate * 1000)
    frames = max(1, int(target_ms / 1000 * rate))
    factor = target_ms / max(1, actual)
    overflow = factor < 0.7 or factor > 1.5
    fitted = samples[:frames] if len(samples) >= frames else samples + [0] * (frames - len(samples))
    return fitted, actual, int(len(fitted) / rate * 1000), factor, overflow


def _modal_url(env: Any, mode: str) -> str:
    if mode == "clone":
        value = getattr(env, "MODAL_CLONE_URL", "")
    elif mode == "ultimate_clone":
        value = getattr(env, "MODAL_ULTIMATE_CLONE_URL", "")
    else:
        value = getattr(env, "MODAL_TTS_URL", "")
    if not value:
        raise TtsProxyError("MODAL_ENDPOINT_MISSING", "Modal endpoint is not configured.", 503)
    return str(value)


def _text(config: dict[str, Any], text: str) -> str:
    hints = ", ".join(v.strip() for v in (config.get("voice_description"), config.get("style_control")) if v and v.strip())
    mode = config.get("voice_mode", "design")
    if mode == "design" and hints:
        return f"({hints}){text}"
    if mode in {"clone", "ultimate_clone"} and config.get("style_control"):
        return f"({config['style_control'].strip()}){text}"
    return text


async def synthesize(env: Any, config: dict[str, Any], text: str) -> tuple[bytes, int, int]:
    mode = config.get("voice_mode", "design")
    if mode in {"clone", "ultimate_clone"} and not config.get("reference_audio_base64"):
        raise TtsProxyError("REFERENCE_AUDIO_REQUIRED", "Reference audio is required for cloning.", 422)
    if mode == "ultimate_clone" and not config.get("reference_transcript"):
        raise TtsProxyError("PROMPT_TEXT_REQUIRED", "Reference transcript is required for ultimate cloning.", 422)
    payload: dict[str, Any] = {"text": _text(config, text), "cfg_value": config.get("cfg_value", 2.0), "inference_timesteps": config.get("inference_timesteps", 10)}
    if mode in {"clone", "ultimate_clone"}:
        payload["reference_audio_base64"] = config.get("reference_audio_base64")
    if mode == "ultimate_clone":
        payload["prompt_audio_base64"] = config.get("reference_audio_base64")
        payload["prompt_text"] = config.get("reference_transcript")
    options = {"method": "POST", "headers": to_js({"Content-Type": "application/json"}, dict_converter=Object.fromEntries), "body": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
    response = await fetch(_modal_url(env, mode), to_js(options, dict_converter=Object.fromEntries))
    status = int(response.status)
    raw = await _response_bytes(response) if status < 400 else b""
    if status >= 400:
        raise TtsProxyError("MODAL_REJECTED", f"Modal returned HTTP {status}.", status)
    rate, channels, duration = _audio_info(raw)
    return raw, duration, rate


async def generate(env: Any, db: Any, user_id: str, config: dict[str, Any], text: str, plan: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
    cost = tts_cost(plan, len(text))
    reference = idempotency_key or f"tts:{uuid.uuid4()}"
    try:
        await add_credits(db, user_id, -cost, "tts_reservation", reference_id=reference, description="TTS generation", idempotency_key=f"reserve:{reference}")
    except CreditError as exc:
        raise TtsProxyError("INSUFFICIENT_CREDITS", str(exc), 402) from exc
    try:
        raw, duration, rate = await synthesize(env, config, text)
        return {"audio_base64": base64.b64encode(raw).decode("ascii"), "actual_duration_ms": duration, "fitted_duration_ms": duration, "stretch_factor": 1.0, "overflow": False, "sample_rate": rate, "credits_charged": cost}
    except Exception:
        await add_credits(db, user_id, cost, "tts_refund", reference_id=reference, description="Refund for failed TTS", idempotency_key=f"refund:{reference}")
        raise


async def batch(env: Any, db: Any, user_id: str, segments: list[dict[str, Any]], config: dict[str, Any], plan: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
    cost = tts_cost(plan, sum(len(str(segment.get("text", ""))) for segment in segments))
    reference = idempotency_key or f"tts-batch:{uuid.uuid4()}"
    try:
        await add_credits(db, user_id, -cost, "tts_batch_reservation", reference_id=reference, description="Batch TTS generation", idempotency_key=f"reserve:{reference}")
    except CreditError as exc:
        raise TtsProxyError("INSUFFICIENT_CREDITS", str(exc), 402) from exc
    try:
        results: list[dict[str, Any]] = []
        max_end = max(int(segment.get("target_end_ms", 0)) for segment in segments)
        canvas = [0] * max(1, int(max_end / 1000 * 48000) + 4800)
        for segment in segments:
            raw, actual, rate = await synthesize(env, config, str(segment.get("text", "")))
            if rate != 48000:
                raise TtsProxyError("UNSUPPORTED_SAMPLE_RATE", "Modal VoxCPM2 must return 48 kHz audio.")
            sample_rate, samples = _mono_pcm(raw)
            fitted, actual_ms, fitted_ms, factor, overflow = _fit(samples, sample_rate, int(segment.get("target_duration_ms", 0)))
            start = int(int(segment.get("target_start_ms", 0)) / 1000 * sample_rate)
            end = min(len(canvas), start + len(fitted))
            for index, sample in enumerate(fitted[:max(0, end - start)]):
                canvas[start + index] = max(-32768, min(32767, canvas[start + index] + sample))
            results.append({"id": segment.get("id"), "audio_base64": base64.b64encode(_wav(sample_rate, fitted)).decode("ascii"), "actual_duration_ms": actual_ms, "fitted_duration_ms": fitted_ms, "stretch_factor": factor, "overflow": overflow})
        combined = _wav(48000, canvas)
        return {"segments": results, "combined_audio_base64": base64.b64encode(combined).decode("ascii"), "total_duration_ms": int(len(canvas) / 48000 * 1000), "credits_charged": cost}
    except Exception:
        await add_credits(db, user_id, cost, "tts_batch_refund", reference_id=reference, description="Refund for failed batch TTS", idempotency_key=f"refund:{reference}")
        raise
