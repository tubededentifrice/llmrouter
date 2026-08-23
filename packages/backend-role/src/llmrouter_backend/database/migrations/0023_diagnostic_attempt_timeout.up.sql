DO $migration$
DECLARE
    definition text;
BEGIN
    SELECT pg_get_functiondef(
        'router.validate_provider_route_execution_snapshot()'::regprocedure
    ) INTO definition;
    IF position(
        'AND NEW.attempt_timeout_ms = 120000' IN definition
    ) = 0 OR position(
        'AND NEW.attempt_timeout_ms BETWEEN 100 AND 120000' IN definition
    ) <> 0 THEN
        RAISE EXCEPTION 'diagnostic attempt timeout guard has an unexpected definition'
            USING ERRCODE = '55000';
    END IF;
    EXECUTE replace(
        definition,
        'AND NEW.attempt_timeout_ms = 120000',
        'AND NEW.attempt_timeout_ms BETWEEN 100 AND 120000'
    );
END;
$migration$;
