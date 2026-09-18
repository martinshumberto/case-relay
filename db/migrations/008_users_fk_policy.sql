ALTER TABLE users DROP CONSTRAINT IF EXISTS users_company_id_fkey;
ALTER TABLE users ADD CONSTRAINT users_company_id_fkey
  FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE RESTRICT;
