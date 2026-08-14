import sqlite3
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "src"))
import main


class Result:
    def __init__(self, rows=None, changes=0):
        self.results = rows or []
        self.meta = type("Meta", (), {"changes": changes})()


class Statement:
    def __init__(self, conn, sql):
        self.conn, self.sql, self.params = conn, sql, ()

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        cur = self.conn.execute(self.sql, self.params)
        self.conn.commit()
        return Result(changes=cur.rowcount if cur.rowcount >= 0 else 0)

    async def all(self):
        cur = self.conn.execute(self.sql, self.params)
        columns = [col[0] for col in cur.description or []]
        return Result([dict(zip(columns, row)) for row in cur.fetchall()])


class D1:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.executescript((Path(__file__).parent / "migrations/0001_initial.sql").read_text())
        self.conn.executescript((Path(__file__).parent / "migrations/0002_ai_provider_models.sql").read_text())
        self.conn.executescript((Path(__file__).parent / "migrations/0003_retry_failed_jobs.sql").read_text())
        self.conn.executescript((Path(__file__).parent / "migrations/0004_runtime_settings.sql").read_text())
        self.conn.executescript((Path(__file__).parent / "migrations/0005_provider_credentials.sql").read_text())
        self.conn.executescript((Path(__file__).parent / "migrations/0006_gemini_provider.sql").read_text())
        self.conn.executescript((Path(__file__).parent / "migrations/0007_external_api_tokens.sql").read_text())

    def prepare(self, sql):
        return Statement(self.conn, sql)


class Env:
    JWT_SECRET = "local-test-jwt-secret-that-is-long-enough-32"
    JWT_ACCESS_TTL_SECONDS = "900"
    JWT_REFRESH_TTL_SECONDS = "2592000"
    GEMINI_MAX_AUDIO_BYTES = "150000000"
    ACTIVE_REQUEST_MAX = "100"
    PROVIDER_TIMEOUT_SECONDS = "900"
    FREE_DAILY_JOB_LIMIT = "3"
    ADMIN_EMAIL = "admin@example.com"
    ADMIN_PASSWORD = "correct horse battery staple"

    def __init__(self):
        self.DB = D1()


class EnvMiddleware:
    def __init__(self, app, env):
        self.app, self.env = app, env

    async def __call__(self, scope, receive, send):
        scope["env"] = self.env
        await self.app(scope, receive, send)


def test_cloudflare_core_lifecycle():
    async def fake_transcribe_and_translate(env, audio, target_language):
        assert audio == b"RIFF-fake-wave"
        assert target_language == "my"
        providers = {"stt": {"provider": "openrouter_stt", "model": "openai/whisper-large-v3", "row_id": "stt-local"}, "translation": {"provider": "opencode_zen", "model": "deepseek-v4-flash-free", "row_id": "translation-local"}}
        return ({"source_language": "en", "target_language": "my", "segments": [{"id": "s1", "start_ms": 0, "end_ms": 500, "original_text": "hello", "translated_text": "မင်္ဂလာပါ", "tts_text": "မင်္ဂလာပါ"}]}, providers)

    providers_module = types.ModuleType("ai_providers")
    providers_module.transcribe_and_translate = fake_transcribe_and_translate
    async def fake_fetch_catalog(env, provider, secret_name="", credential_id=""):
        raise RuntimeError("missing_provider_secret_for_test")
    providers_module.fetch_catalog = fake_fetch_catalog
    original_providers = sys.modules.get("ai_providers")
    sys.modules["ai_providers"] = providers_module
    env = Env()
    with TestClient(EnvMiddleware(main.app, env)) as client:
        registered = client.post("/api/v1/auth/register", json={"email": "admin@example.com", "password": "not-used-for-admin", "display_name": "Admin"})
        assert registered.status_code == 409, registered.text
        logged_in = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery staple"})
        assert logged_in.status_code == 200, logged_in.text
        access = logged_in.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        assert client.get("/api/v1/auth/me", headers=headers).json()["is_admin"] is True
        admin_page = client.get("/admin")
        assert admin_page.status_code == 200 and "VoiceRecap Control Center" in admin_page.text
        assert "Fetch live models" in admin_page.text and "/api/v1/admin/ai-models/catalog" in admin_page.text
        settings = client.get("/api/v1/admin/settings", headers=headers)
        assert settings.status_code == 200 and settings.json()[0]["key"] == "free_daily_job_limit"
        setting_update = client.patch("/api/v1/admin/settings/free_daily_job_limit", headers=headers, json={"key": "free_daily_job_limit", "value": 0})
        assert setting_update.status_code == 200 and setting_update.json()["value"] == "0"
        ai_model = client.post("/api/v1/admin/ai-models", headers=headers, json={"provider": "openrouter_stt", "capability": "stt", "model_id": "openai/whisper-large-v3", "display_name": "Whisper Large V3", "secret_name": "OPENROUTER_API_KEY", "priority": 0, "enabled": True, "rpm_limit": 10, "daily_limit": 100, "concurrency_limit": 1, "catalog": {"is_free": False}})
        assert ai_model.status_code == 201, ai_model.text
        ai_model_id = ai_model.json()["id"]
        ai_list = client.get("/api/v1/admin/ai-models", headers=headers)
        assert ai_list.status_code == 200 and ai_list.json()[0]["model_id"] == "openai/whisper-large-v3"
        assert ai_list.json()[0]["secret_configured"] is False
        ai_test = client.post(f"/api/v1/admin/ai-models/{ai_model_id}/test", headers=headers)
        assert ai_test.status_code == 200 and ai_test.json()["ok"] is False and ai_test.json()["secret_configured"] is False
        update_payload = ai_model.json()
        update_payload["enabled"] = False
        update_payload["catalog"] = {"is_free": False}
        updated_ai = client.patch(f"/api/v1/admin/ai-models/{ai_model_id}", headers=headers, json=update_payload)
        assert updated_ai.status_code == 200 and updated_ai.json()["enabled"] is False
        deleted_ai = client.delete(f"/api/v1/admin/ai-models/{ai_model_id}", headers=headers)
        assert deleted_ai.status_code == 200
        plan = client.post("/api/v1/admin/plans", headers=headers, json={"name": "Creator", "included_credits": 100, "video_credit_cost": 12, "video_credit_cost_per_minute": 1, "tts_credit_per_100_chars": 2, "voice_clone_credit_cost": 5, "price_mmk": 3000, "price_usdt": "1.25", "validity_days": 30, "max_video_duration_seconds": 1800, "active": True, "sort_order": 1})
        assert plan.status_code == 201, plan.text
        assert plan.json()["price_usdt"] == "1.25"
        failed_seed = "failed-stale-job"
        env.DB.conn.execute("INSERT INTO processing_jobs(id,user_id,status,target_language,audio_key,idempotency_key,error_code,error_message) VALUES(?,?,?,?,?,?,?,?)", (failed_seed, logged_in.json()["user"]["id"], "failed", "my", "", "failed-retry-key", "no_gemini_slot_available", "old stale Gemini slot error"))
        env.DB.conn.commit()
        fresh_after_failed = client.post("/api/v1/transcribe", headers={**headers, "X-Target-Language": "my", "X-Video-Duration-Seconds": "61", "Idempotency-Key": "failed-retry-key", "Content-Type": "audio/wav"}, content=b"RIFF-fake-wave")
        assert fresh_after_failed.status_code == 200 and fresh_after_failed.json()["job_id"] != failed_seed
        job = client.post("/api/v1/transcribe", headers={**headers, "X-Target-Language": "my", "X-Video-Duration-Seconds": "61", "Idempotency-Key": "job-1", "Content-Type": "audio/wav"}, content=b"RIFF-fake-wave")
        assert job.status_code == 200, job.text
        assert job.json()["status"] == "completed"
        assert job.json()["result"]["segments"][0]["translated_text"] == "မင်္ဂလာပါ"
        wallet = client.get("/api/v1/credits/balance", headers=headers).json()
        assert wallet["balance"] == 10
        duplicate = client.post("/api/v1/transcribe", headers={**headers, "X-Target-Language": "my", "X-Video-Duration-Seconds": "61", "Idempotency-Key": "job-1", "Content-Type": "audio/wav"}, content=b"second")
        assert duplicate.json()["job_id"] == job.json()["job_id"]
        imported = client.post("/api/v1/backup/import", headers=headers, json={"profile": {"email": "admin@example.com"}, "projects": [{"external_id": "local-1", "title": "Restored project", "target_language": "my"}]})
        assert imported.status_code == 200 and imported.json()["imported_projects"] == 1
        exported = client.post("/api/v1/backup/export", headers=headers)
        assert exported.status_code == 200
        assert exported.json()["backup"]["media_retention"] == "none"
    if original_providers is None:
        del sys.modules["ai_providers"]
    else:
        sys.modules["ai_providers"] = original_providers


def test_admin_encrypted_credential_and_custom_model_routes():
    async def fake_encrypt_secret(env, value):
        return f"enc:v1:test:{value}"

    original_encrypt = main.encrypt_secret
    main.encrypt_secret = fake_encrypt_secret
    env = Env()
    try:
        with TestClient(EnvMiddleware(main.app, env)) as client:
            login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery staple"})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            credential = client.post("/api/v1/admin/provider-credentials", headers=headers, json={
                "name": "Custom translation gateway",
                "provider_type": "custom",
                "api_key": "secret-value-1234",
                "base_url": "https://example.test/v1",
                "models_url": "https://example.test/v1/models",
                "api_format": "openai_chat",
                "auth_type": "bearer",
                "auth_header": "Authorization",
                "enabled": True,
            })
            assert credential.status_code == 201, credential.text
            assert credential.json()["credential_last4"] == "1234"
            assert "credential_ciphertext" not in credential.json()
            custom_credential_id = credential.json()["id"]
            gemini_credential = client.post("/api/v1/admin/provider-credentials", headers=headers, json={
                "name": "Gemini translation key", "provider_type": "gemini", "api_key": "gemini-secret-5678",
                "api_format": "openai_chat", "auth_type": "bearer", "auth_header": "Authorization", "enabled": True,
            })
            assert gemini_credential.status_code == 201, gemini_credential.text
            gemini_credential_id = gemini_credential.json()["id"]
            rows = client.get("/api/v1/admin/provider-credentials", headers=headers)
            assert rows.status_code == 200 and len(rows.json()) == 2
            gemini_model = client.post("/api/v1/admin/ai-models", headers=headers, json={
                "provider": "gemini", "capability": "translation", "model_id": "gemini-2.5-flash",
                "display_name": "Gemini 2.5 Flash", "secret_name": "ADMIN_VAULT", "credential_id": gemini_credential_id,
                "priority": 0, "enabled": True, "rpm_limit": 5, "daily_limit": 20, "concurrency_limit": 1,
                "catalog": {"model_id": "gemini-2.5-flash"},
            })
            assert gemini_model.status_code == 201, gemini_model.text
            custom_model = client.post("/api/v1/admin/ai-models", headers=headers, json={
                "provider": "custom", "capability": "translation", "model_id": "example-model",
                "display_name": "Example model", "secret_name": "ADMIN_VAULT", "credential_id": custom_credential_id,
                "priority": 1, "enabled": True, "rpm_limit": 5, "daily_limit": 20, "concurrency_limit": 1,
                "catalog": {"model_id": "example-model"},
            })
            assert custom_model.status_code == 201, custom_model.text
            assert custom_model.json()["credential_id"] == custom_credential_id
            disabled = client.delete(f"/api/v1/admin/provider-credentials/{custom_credential_id}", headers=headers)
            assert disabled.status_code == 200
            model_rows = client.get("/api/v1/admin/ai-models", headers=headers)
            assert model_rows.status_code == 200
            disabled_row = next(row for row in model_rows.json() if row["model_id"] == "example-model")
            assert disabled_row["enabled"] is False and disabled_row["credential_id"] is None
    finally:
        main.encrypt_secret = original_encrypt


def test_admin_external_token_generate_scope_and_revoke():
    env = Env()
    with TestClient(EnvMiddleware(main.app, env)) as client:
        login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery staple"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post("/api/v1/admin/external-api-tokens", headers=headers, json={"name": "Other project", "expires_days": 30})
        assert created.status_code == 201, created.text
        payload = created.json()
        assert payload["token"].startswith("vrtts_")
        assert payload["scope"] == "tts:voice_clone"
        assert "token_hash" not in payload
        raw_token = payload["token"]
        token_row = env.DB.conn.execute("SELECT token_hash,token_prefix FROM external_api_tokens WHERE id=?", (payload["id"],)).fetchone()
        assert token_row and token_row[0] != raw_token and raw_token.startswith(token_row[1])
        external_headers = {"Authorization": f"Bearer {raw_token}", "Idempotency-Key": "external-test-1"}
        upstream_missing = client.post("/api/v1/tts/generate", headers=external_headers, json={"text": "hello", "voice_mode": "clone", "reference_audio_base64": "UklGRg=="})
        assert upstream_missing.status_code == 503, upstream_missing.text
        assert upstream_missing.json()["detail"]["code"] == "MODAL_ENDPOINT_MISSING"
        listed = client.get("/api/v1/admin/external-api-tokens", headers=headers)
        assert listed.status_code == 200 and listed.json()[0]["request_count"] == 1
        revoked = client.delete(f"/api/v1/admin/external-api-tokens/{payload['id']}", headers=headers)
        assert revoked.status_code == 200
        denied = client.post("/api/v1/tts/generate", headers={"Authorization": f"Bearer {raw_token}"}, json={"text": "hello", "voice_mode": "clone", "reference_audio_base64": "UklGRg=="})
        assert denied.status_code == 401, denied.text


def test_admin_credential_route_surfaces_vault_configuration_error():
    async def missing_master_key(env, value):
        raise ValueError("PROVIDER_CREDENTIAL_MASTER_KEY must be configured with at least 32 characters")

    original_encrypt = main.encrypt_secret
    main.encrypt_secret = missing_master_key
    env = Env()
    try:
        with TestClient(EnvMiddleware(main.app, env)) as client:
            login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery staple"})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            response = client.post("/api/v1/admin/provider-credentials", headers=headers, json={
                "name": "Should fail clearly", "provider_type": "openrouter_stt", "api_key": "secret-value-1234",
                "api_format": "openai_audio_transcription", "auth_type": "bearer", "auth_header": "Authorization",
                "enabled": True,
            })
            assert response.status_code == 503, response.text
            assert "PROVIDER_CREDENTIAL_MASTER_KEY" in str(response.json()["detail"])
    finally:
        main.encrypt_secret = original_encrypt


if __name__ == "__main__":
    test_cloudflare_core_lifecycle()
    test_admin_encrypted_credential_and_custom_model_routes()
    test_admin_external_token_generate_scope_and_revoke()
    print("local API tests passed")
