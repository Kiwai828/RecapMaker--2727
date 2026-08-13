-- Admin-managed provider credential vault and extensible provider/model catalog.
-- credential_ciphertext is AES-GCM encrypted by the Worker using PROVIDER_CREDENTIAL_MASTER_KEY;
-- plaintext API keys are never stored in D1 or returned to the admin UI.
CREATE TABLE IF NOT EXISTS ai_provider_credentials (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  provider_type TEXT NOT NULL CHECK(provider_type IN ('openrouter_stt','opencode_zen','custom')),
  base_url TEXT,
  models_url TEXT,
  api_format TEXT NOT NULL DEFAULT 'openai_chat' CHECK(api_format IN ('openai_chat','openai_responses','openai_audio_transcription','anthropic_messages','custom_json')),
  auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK(auth_type IN ('bearer','x_api_key','query_param','none')),
  auth_header TEXT NOT NULL DEFAULT 'Authorization',
  auth_query_name TEXT,
  credential_ciphertext TEXT NOT NULL,
  credential_last4 TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_tested_at TEXT,
  last_test_status TEXT,
  last_test_message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_provider_credentials_name ON ai_provider_credentials(name);
CREATE INDEX IF NOT EXISTS idx_ai_provider_credentials_enabled ON ai_provider_credentials(enabled, provider_type);

-- Rebuild the original table so the provider CHECK constraint also accepts custom.
ALTER TABLE ai_provider_models RENAME TO ai_provider_models_old_0005;
CREATE TABLE ai_provider_models (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK(provider IN ('openrouter_stt','opencode_zen','custom')),
  capability TEXT NOT NULL CHECK(capability IN ('stt','translation')),
  model_id TEXT NOT NULL,
  display_name TEXT,
  secret_name TEXT NOT NULL DEFAULT 'ADMIN_VAULT',
  credential_id TEXT REFERENCES ai_provider_credentials(id) ON DELETE SET NULL,
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
  UNIQUE(provider, capability, model_id, secret_name, credential_id)
);
INSERT INTO ai_provider_models(
  id,provider,capability,model_id,display_name,secret_name,priority,enabled,rpm_limit,daily_limit,concurrency_limit,
  active_requests,window_started_at,window_used,daily_reset_at,daily_used,cooldown_until,fail_count,last_used_at,
  catalog_json,last_catalog_at,created_at,updated_at
)
SELECT id,provider,capability,model_id,display_name,COALESCE(secret_name,'ADMIN_VAULT'),priority,enabled,rpm_limit,daily_limit,concurrency_limit,
  active_requests,window_started_at,window_used,daily_reset_at,daily_used,cooldown_until,fail_count,last_used_at,
  catalog_json,last_catalog_at,created_at,updated_at
FROM ai_provider_models_old_0005;
DROP TABLE ai_provider_models_old_0005;
CREATE INDEX IF NOT EXISTS idx_ai_provider_models_claim ON ai_provider_models(capability, enabled, cooldown_until, priority, last_used_at);
CREATE INDEX IF NOT EXISTS idx_ai_provider_models_provider ON ai_provider_models(provider, capability, enabled);
CREATE INDEX IF NOT EXISTS idx_ai_provider_models_credential ON ai_provider_models(credential_id);

INSERT OR IGNORE INTO runtime_settings(key,value,description) VALUES
  ('provider_credentials_enabled',1,'Allow encrypted provider credentials managed from the Admin panel');

-- Existing models continue using their legacy Worker secret bindings until an
-- administrator explicitly assigns a credential from the Admin panel.
