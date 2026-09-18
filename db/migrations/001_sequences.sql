SELECT setval(
  pg_get_serial_sequence('companies', 'id'),
  GREATEST((SELECT max(id) FROM companies), 1)
);

SELECT setval(
  pg_get_serial_sequence('users', 'id'),
  GREATEST((SELECT max(id) FROM users), 1)
);

SELECT setval(
  pg_get_serial_sequence('jobs', 'id'),
  GREATEST((SELECT max(id) FROM jobs), 1)
);

SELECT setval(
  pg_get_serial_sequence('job_results', 'id'),
  GREATEST((SELECT max(id) FROM job_results), 1)
);
