from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from auth import current_user, hash_password, issue_tokens, public_user, require_admin, verify_password
from credits import CreditError, active_plan, add_credits, balance, ensure_account, video_cost
from db import all_rows, dumps, first_row, loads, run
from admin_page import ADMIN_HTML
from http_utils import error_response, json_body, json_response

app = FastAPI(title="VoiceRecap Cloudflare API", version="2.0.0")


class RegisterBody(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
            raise ValueError("Valid email is required")
        return value


class LoginBody(BaseModel):
    email: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=20)


class PlanBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    included_credits: int = Field(ge=0)
    video_credit_cost: int = Field(ge=0)
    video_credit_cost_per_minute: int = Field(default=0, ge=0)
    tts_credit_per_100_chars: int = Field(default=1, ge=0)
    voice_clone_credit_cost: int = Field(default=0, ge=0)
    price_mmk: int = Field(default=0, ge=0)
    price_usdt: str = "0"
    validity_days: int = Field(default=30, ge=0, le=3650)
    max_video_duration_seconds: int = Field(default=300, ge=1, le=86400)
    active: bool = True
    sort_order: int = 0


class CreditBody(BaseModel):
    user_id: str
    credits: int = Field(gt=0, le=10_000_000)
    description: str = Field(min_length=1, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=255)


class SlotBody(BaseModel):
    id: str | None = None
    account_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=150)
    model: str = Field(min_length=1, max_length=150)
    priority: int = Field(default=0, ge=0, le=1000)
    secret_name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")
    enabled: bool = True
    rpm_limit: int = Field(default=10, ge=1, le=100_000)
    daily_limit: int = Field(default=100, ge=1, le=10_000_000)
    concurrency_limit: int = Field(default=1, ge=1, le=50)


class TtsGenerateBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    voice_mode: str = Field(default="design", pattern="^(design|clone|ultimate_clone)$")
    voice_description: str | None = None
    reference_audio_base64: str | None = None
    reference_transcript: str | None = None
    style_control: str | None = None
    cfg_value: float = Field(default=2.0, ge=0.1, le=10.0)
    inference_timesteps: int = Field(default=10, ge=1, le=100)
    seed: int | None = None


class TtsSegmentBody(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=20_000)
    target_start_ms: int = Field(ge=0)
    target_end_ms: int = Field(gt=0)
    target_duration_ms: int = Field(gt=0)


class TtsBatchBody(BaseModel):
    segments: list[TtsSegmentBody] = Field(min_length=1, max_length=500)
    voice_config: TtsGenerateBody
    global_settings: dict[str, Any] = Field(default_factory=dict)


class PaymentBody(BaseModel):
    plan_id: str
    currency: str = Field(pattern="^(MMK|USDT)$")
    transaction_reference: str | None = Field(default=None, max_length=255)
    proof_key: str | None = Field(default=None, max_length=500)


class AdminUserPatch(BaseModel):
    is_active: bool | None = None
    is_banned: bool | None = None
    is_admin: bool | None = None
    display_name: str | None = Field(default=None, max_length=100)


class BackupImportBody(BaseModel):
    backup_version: int = Field(default=1, ge=1, le=10)
    profile: dict[str, Any] = Field(default_factory=dict)
    projects: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    preferences: dict[str, Any] = Field(default_factory=dict)


class TranscriptionJobResponse(BaseModel):
    job_id: str
    status: str
    poll_after_seconds: int = 3
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None


def env_of(request: Request) -> Any:
    return request.scope["env"]


def db_of(request: Request) -> Any:
    return env_of(request).DB


def configured_admin_email(env: Any) -> str:
    return str(getattr(env, "ADMIN_EMAIL", "")).strip().lower()


def configured_admin_password(env: Any) -> str:
    password = str(getattr(env, "ADMIN_PASSWORD", ""))
    if len(password) < 12:
        raise ValueError("ADMIN_PASSWORD must be configured with at least 12 characters")
    return password


async def ensure_env_admin(db: Any, env: Any) -> dict[str, Any]:
    email = configured_admin_email(env)
    password = configured_admin_password(env)
    if not email or "@" not in email:
        raise ValueError("ADMIN_EMAIL must be configured")
    row = await first_row(db, "SELECT * FROM users WHERE email=?", email)
    if not row:
        user_id = str(uuid.uuid4())
        await run(db, "INSERT INTO users(id,email,password_hash,display_name,is_admin) VALUES(?,?,?,?,1)", user_id, email, hash_password(password), "Administrator")
        await ensure_account(db, user_id)
        row = await first_row(db, "SELECT * FROM users WHERE id=?", user_id)
    elif not verify_password(password, str(row.get("password_hash") or "")) or not row.get("is_admin"):
        await run(db, "UPDATE users SET password_hash=?,is_admin=1,is_active=1,is_banned=0,updated_at=datetime('now') WHERE id=?", hash_password(password), row["id"])
        row = await first_row(db, "SELECT * FROM users WHERE id=?", row["id"])
    has_plan = await first_row(db, "SELECT id FROM user_plans WHERE user_id=? AND (expires_at IS NULL OR expires_at>datetime('now'))", row["id"])
    if not has_plan:
        free = await first_row(db, "SELECT id,validity_days,included_credits FROM plans WHERE name='Free' AND active=1")
        if free:
            validity = int(free.get("validity_days") or 0)
            expires = f"+{validity} days" if validity else None
            await run(db, "INSERT INTO user_plans(id,user_id,plan_id,expires_at) VALUES(?,?,?,CASE WHEN ? IS NULL THEN NULL ELSE datetime('now',?) END)", str(uuid.uuid4()), row["id"], free["id"], expires, expires)
            if int(free.get("included_credits") or 0) > 0:
                await add_credits(db, row["id"], int(free["included_credits"]), "plan_grant", reference_id=free["id"], description="Administrator welcome credits", idempotency_key=f"welcome:{row['id']}")
    return await first_row(db, "SELECT * FROM users WHERE id=?", row["id"])


async def user_dep(request: Request) -> dict[str, Any]:
    try:
        return await current_user(request, env_of(request), db_of(request))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


async def admin_dep(user: dict[str, Any] = Depends(user_dep)) -> dict[str, Any]:
    try:
        require_admin(user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return user


async def write_audit(db: Any, actor: str | None, action: str, target_type: str | None = None, target_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    await run(db, "INSERT INTO audit_logs(id,actor_user_id,action,target_type,target_id,metadata_json) VALUES(?,?,?,?,?,?)", str(uuid.uuid4()), actor, action, target_type, target_id, dumps(metadata or {}))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        return json_response({}, status=204)
    try:
        response = await call_next(request)
    except Exception as exc:
        print(f"VoiceRecap request failure: {exc!r}")
        return error_response("Internal server error", 500)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(ADMIN_HTML, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@app.get("/health")
async def health(request: Request):
    return json_response({"status": "ok", "service": "voicerecap-cloudflare", "runtime": "python-workers"})


@app.get("/ready")
async def ready(request: Request):
    await run(db_of(request), "SELECT 1")
    return json_response({"status": "ready"})


@app.get("/register-test", response_class=HTMLResponse)
async def register_test_page():
    return HTMLResponse("""
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>VoiceRecap connection test</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:32px auto;padding:0 18px;color:#202124}input,button{width:100%;box-sizing:border-box;padding:13px;margin:7px 0;font-size:16px;border-radius:8px}input{border:1px solid #9aa0a6}button{border:0;background:#6750a4;color:#fff;font-weight:600}pre{white-space:pre-wrap;word-break:break-word;background:#f1f3f4;padding:12px;border-radius:8px}</style></head>
<body><h2>VoiceRecap browser connection test</h2><p>This page sends one same-origin POST to the production registration route. It is for diagnosing the mobile network path; do not use a real password.</p>
<input id="email" type="email" autocomplete="off"><input id="password" type="password" value="BrowserTest123" autocomplete="off"><button id="send">Send registration POST</button><pre id="result">Ready</pre>
<script>
const result=document.getElementById('result');
document.getElementById('email').value='browser-test-'+Date.now()+'@example.com';
document.getElementById('send').onclick=async()=>{result.textContent='Sending...';try{const body={email:document.getElementById('email').value.trim().toLowerCase(),password:document.getElementById('password').value,display_name:'Browser Test'};const response=await fetch('/api/v1/register',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify(body)});const text=await response.text();result.textContent='HTTP '+response.status+'\\n'+text.replace(/access_token":"[^\"]+/g,'access_token":"REDACTED').replace(/refresh_token":"[^\"]+/g,'refresh_token":"REDACTED');}catch(error){result.textContent='FETCH_ERROR\\n'+error;}};
</script></body></html>
""", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@app.get("/login-test", response_class=HTMLResponse)
async def login_test_page():
    return HTMLResponse("""
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>VoiceRecap login test</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:32px auto;padding:0 18px;color:#202124}input,button{width:100%;box-sizing:border-box;padding:13px;margin:7px 0;font-size:16px;border-radius:8px}input{border:1px solid #9aa0a6}button{border:0;background:#6750a4;color:#fff;font-weight:600}pre{white-space:pre-wrap;word-break:break-word;background:#f1f3f4;padding:12px;border-radius:8px}</style></head>
<body><h2>VoiceRecap browser login test</h2><p>Use your account email and password only on your own phone. The response tokens are redacted on this page.</p>
<input id="email" type="email" autocomplete="off" placeholder="Email"><input id="password" type="password" autocomplete="off" placeholder="Password"><button id="send">Send login POST</button><pre id="result">Ready</pre>
<script>
const result=document.getElementById('result');
document.getElementById('send').onclick=async()=>{result.textContent='Sending...';try{const body={email:document.getElementById('email').value.trim().toLowerCase(),password:document.getElementById('password').value};const response=await fetch('/api/v1/login',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify(body)});const text=await response.text();result.textContent='HTTP '+response.status+'\\n'+text.replace(/access_token":"[^\"]+/g,'access_token":"REDACTED').replace(/refresh_token":"[^\"]+/g,'refresh_token":"REDACTED');}catch(error){result.textContent='FETCH_ERROR\\n'+error;}};
</script></body></html>
""", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@app.post("/api/v1/auth/register")
@app.post("/api/v1/register")
async def register(request: Request, body: RegisterBody):
    db = db_of(request)
    if body.email == configured_admin_email(env_of(request)):
        raise HTTPException(status_code=409, detail="Administrator account is managed by ADMIN_PASSWORD")
    existing = await first_row(db, "SELECT id FROM users WHERE email=?", body.email)
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists")
    user_id = str(uuid.uuid4())
    await run(db, "INSERT INTO users(id,email,password_hash,display_name) VALUES(?,?,?,?)", user_id, body.email, hash_password(body.password), body.display_name)
    await ensure_account(db, user_id)
    free = await first_row(db, "SELECT id,validity_days FROM plans WHERE name='Free' AND active=1")
    if free:
        expires = f"+{int(free.get('validity_days') or 0)} days" if int(free.get("validity_days") or 0) else None
        await run(db, "INSERT INTO user_plans(id,user_id,plan_id,expires_at) VALUES(?,?,?,CASE WHEN ? IS NULL THEN NULL ELSE datetime('now',?) END)", str(uuid.uuid4()), user_id, free["id"], expires, expires)
        plan = await first_row(db, "SELECT included_credits FROM plans WHERE id=?", free["id"])
        if plan and int(plan.get("included_credits") or 0) > 0:
            await add_credits(db, user_id, int(plan["included_credits"]), "plan_grant", reference_id=free["id"], description="Welcome credits", idempotency_key=f"welcome:{user_id}")
    user = await first_row(db, "SELECT * FROM users WHERE id=?", user_id)
    return json_response(await issue_tokens(db, env_of(request), user))


@app.post("/api/v1/auth/login")
@app.post("/api/v1/login")
async def login(request: Request, body: LoginBody):
    db = db_of(request)
    email = body.email.strip().lower()
    if email == configured_admin_email(env_of(request)):
        try:
            user = await ensure_env_admin(db, env_of(request))
            valid = hmac_compare(body.password, configured_admin_password(env_of(request)))
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid email or password")
    else:
        user = await first_row(db, "SELECT * FROM users WHERE email=?", email)
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.get("is_active") or user.get("is_banned"):
        raise HTTPException(status_code=403, detail="Account unavailable")
    return json_response(await issue_tokens(db, env_of(request), user))


@app.post("/api/v1/auth/refresh")
async def refresh(request: Request, body: RefreshBody):
    db = db_of(request)
    refresh_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    row = await first_row(db, "SELECT u.*,s.id AS session_id FROM refresh_sessions s JOIN users u ON u.id=s.user_id WHERE s.refresh_hash=? AND s.revoked_at IS NULL AND s.expires_at>datetime('now')", refresh_hash)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    await run(db, "UPDATE refresh_sessions SET revoked_at=datetime('now') WHERE id=?", row["session_id"])
    return json_response(await issue_tokens(db, env_of(request), row))


@app.post("/api/v1/auth/logout")
async def logout(request: Request, user: dict[str, Any] = Depends(user_dep)):
    header = request.headers.get("Authorization", "")
    token = header.split(" ", 1)[1] if " " in header else ""
    from auth import _jwt_decode, _secret
    payload = _jwt_decode(token, _secret(env_of(request)))
    if payload and payload.get("sid"):
        await run(db_of(request), "UPDATE refresh_sessions SET revoked_at=datetime('now') WHERE id=?", payload["sid"])
    return json_response({"ok": True})


@app.get("/api/v1/auth/me")
async def me(user: dict[str, Any] = Depends(user_dep)):
    return json_response(public_user(user))


@app.get("/api/v1/plans")
async def plans(request: Request):
    rows = await all_rows(db_of(request), "SELECT * FROM plans WHERE active=1 ORDER BY sort_order,name")
    return json_response(rows)


@app.get("/api/v1/credits/balance")
async def credit_balance(request: Request, user: dict[str, Any] = Depends(user_dep)):
    return json_response(await balance(db_of(request), user["id"]))


@app.get("/api/v1/usage/summary")
async def usage_summary(request: Request, user: dict[str, Any] = Depends(user_dep)):
    db = db_of(request)
    wallet = await balance(db, user["id"])
    plan = await active_plan(db, user["id"])
    jobs = await first_row(db, "SELECT COUNT(*) AS count FROM processing_jobs WHERE user_id=? AND status='completed' AND created_at>=date('now','start of month')", user["id"])
    return json_response({"credits_balance": wallet["balance"], "credits_lifetime_earned": wallet["lifetime_earned"], "credits_lifetime_spent": wallet["lifetime_spent"], "videos_processed": int((jobs or {}).get("count") or 0), "plan": plan})


@app.post("/api/v1/tts/generate")
async def proxy_tts(request: Request, body: TtsGenerateBody, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255), user: dict[str, Any] = Depends(user_dep)):
    from tts_proxy import TtsProxyError, generate as tts_generate
    try:
        result = await tts_generate(env_of(request), db_of(request), user["id"], body.model_dump(), body.text, await active_plan(db_of(request), user["id"]), idempotency_key)
        return json_response(result)
    except TtsProxyError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


@app.post("/api/v1/tts/batch")
async def proxy_tts_batch(request: Request, body: TtsBatchBody, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255), user: dict[str, Any] = Depends(user_dep)):
    from tts_proxy import TtsProxyError, batch as tts_batch
    plan = await active_plan(db_of(request), user["id"])
    max_end = max(item.target_end_ms for item in body.segments)
    if max_end > int(plan.get("max_video_duration_seconds", 300)) * 1000:
        raise HTTPException(status_code=403, detail="Batch duration exceeds the active plan limit")
    try:
        result = await tts_batch(env_of(request), db_of(request), user["id"], [item.model_dump() for item in body.segments], body.voice_config.model_dump(), plan, idempotency_key)
        return json_response(result)
    except TtsProxyError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


@app.post("/api/v1/payment/orders")
async def create_payment_order(request: Request, body: PaymentBody, user: dict[str, Any] = Depends(user_dep)):
    db = db_of(request)
    plan = await first_row(db, "SELECT * FROM plans WHERE id=? AND active=1", body.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    amount = str(plan["price_mmk"] if body.currency == "MMK" else plan["price_usdt"])
    order_id = str(uuid.uuid4())
    await run(db, "INSERT INTO payment_orders(id,user_id,plan_id,currency,amount,status,transaction_reference,proof_key) VALUES(?,?,?,?,?,'submitted',?,?)", order_id, user["id"], body.plan_id, body.currency, amount, body.transaction_reference, body.proof_key)
    await write_audit(db, user["id"], "payment_order_created", "payment_order", order_id, {"currency": body.currency, "amount": amount})
    return json_response({"id": order_id, "status": "submitted", "plan_id": body.plan_id, "currency": body.currency, "amount": amount})


@app.get("/api/v1/payments/orders")
async def payment_orders(request: Request, user: dict[str, Any] = Depends(user_dep)):
    return json_response(await all_rows(db_of(request), "SELECT po.*,p.name AS plan_name FROM payment_orders po JOIN plans p ON p.id=po.plan_id WHERE po.user_id=? ORDER BY po.created_at DESC LIMIT 100", user["id"]))


async def read_audio_bytes(request: Request) -> bytes:
    length = request.headers.get("Content-Length")
    maximum = int(getattr(env_of(request), "GEMINI_MAX_AUDIO_BYTES", "50000000"))
    if length and int(length) > maximum:
        raise HTTPException(status_code=413, detail="Audio file exceeds the configured limit")
    raw = await request.body()
    if hasattr(raw, "to_py"):
        raw = raw.to_py()
    data = bytes(raw)
    if len(data) > maximum:
        raise HTTPException(status_code=413, detail="Audio file exceeds the configured limit")
    if not data:
        raise HTTPException(status_code=422, detail="Audio file is empty")
    return data


@app.post("/api/v1/transcribe")
async def create_transcription_job(
    request: Request,
    target_language: str = Header(..., alias="X-Target-Language", min_length=2, max_length=64),
    video_duration_seconds: float = Header(default=60.0, alias="X-Video-Duration-Seconds", ge=1.0, le=86400.0),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    user: dict[str, Any] = Depends(user_dep),
):
    """Transcribe request-scoped audio without persisting any media.

    The Android client keeps the video, extracted WAV, transcript, TTS audio,
    and final MP4 locally. This endpoint buffers only the current WAV in memory,
    sends it to Gemini, and releases it before returning.
    """
    from scheduler import transcribe_direct

    db = db_of(request)
    if idempotency_key:
        old = await first_row(db, "SELECT id,status,result_json,error_code,error_message,provider_model FROM processing_jobs WHERE user_id=? AND idempotency_key=?", user["id"], idempotency_key)
        if old:
            return json_response(_job_payload(old))
    plan = await active_plan(db, user["id"])
    jobs_today = await first_row(db, "SELECT COUNT(*) AS count FROM processing_jobs WHERE user_id=? AND created_at>=date('now')", user["id"])
    free_limit = int(getattr(env_of(request), "FREE_DAILY_JOB_LIMIT", "3"))
    if plan.get("name") == "Free" and int((jobs_today or {}).get("count") or 0) >= free_limit:
        raise HTTPException(status_code=429, detail="Daily free processing limit reached")
    depth = await first_row(db, "SELECT COUNT(*) AS count FROM processing_jobs WHERE status IN ('queued','processing')")
    if int((depth or {}).get("count") or 0) >= int(getattr(env_of(request), "ACTIVE_REQUEST_MAX", "100")):
        raise HTTPException(status_code=429, detail="Too many active requests", headers={"Retry-After": "30"})

    data = await read_audio_bytes(request)
    job_id = str(uuid.uuid4())
    cost = video_cost(plan, video_duration_seconds)
    try:
        await add_credits(db, user["id"], -cost, "transcription_reservation", reference_id=job_id, description=f"Reserved for {target_language}", idempotency_key=f"job:{job_id}")
    except CreditError as exc:
        data = b""
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    # audio_key remains an empty compatibility field for the existing D1 schema;
    # no media object is created in R2.
    await run(db, "INSERT INTO processing_jobs(id,user_id,status,target_language,audio_key,idempotency_key,credits_reserved,started_at) VALUES(?,?, 'processing',?,?,?, ?,datetime('now'))", job_id, user["id"], target_language.strip(), "", idempotency_key, cost)
    await write_audit(db, user["id"], "transcription_started", "processing_job", job_id, {"credits_reserved": cost, "target_language": target_language.strip(), "media_retention": "none"})
    try:
        result, slot = await transcribe_direct(env_of(request), data, target_language.strip())
        await run(db, "UPDATE processing_jobs SET status='completed',result_json=?,provider_model=?,provider_slot=?,credits_committed=credits_reserved,completed_at=datetime('now'),updated_at=datetime('now') WHERE id=?", json.dumps(result, ensure_ascii=False, separators=(",", ":")), slot["model"], slot["id"], job_id)
        await write_audit(db, user["id"], "transcription_completed", "processing_job", job_id, {"provider_model": slot["model"], "media_retention": "none"})
        return json_response({"job_id": job_id, "status": "completed", "poll_after_seconds": 0, "result": result, "provider_model": slot["model"]})
    except Exception as exc:
        text = str(exc)[:500]
        await run(db, "UPDATE processing_jobs SET status='failed',error_code='GEMINI_FAILED',error_message=?,completed_at=datetime('now'),updated_at=datetime('now') WHERE id=?", text, job_id)
        await add_credits(db, user["id"], cost, "job_refund", reference_id=job_id, description="Refund for failed transcription", idempotency_key=f"refund:{job_id}")
        if "no_gemini_slot_available" in text:
            raise HTTPException(status_code=429, detail="All Gemini slots are busy. Please retry shortly.", headers={"Retry-After": "30"}) from exc
        raise HTTPException(status_code=502, detail={"code": "GEMINI_FAILED", "message": "Gemini transcription failed; reserved credits were refunded."}) from exc
    finally:
        # Drop the only request-scoped media reference as soon as the call ends.
        data = b""


@app.get("/api/v1/transcribe/{job_id}")
async def transcription_status(request: Request, job_id: str, user: dict[str, Any] = Depends(user_dep)):
    row = await first_row(db_of(request), "SELECT id,status,result_json,error_code,error_message,provider_model,credits_reserved,credits_committed FROM processing_jobs WHERE id=? AND user_id=?", job_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return json_response(_job_payload(row))


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": row["id"], "status": row["status"], "poll_after_seconds": 3, "result": loads(row.get("result_json")), "provider_model": row.get("provider_model"), "error_code": row.get("error_code"), "error": row.get("error_message")}


@app.post("/api/v1/backup/export")
async def backup_export(request: Request, user: dict[str, Any] = Depends(user_dep)):
    """Return metadata only; the backend never writes a backup object.

    The Android app is responsible for saving this JSON locally beside its local
    project media. Server-owned credits and account state are intentionally not
    imported from this payload.
    """
    db = db_of(request)
    projects = await all_rows(db, "SELECT external_id,title,target_language,source_name,settings_json,created_at,updated_at FROM user_projects WHERE user_id=? ORDER BY updated_at DESC LIMIT 500", user["id"])
    jobs = await all_rows(db, "SELECT id,status,target_language,provider_model,created_at,completed_at,error_code FROM processing_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 500", user["id"])
    ledger = await all_rows(db, "SELECT delta,kind,reference_id,description,created_at FROM credit_ledger WHERE user_id=? ORDER BY created_at DESC LIMIT 1000", user["id"])
    backup = {"backup_version": 2, "exported_at": datetime.now(timezone.utc).isoformat(), "profile": public_user(user), "projects": projects, "processing_history": jobs, "credit_history": ledger, "media_retention": "none"}
    raw = json.dumps(backup, ensure_ascii=False, separators=(",", ":")).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    await write_audit(db, user["id"], "backup_exported", "backup", user["id"], {"bytes": len(raw), "media_retention": "none"})
    return json_response({"checksum": checksum, "byte_size": len(raw), "backup": backup})


@app.post("/api/v1/backup/import")
async def backup_import(request: Request, body: BackupImportBody, user: dict[str, Any] = Depends(user_dep)):
    db = db_of(request)
    if body.profile.get("email") and str(body.profile["email"]).lower() != str(user["email"]).lower():
        raise HTTPException(status_code=403, detail="Backup belongs to another account")
    restored = 0
    for project in body.projects:
        external_id = str(project.get("external_id") or project.get("id") or "").strip()
        title = str(project.get("title") or "Imported project").strip()[:200]
        if not external_id or not title:
            continue
        await run(db, "INSERT INTO user_projects(id,user_id,external_id,title,target_language,source_name,source_object_key,settings_json,updated_at) VALUES(?,?,?,?,?,?,NULL,?,datetime('now')) ON CONFLICT(user_id,external_id) DO UPDATE SET title=excluded.title,target_language=excluded.target_language,source_name=excluded.source_name,source_object_key=NULL,settings_json=excluded.settings_json,updated_at=datetime('now')", str(uuid.uuid4()), user["id"], external_id, title, project.get("target_language"), project.get("source_name"), dumps(project.get("settings") or project.get("settings_json") or {}))
        restored += 1
    await write_audit(db, user["id"], "backup_imported", "user", user["id"], {"project_count": restored})
    return json_response({"ok": True, "imported_projects": restored, "note": "Project metadata was restored. Processing history and credit balance remain server-owned."})


@app.get("/api/v1/admin/summary")
async def admin_summary(request: Request, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    async def count(sql: str) -> int:
        row = await first_row(db, sql)
        return int((row or {}).get("count") or 0)
    return json_response({"users": await count("SELECT COUNT(*) AS count FROM users"), "active_jobs": await count("SELECT COUNT(*) AS count FROM processing_jobs WHERE status IN ('queued','processing')"), "completed_jobs": await count("SELECT COUNT(*) AS count FROM processing_jobs WHERE status='completed'"), "pending_payments": await count("SELECT COUNT(*) AS count FROM payment_orders WHERE status IN ('submitted','pending')"), "credits_issued": await count("SELECT COALESCE(SUM(delta),0) AS count FROM credit_ledger WHERE delta>0"), "credits_spent": await count("SELECT COALESCE(SUM(-delta),0) AS count FROM credit_ledger WHERE delta<0")})


@app.get("/api/v1/admin/users")
async def admin_users(request: Request, q: str = Query(default="", max_length=100), limit: int = Query(default=50, ge=1, le=200), admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    like = f"%{q.strip()}%"
    return json_response(await all_rows(db, "SELECT u.id,u.email,u.display_name,u.is_admin,u.is_active,u.is_banned,u.created_at,COALESCE(c.balance,0) AS credits FROM users u LEFT JOIN credit_accounts c ON c.user_id=u.id WHERE u.email LIKE ? OR COALESCE(u.display_name,'') LIKE ? ORDER BY u.created_at DESC LIMIT ?", like, like, limit))


@app.patch("/api/v1/admin/users/{user_id}")
async def admin_user_patch(request: Request, user_id: str, body: AdminUserPatch, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    updates: list[str] = []
    values: list[Any] = []
    for field in ("is_active", "is_banned", "is_admin", "display_name"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=?")
            values.append(int(value) if isinstance(value, bool) else value)
    if updates:
        values.extend([datetime.now(timezone.utc).isoformat(), user_id])
        await run(db, f"UPDATE users SET {', '.join(updates)},updated_at=? WHERE id=?", *values)
        await write_audit(db, admin["id"], "admin_user_updated", "user", user_id, body.model_dump(exclude_none=True))
    row = await first_row(db, "SELECT * FROM users WHERE id=?", user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return json_response(public_user(row))


@app.get("/api/v1/admin/plans")
async def admin_plans(request: Request, admin: dict[str, Any] = Depends(admin_dep)):
    return json_response(await all_rows(db_of(request), "SELECT * FROM plans ORDER BY sort_order,name"))


@app.post("/api/v1/admin/plans")
async def admin_plan_create(request: Request, body: PlanBody, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    plan_id = str(uuid.uuid4())
    await run(db, "INSERT INTO plans(id,name,description,included_credits,video_credit_cost,video_credit_cost_per_minute,tts_credit_per_100_chars,voice_clone_credit_cost,price_mmk,price_usdt,validity_days,max_video_duration_seconds,active,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", plan_id, body.name.strip(), body.description, body.included_credits, body.video_credit_cost, body.video_credit_cost_per_minute, body.tts_credit_per_100_chars, body.voice_clone_credit_cost, body.price_mmk, body.price_usdt, body.validity_days, body.max_video_duration_seconds, int(body.active), body.sort_order)
    await write_audit(db, admin["id"], "plan_created", "plan", plan_id, body.model_dump())
    return json_response(await first_row(db, "SELECT * FROM plans WHERE id=?", plan_id), status=201)


@app.patch("/api/v1/admin/plans/{plan_id}")
async def admin_plan_update(request: Request, plan_id: str, body: PlanBody, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    await run(db, "UPDATE plans SET name=?,description=?,included_credits=?,video_credit_cost=?,video_credit_cost_per_minute=?,tts_credit_per_100_chars=?,voice_clone_credit_cost=?,price_mmk=?,price_usdt=?,validity_days=?,max_video_duration_seconds=?,active=?,sort_order=?,updated_at=datetime('now') WHERE id=?", body.name.strip(), body.description, body.included_credits, body.video_credit_cost, body.video_credit_cost_per_minute, body.tts_credit_per_100_chars, body.voice_clone_credit_cost, body.price_mmk, body.price_usdt, body.validity_days, body.max_video_duration_seconds, int(body.active), body.sort_order, plan_id)
    await write_audit(db, admin["id"], "plan_updated", "plan", plan_id, body.model_dump())
    return json_response(await first_row(db, "SELECT * FROM plans WHERE id=?", plan_id))


@app.post("/api/v1/admin/credits/grant")
async def admin_grant_credits(request: Request, body: CreditBody, admin: dict[str, Any] = Depends(admin_dep)):
    try:
        result = await add_credits(db_of(request), body.user_id, body.credits, "admin_grant", description=body.description, actor_user_id=admin["id"], idempotency_key=body.idempotency_key)
    except CreditError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit(db_of(request), admin["id"], "credits_granted", "user", body.user_id, {"credits": body.credits, "description": body.description})
    return json_response(result)


@app.post("/api/v1/admin/credits/revoke")
async def admin_revoke_credits(request: Request, body: CreditBody, admin: dict[str, Any] = Depends(admin_dep)):
    try:
        result = await add_credits(db_of(request), body.user_id, -body.credits, "admin_revoke", description=body.description, actor_user_id=admin["id"], idempotency_key=body.idempotency_key)
    except CreditError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit(db_of(request), admin["id"], "credits_revoked", "user", body.user_id, {"credits": body.credits, "description": body.description})
    return json_response(result)


@app.get("/api/v1/admin/jobs")
async def admin_jobs(request: Request, status_filter: str = Query(default="", alias="status"), limit: int = Query(default=100, ge=1, le=500), admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    if status_filter:
        rows = await all_rows(db, "SELECT j.*,u.email FROM processing_jobs j JOIN users u ON u.id=j.user_id WHERE j.status=? ORDER BY j.created_at DESC LIMIT ?", status_filter, limit)
    else:
        rows = await all_rows(db, "SELECT j.*,u.email FROM processing_jobs j JOIN users u ON u.id=j.user_id ORDER BY j.created_at DESC LIMIT ?", limit)
    return json_response(rows)


@app.get("/api/v1/admin/payments")
async def admin_payments(request: Request, status_filter: str = Query(default="", alias="status"), limit: int = Query(default=100, ge=1, le=500), admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    if status_filter:
        rows = await all_rows(db, "SELECT po.*,u.email,p.name AS plan_name FROM payment_orders po JOIN users u ON u.id=po.user_id JOIN plans p ON p.id=po.plan_id WHERE po.status=? ORDER BY po.created_at DESC LIMIT ?", status_filter, limit)
    else:
        rows = await all_rows(db, "SELECT po.*,u.email,p.name AS plan_name FROM payment_orders po JOIN users u ON u.id=po.user_id JOIN plans p ON p.id=po.plan_id ORDER BY po.created_at DESC LIMIT ?", limit)
    return json_response(rows)


@app.post("/api/v1/admin/payments/{order_id}/approve")
async def admin_payment_approve(request: Request, order_id: str, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    order = await first_row(db, "SELECT * FROM payment_orders WHERE id=? AND status IN ('submitted','pending')", order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending payment not found")
    plan = await first_row(db, "SELECT * FROM plans WHERE id=?", order["plan_id"])
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    await run(db, "UPDATE payment_orders SET status='approved',reviewed_by=?,reviewed_at=datetime('now'),updated_at=datetime('now') WHERE id=?", admin["id"], order_id)
    expires_days = int(plan.get("validity_days") or 0)
    await run(db, "INSERT INTO user_plans(id,user_id,plan_id,status,expires_at) VALUES(?,?,?,'active',CASE WHEN ?=0 THEN NULL ELSE datetime('now',?) END)", str(uuid.uuid4()), order["user_id"], plan["id"], expires_days, f"+{expires_days} days")
    await add_credits(db, order["user_id"], int(plan.get("included_credits") or 0), "plan_purchase", reference_id=order_id, description=f"Approved {plan['name']} plan", actor_user_id=admin["id"], idempotency_key=f"payment:{order_id}")
    await write_audit(db, admin["id"], "payment_approved", "payment_order", order_id, {"user_id": order["user_id"], "plan_id": plan["id"]})
    return json_response({"ok": True, "order_id": order_id, "plan": plan["name"]})


@app.get("/api/v1/admin/gemini-slots")
async def admin_slots(request: Request, admin: dict[str, Any] = Depends(admin_dep)):
    return json_response(await all_rows(db_of(request), "SELECT id,account_id,project_id,model,priority,secret_name,enabled,rpm_limit,daily_limit,concurrency_limit,active_jobs,window_used,daily_used,cooldown_until,fail_count,last_used_at,updated_at FROM gemini_slots ORDER BY account_id,project_id,model"))


@app.post("/api/v1/admin/gemini-slots")
async def admin_slot_create(request: Request, body: SlotBody, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    slot_id = body.id or str(uuid.uuid4())
    await run(db, "INSERT INTO gemini_slots(id,account_id,project_id,model,priority,secret_name,enabled,rpm_limit,daily_limit,concurrency_limit) VALUES(?,?,?,?,?,?,?,?,?,?)", slot_id, body.account_id, body.project_id, body.model, body.priority, body.secret_name, int(body.enabled), body.rpm_limit, body.daily_limit, body.concurrency_limit)
    await write_audit(db, admin["id"], "gemini_slot_created", "gemini_slot", slot_id, {"account_id": body.account_id, "project_id": body.project_id, "model": body.model})
    return json_response(await first_row(db, "SELECT * FROM gemini_slots WHERE id=?", slot_id), status=201)


@app.patch("/api/v1/admin/gemini-slots/{slot_id}")
async def admin_slot_update(request: Request, slot_id: str, body: SlotBody, admin: dict[str, Any] = Depends(admin_dep)):
    db = db_of(request)
    await run(db, "UPDATE gemini_slots SET account_id=?,project_id=?,model=?,priority=?,secret_name=?,enabled=?,rpm_limit=?,daily_limit=?,concurrency_limit=?,updated_at=datetime('now') WHERE id=?", body.account_id, body.project_id, body.model, body.priority, body.secret_name, int(body.enabled), body.rpm_limit, body.daily_limit, body.concurrency_limit, slot_id)
    await write_audit(db, admin["id"], "gemini_slot_updated", "gemini_slot", slot_id, body.model_dump(exclude={"id"}))
    return json_response(await first_row(db, "SELECT * FROM gemini_slots WHERE id=?", slot_id))


@app.get("/api/v1/admin/audit")
async def admin_audit(request: Request, limit: int = Query(default=200, ge=1, le=500), admin: dict[str, Any] = Depends(admin_dep)):
    return json_response(await all_rows(db_of(request), "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", limit))


def hmac_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)
