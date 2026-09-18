ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;

UPDATE jobs
   SET status = 'failed',
       failed_at = coalesce(failed_at, now()),
       failure_reason = coalesce(failure_reason, 'kind não suportado: ' || kind),
       locked_by = NULL,
       locked_until = NULL
 WHERE status IN ('queued', 'running')
   AND kind NOT IN ('report', 'import');

ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
  CHECK (kind IN ('report', 'import')) NOT VALID;
