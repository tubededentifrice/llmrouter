DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.diagnostic_route_grants)
       OR EXISTS (SELECT 1 FROM router.diagnostic_route_authorizations)
       OR EXISTS (SELECT 1 FROM router.routing_attempt_claims)
       OR EXISTS (SELECT 1 FROM router.routing_attempt_starts)
       OR EXISTS (SELECT 1 FROM router.routing_attempt_dispatches)
       OR EXISTS (SELECT 1 FROM router.routing_attempt_usage_reports)
       OR EXISTS (
           SELECT 1 FROM router.routing_candidate_decisions
           WHERE NOT migration_0015_backfilled
       )
       OR EXISTS (
           SELECT 1 FROM router.provider_route_execution_snapshots
           WHERE NOT migration_0015_backfilled
       )
       OR EXISTS (
           SELECT 1 FROM router.provider_attempts
           WHERE NOT migration_0015_backfilled
       ) THEN
        RAISE EXCEPTION 'provider routing data exists; cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.provider_attempts AS attempt
        JOIN router.provider_model_routes AS route
          ON route.id = attempt.provider_model_route_id
        WHERE route.generation <> attempt.route_generation
    ) THEN
        RAISE EXCEPTION 'historical provider attempts prevent mutable route rollback'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP TRIGGER provider_attempts_terminal_decision ON router.provider_attempts;
DROP FUNCTION router.require_terminal_attempt_decision();
DROP TRIGGER routing_candidate_decisions_guard ON router.routing_candidate_decisions;
DROP FUNCTION router.validate_routing_candidate_decision();
DROP TRIGGER provider_attempts_routing_start_guard ON router.provider_attempts;
DROP FUNCTION router.validate_provider_attempt_start();
DROP TRIGGER provider_attempts_sequence ON router.provider_attempts;
DROP FUNCTION router.check_provider_attempt_sequence();

ALTER TABLE router.provider_attempts
DROP CONSTRAINT provider_attempts_safe_provider_code_bound,
DROP CONSTRAINT provider_attempts_failure_evidence,
DROP CONSTRAINT provider_attempts_timeout_bounds,
DROP CONSTRAINT provider_attempts_route_snapshot_fk,
DROP CONSTRAINT provider_attempts_budget_reservation_fk,
DROP CONSTRAINT provider_attempts_new_work_has_budget,
DROP COLUMN migration_0015_backfilled,
DROP COLUMN budget_reservation_id,
DROP COLUMN redacted_evidence,
DROP COLUMN safe_provider_code,
DROP COLUMN affected_scope_id,
DROP COLUMN attempt_deadline,
DROP COLUMN logical_deadline,
DROP COLUMN execution_timeout_ms,
DROP COLUMN idle_timeout_ms,
DROP COLUMN first_byte_timeout_ms,
DROP COLUMN connect_timeout_ms,
DROP COLUMN credential_generation,
DROP COLUMN credential_id,
DROP COLUMN provider_instance_generation,
DROP COLUMN provider_instance_id,
DROP COLUMN candidate_ordinal,
DROP COLUMN route_snapshot_id,
ADD CONSTRAINT provider_attempts_provider_model_route_id_route_generation_fkey
    FOREIGN KEY (provider_model_route_id, route_generation)
    REFERENCES router.provider_model_routes (id, generation) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION router.protect_provider_attempt()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.request_row_id <> OLD.request_row_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.attempt_number <> OLD.attempt_number
       OR NEW.provider_model_route_id <> OLD.provider_model_route_id
       OR NEW.route_generation <> OLD.route_generation
       OR NEW.assignment_revision_id <> OLD.assignment_revision_id
       OR NEW.price_version_id <> OLD.price_version_id
       OR NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION 'provider attempt identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state <> 'started' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal provider attempt is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'started' AND NEW.state = 'started'
       AND NEW.finished_at IS NOT NULL THEN
        RAISE EXCEPTION 'started provider attempt cannot have finished_at'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER diagnostic_route_authorizations_guard ON router.diagnostic_route_authorizations;
DROP FUNCTION router.check_diagnostic_route_authorization();
DROP TRIGGER diagnostic_route_authorizations_append_only ON router.diagnostic_route_authorizations;
DROP TABLE router.diagnostic_route_authorizations;
DROP TRIGGER diagnostic_route_grants_audit_guard ON router.diagnostic_route_grants;
DROP FUNCTION router.check_diagnostic_route_grant_audit();
ALTER TABLE router.diagnostic_route_grants
DROP CONSTRAINT diagnostic_route_grants_complete_identity;
DROP TRIGGER diagnostic_route_grants_append_only ON router.diagnostic_route_grants;
DROP TABLE router.diagnostic_route_grants;
DROP TRIGGER routing_attempt_usage_append_only ON router.routing_attempt_usage_reports;
DROP TRIGGER routing_attempt_usage_guard ON router.routing_attempt_usage_reports;
DROP FUNCTION router.validate_routing_attempt_usage();
DROP TABLE router.routing_attempt_usage_reports;
DROP TRIGGER routing_candidate_decisions_append_only ON router.routing_candidate_decisions;
DROP TABLE router.routing_candidate_decisions;
DROP TRIGGER routing_attempt_starts_guard ON router.routing_attempt_starts;
DROP FUNCTION router.validate_routing_attempt_start();
DROP TRIGGER routing_attempt_dispatches_guard ON router.routing_attempt_dispatches;
DROP FUNCTION router.validate_routing_attempt_dispatch();
DROP TRIGGER routing_attempt_dispatches_append_only ON router.routing_attempt_dispatches;
DROP TABLE router.routing_attempt_dispatches;
DROP TRIGGER routing_attempt_starts_append_only ON router.routing_attempt_starts;
DROP TABLE router.routing_attempt_starts;
DROP TRIGGER routing_attempt_claims_delete_guard ON router.routing_attempt_claims;
DROP FUNCTION router.guard_routing_claim_delete();
DROP TRIGGER routing_attempt_claims_change_guard ON router.routing_attempt_claims;
DROP FUNCTION router.protect_routing_claim();
DROP TABLE router.routing_attempt_claims;
DROP FUNCTION router.valid_redacted_routing_evidence(jsonb);
DROP TRIGGER provider_route_execution_snapshots_insert_guard
ON router.provider_route_execution_snapshots;
DROP FUNCTION router.validate_provider_route_execution_snapshot();
DROP TRIGGER provider_route_execution_snapshots_append_only
ON router.provider_route_execution_snapshots;
DROP TABLE router.provider_route_execution_snapshots;
DROP FUNCTION router.provider_resource_is_enabled(text, uuid, uuid, uuid);
DROP FUNCTION router.provider_route_is_eligible(uuid, uuid);
DROP FUNCTION router.active_request_scope(uuid, uuid);
