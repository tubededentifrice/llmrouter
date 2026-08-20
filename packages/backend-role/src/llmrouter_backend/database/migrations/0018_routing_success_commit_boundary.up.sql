-- A successful attempt can commit output before its terminal routing decision.
CREATE OR REPLACE FUNCTION router.validate_routing_candidate_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_scope_id text;
BEGIN
    PERFORM 1 FROM router.logical_requests
    WHERE row_id = NEW.request_row_id FOR UPDATE;
    IF NEW.decision_sequence <> 1 + COALESCE((
        SELECT max(decision_sequence)
        FROM router.routing_candidate_decisions
        WHERE request_row_id = NEW.request_row_id
    ), 0) THEN
        RAISE EXCEPTION 'routing decisions must use the next sequence number'
            USING ERRCODE = '40001';
    END IF;
    SELECT CASE NEW.affected_scope
        WHEN 'attempt' THEN NEW.attempt_id::text
        WHEN 'provider_model_route' THEN snapshot.provider_model_route_id::text
        WHEN 'provider_instance' THEN snapshot.provider_instance_id::text
        WHEN 'credential' THEN snapshot.credential_id::text
        WHEN 'assignment_candidate' THEN
            COALESCE(request.assignment_id::text, 'exact:' || request.exact_route_id::text)
            || ':' || NEW.candidate_ordinal::text
        WHEN 'logical_request' THEN request.request_id::text
        ELSE NULL
    END
    INTO expected_scope_id
    FROM router.logical_requests AS request
    JOIN router.provider_route_execution_snapshots AS snapshot
      ON snapshot.id = NEW.route_snapshot_id
    WHERE request.row_id = NEW.request_row_id;

    IF NEW.attempt_state <> 'succeeded'
       AND NEW.affected_scope_id IS DISTINCT FROM expected_scope_id THEN
        RAISE EXCEPTION 'routing decision affected identity is not canonical'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.affected_scope = 'logical_request'
       AND NEW.fallback_decision = 'next_candidate' THEN
        RAISE EXCEPTION 'request-wide failures cannot use candidate fallback'
            USING ERRCODE = '23514';
    END IF;
    IF (NEW.attempt_state = 'cancelled' OR NEW.normalized_error_class = 'cancelled')
       AND NEW.fallback_decision <> 'cancelled' THEN
        RAISE EXCEPTION 'cancelled work must stop as cancelled'
            USING ERRCODE = '23514';
    END IF;
    IF (NEW.attempt_state = 'uncertain' OR NEW.normalized_error_class = 'uncertain_effect')
       AND NEW.fallback_decision <> 'commit_boundary' THEN
        RAISE EXCEPTION 'uncertain work must stop at the commit boundary'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.attempt_state <> 'succeeded' AND EXISTS (
        SELECT 1 FROM router.logical_requests AS request
        WHERE request.row_id = NEW.request_row_id
          AND (request.partial_output OR request.committed_effect)
    ) AND (
        NEW.attempt_state NOT IN ('interrupted', 'cancelled', 'uncertain')
        OR NEW.fallback_decision NOT IN ('commit_boundary', 'cancelled')
    ) THEN
        RAISE EXCEPTION 'a committed request must stop at its commit boundary'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.provider_attempts AS attempt
        WHERE attempt.id = NEW.attempt_id
    ) THEN
        IF NOT EXISTS (
            SELECT 1 FROM router.provider_attempts AS attempt
            LEFT JOIN router.routing_attempt_starts AS attempt_start
              ON attempt_start.attempt_id = attempt.id
            LEFT JOIN router.routing_attempt_claims AS claim
              ON claim.attempt_id = attempt.id
             AND claim.claim_id = attempt_start.claim_id
            WHERE attempt.id = NEW.attempt_id
              AND attempt.request_row_id = NEW.request_row_id
              AND attempt.migration_0015_backfilled = NEW.migration_0015_backfilled
              AND attempt.attempt_number = NEW.attempt_number
              AND attempt.candidate_ordinal = NEW.candidate_ordinal
              AND attempt.route_snapshot_id = NEW.route_snapshot_id
              AND (
                  NEW.migration_0015_backfilled OR (
                      claim.claim_id = NEW.claim_id
                      AND claim.claim_generation = NEW.claim_generation
                      AND attempt_start.claim_generation <= NEW.claim_generation
                      AND attempt.connect_timeout_ms = NEW.connect_timeout_ms
                      AND attempt.first_byte_timeout_ms = NEW.first_byte_timeout_ms
                      AND attempt.idle_timeout_ms = NEW.idle_timeout_ms
                      AND attempt.execution_timeout_ms = NEW.execution_timeout_ms
                      AND attempt.logical_deadline = NEW.logical_deadline
                      AND attempt.attempt_deadline = NEW.attempt_deadline
                  )
              )
              AND attempt.state::text = NEW.attempt_state
              AND attempt.normalized_error_class IS NOT DISTINCT FROM NEW.normalized_error_class
              AND attempt.affected_scope IS NOT DISTINCT FROM NEW.affected_scope
              AND attempt.affected_scope_id IS NOT DISTINCT FROM NEW.affected_scope_id
              AND attempt.retry_decision IS NOT DISTINCT FROM NEW.fallback_decision
              AND attempt.safe_provider_code IS NOT DISTINCT FROM NEW.safe_provider_code
              AND attempt.redacted_evidence IS NOT DISTINCT FROM NEW.redacted_evidence
              AND attempt.finished_at = NEW.occurred_at
        ) THEN
            RAISE EXCEPTION 'routing decision does not match its provider attempt'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM router.routing_attempt_claims AS claim
        WHERE claim.request_row_id = NEW.request_row_id
          AND claim.attempt_id = NEW.attempt_id
          AND claim.claim_id = NEW.claim_id
          AND claim.claim_generation = NEW.claim_generation
          AND claim.attempt_number = NEW.attempt_number
          AND claim.candidate_ordinal = NEW.candidate_ordinal
          AND claim.route_snapshot_id = NEW.route_snapshot_id
          AND claim.connect_timeout_ms = NEW.connect_timeout_ms
          AND claim.first_byte_timeout_ms = NEW.first_byte_timeout_ms
          AND claim.idle_timeout_ms = NEW.idle_timeout_ms
          AND claim.execution_timeout_ms = NEW.execution_timeout_ms
          AND claim.logical_deadline = NEW.logical_deadline
          AND claim.attempt_deadline = NEW.attempt_deadline
          AND NEW.attempt_state <> 'succeeded'
    ) THEN
        RAISE EXCEPTION 'pre-attempt routing decision does not match its claim'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
