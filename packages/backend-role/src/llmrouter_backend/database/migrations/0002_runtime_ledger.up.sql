CREATE TYPE router.execution_state AS ENUM (
    'admitted',
    'running',
    'waiting_for_tool',
    'cancel_requested',
    'succeeded',
    'failed',
    'interrupted',
    'cancelled',
    'uncertain'
);

CREATE FUNCTION router.protect_execution_state()
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

CREATE FUNCTION router.protect_logical_request_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.row_id <> OLD.row_id
       OR NEW.request_id <> OLD.request_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.request_kind <> OLD.request_kind
       OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
       OR NEW.configuration_revision_id <> OLD.configuration_revision_id
       OR NEW.fingerprint_version <> OLD.fingerprint_version
       OR NEW.fingerprint_sha256 <> OLD.fingerprint_sha256
       OR NEW.data_profile <> OLD.data_profile
       OR NEW.admitted_at <> OLD.admitted_at THEN
        RAISE EXCEPTION 'logical request admission identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain')
       AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal logical request is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_agent_run_identity()
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

CREATE TABLE router.attachments (
    id uuid PRIMARY KEY,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    media_type text NOT NULL CHECK (media_type IN (
        'text/plain', 'text/markdown', 'application/json', 'application/pdf',
        'image/jpeg', 'image/png', 'image/webp', 'audio/mpeg', 'audio/wav'
    )),
    byte_length bigint NOT NULL CHECK (byte_length BETWEEN 0 AND 26214400),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    object_manifest_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    UNIQUE (id, service_id, workspace_id),
    UNIQUE (object_manifest_id)
);

CREATE TRIGGER attachments_append_only
BEFORE UPDATE OR DELETE ON router.attachments
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.attachment_status (
    attachment_id uuid PRIMARY KEY REFERENCES router.attachments (id) ON DELETE RESTRICT,
    state text NOT NULL CHECK (state IN ('pending', 'ready', 'failed', 'expired')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    verified_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK ((state = 'ready') = (verified_at IS NOT NULL))
);

CREATE TABLE router.logical_requests (
    row_id uuid PRIMARY KEY,
    request_id uuid NOT NULL CHECK (request_id <> '00000000-0000-0000-0000-000000000000'),
    request_kind text NOT NULL CHECK (request_kind IN ('model', 'shared_tool')),
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    assignment_id uuid REFERENCES router.assignment_definitions (id) ON DELETE RESTRICT,
    configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    fingerprint_version smallint NOT NULL CHECK (fingerprint_version > 0),
    fingerprint_sha256 bytea NOT NULL CHECK (octet_length(fingerprint_sha256) = 32),
    data_profile text NOT NULL CHECK (data_profile = 'service-data'),
    state router.execution_state NOT NULL DEFAULT 'admitted',
    state_revision bigint NOT NULL DEFAULT 1 CHECK (state_revision > 0),
    capture_enabled boolean NOT NULL,
    capture_pressure_reason text,
    partial_output boolean NOT NULL DEFAULT false,
    committed_effect boolean NOT NULL DEFAULT false,
    admitted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    last_transition_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    terminal_at timestamptz,
    expires_at timestamptz,
    safe_error jsonb,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (capture_enabled OR capture_pressure_reason IS NOT NULL),
    CHECK (
        (state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain'))
        = (terminal_at IS NOT NULL)
    ),
    CHECK (terminal_at IS NULL OR expires_at >= terminal_at + interval '24 hours'),
    UNIQUE NULLS NOT DISTINCT (service_id, workspace_id, request_id),
    UNIQUE NULLS NOT DISTINCT (row_id, service_id, workspace_id)
);

CREATE INDEX logical_requests_scope_status_idx
    ON router.logical_requests (service_id, workspace_id, state, admitted_at DESC);

CREATE INDEX logical_requests_expiry_idx
    ON router.logical_requests (expires_at)
    WHERE terminal_at IS NOT NULL;

CREATE TRIGGER logical_requests_stable_identity
BEFORE UPDATE ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.protect_logical_request_identity();

CREATE TRIGGER logical_requests_state_guard
BEFORE UPDATE OF state, state_revision, terminal_at ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_state();

CREATE FUNCTION router.check_execution_configuration_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.configuration_revisions
        WHERE id = NEW.configuration_revision_id
          AND (
              scope_kind = 'global'
              OR (scope_kind = 'service'
                  AND service_id = NEW.service_id)
              OR (scope_kind = 'workspace'
                  AND service_id = NEW.service_id
                  AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
          )
    ) THEN
        RAISE EXCEPTION 'execution configuration does not match its scope'
            USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'logical_requests'
       AND to_jsonb(NEW)->>'assignment_id' IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM router.assignment_definitions
           WHERE id = (to_jsonb(NEW)->>'assignment_id')::uuid
             AND configuration_revision_id = NEW.configuration_revision_id
       ) THEN
        RAISE EXCEPTION 'request assignment does not match its configuration revision'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER logical_requests_configuration_scope
BEFORE INSERT OR UPDATE OF service_id, workspace_id, assignment_id,
    configuration_revision_id ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.check_execution_configuration_scope();

CREATE TABLE router.request_attachments (
    request_row_id uuid NOT NULL,
    service_id uuid NOT NULL,
    workspace_id uuid,
    attachment_id uuid NOT NULL,
    ordinal smallint NOT NULL CHECK (ordinal BETWEEN 1 AND 20),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    byte_length bigint NOT NULL CHECK (byte_length BETWEEN 0 AND 26214400),
    PRIMARY KEY (request_row_id, ordinal),
    UNIQUE (request_row_id, attachment_id),
    FOREIGN KEY (request_row_id, service_id, workspace_id)
        REFERENCES router.logical_requests (row_id, service_id, workspace_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (attachment_id, service_id, workspace_id)
        REFERENCES router.attachments (id, service_id, workspace_id)
        ON DELETE RESTRICT
);

CREATE FUNCTION router.check_request_attachment_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.logical_requests
        WHERE row_id = NEW.request_row_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) OR NOT EXISTS (
        SELECT 1 FROM router.attachments
        WHERE id = NEW.attachment_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'request attachment scope does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER request_attachments_scope
BEFORE INSERT OR UPDATE ON router.request_attachments
FOR EACH ROW EXECUTE FUNCTION router.check_request_attachment_scope();

CREATE FUNCTION router.check_request_attachment_bounds()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM router.logical_requests
    WHERE row_id = NEW.request_row_id
    FOR UPDATE;
    IF (SELECT count(*) FROM router.request_attachments
        WHERE request_row_id = NEW.request_row_id) > 20
       OR (SELECT sum(byte_length) FROM router.request_attachments
           WHERE request_row_id = NEW.request_row_id) > 104857600 THEN
        RAISE EXCEPTION 'request attachment bounds are exceeded'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM router.attachments
        WHERE id = NEW.attachment_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND content_sha256 = NEW.content_sha256
          AND byte_length = NEW.byte_length
    ) THEN
        RAISE EXCEPTION 'request attachment metadata does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER request_attachments_bounds
AFTER INSERT OR UPDATE ON router.request_attachments
FOR EACH ROW EXECUTE FUNCTION router.check_request_attachment_bounds();

CREATE TABLE router.provider_attempts (
    id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL,
    service_id uuid NOT NULL,
    workspace_id uuid,
    attempt_number smallint NOT NULL CHECK (attempt_number BETWEEN 1 AND 8),
    provider_model_route_id uuid NOT NULL,
    route_generation bigint NOT NULL,
    assignment_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    price_version_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN (
        'started', 'succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain'
    )),
    normalized_error_class text,
    affected_scope text,
    retry_decision text,
    started_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    finished_at timestamptz,
    FOREIGN KEY (request_row_id, service_id, workspace_id)
        REFERENCES router.logical_requests (row_id, service_id, workspace_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (provider_model_route_id, route_generation)
        REFERENCES router.provider_model_routes (id, generation) ON DELETE RESTRICT,
    FOREIGN KEY (price_version_id, provider_model_route_id)
        REFERENCES router.route_price_versions (id, provider_model_route_id)
        ON DELETE RESTRICT,
    CHECK ((state = 'started') = (finished_at IS NULL)),
    UNIQUE (request_row_id, attempt_number)
);

CREATE FUNCTION router.protect_provider_attempt()
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

CREATE TRIGGER provider_attempts_identity_state
BEFORE UPDATE ON router.provider_attempts
FOR EACH ROW EXECUTE FUNCTION router.protect_provider_attempt();

CREATE FUNCTION router.check_attempt_request_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.logical_requests
        WHERE row_id = NEW.request_row_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'provider attempt scope does not match its request'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_attempts_request_scope
BEFORE INSERT OR UPDATE ON router.provider_attempts
FOR EACH ROW EXECUTE FUNCTION router.check_attempt_request_scope();

CREATE INDEX provider_attempts_route_recent_idx
    ON router.provider_attempts (provider_model_route_id, started_at DESC);

CREATE TABLE router.agent_runs (
    row_id uuid PRIMARY KEY,
    run_id uuid NOT NULL CHECK (run_id <> '00000000-0000-0000-0000-000000000000'),
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    fingerprint_version smallint NOT NULL CHECK (fingerprint_version > 0),
    fingerprint_sha256 bytea NOT NULL CHECK (octet_length(fingerprint_sha256) = 32),
    state router.execution_state NOT NULL DEFAULT 'admitted',
    state_revision bigint NOT NULL DEFAULT 1 CHECK (state_revision > 0),
    durable_checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(durable_checkpoint) = 'object'),
    partial_output boolean NOT NULL DEFAULT false,
    committed_effect boolean NOT NULL DEFAULT false,
    admitted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    last_transition_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    terminal_at timestamptz,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (
        (state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain'))
        = (terminal_at IS NOT NULL)
    ),
    UNIQUE NULLS NOT DISTINCT (service_id, workspace_id, run_id),
    UNIQUE NULLS NOT DISTINCT (row_id, service_id, workspace_id)
);

CREATE INDEX agent_runs_scope_status_idx
    ON router.agent_runs (service_id, workspace_id, state, admitted_at DESC);

CREATE TRIGGER agent_runs_stable_identity
BEFORE UPDATE ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.protect_agent_run_identity();

CREATE TRIGGER agent_runs_state_guard
BEFORE UPDATE OF state, state_revision, terminal_at ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.protect_execution_state();

CREATE TRIGGER agent_runs_configuration_scope
BEFORE INSERT OR UPDATE OF service_id, workspace_id, configuration_revision_id
ON router.agent_runs
FOR EACH ROW EXECUTE FUNCTION router.check_execution_configuration_scope();

CREATE TABLE router.control_epochs (
    epoch bigint PRIMARY KEY CHECK (epoch > 0),
    established_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    fencing_evidence text NOT NULL CHECK (fencing_evidence <> '')
);

CREATE FUNCTION router.check_control_epoch_sequence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    LOCK TABLE router.control_epochs IN EXCLUSIVE MODE;
    IF NEW.epoch <> COALESCE((SELECT max(epoch) FROM router.control_epochs), 0) + 1 THEN
        RAISE EXCEPTION 'control epoch must increase by one'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER control_epochs_sequence
BEFORE INSERT ON router.control_epochs
FOR EACH ROW EXECUTE FUNCTION router.check_control_epoch_sequence();

CREATE TRIGGER control_epochs_append_only
BEFORE UPDATE OR DELETE ON router.control_epochs
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.run_leases (
    run_row_id uuid PRIMARY KEY REFERENCES router.agent_runs (row_id) ON DELETE RESTRICT,
    owner_node_id uuid NOT NULL,
    control_epoch bigint NOT NULL REFERENCES router.control_epochs (epoch) ON DELETE RESTRICT,
    owner_epoch bigint NOT NULL CHECK (owner_epoch > 0),
    lease_generation bigint NOT NULL CHECK (lease_generation > 0),
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (run_row_id, owner_epoch),
    UNIQUE (run_row_id, lease_generation)
);

CREATE FUNCTION router.check_run_lease_fence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.control_epoch <> (SELECT max(epoch) FROM router.control_epochs) THEN
        RAISE EXCEPTION 'run lease must use the current control epoch'
            USING ERRCODE = '40001';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.run_row_id <> OLD.run_row_id THEN
            RAISE EXCEPTION 'run lease identity is immutable'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.lease_generation <= OLD.lease_generation THEN
            RAISE EXCEPTION 'lease generation must increase'
                USING ERRCODE = '40001';
        END IF;
        IF NEW.owner_node_id IS DISTINCT FROM OLD.owner_node_id
           AND NEW.owner_epoch <= OLD.owner_epoch THEN
            RAISE EXCEPTION 'owner epoch must increase on takeover'
                USING ERRCODE = '40001';
        END IF;
        IF NEW.owner_epoch < OLD.owner_epoch
           OR NEW.control_epoch < OLD.control_epoch THEN
            RAISE EXCEPTION 'run lease fence cannot decrease'
                USING ERRCODE = '40001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_fenced_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.lease_generation <= OLD.lease_generation THEN
        RAISE EXCEPTION 'lease generation must increase'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER run_leases_fenced_generation
BEFORE INSERT OR UPDATE ON router.run_leases
FOR EACH ROW EXECUTE FUNCTION router.check_run_lease_fence();

CREATE INDEX run_leases_expiry_idx ON router.run_leases (expires_at);

CREATE TABLE router.effect_intents (
    id uuid PRIMARY KEY,
    run_row_id uuid NOT NULL REFERENCES router.agent_runs (row_id) ON DELETE RESTRICT,
    owner_epoch bigint NOT NULL CHECK (owner_epoch > 0),
    operation_identity text NOT NULL CHECK (operation_identity <> ''),
    effect_kind text NOT NULL CHECK (effect_kind <> ''),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    state text NOT NULL CHECK (state IN ('intent', 'confirmed', 'failed', 'uncertain')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    resolved_at timestamptz,
    UNIQUE (run_row_id, operation_identity),
    CHECK ((state = 'intent') = (resolved_at IS NULL))
);

CREATE FUNCTION router.protect_effect_intent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.run_row_id <> OLD.run_row_id
       OR NEW.owner_epoch <> OLD.owner_epoch
       OR NEW.operation_identity <> OLD.operation_identity
       OR NEW.effect_kind <> OLD.effect_kind
       OR NEW.request_fingerprint <> OLD.request_fingerprint
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'effect intent identity and fence are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state <> 'intent' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'resolved effect intent is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER effect_intents_identity_state
BEFORE UPDATE ON router.effect_intents
FOR EACH ROW EXECUTE FUNCTION router.protect_effect_intent();

CREATE FUNCTION router.check_effect_owner_epoch()
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

CREATE TRIGGER effect_intents_owner_epoch
BEFORE INSERT OR UPDATE ON router.effect_intents
FOR EACH ROW EXECUTE FUNCTION router.check_effect_owner_epoch();

CREATE TABLE router.budget_scopes (
    id uuid PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'service', 'workspace', 'assignment')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    assignment_id uuid REFERENCES router.assignment_definitions (id) ON DELETE RESTRICT,
    parent_budget_scope_id uuid REFERENCES router.budget_scopes (id) ON DELETE RESTRICT,
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    hard_limit numeric(38, 18) NOT NULL CHECK (hard_limit >= 0),
    warning_threshold numeric(38, 18) CHECK (
        warning_threshold IS NULL OR warning_threshold BETWEEN 0 AND hard_limit
    ),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (
        (scope_kind = 'global' AND service_id IS NULL AND workspace_id IS NULL AND assignment_id IS NULL)
        OR (scope_kind = 'service' AND service_id IS NOT NULL AND workspace_id IS NULL AND assignment_id IS NULL)
        OR (scope_kind = 'workspace' AND service_id IS NOT NULL AND workspace_id IS NOT NULL AND assignment_id IS NULL)
        OR (scope_kind = 'assignment' AND service_id IS NOT NULL AND assignment_id IS NOT NULL)
    ),
    UNIQUE NULLS NOT DISTINCT (scope_kind, service_id, workspace_id, assignment_id),
    UNIQUE (id, currency)
);

CREATE FUNCTION router.check_budget_hierarchy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_currency char(3);
    parent_limit numeric(38, 18);
BEGIN
    IF NEW.parent_budget_scope_id IS NOT NULL THEN
        SELECT currency, hard_limit INTO parent_currency, parent_limit
        FROM router.budget_scopes WHERE id = NEW.parent_budget_scope_id;
        IF parent_currency IS DISTINCT FROM NEW.currency OR parent_limit < NEW.hard_limit THEN
            RAISE EXCEPTION 'child budget must use its parent currency and limit'
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            WITH RECURSIVE ancestors AS (
                SELECT parent_budget_scope_id
                FROM router.budget_scopes WHERE id = NEW.parent_budget_scope_id
              UNION ALL
                SELECT budget.parent_budget_scope_id
                FROM router.budget_scopes AS budget
                JOIN ancestors ON budget.id = ancestors.parent_budget_scope_id
                WHERE budget.parent_budget_scope_id IS NOT NULL
            )
            SELECT 1 FROM ancestors WHERE parent_budget_scope_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'budget parent chain contains a cycle'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.budget_scopes AS child
        WHERE child.parent_budget_scope_id = NEW.id
          AND (child.currency <> NEW.currency OR child.hard_limit > NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'parent budget cannot exclude an existing child budget'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER budget_scopes_hierarchy
AFTER INSERT OR UPDATE ON router.budget_scopes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_budget_hierarchy();

CREATE TABLE router.budget_allowance_leases (
    id uuid PRIMARY KEY,
    budget_scope_id uuid NOT NULL,
    currency char(3) NOT NULL,
    owner_node_id uuid NOT NULL,
    lease_generation bigint NOT NULL CHECK (lease_generation > 0),
    issued_amount numeric(38, 18) NOT NULL CHECK (issued_amount >= 0),
    consumed_amount numeric(38, 18) NOT NULL DEFAULT 0
        CHECK (consumed_amount >= 0 AND consumed_amount <= issued_amount),
    issued_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expires_at timestamptz NOT NULL,
    safety_until timestamptz NOT NULL,
    FOREIGN KEY (budget_scope_id, currency)
        REFERENCES router.budget_scopes (id, currency) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK (safety_until >= expires_at),
    UNIQUE (budget_scope_id, owner_node_id, lease_generation)
);

CREATE FUNCTION router.check_budget_allowance()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    scope_limit numeric(38, 18);
    live_issued numeric(38, 18);
    central_reserved numeric(38, 18);
BEGIN
    SELECT hard_limit
    INTO scope_limit
    FROM router.budget_scopes
    WHERE id = NEW.budget_scope_id AND currency = NEW.currency
    FOR UPDATE;
    IF scope_limit IS NULL THEN
        RAISE EXCEPTION 'budget allowance scope or currency does not exist'
            USING ERRCODE = '23503';
    END IF;
    SELECT COALESCE(sum(issued_amount), 0)
    INTO live_issued
    FROM router.budget_allowance_leases
    WHERE budget_scope_id = NEW.budget_scope_id
      AND expires_at > transaction_timestamp()
      AND id <> NEW.id;
    SELECT COALESCE(sum(reserved_amount - released_amount), 0)
    INTO central_reserved
    FROM router.budget_reservations
    WHERE budget_scope_id = NEW.budget_scope_id
      AND allowance_lease_id IS NULL
      AND reconciled_at IS NULL;
    IF NEW.expires_at > transaction_timestamp()
       AND live_issued + NEW.issued_amount + central_reserved > scope_limit THEN
        RAISE EXCEPTION 'live budget allowances exceed the hard limit'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id
           OR NEW.budget_scope_id <> OLD.budget_scope_id
           OR NEW.currency <> OLD.currency
           OR NEW.owner_node_id <> OLD.owner_node_id
           OR NEW.issued_amount <> OLD.issued_amount
           OR NEW.issued_at <> OLD.issued_at
           OR NEW.safety_until <> OLD.safety_until THEN
            RAISE EXCEPTION 'budget allowance identity and issued amount are immutable'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.lease_generation <= OLD.lease_generation THEN
            RAISE EXCEPTION 'budget allowance generation must increase'
                USING ERRCODE = '40001';
        END IF;
        IF NEW.consumed_amount < OLD.consumed_amount THEN
            RAISE EXCEPTION 'budget allowance consumption cannot decrease'
                USING ERRCODE = '40001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_allowance_leases_guard
BEFORE INSERT OR UPDATE ON router.budget_allowance_leases
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance();

CREATE INDEX budget_allowance_leases_expiry_idx
    ON router.budget_allowance_leases (expires_at, budget_scope_id);

CREATE TABLE router.budget_reservations (
    id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    budget_scope_id uuid NOT NULL,
    currency char(3) NOT NULL,
    allowance_lease_id uuid REFERENCES router.budget_allowance_leases (id) ON DELETE RESTRICT,
    estimated_amount numeric(38, 18) NOT NULL CHECK (estimated_amount >= 0),
    reserved_amount numeric(38, 18) NOT NULL CHECK (reserved_amount >= estimated_amount),
    released_amount numeric(38, 18) NOT NULL DEFAULT 0
        CHECK (released_amount >= 0 AND released_amount <= reserved_amount),
    actual_amount numeric(38, 18) CHECK (actual_amount IS NULL OR actual_amount >= 0),
    corrected_amount numeric(38, 18) CHECK (corrected_amount IS NULL OR corrected_amount >= 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    reconciled_at timestamptz,
    FOREIGN KEY (budget_scope_id, currency)
        REFERENCES router.budget_scopes (id, currency) ON DELETE RESTRICT,
    UNIQUE (request_row_id, budget_scope_id)
);

CREATE FUNCTION router.check_budget_reservation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    scope_limit numeric(38, 18);
    unleased_reserved numeric(38, 18);
    live_issued numeric(38, 18);
BEGIN
    SELECT hard_limit
    INTO scope_limit
    FROM router.budget_scopes
    WHERE id = NEW.budget_scope_id AND currency = NEW.currency
    FOR UPDATE;
    IF scope_limit IS NULL THEN
        RAISE EXCEPTION 'budget reservation scope or currency does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.allowance_lease_id IS NULL THEN
        SELECT COALESCE(sum(reserved_amount - released_amount), 0)
        INTO unleased_reserved
        FROM router.budget_reservations
        WHERE budget_scope_id = NEW.budget_scope_id
          AND allowance_lease_id IS NULL
          AND reconciled_at IS NULL
          AND id <> NEW.id;
        SELECT COALESCE(sum(issued_amount), 0)
        INTO live_issued
        FROM router.budget_allowance_leases
        WHERE budget_scope_id = NEW.budget_scope_id
          AND expires_at > transaction_timestamp();
        IF NEW.reconciled_at IS NULL
           AND unleased_reserved + NEW.reserved_amount - NEW.released_amount
               + live_issued > scope_limit THEN
            RAISE EXCEPTION 'central budget reservations exceed the hard limit'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id
           OR NEW.request_row_id <> OLD.request_row_id
           OR NEW.budget_scope_id <> OLD.budget_scope_id
           OR NEW.currency <> OLD.currency
           OR NEW.allowance_lease_id IS DISTINCT FROM OLD.allowance_lease_id
           OR NEW.estimated_amount <> OLD.estimated_amount
           OR NEW.reserved_amount <> OLD.reserved_amount
           OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'budget reservation identity and reservation are immutable'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.released_amount < OLD.released_amount THEN
            RAISE EXCEPTION 'released budget amount cannot decrease'
                USING ERRCODE = '40001';
        END IF;
        IF OLD.reconciled_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'reconciled budget reservation is immutable'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_reservations_guard
BEFORE INSERT OR UPDATE ON router.budget_reservations
FOR EACH ROW EXECUTE FUNCTION router.check_budget_reservation();

CREATE FUNCTION router.check_reservation_allowance_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.allowance_lease_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.budget_allowance_leases
        WHERE id = NEW.allowance_lease_id
          AND budget_scope_id = NEW.budget_scope_id
          AND currency = NEW.currency
    ) THEN
        RAISE EXCEPTION 'reservation allowance scope or currency does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_reservations_allowance_scope
BEFORE INSERT OR UPDATE ON router.budget_reservations
FOR EACH ROW EXECUTE FUNCTION router.check_reservation_allowance_scope();

CREATE TABLE router.accounting_events (
    event_id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    attempt_id uuid REFERENCES router.provider_attempts (id) ON DELETE RESTRICT,
    budget_scope_id uuid NOT NULL,
    currency char(3) NOT NULL,
    event_kind text NOT NULL CHECK (event_kind IN (
        'reservation', 'usage', 'release', 'price_correction', 'usage_correction'
    )),
    quantity numeric(38, 18) NOT NULL,
    amount numeric(38, 18) NOT NULL,
    price_version_id uuid REFERENCES router.route_price_versions (id) ON DELETE RESTRICT,
    source_event_id uuid REFERENCES router.accounting_events (event_id) ON DELETE RESTRICT,
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (budget_scope_id, currency)
        REFERENCES router.budget_scopes (id, currency) ON DELETE RESTRICT
);

CREATE FUNCTION router.check_accounting_attempt_request()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.provider_attempts
        WHERE id = NEW.attempt_id AND request_row_id = NEW.request_row_id
    ) THEN
        RAISE EXCEPTION 'accounting attempt does not belong to its request'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER accounting_events_attempt_request
BEFORE INSERT ON router.accounting_events
FOR EACH ROW EXECUTE FUNCTION router.check_accounting_attempt_request();

CREATE INDEX accounting_events_scope_time_idx
    ON router.accounting_events (budget_scope_id, occurred_at, event_id);

CREATE TRIGGER accounting_events_append_only
BEFORE UPDATE OR DELETE ON router.accounting_events
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.canonical_events (
    event_id uuid PRIMARY KEY,
    source_node_id uuid NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence > 0),
    event_class text NOT NULL CHECK (event_class IN ('accounting', 'audit')),
    payload_sha256 bytea NOT NULL CHECK (octet_length(payload_sha256) = 32),
    durable_replay_position text NOT NULL CHECK (durable_replay_position <> ''),
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (source_node_id, source_sequence)
);

CREATE TRIGGER canonical_events_append_only
BEFORE UPDATE OR DELETE ON router.canonical_events
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.administrators (
    id uuid PRIMARY KEY,
    issuer text NOT NULL CHECK (issuer <> ''),
    subject text NOT NULL CHECK (subject <> ''),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (issuer, subject)
);

CREATE TABLE router.administrator_grants (
    id uuid PRIMARY KEY,
    administrator_id uuid NOT NULL REFERENCES router.administrators (id) ON DELETE RESTRICT,
    authority_class text NOT NULL CHECK (authority_class IN ('global', 'service')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    operations text[] NOT NULL CHECK (cardinality(operations) > 0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (
        (authority_class = 'global' AND service_id IS NULL AND workspace_id IS NULL)
        OR (authority_class = 'service' AND service_id IS NOT NULL)
    )
);

CREATE TABLE router.administrator_sessions (
    id uuid PRIMARY KEY,
    administrator_id uuid NOT NULL REFERENCES router.administrators (id) ON DELETE RESTRICT,
    token_digest bytea NOT NULL UNIQUE CHECK (octet_length(token_digest) = 32),
    csrf_digest bytea NOT NULL CHECK (octet_length(csrf_digest) = 32),
    exact_origin text NOT NULL CHECK (exact_origin <> ''),
    authenticated_at timestamptz NOT NULL,
    account_checked_at timestamptz NOT NULL,
    last_used_at timestamptz NOT NULL,
    idle_expires_at timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (idle_expires_at <= last_used_at + interval '15 minutes'),
    CHECK (absolute_expires_at <= authenticated_at + interval '8 hours')
);

CREATE INDEX administrator_sessions_expiry_idx
    ON router.administrator_sessions (absolute_expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE router.service_bootstrap_generations (
    id uuid PRIMARY KEY,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    generation bigint NOT NULL CHECK (generation > 0),
    argon2id_verifier text NOT NULL CHECK (argon2id_verifier <> ''),
    allowed_operations text[] NOT NULL CHECK (cardinality(allowed_operations) > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    valid_until timestamptz,
    revoked_at timestamptz,
    UNIQUE (service_id, generation)
);

CREATE TABLE router.service_access_tokens (
    token_id uuid PRIMARY KEY,
    token_digest bytea NOT NULL UNIQUE CHECK (octet_length(token_digest) = 32),
    service_id uuid NOT NULL,
    bootstrap_generation bigint NOT NULL,
    audience text NOT NULL CHECK (audience <> ''),
    operations text[] NOT NULL CHECK (cardinality(operations) > 0),
    workspace_ids uuid[] NOT NULL DEFAULT '{}',
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (service_id, bootstrap_generation)
        REFERENCES router.service_bootstrap_generations (service_id, generation)
        ON DELETE RESTRICT,
    CHECK (expires_at <= issued_at + interval '5 minutes')
);

CREATE INDEX service_access_tokens_expiry_idx
    ON router.service_access_tokens (expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE router.embed_sessions (
    id uuid PRIMARY KEY,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_ids uuid[] NOT NULL DEFAULT '{}',
    host_subject text NOT NULL CHECK (host_subject <> ''),
    permitted_actions text[] NOT NULL CHECK (cardinality(permitted_actions) > 0),
    host_origin text NOT NULL CHECK (host_origin <> ''),
    frame_origin text NOT NULL CHECK (frame_origin <> ''),
    bootstrap_token_digest bytea NOT NULL UNIQUE CHECK (octet_length(bootstrap_token_digest) = 32),
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE router.audit_events (
    event_id uuid PRIMARY KEY,
    audit_class text NOT NULL CHECK (audit_class IN (
        'security', 'global_administration', 'agent_run', 'business_tool'
    )),
    actor_kind text NOT NULL CHECK (actor_kind IN ('system', 'administrator', 'service', 'node')),
    actor_id text NOT NULL CHECK (actor_id <> ''),
    authority_class text NOT NULL CHECK (authority_class IN (
        'service', 'global_administrator', 'system'
    )),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    action text NOT NULL CHECK (action <> ''),
    permission_result text NOT NULL CHECK (permission_result IN ('permitted', 'denied')),
    safe_details jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(safe_details) = 'object'),
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT
);

CREATE INDEX audit_events_scope_time_idx
    ON router.audit_events (service_id, workspace_id, occurred_at DESC);

CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON router.audit_events
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.retention_policies (
    id uuid PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'service', 'workspace')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    data_class text NOT NULL CHECK (data_class IN (
        'diagnostic_logs', 'captured_content', 'raw_accounting',
        'agent_business_audit', 'daily_accounting', 'security_global_audit',
        'configuration_revisions'
    )),
    retention_days integer NOT NULL CHECK (retention_days > 0),
    minimum_revision_count integer,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    effective_at timestamptz NOT NULL,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (
        (scope_kind = 'global' AND service_id IS NULL AND workspace_id IS NULL)
        OR (scope_kind = 'service' AND service_id IS NOT NULL AND workspace_id IS NULL)
        OR (scope_kind = 'workspace' AND service_id IS NOT NULL AND workspace_id IS NOT NULL)
    ),
    CHECK (
        (data_class = 'configuration_revisions' AND minimum_revision_count IS NOT NULL)
        OR (data_class <> 'configuration_revisions' AND minimum_revision_count IS NULL)
    ),
    CHECK (
        data_class <> 'agent_business_audit'
        OR retention_days BETWEEN 7 AND 365
    ),
    UNIQUE NULLS NOT DISTINCT (scope_kind, service_id, workspace_id, data_class, revision)
);

CREATE TABLE router.worker_jobs (
    id uuid PRIMARY KEY,
    job_kind text NOT NULL CHECK (job_kind <> ''),
    scope_key text NOT NULL CHECK (scope_key <> ''),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    state text NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')),
    priority smallint NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    owner_node_id uuid,
    lease_generation bigint NOT NULL DEFAULT 1 CHECK (lease_generation > 0),
    lease_expires_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (job_kind, scope_key)
);

CREATE FUNCTION router.protect_worker_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allowed boolean := false;
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.job_kind <> OLD.job_kind
       OR NEW.scope_key <> OLD.scope_key
       OR NEW.payload <> OLD.payload
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'worker job identity and payload are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state IN ('succeeded', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'terminal worker job is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.lease_generation <= OLD.lease_generation THEN
        RAISE EXCEPTION 'worker job lease generation must increase'
            USING ERRCODE = '40001';
    END IF;
    IF OLD.state = 'running' AND (
        OLD.owner_node_id IS NULL
        OR OLD.lease_expires_at IS NULL
        OR OLD.lease_expires_at <= transaction_timestamp()
        OR NEW.owner_node_id IS DISTINCT FROM OLD.owner_node_id
    ) THEN
        RAISE EXCEPTION 'worker job update does not have the current live owner'
            USING ERRCODE = '40001';
    END IF;
    allowed := CASE OLD.state
        WHEN 'ready' THEN NEW.state IN ('running', 'cancelled')
        WHEN 'retry_wait' THEN NEW.state IN ('running', 'cancelled')
        WHEN 'running' THEN NEW.state IN ('running', 'retry_wait', 'succeeded', 'failed', 'cancelled')
        ELSE false
    END;
    IF NOT allowed THEN
        RAISE EXCEPTION 'invalid worker job state transition from % to %',
            OLD.state, NEW.state USING ERRCODE = '23514';
    END IF;
    IF NEW.state = 'running'
       AND (NEW.owner_node_id IS NULL
            OR NEW.lease_expires_at IS NULL
            OR NEW.lease_expires_at <= transaction_timestamp()) THEN
        RAISE EXCEPTION 'running worker job needs a live owner lease'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state <> 'running' AND NEW.lease_expires_at IS NOT NULL THEN
        RAISE EXCEPTION 'non-running worker job cannot keep a live lease'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE INDEX worker_jobs_due_idx
    ON router.worker_jobs (priority DESC, available_at, id)
    WHERE state IN ('ready', 'retry_wait');

CREATE INDEX worker_jobs_lease_expiry_idx
    ON router.worker_jobs (lease_expires_at)
    WHERE state = 'running';

CREATE TRIGGER worker_jobs_fenced_generation
BEFORE UPDATE ON router.worker_jobs
FOR EACH ROW EXECUTE FUNCTION router.protect_worker_job();
