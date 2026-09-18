CREATE TABLE IF NOT EXISTS job_events (
  id          BIGSERIAL PRIMARY KEY,
  job_id      INT NOT NULL REFERENCES jobs(id),
  company_id  INT NOT NULL REFERENCES companies(id),
  event       TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  actor       TEXT,
  request_id  TEXT,
  detail      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_events_job_idx ON job_events (job_id, id DESC);

CREATE INDEX IF NOT EXISTS job_events_request_idx
  ON job_events (request_id) WHERE request_id IS NOT NULL;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key_uniq
  ON jobs (company_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS jobs_running_by_company_idx
  ON jobs (company_id) WHERE status = 'running';

ANALYZE jobs;
