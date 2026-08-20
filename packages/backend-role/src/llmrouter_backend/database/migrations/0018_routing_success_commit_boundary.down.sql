-- Restore the original commit-boundary guard from migration 0015.
DO $$
DECLARE
    definition text;
BEGIN
    SELECT pg_get_functiondef(
        'router.validate_routing_candidate_decision()'::regprocedure
    ) INTO definition;
    IF position(
        'IF NEW.attempt_state <> ''succeeded'' AND EXISTS (' IN definition
    ) = 0 THEN
        RAISE EXCEPTION 'the routing decision guard is not at migration 0018';
    END IF;
    definition := replace(
        definition,
        'IF NEW.attempt_state <> ''succeeded'' AND EXISTS (',
        'IF EXISTS ('
    );
    EXECUTE definition;
END;
$$;
