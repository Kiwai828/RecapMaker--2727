-- Admin-managed runtime configuration. Values are validated by the API.
CREATE TABLE IF NOT EXISTS runtime_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  description TEXT,
  updated_by TEXT REFERENCES users(id),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO runtime_settings(key,value,description)
VALUES('free_daily_job_limit','3','Completed transcription jobs allowed per Free user per UTC day; 0 means unlimited')
ON CONFLICT(key) DO NOTHING;
