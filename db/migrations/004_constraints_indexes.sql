CREATE UNIQUE INDEX IF NOT EXISTS job_results_job_id_key ON job_results (job_id);

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
  CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'));

UPDATE companies SET job_quota = 0 WHERE job_quota < 0;

ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_job_quota_check;
ALTER TABLE companies ADD CONSTRAINT companies_job_quota_check
  CHECK (job_quota >= 0);

ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_max_concurrent_check;
ALTER TABLE companies ADD CONSTRAINT companies_max_concurrent_check
  CHECK (max_concurrent_jobs > 0);

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_max_attempts_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_max_attempts_check
  CHECK (max_attempts > 0 AND attempts >= 0);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (email);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jobs_set_updated_at ON jobs;
CREATE TRIGGER jobs_set_updated_at
  BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS jobs_company_created_idx
  ON jobs (company_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS jobs_queued_idx
  ON jobs (id) WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS jobs_running_lease_idx
  ON jobs (locked_until) WHERE status = 'running';

CREATE INDEX IF NOT EXISTS jobs_request_id_idx
  ON jobs (request_id) WHERE request_id IS NOT NULL;

ANALYZE jobs;
ANALYZE job_results;
ANALYZE companies;
ANALYZE users;
