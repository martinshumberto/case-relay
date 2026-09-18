ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failure_reason TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INT NOT NULL DEFAULT 3;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS quota_charged BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS request_id TEXT;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS locked_by TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;

UPDATE jobs
   SET quota_charged = true
 WHERE status = 'done'
   AND NOT quota_charged;

UPDATE jobs
   SET max_attempts = attempts + 1
 WHERE status = 'failed'
   AND attempts >= max_attempts;
