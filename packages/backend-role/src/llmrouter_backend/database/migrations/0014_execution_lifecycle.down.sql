DO $$
BEGIN
    IF EXISTS (
           SELECT 1 FROM router.execution_stream_events
           WHERE sequence <> 1 OR event_name <> 'request.admitted'
       )
       OR EXISTS (SELECT 1 FROM router.execution_cancellations)
       OR EXISTS (SELECT 1 FROM router.execution_cancellation_audit)
       OR EXISTS (
           SELECT 1 FROM router.agent_runs
           WHERE NOT execution_lifecycle_backfilled
       ) THEN
        RAISE EXCEPTION 'cannot roll back without data loss: execution lifecycle data exists';
    END IF;
END;
$$;

DROP INDEX router.agent_runs_expiry_idx;
DROP TRIGGER agent_runs_fill_locations ON router.agent_runs;
DROP FUNCTION router.fill_agent_run_locations();
DROP INDEX router.effect_intents_one_active_per_run;
ALTER TABLE router.effect_intents
DROP CONSTRAINT effect_intents_operation_identity_length_check;
DROP TRIGGER effect_intents_stop_new_work ON router.effect_intents;
DROP FUNCTION router.stop_new_run_effect();
DROP TRIGGER provider_attempts_stop_new_work ON router.provider_attempts;
DROP FUNCTION router.stop_new_execution_work();
DROP TRIGGER provider_attempts_running_journal ON router.provider_attempts;
DROP FUNCTION router.require_attempt_running_journal();
DROP TRIGGER logical_requests_cancellation_audit_complete
    ON router.logical_requests;
DROP TRIGGER agent_runs_cancellation_audit_complete ON router.agent_runs;
DROP FUNCTION router.require_execution_cancellation_audit();
DROP TRIGGER execution_cancellation_audit_scope
    ON router.execution_cancellation_audit;
DROP FUNCTION router.check_execution_cancellation_audit_scope();
DROP TABLE router.execution_cancellation_audit;
DROP TRIGGER execution_cancellations_guard ON router.execution_cancellations;
DROP FUNCTION router.protect_execution_cancellation();
CREATE OR REPLACE FUNCTION router.check_effect_owner_epoch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.run_leases
        WHERE run_row_id = NEW.run_row_id
          AND owner_epoch = NEW.owner_epoch
          AND expires_at > transaction_timestamp()
    ) THEN
        RAISE EXCEPTION 'effect intent does not match the current run owner'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER execution_cancellations_scope ON router.execution_cancellations;
DROP FUNCTION router.check_execution_cancellation_scope();
DROP TABLE router.execution_cancellations;
DROP FUNCTION router.valid_adapter_stop_evidence(jsonb);
DROP TRIGGER logical_requests_journal_complete ON router.logical_requests;
DROP TRIGGER agent_runs_journal_complete ON router.agent_runs;
DROP FUNCTION router.check_execution_journal_complete();
DROP TRIGGER execution_stream_event_applied ON router.execution_stream_events;
DROP FUNCTION router.check_stream_event_applied();
DROP TRIGGER execution_stream_events_append_only ON router.execution_stream_events;
DROP FUNCTION router.protect_execution_stream_event();
DROP TRIGGER execution_stream_events_guard ON router.execution_stream_events;
DROP FUNCTION router.check_execution_stream_event();
DROP TRIGGER logical_requests_admission_event ON router.logical_requests;
DROP TRIGGER agent_runs_admission_event ON router.agent_runs;
DROP FUNCTION router.create_execution_admission_event();
DROP TABLE router.execution_stream_events;
DROP FUNCTION router.valid_execution_stream_payload(text, jsonb);

DROP TRIGGER agent_runs_admission_state ON router.agent_runs;
DROP TRIGGER logical_requests_admission_state ON router.logical_requests;
DROP FUNCTION router.require_execution_admission_state();

DROP TRIGGER logical_requests_state_guard ON router.logical_requests;
DROP TRIGGER agent_runs_state_guard ON router.agent_runs;
ALTER TABLE router.logical_requests
DROP CONSTRAINT logical_requests_safe_error_check;

CREATE OR REPLACE FUNCTION router.protect_execution_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allowed boolean := false;
BEGIN
    IF NEW.state_revision <= OLD.state_revision THEN
        RAISE EXCEPTION 'state revision must increase'
            USING ERRCODE = '40001';
    END IF;
    IF OLD.state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain') THEN
        RAISE EXCEPTION 'terminal execution state is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_TABLE_NAME = 'logical_requests' AND NEW.state = 'waiting_for_tool' THEN
        RAISE EXCEPTION 'a logical request cannot wait for a business tool'
            USING ERRCODE = '23514';
    END IF;
    allowed := CASE OLD.state
        WHEN 'admitted' THEN NEW.state IN ('running', 'cancel_requested', 'failed')
        WHEN 'running' THEN NEW.state IN (
            'waiting_for_tool', 'succeeded', 'failed', 'interrupted',
            'cancel_requested', 'uncertain'
        )
        WHEN 'waiting_for_tool' THEN NEW.state IN (
            'running', 'failed', 'cancel_requested', 'uncertain'
        )
        WHEN 'cancel_requested' THEN NEW.state IN ('cancelled', 'uncertain')
        ELSE false
    END;
    IF NOT allowed THEN
        RAISE EXCEPTION 'invalid execution state transition from % to %',
            OLD.state, NEW.state USING ERRCODE = '23514';
    END IF;
    IF NEW.state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain')
       AND NEW.terminal_at IS NULL THEN
        RAISE EXCEPTION 'terminal execution state needs terminal_at'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER logical_requests_state_guard
BEFORE UPDATE OF state, state_revision, terminal_at ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_state();
CREATE TRIGGER agent_runs_state_guard
BEFORE UPDATE OF state, state_revision, terminal_at ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_state();

CREATE OR REPLACE FUNCTION router.protect_agent_run_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.row_id <> OLD.row_id OR NEW.run_id <> OLD.run_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.configuration_revision_id <> OLD.configuration_revision_id
       OR NEW.fingerprint_version <> OLD.fingerprint_version
       OR NEW.fingerprint_sha256 <> OLD.fingerprint_sha256
       OR NEW.admitted_at <> OLD.admitted_at THEN
        RAISE EXCEPTION 'agent run admission identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain')
       AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal agent run is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE router.agent_runs
DROP CONSTRAINT agent_runs_terminal_expiry_check,
DROP CONSTRAINT agent_runs_capture_reason_check,
DROP CONSTRAINT agent_runs_location_check,
DROP CONSTRAINT agent_runs_safe_error_check,
DROP COLUMN expires_at,
DROP COLUMN safe_error,
DROP COLUMN events_location,
DROP COLUMN cancel_location,
DROP COLUMN status_location,
DROP COLUMN capture_enabled,
DROP COLUMN capture_reason,
DROP COLUMN execution_lifecycle_backfilled;
