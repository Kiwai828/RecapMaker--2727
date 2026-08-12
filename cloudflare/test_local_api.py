import sqlite3
import sys
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

    def prepare(self, sql):
        return Statement(self.conn, sql)


class R2:
    def __init__(self):
        self.objects = {}

    async def put(self, key, value, *args):
        self.objects[key] = bytes(value)

    async def delete(self, key):
        self.objects.pop(key, None)


class Queue:
    def __init__(self):
        self.messages = []

    async def send(self, value, *args, **kwargs):
        self.messages.append(value)


class Env:
    JWT_SECRET = "local-test-jwt-secret-that-is-long-enough-32"
    JWT_ACCESS_TTL_SECONDS = "900"
    JWT_REFRESH_TTL_SECONDS = "2592000"
    GEMINI_MAX_AUDIO_BYTES = "150000000"
    QUEUE_MAX_DEPTH = "500"
    FREE_DAILY_JOB_LIMIT = "3"
    ADMIN_EMAIL = "admin@example.com"
    ADMIN_PASSWORD = "correct horse battery staple"

    def __init__(self):
        self.DB = D1()
        self.MEDIA = R2()
        self.BACKUPS = R2()
        self.JOB_QUEUE = Queue()


class EnvMiddleware:
    def __init__(self, app, env):
        self.app, self.env = app, env

    async def __call__(self, scope, receive, send):
        scope["env"] = self.env
        await self.app(scope, receive, send)


def test_cloudflare_core_lifecycle():
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
        plan = client.post("/api/v1/admin/plans", headers=headers, json={"name": "Creator", "included_credits": 100, "video_credit_cost": 12, "video_credit_cost_per_minute": 1, "tts_credit_per_100_chars": 2, "voice_clone_credit_cost": 5, "price_mmk": 3000, "price_usdt": "1.25", "validity_days": 30, "max_video_duration_seconds": 1800, "active": True, "sort_order": 1})
        assert plan.status_code == 201, plan.text
        assert plan.json()["price_usdt"] == "1.25"
        job = client.post("/api/v1/transcribe", headers={**headers, "X-Target-Language": "my", "X-Video-Duration-Seconds": "61", "Idempotency-Key": "job-1", "Content-Type": "audio/wav"}, content=b"RIFF-fake-wave")
        assert job.status_code == 200, job.text
        assert job.json()["status"] == "queued"
        assert len(env.JOB_QUEUE.messages) == 1
        wallet = client.get("/api/v1/credits/balance", headers=headers).json()
        assert wallet["balance"] == 20
        duplicate = client.post("/api/v1/transcribe", headers={**headers, "X-Target-Language": "my", "X-Video-Duration-Seconds": "61", "Idempotency-Key": "job-1", "Content-Type": "audio/wav"}, content=b"second")
        assert duplicate.json()["job_id"] == job.json()["job_id"]
        imported = client.post("/api/v1/backup/import", headers=headers, json={"profile": {"email": "admin@example.com"}, "projects": [{"external_id": "local-1", "title": "Restored project", "target_language": "my"}]})
        assert imported.status_code == 200 and imported.json()["imported_projects"] == 1
