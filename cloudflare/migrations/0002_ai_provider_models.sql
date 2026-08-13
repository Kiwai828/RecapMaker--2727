-- Provider-neutral runtime AI configuration.
-- Secret values are stored only in Cloudflare Worker secrets; this table stores binding names.
CREATE TABLE IF NOT EXISTS ai_provider_models (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK(provider IN ('openrouter_stt','opencode_zen')),
  capability TEXT NOT NULL CHECK(capability IN ('stt','translation')),
  model_id TEXT NOT NULL,
  display_name TEXT,
  secret_name TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  rpm_limit INTEGER NOT NULL DEFAULT 10,
  daily_limit INTEGER NOT NULL DEFAULT 100,
  concurrency_limit INTEGER NOT NULL DEFAULT 1,
  active_requests INTEGER NOT NULL DEFAULT 0,
  window_started_at TEXT,
  window_used INTEGER NOT NULL DEFAULT 0,
  daily_reset_at TEXT,
  daily_used INTEGER NOT NULL DEFAULT 0,
  cooldown_until TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  catalog_json TEXT NOT NULL DEFAULT '{}',
  last_catalog_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(provider, capability, model_id, secret_name)
);
CREATE INDEX IF NOT EXISTS idx_ai_provider_models_claim ON ai_provider_models(capability, enabled, cooldown_until, priority, last_used_at);
CREATE INDEX IF NOT EXISTS idx_ai_provider_models_provider ON ai_provider_models(provider, capability, enabled);
