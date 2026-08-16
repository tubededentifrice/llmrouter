DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.configuration_write_idempotency_bindings) THEN
        RAISE EXCEPTION 'administration API migration cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP TRIGGER configuration_write_idempotency_bindings_append_only
    ON router.configuration_write_idempotency_bindings;
DROP TABLE router.configuration_write_idempotency_bindings;
