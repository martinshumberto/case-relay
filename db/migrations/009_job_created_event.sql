CREATE OR REPLACE FUNCTION record_job_created() RETURNS trigger AS $$
BEGIN
  INSERT INTO job_events (job_id, company_id, event, to_status, actor, request_id, detail)
  VALUES (NEW.id, NEW.company_id, 'job.created', NEW.status, 'db', NEW.request_id,
          'kind=' || NEW.kind);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jobs_record_creation ON jobs;
CREATE TRIGGER jobs_record_creation
  AFTER INSERT ON jobs
  FOR EACH ROW EXECUTE FUNCTION record_job_created();

INSERT INTO job_events (job_id, company_id, event, to_status, actor, request_id, detail, created_at)
SELECT j.id, j.company_id, 'job.created', 'queued', 'backfill', j.request_id,
       'kind=' || j.kind, j.created_at
  FROM jobs j
 WHERE NOT EXISTS (
     SELECT 1 FROM job_events e WHERE e.job_id = j.id AND e.event = 'job.created'
 );
