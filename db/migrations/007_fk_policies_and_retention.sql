ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_company_id_fkey;
ALTER TABLE jobs ADD CONSTRAINT jobs_company_id_fkey
  FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE RESTRICT;

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_created_by_fkey;
ALTER TABLE jobs ADD CONSTRAINT jobs_created_by_fkey
  FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE job_results DROP CONSTRAINT IF EXISTS job_results_job_id_fkey;
ALTER TABLE job_results ADD CONSTRAINT job_results_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE job_events DROP CONSTRAINT IF EXISTS job_events_job_id_fkey;
ALTER TABLE job_events ADD CONSTRAINT job_events_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE job_events DROP CONSTRAINT IF EXISTS job_events_company_id_fkey;
ALTER TABLE job_events ADD CONSTRAINT job_events_company_id_fkey
  FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE RESTRICT;

ALTER TABLE companies
  ADD COLUMN IF NOT EXISTS result_retention_days INT NOT NULL DEFAULT 90;

ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_retention_check;
ALTER TABLE companies ADD CONSTRAINT companies_retention_check
  CHECK (result_retention_days BETWEEN 1 AND 3650);

ALTER TABLE job_results ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS job_results_purge_idx
  ON job_results (job_id) WHERE purged_at IS NULL;

ANALYZE job_results;
