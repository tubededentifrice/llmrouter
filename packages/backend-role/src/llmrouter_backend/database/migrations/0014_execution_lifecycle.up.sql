DO $$
BEGIN
    IF EXISTS (
           SELECT 1 FROM router.logical_requests
           WHERE state <> 'admitted' OR state_revision <> 1
              OR partial_output OR committed_effect OR terminal_at IS NOT NULL
              OR safe_error IS NOT NULL OR expires_at IS NOT NULL
              OR last_transition_at <> admitted_at
       ) OR EXISTS (
           SELECT 1 FROM router.agent_runs
           WHERE state <> 'admitted' OR state_revision <> 1
              OR partial_output OR committed_effect OR terminal_at IS NOT NULL
              OR last_transition_at <> admitted_at
              OR durable_checkpoint <> '{}'::jsonb
       ) OR EXISTS (
           SELECT 1 FROM router.effect_intents AS effect
           JOIN router.agent_runs AS run ON run.row_id = effect.run_row_id
           WHERE run.state = 'admitted'
       ) OR EXISTS (
           SELECT 1 FROM router.provider_attempts
       ) OR EXISTS (
           SELECT 1 FROM router.run_leases
       ) THEN
        RAISE EXCEPTION 'cannot migrate execution journal with non-admitted execution data';
    END IF;
END;
$$;

ALTER TABLE router.agent_runs
ADD COLUMN status_location text,
ADD COLUMN cancel_location text,
ADD COLUMN events_location text,
ADD COLUMN safe_error jsonb,
ADD COLUMN expires_at timestamptz,
ADD COLUMN capture_enabled boolean,
ADD COLUMN capture_reason text,
ADD COLUMN execution_lifecycle_backfilled boolean NOT NULL DEFAULT false;

ALTER TABLE router.agent_runs DISABLE TRIGGER agent_runs_stable_identity;

UPDATE router.agent_runs
SET status_location = '/v1/agent-runs/' || run_id::text,
    cancel_location = '/v1/agent-runs/' || run_id::text || '/cancel',
    events_location = '/v1/agent-runs/' || run_id::text || '/events',
    capture_enabled = false,
    capture_reason = 'configured',
    execution_lifecycle_backfilled = true,
    expires_at = CASE WHEN terminal_at IS NULL THEN NULL
        ELSE terminal_at + interval '24 hours' END;

ALTER TABLE router.agent_runs ENABLE TRIGGER agent_runs_stable_identity;

ALTER TABLE router.agent_runs
ALTER COLUMN status_location SET NOT NULL,
ALTER COLUMN cancel_location SET NOT NULL,
ALTER COLUMN events_location SET NOT NULL,
ALTER COLUMN capture_enabled SET NOT NULL,
ALTER COLUMN capture_reason SET NOT NULL,
ADD CONSTRAINT agent_runs_location_check CHECK (
    status_location = '/v1/agent-runs/' || run_id::text
    AND cancel_location = '/v1/agent-runs/' || run_id::text || '/cancel'
    AND events_location = '/v1/agent-runs/' || run_id::text || '/events'
),
ADD CONSTRAINT agent_runs_capture_reason_check CHECK (
    capture_reason IN ('configured', 'spool_pressure')
),
ADD CONSTRAINT agent_runs_safe_error_check CHECK (
    safe_error IS NULL OR (state IN (
        'failed', 'interrupted', 'cancelled', 'uncertain'
    ) AND (
        jsonb_typeof(safe_error) = 'object'
        AND safe_error ?& ARRAY['class','affected_scope','message']
        AND safe_error - ARRAY['class','affected_scope','message','safe_provider_code'] = '{}'::jsonb
        AND jsonb_typeof(safe_error->'class') = 'string'
        AND jsonb_typeof(safe_error->'affected_scope') = 'string'
        AND jsonb_typeof(safe_error->'message') = 'string'
        AND (NOT safe_error ? 'safe_provider_code'
             OR jsonb_typeof(safe_error->'safe_provider_code') = 'string')
        AND safe_error->>'class' IN (
            'authentication','policy','budget','rate_limit','timeout','transport',
            'provider_unavailable','invalid_provider_response','incompatible_request',
            'cancelled','uncertain_effect','router_internal'
        )
        AND safe_error->>'affected_scope' IN (
            'attempt','provider_model_route','provider_instance','credential',
            'assignment_candidate','logical_request'
        )
        AND length(safe_error->>'message') <= 1000
        AND (NOT safe_error ? 'safe_provider_code'
             OR length(safe_error->>'safe_provider_code') <= 200)
    ))
),
ADD CONSTRAINT agent_runs_terminal_expiry_check CHECK (
    terminal_at IS NULL
    OR (expires_at IS NOT NULL AND expires_at >= terminal_at + interval '24 hours')
);

CREATE FUNCTION router.fill_agent_run_locations()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.status_location := '/v1/agent-runs/' || NEW.run_id::text;
    NEW.cancel_location := '/v1/agent-runs/' || NEW.run_id::text || '/cancel';
    NEW.events_location := '/v1/agent-runs/' || NEW.run_id::text || '/events';
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_runs_fill_locations
BEFORE INSERT ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.fill_agent_run_locations();

ALTER TABLE router.logical_requests
ADD CONSTRAINT logical_requests_safe_error_check CHECK (
    safe_error IS NULL OR (state IN (
        'failed', 'interrupted', 'cancelled', 'uncertain'
    ) AND (
        jsonb_typeof(safe_error) = 'object'
        AND safe_error ?& ARRAY['class','affected_scope','message']
        AND safe_error - ARRAY['class','affected_scope','message','safe_provider_code'] = '{}'::jsonb
        AND jsonb_typeof(safe_error->'class') = 'string'
        AND jsonb_typeof(safe_error->'affected_scope') = 'string'
        AND jsonb_typeof(safe_error->'message') = 'string'
        AND (NOT safe_error ? 'safe_provider_code'
             OR jsonb_typeof(safe_error->'safe_provider_code') = 'string')
        AND safe_error->>'class' IN (
            'authentication','policy','budget','rate_limit','timeout','transport',
            'provider_unavailable','invalid_provider_response','incompatible_request',
            'cancelled','uncertain_effect','router_internal'
        )
        AND safe_error->>'affected_scope' IN (
            'attempt','provider_model_route','provider_instance','credential',
            'assignment_candidate','logical_request'
        )
        AND length(safe_error->>'message') <= 1000
        AND (NOT safe_error ? 'safe_provider_code'
             OR length(safe_error->>'safe_provider_code') <= 200)
    ))
);

CREATE OR REPLACE FUNCTION router.protect_execution_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allowed boolean := false;
    terminal_states constant router.execution_state[] := ARRAY[
        'succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain'
    ]::router.execution_state[];
BEGIN
    IF OLD.state = ANY(terminal_states) AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal execution state is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF (OLD.partial_output AND NOT NEW.partial_output)
       OR (OLD.committed_effect AND NOT NEW.committed_effect) THEN
        RAISE EXCEPTION 'execution commit indicators cannot clear'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.safe_error IS NOT NULL
       AND NEW.state NOT IN ('failed', 'interrupted', 'cancelled', 'uncertain') THEN
        RAISE EXCEPTION 'safe error is valid only for an error terminal state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state = OLD.state THEN
        IF NEW.state_revision <> OLD.state_revision
           OR NEW.last_transition_at <> OLD.last_transition_at
           OR NEW.terminal_at IS DISTINCT FROM OLD.terminal_at THEN
            RAISE EXCEPTION 'unchanged state cannot change transition metadata'
                USING ERRCODE = '40001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state_revision <> OLD.state_revision + 1 THEN
        RAISE EXCEPTION 'state revision must increase by one'
            USING ERRCODE = '40001';
    END IF;
    IF NEW.last_transition_at < OLD.last_transition_at THEN
        RAISE EXCEPTION 'state transition time cannot decrease'
            USING ERRCODE = '40001';
    END IF;
    IF TG_TABLE_NAME = 'logical_requests' THEN
        allowed := CASE OLD.state
            WHEN 'admitted' THEN NEW.state IN ('running', 'cancel_requested', 'failed')
            WHEN 'running' THEN NEW.state IN (
                'succeeded', 'failed', 'interrupted', 'cancel_requested'
            )
            WHEN 'cancel_requested' THEN NEW.state IN ('cancelled', 'uncertain')
            ELSE false
        END;
    ELSE
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
    END IF;
    IF NOT allowed THEN
        RAISE EXCEPTION 'invalid execution state transition from % to %',
            OLD.state, NEW.state USING ERRCODE = '23514';
    END IF;
    IF (NEW.state = ANY(terminal_states)) <> (NEW.terminal_at IS NOT NULL) THEN
        RAISE EXCEPTION 'terminal state and terminal time must match'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state = ANY(terminal_states)
       AND NEW.terminal_at IS DISTINCT FROM NEW.last_transition_at THEN
        RAISE EXCEPTION 'terminal time must equal the last transition time'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state = ANY(terminal_states)
       AND (NEW.expires_at IS NULL
            OR NEW.expires_at < NEW.terminal_at + interval '24 hours') THEN
        RAISE EXCEPTION 'terminal status needs 24-hour recovery'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER logical_requests_state_guard ON router.logical_requests;
CREATE TRIGGER logical_requests_state_guard
BEFORE UPDATE OF state, state_revision, partial_output, committed_effect,
    last_transition_at, terminal_at, expires_at, safe_error
ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_state();

DROP TRIGGER agent_runs_state_guard ON router.agent_runs;
CREATE TRIGGER agent_runs_state_guard
BEFORE UPDATE OF state, state_revision, partial_output, committed_effect,
    last_transition_at, terminal_at, expires_at, safe_error
ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_state();

CREATE FUNCTION router.require_execution_admission_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state <> 'admitted'
       OR NEW.state_revision <> 1
       OR NEW.partial_output
       OR NEW.committed_effect
       OR NEW.terminal_at IS NOT NULL
       OR NEW.expires_at IS NOT NULL
       OR NEW.last_transition_at <> NEW.admitted_at
       OR (TG_TABLE_NAME = 'agent_runs'
           AND to_jsonb(NEW)->'durable_checkpoint' <> '{}'::jsonb)
       OR (TG_TABLE_NAME = 'agent_runs'
           AND (to_jsonb(NEW)->>'execution_lifecycle_backfilled')::boolean)
       OR NEW.safe_error IS NOT NULL THEN
        RAISE EXCEPTION 'execution must start in its clean admitted state'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER logical_requests_admission_state
BEFORE INSERT ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.require_execution_admission_state();

CREATE TRIGGER agent_runs_admission_state
BEFORE INSERT ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.require_execution_admission_state();

CREATE OR REPLACE FUNCTION router.protect_agent_run_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.row_id <> OLD.row_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.configuration_revision_id <> OLD.configuration_revision_id
       OR NEW.fingerprint_version <> OLD.fingerprint_version
       OR NEW.fingerprint_sha256 <> OLD.fingerprint_sha256
       OR NEW.admitted_at <> OLD.admitted_at
       OR NEW.status_location <> OLD.status_location
       OR NEW.cancel_location <> OLD.cancel_location
       OR NEW.events_location <> OLD.events_location
       OR NEW.capture_enabled <> OLD.capture_enabled
       OR NEW.capture_reason <> OLD.capture_reason
       OR NEW.execution_lifecycle_backfilled <> OLD.execution_lifecycle_backfilled THEN
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

CREATE FUNCTION router.valid_execution_stream_payload(event_type text, payload jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    RETURN COALESCE((CASE event_type
        WHEN 'request.admitted' THEN
            payload->>'state' = 'admitted'
            AND jsonb_typeof(payload->'state_revision') = 'number'
            AND payload->>'state_revision' ~ '^[1-9][0-9]*$'
            AND jsonb_typeof(payload->'admission') = 'object'
        WHEN 'request.running' THEN
            jsonb_typeof(payload->'state_revision') = 'number'
            AND payload->>'state_revision' ~ '^[1-9][0-9]*$'
        WHEN 'request.waiting_for_tool' THEN
            jsonb_typeof(payload->'state_revision') = 'number'
            AND payload->>'state_revision' ~ '^[1-9][0-9]*$'
            AND jsonb_typeof(payload->'tool_call_id') = 'string'
            AND payload->>'tool_call_id' <> ''
            AND jsonb_typeof(payload->'expires_at') = 'string'
        WHEN 'output.delta' THEN
            jsonb_typeof(payload->'output_index') = 'number'
            AND payload->>'output_index' ~ '^(0|[1-9][0-9]*)$'
            AND jsonb_typeof(payload->'content_type') = 'string'
            AND payload->>'content_type' <> ''
            AND jsonb_typeof(payload->'delta') = 'string'
            AND octet_length(payload->>'delta') <= 262144
        WHEN 'output.completed' THEN
            jsonb_typeof(payload->'output_index') = 'number'
            AND payload->>'output_index' ~ '^(0|[1-9][0-9]*)$'
            AND jsonb_typeof(payload->'content_type') = 'string'
            AND payload->>'content_type' <> ''
        WHEN 'tool.call' THEN
            jsonb_typeof(payload->'tool_call_id') = 'string'
            AND payload->>'tool_call_id' <> ''
            AND jsonb_typeof(payload->'tool_name') = 'string'
            AND payload->>'tool_name' <> ''
            AND jsonb_typeof(payload->'arguments_delta') = 'string'
            AND jsonb_typeof(payload->'complete') = 'boolean'
        WHEN 'tool.started' THEN
            jsonb_typeof(payload->'tool_call_id') = 'string'
            AND payload->>'tool_call_id' <> ''
            AND jsonb_typeof(payload->'tool_kind') = 'string'
            AND payload->>'tool_kind' IN ('shared','business')
        WHEN 'tool.completed' THEN
            jsonb_typeof(payload->'tool_call_id') = 'string'
            AND payload->>'tool_call_id' <> ''
            AND payload ? 'result_summary'
        WHEN 'tool.failed' THEN
            jsonb_typeof(payload->'tool_call_id') = 'string'
            AND payload->>'tool_call_id' <> ''
            AND jsonb_typeof(payload->'error') = 'object'
            AND jsonb_typeof(payload->'uncertain_effect') = 'boolean'
        WHEN 'usage.updated' THEN
            jsonb_typeof(payload->'usage') = 'object'
            AND jsonb_typeof(payload->'estimated') = 'boolean'
        WHEN 'request.cancel_requested' THEN
            jsonb_typeof(payload->'state_revision') = 'number'
            AND payload->>'state_revision' ~ '^[1-9][0-9]*$'
        WHEN 'request.terminal' THEN
            jsonb_typeof(payload->'state') = 'string'
            AND payload->>'state' IN (
                'succeeded','failed','interrupted','cancelled','uncertain'
            )
            AND jsonb_typeof(payload->'state_revision') = 'number'
            AND payload->>'state_revision' ~ '^[1-9][0-9]*$'
            AND jsonb_typeof(payload->'partial_output') = 'boolean'
            AND jsonb_typeof(payload->'committed_effects') = 'boolean'
            AND (NOT payload ? 'error' OR jsonb_typeof(payload->'error') = 'object')
        ELSE event_type ~ '^extension\.[a-z0-9][a-z0-9._-]{0,99}$'
    END), false);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN false;
END;
$$;

CREATE TABLE router.execution_stream_events (
    request_row_id uuid REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    run_row_id uuid REFERENCES router.agent_runs (row_id) ON DELETE RESTRICT,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_name text NOT NULL CHECK (
        event_name IN (
            'request.admitted', 'request.running', 'request.waiting_for_tool',
            'output.delta', 'output.completed', 'tool.call', 'tool.started',
            'tool.completed', 'tool.failed', 'usage.updated',
            'request.cancel_requested', 'request.terminal'
        ) OR event_name ~ '^extension\.[a-z0-9][a-z0-9._-]{0,99}$'
    ),
    occurred_at timestamptz NOT NULL,
    wire_data text NOT NULL CHECK (
        octet_length(wire_data) <= 1048576
        AND position(E'\\n' in wire_data) = 0
        AND jsonb_typeof(wire_data::jsonb) = 'object'
    ),
    wire_sha256 bytea NOT NULL CHECK (octet_length(wire_sha256) = 32),
    owner_epoch bigint CHECK (owner_epoch IS NULL OR owner_epoch > 0),
    expires_at timestamptz,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK ((request_row_id IS NULL) <> (run_row_id IS NULL)),
    CHECK (router.valid_execution_stream_payload(
        event_name, wire_data::jsonb->'payload'
    )),
    UNIQUE NULLS NOT DISTINCT (request_row_id, run_row_id, sequence)
);

WITH request_event AS (
    SELECT request.row_id, request.service_id, request.workspace_id,
           request.admitted_at,
           jsonb_build_object(
               'stream_version', '1',
               'request_id', request.request_id::text,
               'sequence', 1,
               'occurred_at', to_char(
                   request.admitted_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
               ),
               'payload', jsonb_build_object(
                   'state', 'admitted', 'state_revision', 1,
                   'admission', jsonb_strip_nulls(jsonb_build_object(
                       'request_id', request.request_id::text,
                       'admitted_at', request.admitted_at,
                       'status_url', request.status_location,
                       'cancel_url', request.cancel_location,
                       'events_url', request.events_location,
                       'fingerprint_version', 'rfc8785-sha256-v1',
                       'capture_enabled', request.capture_enabled,
                       'capture_reason', request.capture_reason
                   ))
               )
           )::text AS wire_data
    FROM router.logical_requests AS request
)
INSERT INTO router.execution_stream_events (
    request_row_id, service_id, workspace_id, sequence, event_name,
    occurred_at, wire_data, wire_sha256
)
SELECT row_id, service_id, workspace_id, 1, 'request.admitted',
       date_trunc('milliseconds', admitted_at),
       wire_data, pg_catalog.sha256(convert_to(wire_data, 'UTF8'))
FROM request_event;

WITH run_event AS (
    SELECT run.row_id, run.service_id, run.workspace_id, run.admitted_at,
           jsonb_build_object(
               'stream_version', '1',
               'request_id', run.run_id::text,
               'run_id', run.run_id::text,
               'sequence', 1,
               'occurred_at', to_char(
                   run.admitted_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
               ),
               'payload', jsonb_build_object(
                   'state', 'admitted', 'state_revision', 1,
                   'admission', jsonb_build_object(
                       'request_id', run.run_id::text,
                       'run_id', run.run_id::text,
                       'admitted_at', run.admitted_at,
                       'status_url', run.status_location,
                       'cancel_url', run.cancel_location,
                       'events_url', run.events_location,
                       'fingerprint_version', 'rfc8785-sha256-v1',
                       'capture_enabled', run.capture_enabled,
                       'capture_reason', run.capture_reason
                   )
               )
           )::text AS wire_data
    FROM router.agent_runs AS run
)
INSERT INTO router.execution_stream_events (
    run_row_id, service_id, workspace_id, sequence, event_name,
    occurred_at, wire_data, wire_sha256
)
SELECT row_id, service_id, workspace_id, 1, 'request.admitted',
       date_trunc('milliseconds', admitted_at),
       wire_data, pg_catalog.sha256(convert_to(wire_data, 'UTF8'))
FROM run_event;

CREATE FUNCTION router.create_execution_admission_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    event_time timestamptz := date_trunc('milliseconds', NEW.admitted_at);
    wire jsonb;
BEGIN
    IF TG_TABLE_NAME = 'logical_requests' THEN
        wire := jsonb_build_object(
            'stream_version', '1', 'request_id', NEW.request_id::text,
            'sequence', 1,
            'occurred_at', to_char(event_time AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
            'payload', jsonb_build_object(
                'state', 'admitted', 'state_revision', 1,
                'admission', jsonb_strip_nulls(jsonb_build_object(
                    'request_id', NEW.request_id::text,
                    'admitted_at', NEW.admitted_at,
                    'status_url', NEW.status_location,
                    'cancel_url', NEW.cancel_location,
                    'events_url', NEW.events_location,
                    'fingerprint_version', 'rfc8785-sha256-v1',
                    'capture_enabled', NEW.capture_enabled,
                    'capture_reason', NEW.capture_reason
                ))
            )
        );
        INSERT INTO router.execution_stream_events (
            request_row_id, service_id, workspace_id, sequence, event_name,
            occurred_at, wire_data, wire_sha256
        ) VALUES (
            NEW.row_id, NEW.service_id, NEW.workspace_id, 1,
            'request.admitted', event_time, wire::text,
            pg_catalog.sha256(convert_to(wire::text, 'UTF8'))
        );
    ELSE
        wire := jsonb_build_object(
            'stream_version', '1', 'request_id', NEW.run_id::text,
            'run_id', NEW.run_id::text, 'sequence', 1,
            'occurred_at', to_char(event_time AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
            'payload', jsonb_build_object(
                'state', 'admitted', 'state_revision', 1,
                'admission', jsonb_build_object(
                    'request_id', NEW.run_id::text, 'run_id', NEW.run_id::text,
                    'admitted_at', NEW.admitted_at,
                    'status_url', NEW.status_location,
                    'cancel_url', NEW.cancel_location,
                    'events_url', NEW.events_location,
                    'fingerprint_version', 'rfc8785-sha256-v1',
                    'capture_enabled', NEW.capture_enabled,
                    'capture_reason', NEW.capture_reason
                )
            )
        );
        INSERT INTO router.execution_stream_events (
            run_row_id, service_id, workspace_id, sequence, event_name,
            occurred_at, wire_data, wire_sha256
        ) VALUES (
            NEW.row_id, NEW.service_id, NEW.workspace_id, 1,
            'request.admitted', event_time, wire::text,
            pg_catalog.sha256(convert_to(wire::text, 'UTF8'))
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER logical_requests_admission_event
AFTER INSERT ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.create_execution_admission_event();

CREATE TRIGGER agent_runs_admission_event
AFTER INSERT ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.create_execution_admission_event();

CREATE INDEX execution_stream_events_scope_replay_idx
ON router.execution_stream_events (
    service_id, workspace_id, request_row_id, run_row_id, sequence
);

CREATE FUNCTION router.check_execution_stream_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    execution record;
    prior_sequence bigint;
    prior_event text;
    prior_state_revision bigint;
    envelope jsonb;
    envelope_keys text[];
    expected_admission jsonb;
BEGIN
    IF NEW.request_row_id IS NOT NULL THEN
        SELECT request_id AS public_id, service_id, workspace_id, state,
               state_revision, partial_output, committed_effect, terminal_at,
               admitted_at, status_location, cancel_location, events_location,
               capture_enabled, capture_reason
        INTO execution FROM router.logical_requests
        WHERE row_id = NEW.request_row_id FOR UPDATE;
    ELSE
        SELECT run_id AS public_id, service_id, workspace_id, state,
               state_revision, partial_output, committed_effect, terminal_at,
               admitted_at, status_location, cancel_location, events_location,
               capture_enabled, capture_reason
        INTO execution FROM router.agent_runs
        WHERE row_id = NEW.run_row_id FOR UPDATE;
        IF NEW.owner_epoch IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM router.run_leases
            WHERE run_row_id = NEW.run_row_id
              AND owner_epoch = NEW.owner_epoch
              AND expires_at > transaction_timestamp()
        ) THEN
            RAISE EXCEPTION 'stream event owner is fenced'
                USING ERRCODE = '40001';
        END IF;
    END IF;
    IF execution.public_id IS NULL
       OR execution.service_id <> NEW.service_id
       OR execution.workspace_id IS DISTINCT FROM NEW.workspace_id THEN
        RAISE EXCEPTION 'stream event scope does not match execution'
            USING ERRCODE = '23514';
    END IF;
    SELECT sequence, event_name INTO prior_sequence, prior_event
    FROM router.execution_stream_events
    WHERE request_row_id IS NOT DISTINCT FROM NEW.request_row_id
      AND run_row_id IS NOT DISTINCT FROM NEW.run_row_id
    ORDER BY sequence DESC LIMIT 1;
    SELECT max((wire_data::jsonb #>> '{payload,state_revision}')::bigint)
    INTO prior_state_revision
    FROM router.execution_stream_events
    WHERE request_row_id IS NOT DISTINCT FROM NEW.request_row_id
      AND run_row_id IS NOT DISTINCT FROM NEW.run_row_id
      AND event_name IN (
          'request.admitted','request.running','request.waiting_for_tool',
          'request.cancel_requested','request.terminal'
      );
    IF NEW.sequence <> COALESCE(prior_sequence, 0) + 1 THEN
        RAISE EXCEPTION 'stream event sequence must increase by one'
            USING ERRCODE = '40001';
    END IF;
    IF NEW.sequence = 1 AND NEW.event_name <> 'request.admitted' THEN
        RAISE EXCEPTION 'request.admitted must be the first stream event'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.sequence > 1 AND NEW.event_name = 'request.admitted' THEN
        RAISE EXCEPTION 'request.admitted can only be the first stream event'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name IN (
           'request.running','request.waiting_for_tool',
           'request.cancel_requested','request.terminal'
       ) AND (NEW.wire_data::jsonb #>> '{payload,state_revision}')::bigint
           IS DISTINCT FROM prior_state_revision + 1 THEN
        RAISE EXCEPTION 'lifecycle event revision must increase by one'
            USING ERRCODE = '40001';
    END IF;
    IF NEW.event_name IN (
           'request.running','request.waiting_for_tool',
           'request.cancel_requested','request.terminal'
       ) AND NEW.occurred_at IS DISTINCT FROM
           date_trunc('milliseconds', transaction_timestamp()) THEN
        RAISE EXCEPTION 'runtime lifecycle event time must match database time'
            USING ERRCODE = '23514';
    END IF;
    IF prior_event = 'request.terminal' THEN
        RAISE EXCEPTION 'request.terminal is the final stream event'
            USING ERRCODE = '55000';
    END IF;
    envelope := NEW.wire_data::jsonb;
    SELECT array_agg(key ORDER BY key) INTO envelope_keys
    FROM jsonb_object_keys(envelope) AS key;
    IF jsonb_typeof(envelope->'stream_version') IS DISTINCT FROM 'string'
       OR envelope->>'stream_version' IS DISTINCT FROM '1'
       OR jsonb_typeof(envelope->'request_id') IS DISTINCT FROM 'string'
       OR (envelope->>'request_id')::uuid IS DISTINCT FROM execution.public_id
       OR jsonb_typeof(envelope->'sequence') IS DISTINCT FROM 'number'
       OR envelope->>'sequence' !~ '^[1-9][0-9]*$'
       OR (envelope->>'sequence')::bigint IS DISTINCT FROM NEW.sequence
       OR jsonb_typeof(envelope->'occurred_at') IS DISTINCT FROM 'string'
       OR (envelope->>'occurred_at')::timestamptz IS DISTINCT FROM NEW.occurred_at
       OR jsonb_typeof(envelope->'payload') IS DISTINCT FROM 'object'
       OR pg_catalog.sha256(convert_to(NEW.wire_data, 'UTF8')) <> NEW.wire_sha256
       OR (NEW.request_row_id IS NOT NULL
           AND envelope_keys <> ARRAY['occurred_at','payload','request_id','sequence','stream_version'])
       OR (NEW.run_row_id IS NOT NULL
           AND (envelope_keys <> ARRAY['occurred_at','payload','request_id','run_id','sequence','stream_version']
                OR jsonb_typeof(envelope->'run_id') IS DISTINCT FROM 'string'
                OR (envelope->>'run_id')::uuid IS DISTINCT FROM execution.public_id)) THEN
        RAISE EXCEPTION 'stream event wire envelope does not match its durable row'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.sequence = 1 AND (
        execution.state <> 'admitted'
        OR execution.state_revision <> 1
        OR execution.partial_output
        OR execution.committed_effect
        OR execution.terminal_at IS NOT NULL
        OR envelope #>> '{payload,state}' IS DISTINCT FROM 'admitted'
        OR (envelope #>> '{payload,state_revision}')::bigint IS DISTINCT FROM 1
    ) THEN
        RAISE EXCEPTION 'request.admitted does not match clean admission state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.sequence = 1 THEN
        expected_admission := jsonb_strip_nulls(jsonb_build_object(
            'request_id', execution.public_id::text,
            'admitted_at', execution.admitted_at,
            'status_url', execution.status_location,
            'cancel_url', execution.cancel_location,
            'events_url', execution.events_location,
            'fingerprint_version', 'rfc8785-sha256-v1',
            'capture_enabled', execution.capture_enabled,
            'capture_reason', execution.capture_reason
        ));
        IF NEW.run_row_id IS NOT NULL THEN
            expected_admission := expected_admission
                || jsonb_build_object('run_id', execution.public_id::text);
        END IF;
        IF envelope #> '{payload,admission}' IS DISTINCT FROM expected_admission THEN
            RAISE EXCEPTION 'request.admitted receipt does not match execution identity'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.request_row_id IS NOT NULL AND NEW.owner_epoch IS NOT NULL THEN
        RAISE EXCEPTION 'request stream event cannot have a run owner epoch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.run_row_id IS NOT NULL AND NEW.sequence > 1
       AND NEW.owner_epoch IS NULL
       AND NOT (
           NEW.event_name IN ('request.cancel_requested','request.terminal')
           AND EXISTS (
               SELECT 1 FROM router.execution_cancellations
               WHERE run_row_id = NEW.run_row_id
           )
       ) THEN
        RAISE EXCEPTION 'run stream event needs its live owner epoch'
            USING ERRCODE = '40001';
    END IF;
    IF execution.terminal_at IS NOT NULL THEN
        RAISE EXCEPTION 'request.terminal must be the final stream event'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.event_name = 'request.terminal'
       AND ((envelope #>> '{payload,state}') IS NULL
            OR (envelope #>> '{payload,state}') NOT IN (
                'succeeded','failed','interrupted','cancelled','uncertain'
            )) THEN
        RAISE EXCEPTION 'request.terminal needs a valid next terminal state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.running'
       AND (NOT (execution.state = 'admitted'
                 OR (NEW.run_row_id IS NOT NULL
                     AND execution.state = 'waiting_for_tool'))
            OR (envelope #>> '{payload,state_revision}')::bigint
               IS DISTINCT FROM execution.state_revision + 1) THEN
        RAISE EXCEPTION 'request.running does not match execution state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.waiting_for_tool'
       AND (execution.state <> 'running'
            OR (envelope #>> '{payload,state_revision}')::bigint
               IS DISTINCT FROM execution.state_revision + 1
            OR jsonb_typeof(envelope #> '{payload,tool_call_id}')
               IS DISTINCT FROM 'string'
            OR envelope #>> '{payload,tool_call_id}' = ''
            OR jsonb_typeof(envelope #> '{payload,expires_at}')
               IS DISTINCT FROM 'string'
            OR (envelope #>> '{payload,expires_at}')::timestamptz <= NEW.occurred_at
            OR (envelope #>> '{payload,expires_at}')::timestamptz
               > NEW.occurred_at + interval '15 minutes') THEN
        RAISE EXCEPTION 'request.waiting_for_tool does not match run state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.cancel_requested'
       AND (execution.state NOT IN ('admitted','running','waiting_for_tool')
            OR (envelope #>> '{payload,state_revision}')::bigint
               IS DISTINCT FROM execution.state_revision + 1) THEN
        RAISE EXCEPTION 'request.cancel_requested does not match execution state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.terminal'
       AND ((envelope #>> '{payload,state_revision}')::bigint
               IS DISTINCT FROM execution.state_revision + 1
            OR (envelope #>> '{payload,partial_output}')::boolean
               IS DISTINCT FROM execution.partial_output
            OR (envelope #>> '{payload,committed_effects}')::boolean
               IS DISTINCT FROM execution.committed_effect
            OR CASE execution.state
                WHEN 'admitted' THEN (envelope #>> '{payload,state}') = 'failed'
                WHEN 'running' THEN (envelope #>> '{payload,state}') IN (
                    'succeeded','failed','interrupted','uncertain'
                )
                WHEN 'waiting_for_tool' THEN (envelope #>> '{payload,state}') IN (
                    'failed','uncertain'
                )
                WHEN 'cancel_requested' THEN (envelope #>> '{payload,state}') IN (
                    'cancelled','uncertain'
                )
                ELSE false
            END IS DISTINCT FROM true) THEN
        RAISE EXCEPTION 'request.terminal does not match terminal state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name IN ('output.delta','output.completed','tool.call','tool.started')
       AND execution.state NOT IN ('running','waiting_for_tool') THEN
        RAISE EXCEPTION 'execution state stops new stream work'
            USING ERRCODE = '55000';
    END IF;
    IF (NEW.event_name <> 'request.terminal' AND NEW.expires_at IS NOT NULL)
       OR (NEW.event_name = 'request.terminal' AND NEW.expires_at IS NULL) THEN
        RAISE EXCEPTION 'stream replay retention does not match execution state'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_stream_events_guard
BEFORE INSERT ON router.execution_stream_events
FOR EACH ROW EXECUTE FUNCTION router.check_execution_stream_event();

CREATE FUNCTION router.protect_execution_stream_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'stream events are append-only' USING ERRCODE = '55000';
    END IF;
    IF NEW.request_row_id IS DISTINCT FROM OLD.request_row_id
       OR NEW.run_row_id IS DISTINCT FROM OLD.run_row_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.sequence <> OLD.sequence
       OR NEW.event_name <> OLD.event_name
       OR NEW.occurred_at <> OLD.occurred_at
       OR NEW.wire_data <> OLD.wire_data
       OR NEW.wire_sha256 <> OLD.wire_sha256
       OR NEW.owner_epoch IS DISTINCT FROM OLD.owner_epoch
       OR OLD.expires_at IS NOT NULL
       OR NEW.expires_at IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM router.execution_stream_events AS terminal
           WHERE terminal.request_row_id IS NOT DISTINCT FROM NEW.request_row_id
             AND terminal.run_row_id IS NOT DISTINCT FROM NEW.run_row_id
             AND terminal.event_name = 'request.terminal'
             AND terminal.expires_at = NEW.expires_at
       ) THEN
        RAISE EXCEPTION 'stream event content is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_stream_events_append_only
BEFORE UPDATE OR DELETE ON router.execution_stream_events
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_stream_event();

CREATE FUNCTION router.check_execution_journal_complete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    execution record;
    latest record;
BEGIN
    IF TG_TABLE_NAME = 'logical_requests' THEN
        SELECT * INTO execution FROM router.logical_requests WHERE row_id = NEW.row_id;
        SELECT * INTO latest FROM router.execution_stream_events
        WHERE request_row_id = NEW.row_id ORDER BY sequence DESC LIMIT 1;
    ELSE
        SELECT * INTO execution FROM router.agent_runs WHERE row_id = NEW.row_id;
        SELECT * INTO latest FROM router.execution_stream_events
        WHERE run_row_id = NEW.row_id ORDER BY sequence DESC LIMIT 1;
    END IF;
    IF latest.sequence IS NULL OR latest.sequence < 1 THEN
        RAISE EXCEPTION 'execution needs its durable stream journal'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state = 'running'
       AND NOT EXISTS (
           SELECT 1 FROM router.execution_stream_events
           WHERE request_row_id IS NOT DISTINCT FROM
                 CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
             AND run_row_id IS NOT DISTINCT FROM
                 CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
             AND event_name = 'request.running'
             AND (wire_data::jsonb #>> '{payload,state_revision}')::bigint = execution.state_revision
       ) THEN
        RAISE EXCEPTION 'running state needs its matching stream event'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state = 'waiting_for_tool'
       AND (latest.event_name <> 'request.waiting_for_tool'
            OR (latest.wire_data::jsonb #>> '{payload,state_revision}')::bigint
               <> execution.state_revision) THEN
        RAISE EXCEPTION 'waiting state needs its matching stream event'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state = 'cancel_requested'
       AND (latest.event_name <> 'request.cancel_requested'
            OR (latest.wire_data::jsonb #>> '{payload,state_revision}')::bigint
               <> execution.state_revision) THEN
        RAISE EXCEPTION 'cancel request needs its matching stream event'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state = 'cancel_requested' AND NOT EXISTS (
        SELECT 1 FROM router.execution_cancellations
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND final_state IS NULL
    ) THEN
        RAISE EXCEPTION 'cancel request needs its durable cancellation intent'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state = 'cancelled' AND NOT EXISTS (
        SELECT 1 FROM router.execution_cancellations
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND final_state = 'cancelled'
    ) THEN
        RAISE EXCEPTION 'cancelled state needs its final cancellation proof'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state = 'uncertain' AND EXISTS (
        SELECT 1 FROM router.execution_cancellations
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
    ) AND NOT EXISTS (
        SELECT 1 FROM router.execution_cancellations
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND final_state = 'uncertain'
    ) THEN
        RAISE EXCEPTION 'uncertain cancellation needs its final reconciliation result'
            USING ERRCODE = '23514';
    END IF;
    IF execution.state IN ('succeeded','failed','interrupted','cancelled','uncertain')
       AND (latest.event_name <> 'request.terminal'
            OR (latest.wire_data::jsonb #>> '{payload,state_revision}')::bigint
               <> execution.state_revision
            OR latest.wire_data::jsonb #>> '{payload,state}' <> execution.state::text
            OR EXISTS (
                SELECT 1 FROM router.execution_stream_events
                WHERE request_row_id IS NOT DISTINCT FROM
                      CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
                  AND run_row_id IS NOT DISTINCT FROM
                      CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
                  AND (expires_at IS NULL
                       OR expires_at < execution.terminal_at + interval '15 minutes')
            )) THEN
        RAISE EXCEPTION 'terminal state needs its final retained stream event'
            USING ERRCODE = '23514';
    END IF;
    IF execution.terminal_at IS NULL AND EXISTS (
        SELECT 1 FROM router.execution_stream_events
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND expires_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'nonterminal stream events cannot expire'
            USING ERRCODE = '23514';
    END IF;
    IF execution.partial_output AND NOT EXISTS (
        SELECT 1 FROM router.execution_stream_events
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND event_name = 'output.delta'
    ) THEN
        RAISE EXCEPTION 'partial output needs its durable commit event'
            USING ERRCODE = '23514';
    END IF;
    IF execution.committed_effect AND NOT EXISTS (
        SELECT 1 FROM router.execution_stream_events
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND event_name = 'tool.started'
          AND wire_data::jsonb #>> '{payload,tool_kind}' = 'business'
    ) THEN
        RAISE EXCEPTION 'committed effect needs its durable commit event'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER logical_requests_journal_complete
AFTER UPDATE OF state, state_revision, partial_output, committed_effect,
    terminal_at ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.check_execution_journal_complete();

CREATE TRIGGER agent_runs_journal_complete
AFTER UPDATE OF state, state_revision, partial_output, committed_effect,
    terminal_at ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.check_execution_journal_complete();

CREATE FUNCTION router.check_stream_event_applied()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    execution record;
    payload jsonb := NEW.wire_data::jsonb->'payload';
BEGIN
    IF NEW.request_row_id IS NOT NULL THEN
        SELECT * INTO execution FROM router.logical_requests
        WHERE row_id = NEW.request_row_id;
    ELSE
        SELECT * INTO execution FROM router.agent_runs
        WHERE row_id = NEW.run_row_id;
    END IF;
    IF NEW.event_name IN (
           'request.running','request.waiting_for_tool',
           'request.cancel_requested','request.terminal'
       ) AND NEW.occurred_at IS DISTINCT FROM
           date_trunc('milliseconds', execution.last_transition_at) THEN
        RAISE EXCEPTION 'lifecycle event time does not match execution transition'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.running'
       AND (execution.state_revision < (payload->>'state_revision')::bigint
            OR (payload->>'state_revision')::bigint IS NULL) THEN
        RAISE EXCEPTION 'running event was not applied to execution state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.waiting_for_tool'
       AND (execution.state_revision < (payload->>'state_revision')::bigint
            OR (payload->>'state_revision')::bigint IS NULL) THEN
        RAISE EXCEPTION 'waiting event was not applied to run state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.cancel_requested'
       AND (execution.state_revision < (payload->>'state_revision')::bigint
            OR (payload->>'state_revision')::bigint IS NULL) THEN
        RAISE EXCEPTION 'cancel event was not applied to execution state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'request.terminal'
       AND (execution.state::text IS DISTINCT FROM payload->>'state'
            OR execution.state_revision IS DISTINCT FROM
               (payload->>'state_revision')::bigint
            OR execution.partial_output IS DISTINCT FROM
               (payload->>'partial_output')::boolean
            OR execution.committed_effect IS DISTINCT FROM
               (payload->>'committed_effects')::boolean
            OR payload->'error' IS DISTINCT FROM execution.safe_error
            OR execution.terminal_at IS NULL
            OR EXISTS (
                SELECT 1 FROM router.execution_stream_events
                WHERE request_row_id IS NOT DISTINCT FROM NEW.request_row_id
                  AND run_row_id IS NOT DISTINCT FROM NEW.run_row_id
                  AND (expires_at IS NULL
                       OR expires_at < execution.terminal_at + interval '15 minutes')
            )) THEN
        RAISE EXCEPTION 'terminal event was not applied to retained terminal state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'output.delta' AND NOT execution.partial_output THEN
        RAISE EXCEPTION 'output event was not applied to commit state'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_name = 'tool.started'
       AND payload->>'tool_kind' = 'business'
       AND NOT execution.committed_effect THEN
        RAISE EXCEPTION 'business effect event was not applied to commit state'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER execution_stream_event_applied
AFTER INSERT ON router.execution_stream_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_stream_event_applied();

CREATE FUNCTION router.valid_adapter_stop_evidence(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    item jsonb;
BEGIN
    IF jsonb_typeof(value) IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(value)
    LOOP
        IF jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR NOT item ?& ARRAY[
               'operation_id','supported','stop_requested','confirmed_stopped','safe_code'
           ]
           OR item - ARRAY[
               'operation_id','supported','stop_requested','confirmed_stopped','safe_code'
           ] <> '{}'::jsonb
           OR jsonb_typeof(item->'operation_id') IS DISTINCT FROM 'string'
           OR length(item->>'operation_id') NOT BETWEEN 1 AND 500
           OR jsonb_typeof(item->'supported') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(item->'stop_requested') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(item->'confirmed_stopped') IS DISTINCT FROM 'boolean'
           OR (item->'safe_code' <> 'null'::jsonb
               AND jsonb_typeof(item->'safe_code') IS DISTINCT FROM 'string')
           OR (item->'safe_code' <> 'null'::jsonb
               AND length(item->>'safe_code') > 100)
           OR ((item->>'confirmed_stopped')::boolean
               AND (NOT (item->>'supported')::boolean
                    OR NOT (item->>'stop_requested')::boolean)) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

CREATE TABLE router.execution_cancellations (
    request_row_id uuid REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    run_row_id uuid REFERENCES router.agent_runs (row_id) ON DELETE RESTRICT,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    actor_kind text NOT NULL CHECK (actor_kind IN ('service', 'system')),
    actor_id text NOT NULL CHECK (actor_id <> ''),
    prior_state router.execution_state NOT NULL,
    reason_sha256 bytea NOT NULL CHECK (octet_length(reason_sha256) = 32),
    requested_at timestamptz NOT NULL,
    reconcile_deadline timestamptz NOT NULL,
    adapter_stop_evidence jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (router.valid_adapter_stop_evidence(adapter_stop_evidence)),
    evidence_updated_at timestamptz,
    final_state router.execution_state,
    completed_at timestamptz,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK ((request_row_id IS NULL) <> (run_row_id IS NULL)),
    CHECK (reconcile_deadline = requested_at + interval '10 minutes'),
    CHECK ((final_state IS NULL) = (completed_at IS NULL)),
    CHECK (final_state IS NULL OR final_state IN ('cancelled', 'uncertain')),
    UNIQUE NULLS NOT DISTINCT (request_row_id, run_row_id)
);

CREATE FUNCTION router.check_execution_cancellation_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.request_row_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.logical_requests
        WHERE row_id = NEW.request_row_id AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'cancellation scope does not match request'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.run_row_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.agent_runs
        WHERE row_id = NEW.run_row_id AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'cancellation scope does not match run'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_cancellations_scope
BEFORE INSERT OR UPDATE ON router.execution_cancellations
FOR EACH ROW EXECUTE FUNCTION router.check_execution_cancellation_scope();

CREATE FUNCTION router.protect_execution_cancellation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'cancellation intent cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.request_row_id IS DISTINCT FROM OLD.request_row_id
       OR NEW.run_row_id IS DISTINCT FROM OLD.run_row_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.actor_kind <> OLD.actor_kind
       OR NEW.actor_id <> OLD.actor_id
       OR NEW.prior_state <> OLD.prior_state
       OR NEW.reason_sha256 <> OLD.reason_sha256
       OR NEW.requested_at <> OLD.requested_at
       OR NEW.reconcile_deadline <> OLD.reconcile_deadline
       OR OLD.final_state IS NOT NULL
       OR jsonb_array_length(NEW.adapter_stop_evidence) < jsonb_array_length(OLD.adapter_stop_evidence)
       OR EXISTS (
           SELECT 1
           FROM jsonb_array_elements(OLD.adapter_stop_evidence) WITH ORDINALITY AS old_item(value, position)
           WHERE NEW.adapter_stop_evidence->(old_item.position - 1)::integer <> old_item.value
       ) THEN
        RAISE EXCEPTION 'cancellation identity or final result is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_cancellations_guard
BEFORE UPDATE OR DELETE ON router.execution_cancellations
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_cancellation();

CREATE OR REPLACE FUNCTION router.check_effect_owner_epoch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.run_leases
        WHERE run_row_id = NEW.run_row_id
          AND owner_epoch = NEW.owner_epoch
          AND expires_at > transaction_timestamp()
    ) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state = 'intent'
       AND NEW.state = 'uncertain' AND NEW.resolved_at IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM router.run_leases
           WHERE run_row_id = NEW.run_row_id
             AND owner_epoch > NEW.owner_epoch
             AND expires_at > transaction_timestamp()
       ) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state = 'intent'
       AND NEW.state = 'uncertain' AND NEW.resolved_at IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM router.execution_cancellations
           WHERE run_row_id = NEW.run_row_id AND final_state = 'uncertain'
       ) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state = 'intent'
       AND NEW.state = 'failed' AND NEW.resolved_at IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM router.execution_cancellations AS cancellation,
                jsonb_array_elements(cancellation.adapter_stop_evidence) AS evidence
           WHERE cancellation.run_row_id = NEW.run_row_id
             AND evidence->>'operation_id' = NEW.operation_identity
             AND (evidence->>'confirmed_stopped')::boolean IS TRUE
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'effect intent does not match the current run owner'
        USING ERRCODE = '40001';
END;
$$;

CREATE TABLE router.execution_cancellation_audit (
    event_id uuid PRIMARY KEY,
    request_row_id uuid REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    run_row_id uuid REFERENCES router.agent_runs (row_id) ON DELETE RESTRICT,
    target_public_id uuid NOT NULL,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    actor_kind text NOT NULL CHECK (actor_kind IN ('service', 'system')),
    actor_id text NOT NULL CHECK (actor_id <> ''),
    permission_result text NOT NULL CHECK (permission_result IN ('allowed', 'denied')),
    action text NOT NULL CHECK (action IN ('model.cancel', 'tool.cancel', 'run.cancel')),
    prior_state router.execution_state,
    adapter_stop_evidence jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (router.valid_adapter_stop_evidence(adapter_stop_evidence)),
    final_result text NOT NULL CHECK (final_result IN (
        'denied', 'accepted', 'cancelled', 'pending', 'too_late', 'uncertain'
    )),
    occurred_at timestamptz NOT NULL,
    CHECK (NOT (request_row_id IS NOT NULL AND run_row_id IS NOT NULL))
    ,CHECK ((permission_result = 'denied') = (request_row_id IS NULL AND run_row_id IS NULL))
);

CREATE FUNCTION router.check_execution_cancellation_audit_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.permission_result = 'allowed' AND NEW.request_row_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM router.logical_requests
           WHERE row_id = NEW.request_row_id AND request_id = NEW.target_public_id
             AND service_id = NEW.service_id
             AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
             AND NEW.action = CASE request_kind WHEN 'model' THEN 'model.cancel' ELSE 'tool.cancel' END
       ) THEN
        RAISE EXCEPTION 'allowed cancellation audit does not match request'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.permission_result = 'allowed' AND NEW.run_row_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM router.agent_runs
           WHERE row_id = NEW.run_row_id AND run_id = NEW.target_public_id
             AND service_id = NEW.service_id
             AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
             AND NEW.action = 'run.cancel'
       ) THEN
        RAISE EXCEPTION 'allowed cancellation audit does not match run'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_cancellation_audit_scope
BEFORE INSERT ON router.execution_cancellation_audit
FOR EACH ROW EXECUTE FUNCTION router.check_execution_cancellation_audit_scope();

CREATE INDEX execution_cancellation_audit_scope_time_idx
ON router.execution_cancellation_audit (service_id, workspace_id, occurred_at DESC);

CREATE TRIGGER execution_cancellation_audit_append_only
BEFORE UPDATE OR DELETE ON router.execution_cancellation_audit
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.require_execution_cancellation_audit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    execution_state router.execution_state;
    expected_result text;
    has_cancellation boolean;
BEGIN
    IF TG_TABLE_NAME = 'logical_requests' THEN
        SELECT state INTO execution_state FROM router.logical_requests
        WHERE row_id = NEW.row_id;
        SELECT EXISTS (
            SELECT 1 FROM router.execution_cancellations
            WHERE request_row_id = NEW.row_id
        ) INTO has_cancellation;
    ELSE
        SELECT state INTO execution_state FROM router.agent_runs
        WHERE row_id = NEW.row_id;
        SELECT EXISTS (
            SELECT 1 FROM router.execution_cancellations
            WHERE run_row_id = NEW.row_id
        ) INTO has_cancellation;
    END IF;
    expected_result := CASE execution_state
        WHEN 'cancel_requested' THEN 'accepted'
        WHEN 'cancelled' THEN 'cancelled'
        WHEN 'uncertain' THEN CASE WHEN has_cancellation THEN 'uncertain' END
        ELSE NULL
    END;
    IF expected_result IS NULL THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM router.execution_cancellation_audit
        WHERE request_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'logical_requests' THEN NEW.row_id ELSE NULL END
          AND run_row_id IS NOT DISTINCT FROM
              CASE WHEN TG_TABLE_NAME = 'agent_runs' THEN NEW.row_id ELSE NULL END
          AND permission_result = 'allowed'
          AND final_result = expected_result
    ) THEN
        RAISE EXCEPTION '% state needs its allowed cancellation audit',
            execution_state USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER logical_requests_cancellation_audit_complete
AFTER UPDATE OF state ON router.logical_requests
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.require_execution_cancellation_audit();

CREATE CONSTRAINT TRIGGER agent_runs_cancellation_audit_complete
AFTER UPDATE OF state ON router.agent_runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.require_execution_cancellation_audit();

CREATE FUNCTION router.stop_new_execution_work()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    execution record;
BEGIN
    SELECT state, partial_output, committed_effect INTO execution
    FROM router.logical_requests WHERE row_id = NEW.request_row_id FOR UPDATE;
    IF execution.state IN (
           'cancel_requested','succeeded','failed','interrupted','cancelled','uncertain'
       ) OR execution.partial_output OR execution.committed_effect THEN
        RAISE EXCEPTION 'request state or commit boundary stops a new attempt'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_attempts_stop_new_work
BEFORE INSERT ON router.provider_attempts
FOR EACH ROW EXECUTE FUNCTION router.stop_new_execution_work();

CREATE FUNCTION router.require_attempt_running_journal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.execution_stream_events
        WHERE request_row_id = NEW.request_row_id
          AND event_name = 'request.running'
    ) THEN
        RAISE EXCEPTION 'provider attempt needs a durable running transition'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER provider_attempts_running_journal
AFTER INSERT ON router.provider_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.require_attempt_running_journal();

CREATE FUNCTION router.stop_new_run_effect()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.agent_runs
        WHERE row_id = NEW.run_row_id
          AND state NOT IN ('running','waiting_for_tool')
    ) THEN
        RAISE EXCEPTION 'run state or commit boundary stops a new effect'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER effect_intents_stop_new_work
BEFORE INSERT ON router.effect_intents
FOR EACH ROW EXECUTE FUNCTION router.stop_new_run_effect();

CREATE UNIQUE INDEX effect_intents_one_active_per_run
ON router.effect_intents (run_row_id)
WHERE state = 'intent';

ALTER TABLE router.effect_intents
ADD CONSTRAINT effect_intents_operation_identity_length_check
CHECK (length(operation_identity) <= 500);

CREATE INDEX agent_runs_expiry_idx ON router.agent_runs (expires_at)
WHERE terminal_at IS NOT NULL;
