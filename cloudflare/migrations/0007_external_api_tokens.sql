-- Admin-generated external API tokens for scoped VoxCPM2 gateway access.
-- Only token_hash is stored; the raw token is shown once at creation time.
CREATE TABLE IF NOT EXISTS external_api_tokens (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  token_prefix TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'tts:voice_clone' CHECK(scope IN ('tts:voice_clone')),
  owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT,
  revoked_at TEXT,
  last_used_at TEXT,
  request_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_external_api_tokens_active ON external_api_tokens(token_hash, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_external_api_tokens_owner ON external_api_tokens(owner_user_id, revoked_at, created_at);
