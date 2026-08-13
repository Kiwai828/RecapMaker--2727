-- Failed jobs are historical attempts. They must not reserve an idempotency key
-- forever, otherwise Android WorkManager retries return the same stale failure.
-- Only active or completed jobs are idempotent replays.
DROP INDEX IF EXISTS idx_jobs_user_idempotency;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_user_idempotency_active
  ON processing_jobs(user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND status IN ('queued', 'processing', 'completed');
