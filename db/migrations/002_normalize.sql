DELETE FROM job_results a
 USING job_results b
 WHERE a.job_id = b.job_id
   AND a.id > b.id;

UPDATE jobs
   SET status = 'queued',
       updated_at = now()
 WHERE status = 'running'
   AND updated_at < now() - interval '1 hour';
