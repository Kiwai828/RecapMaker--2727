import sqlite3
from pathlib import Path

migration = Path(__file__).parent / "migrations" / "0001_initial.sql"
db = sqlite3.connect(":memory:")
db.executescript(migration.read_text())
tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
required = {"users", "plans", "credit_accounts", "credit_ledger", "user_projects", "processing_jobs", "gemini_slots", "payment_orders", "backup_manifests", "audit_logs"}
missing = required - tables
assert not missing, missing
plan = db.execute("SELECT name,included_credits,video_credit_cost,price_usdt FROM plans WHERE id='free'").fetchone()
assert plan == ("Free", 30, 10, "0"), plan
print("D1 migration OK", len(tables), "tables")
