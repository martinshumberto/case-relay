CREATE INDEX CONCURRENTLY IF NOT EXISTS jobs_queued_by_company_idx
  ON jobs (company_id, created_at, id) WHERE status = 'queued';

CREATE INDEX CONCURRENTLY IF NOT EXISTS jobs_active_by_company_idx
  ON jobs (company_id, status) WHERE status IN ('queued', 'running');

CREATE INDEX CONCURRENTLY IF NOT EXISTS jobs_failed_recent_idx
  ON jobs (failed_at) WHERE status = 'failed';

DROP INDEX CONCURRENTLY IF EXISTS jobs_queued_idx;
