DO $$
DECLARE invalidos BIGINT;
BEGIN
  SELECT count(*) INTO invalidos FROM jobs WHERE kind NOT IN ('report', 'import');
  IF invalidos > 0 THEN
    RAISE EXCEPTION
      'jobs_kind_check: % linha(s) com kind inválido em estado terminal. A migration 010 só trata queued e running, porque reescrever o estado de um job concluído falsifica o histórico. Decida caso a caso antes de validar.',
      invalidos;
  END IF;
END $$;

ALTER TABLE jobs VALIDATE CONSTRAINT jobs_kind_check;
