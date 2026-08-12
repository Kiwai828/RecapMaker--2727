PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_config (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_banned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);

CREATE TABLE IF NOT EXISTS refresh_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  refresh_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON refresh_sessions(user_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  description TEXT,
  included_credits INTEGER NOT NULL CHECK(included_credits >= 0),
  video_credit_cost INTEGER NOT NULL CHECK(video_credit_cost >= 0),
  video_credit_cost_per_minute INTEGER NOT NULL DEFAULT 0 CHECK(video_credit_cost_per_minute >= 0),
  tts_credit_per_100_chars INTEGER NOT NULL DEFAULT 1 CHECK(tts_credit_per_100_chars >= 0),
  voice_clone_credit_cost INTEGER NOT NULL DEFAULT 0 CHECK(voice_clone_credit_cost >= 0),
  price_mmk INTEGER NOT NULL DEFAULT 0 CHECK(price_mmk >= 0),
  price_usdt TEXT NOT NULL DEFAULT '0' CHECK(CAST(price_usdt AS REAL) >= 0),
  validity_days INTEGER NOT NULL DEFAULT 30 CHECK(validity_days >= 0),
  max_video_duration_seconds INTEGER NOT NULL DEFAULT 300 CHECK(max_video_duration_seconds > 0),
  active INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_plans (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL REFERENCES plans(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','expired','cancelled')),
  starts_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_plans_active ON user_plans(user_id, status, expires_at);

CREATE TABLE IF NOT EXISTS credit_accounts (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
  lifetime_earned INTEGER NOT NULL DEFAULT 0,
  lifetime_spent INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  delta INTEGER NOT NULL,
  balance_after INTEGER NOT NULL CHECK(balance_after >= 0),
  kind TEXT NOT NULL,
  reference_id TEXT,
  description TEXT,
  actor_user_id TEXT,
  idempotency_key TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_created ON credit_ledger(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_projects (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  target_language TEXT,
  source_name TEXT,
  source_object_key TEXT,
  settings_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_user_projects_user ON user_projects(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS processing_jobs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','processing','completed','failed','cancelled')),
  target_language TEXT NOT NULL,
  audio_key TEXT NOT NULL,
  idempotency_key TEXT,
  result_json TEXT,
  provider_model TEXT,
  provider_slot TEXT,
  credits_reserved INTEGER NOT NULL DEFAULT 0,
  credits_committed INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  queue_attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  started_at TEXT,
  completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_user_idempotency ON processing_jobs(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON processing_jobs(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON processing_jobs(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS gemini_slots (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  model TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  secret_name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  rpm_limit INTEGER NOT NULL DEFAULT 10,
  daily_limit INTEGER NOT NULL DEFAULT 100,
  concurrency_limit INTEGER NOT NULL DEFAULT 1,
  active_jobs INTEGER NOT NULL DEFAULT 0,
  window_started_at TEXT,
  window_used INTEGER NOT NULL DEFAULT 0,
  daily_reset_at TEXT,
  daily_used INTEGER NOT NULL DEFAULT 0,
  cooldown_until TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(account_id, project_id, model, secret_name)
);
CREATE INDEX IF NOT EXISTS idx_gemini_slots_eligible ON gemini_slots(enabled, cooldown_until, active_jobs, last_used_at);

CREATE TABLE IF NOT EXISTS scheduler_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payment_orders (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL REFERENCES plans(id),
  currency TEXT NOT NULL CHECK(currency IN ('MMK','USDT')),
  amount TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','submitted','approved','rejected','expired')),
  provider TEXT,
  transaction_reference TEXT,
  proof_key TEXT,
  note TEXT,
  reviewed_by TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_orders_status ON payment_orders(status, created_at);

CREATE TABLE IF NOT EXISTS backup_manifests (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  object_key TEXT NOT NULL UNIQUE,
  checksum TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY,
  actor_user_id TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  metadata_json TEXT,
  ip_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);

INSERT OR IGNORE INTO app_config(key, value_json) VALUES
  ('video_credit_cost_default', '10'),
  ('video_credit_cost_per_minute_default', '0'),
  ('tts_credit_per_100_chars_default', '1'),
  ('max_queue_depth', '500'),
  ('free_daily_job_limit', '3');

INSERT OR IGNORE INTO plans(id, name, description, included_credits, video_credit_cost, video_credit_cost_per_minute, tts_credit_per_100_chars, voice_clone_credit_cost, price_mmk, price_usdt, validity_days, max_video_duration_seconds, active, sort_order)
VALUES ('free', 'Free', 'Starter credits', 30, 10, 0, 1, 0, 0, '0', 30, 300, 1, 0);
