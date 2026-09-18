ALTER TABLE jobs SET (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_analyze_scale_factor = 0.01,
  autovacuum_vacuum_insert_scale_factor = 0.02
);

ALTER TABLE job_events SET (
  autovacuum_vacuum_insert_scale_factor = 0.02
);

ALTER TABLE job_results SET (
  autovacuum_vacuum_scale_factor = 0.05
);
