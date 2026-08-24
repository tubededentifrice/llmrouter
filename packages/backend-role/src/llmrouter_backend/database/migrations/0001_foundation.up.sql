CREATE SCHEMA router;

COMMENT ON SCHEMA router IS 'LLM Router application data';

CREATE DOMAIN router.api_name AS text
    CHECK (VALUE ~ '^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$');

CREATE DOMAIN router.assignment_name AS text
    CHECK (VALUE ~ '^[a-z0-9][a-z0-9._-]{0,126}$');

CREATE TABLE router.services (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    api_name router.api_name NOT NULL UNIQUE,
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    parent_service_id uuid REFERENCES router.services(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (parent_service_id IS DISTINCT FROM id)
);

CREATE FUNCTION router.reject_service_cycle() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    -- Serialize parent changes. This prevents two concurrent, individually valid
    -- moves from creating one cycle after both transactions commit.
    PERFORM pg_advisory_xact_lock(4993044345822);
    IF NEW.parent_service_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        WITH RECURSIVE ancestors(id, parent_service_id) AS (
            SELECT id, parent_service_id
            FROM router.services
            WHERE id = NEW.parent_service_id
          UNION ALL
            SELECT service.id, service.parent_service_id
            FROM router.services AS service
            JOIN ancestors ON service.id = ancestors.parent_service_id
        )
        SELECT 1 FROM ancestors WHERE id = NEW.id
    ) THEN
        RAISE EXCEPTION 'A service parent cycle is not permitted.'
            USING ERRCODE = '23514', CONSTRAINT = 'services_parent_cycle';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER services_reject_parent_cycle
BEFORE INSERT OR UPDATE OF parent_service_id ON router.services
FOR EACH ROW EXECUTE FUNCTION router.reject_service_cycle();

CREATE TABLE router.workspaces (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL REFERENCES router.services(id) ON DELETE CASCADE,
    api_name router.api_name NOT NULL,
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (service_id, api_name),
    UNIQUE (service_id, id)
);

CREATE TABLE router.service_api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL REFERENCES router.services(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    verifier bytea NOT NULL UNIQUE CHECK (octet_length(verifier) = 32),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    last_used_at timestamptz
);

CREATE INDEX service_api_keys_service_created
    ON router.service_api_keys(service_id, created_at, id);

CREATE TABLE router.administrator_oidc_flows (
    state_verifier bytea PRIMARY KEY CHECK (octet_length(state_verifier) = 32),
    encrypted_control bytea NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE INDEX administrator_oidc_flows_expiry
    ON router.administrator_oidc_flows(expires_at);

CREATE TABLE router.administrator_sessions (
    session_verifier bytea PRIMARY KEY CHECK (octet_length(session_verifier) = 32),
    csrf_verifier bytea NOT NULL CHECK (octet_length(csrf_verifier) = 32),
    encrypted_csrf_token bytea NOT NULL,
    issuer text NOT NULL CHECK (char_length(issuer) BETWEEN 1 AND 500),
    subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 500),
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE INDEX administrator_sessions_expiry
    ON router.administrator_sessions(expires_at);

CREATE TABLE router.activity_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_subject text NOT NULL CHECK (char_length(actor_subject) BETWEEN 1 AND 500),
    action text NOT NULL CHECK (char_length(action) BETWEEN 1 AND 200),
    resource_type text NOT NULL CHECK (char_length(resource_type) BETWEEN 1 AND 200),
    service_api_name router.api_name,
    resource_api_name router.api_name,
    resource_id uuid,
    result text NOT NULL CHECK (result IN ('succeeded', 'failed')),
    occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (resource_api_name IS NOT NULL OR resource_id IS NOT NULL)
);

CREATE INDEX activity_events_time ON router.activity_events(occurred_at DESC, id DESC);

CREATE TABLE router.global_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    log_retention_days integer NOT NULL DEFAULT 7
        CHECK (log_retention_days BETWEEN 1 AND 30),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

INSERT INTO router.global_settings (singleton) VALUES (true);

-- These service and workspace ownership roots make later call, accounting,
-- log, and media migrations deletion-safe. A late result cannot recreate data
-- after its service or workspace has been removed because each insert needs the
-- live composite workspace relationship.
CREATE TABLE router.assignment_definitions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL REFERENCES router.services(id) ON DELETE CASCADE,
    api_name router.assignment_name NOT NULL,
    display_name text NOT NULL DEFAULT 'Assignment'
        CHECK (char_length(display_name) BETWEEN 1 AND 200),
    inherits_assignment_api_name router.assignment_name,
    reasoning_level text CHECK (reasoning_level IN ('none', 'low', 'medium', 'high')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (service_id, api_name)
);

CREATE FUNCTION router.text_array_is_unique(input_values text[]) RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT cardinality(input_values) = count(DISTINCT item)
    FROM unnest(input_values) AS item
$$;

CREATE TABLE router.assignment_usage (
    service_id uuid NOT NULL REFERENCES router.services(id) ON DELETE CASCADE,
    api_name router.assignment_name NOT NULL,
    observed_requirements text[] NOT NULL DEFAULT '{}',
    last_used_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (service_id, api_name),
    CHECK (observed_requirements <@ ARRAY[
        'text_input', 'image_input', 'text_output',
        'structured_json_output', 'tool_calling', 'streaming', 'reasoning',
        'embedding_output', 'image_output', 'video_output', 'audio_output'
    ]::text[]),
    CHECK (router.text_array_is_unique(observed_requirements))
);

CREATE TABLE router.provider_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    api_name router.api_name NOT NULL UNIQUE,
    encrypted_secret bytea NOT NULL CHECK (octet_length(encrypted_secret) BETWEEN 90 AND 40100),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{12}$'),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE router.provider_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    api_name router.api_name NOT NULL UNIQUE,
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    adapter text NOT NULL CHECK (adapter IN (
        'openai', 'openai_compatible', 'openrouter', 'custom', 'wavespeed',
        'ollama', 'local_embeddings', 'fake'
    )),
    endpoint text CHECK (char_length(endpoint) BETWEEN 1 AND 4096),
    credential_id uuid REFERENCES router.provider_credentials(id) ON DELETE RESTRICT,
    enabled boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE router.canonical_models (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    api_name router.api_name NOT NULL UNIQUE,
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    input_modalities text[] NOT NULL CHECK (cardinality(input_modalities) BETWEEN 1 AND 2),
    output_modalities text[] NOT NULL CHECK (cardinality(output_modalities) BETWEEN 1 AND 6),
    capabilities text[] NOT NULL CHECK (cardinality(capabilities) BETWEEN 0 AND 3),
    constraints jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(constraints) = 'object'),
    price_source text CHECK (char_length(price_source) BETWEEN 1 AND 500),
    price_lookup_key text CHECK (char_length(price_lookup_key) BETWEEN 1 AND 500),
    manual_price jsonb CHECK (manual_price IS NULL OR jsonb_typeof(manual_price) = 'object'),
    synchronized_price jsonb CHECK (
        synchronized_price IS NULL OR jsonb_typeof(synchronized_price) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK ((price_source IS NULL) = (price_lookup_key IS NULL)),
    CHECK (price_source IS NULL OR manual_price IS NULL),
    CHECK (synchronized_price IS NULL OR price_source IS NOT NULL)
);

CREATE TABLE router.provider_models (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    api_name router.api_name NOT NULL UNIQUE,
    provider_id uuid NOT NULL REFERENCES router.provider_connections(id) ON DELETE RESTRICT,
    model_id uuid NOT NULL REFERENCES router.canonical_models(id) ON DELETE RESTRICT,
    provider_model_name text NOT NULL CHECK (char_length(provider_model_name) BETWEEN 1 AND 500),
    enabled boolean NOT NULL,
    input_modalities text[] NOT NULL,
    output_modalities text[] NOT NULL,
    capabilities text[] NOT NULL,
    constraints jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(constraints) = 'object'),
    reasoning_mappings jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(reasoning_mappings) = 'array' AND jsonb_array_length(reasoning_mappings) <= 4),
    price_source text CHECK (char_length(price_source) BETWEEN 1 AND 500),
    price_lookup_key text CHECK (char_length(price_lookup_key) BETWEEN 1 AND 500),
    manual_price jsonb CHECK (manual_price IS NULL OR jsonb_typeof(manual_price) = 'object'),
    synchronized_price jsonb CHECK (
        synchronized_price IS NULL OR jsonb_typeof(synchronized_price) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (provider_id, provider_model_name),
    CHECK ((price_source IS NULL) = (price_lookup_key IS NULL)),
    CHECK (price_source IS NULL OR manual_price IS NULL),
    CHECK (synchronized_price IS NULL OR price_source IS NOT NULL)
);

CREATE TABLE router.assignment_candidates (
    assignment_id uuid NOT NULL REFERENCES router.assignment_definitions(id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position BETWEEN 0 AND 15),
    provider_model_id uuid NOT NULL REFERENCES router.provider_models(id) ON DELETE RESTRICT,
    PRIMARY KEY (assignment_id, position),
    UNIQUE (assignment_id, provider_model_id)
);

CREATE TABLE router.request_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    logical_call_id uuid UNIQUE NOT NULL,
    call_actor text NOT NULL DEFAULT 'service'
        CHECK (call_actor IN ('service', 'administrator')),
    service_id uuid,
    workspace_id uuid,
    administrator_subject text CHECK (
        administrator_subject IS NULL OR char_length(administrator_subject) BETWEEN 1 AND 500
    ),
    configuration_service_api_name router.api_name,
    assignment_api_name router.assignment_name,
    provider_model_api_name router.api_name,
    kind text NOT NULL CHECK (kind IN ('model', 'embedding', 'media')),
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    tags jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(tags) = 'array'),
    request_json text NOT NULL CHECK (char_length(request_json) <= 5000000),
    response_json text CHECK (char_length(response_json) <= 10000000),
    attempts jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(attempts) = 'array'
               AND jsonb_array_length(attempts) <= 16),
    started_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (service_id, workspace_id, id),
    UNIQUE (call_actor, id),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE,
    CHECK (
        (call_actor = 'service' AND service_id IS NOT NULL
         AND workspace_id IS NOT NULL AND administrator_subject IS NULL
         AND configuration_service_api_name IS NULL)
        OR
        (call_actor = 'administrator' AND service_id IS NULL
         AND workspace_id IS NULL AND administrator_subject IS NOT NULL
         AND logical_call_id IS NOT NULL)
    ),
    CHECK (
        call_actor = 'service'
        OR (
            ((assignment_api_name IS NULL) =
             (configuration_service_api_name IS NULL))
            AND (assignment_api_name IS NOT NULL
                 OR provider_model_api_name IS NOT NULL)
        )
    )
);

CREATE INDEX request_logs_time
    ON router.request_logs(started_at DESC, id DESC);
CREATE INDEX request_logs_scope_time
    ON router.request_logs(service_id, workspace_id, started_at DESC, id DESC);

CREATE FUNCTION router.reject_request_log_update() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    RAISE EXCEPTION 'Detailed request logs are immutable.'
        USING ERRCODE = '23514', CONSTRAINT = 'request_log_immutable';
END;
$$;

CREATE TRIGGER request_logs_reject_update
BEFORE UPDATE ON router.request_logs
FOR EACH ROW EXECUTE FUNCTION router.reject_request_log_update();

CREATE TABLE router.raw_accounting_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_actor text NOT NULL DEFAULT 'service'
        CHECK (call_actor IN ('service', 'administrator')),
    service_id uuid,
    workspace_id uuid,
    administrator_subject text CHECK (
        administrator_subject IS NULL OR char_length(administrator_subject) BETWEEN 1 AND 500
    ),
    configuration_service_api_name router.api_name,
    assignment_api_name router.assignment_name,
    exact_provider_model_api_name router.api_name,
    kind text NOT NULL DEFAULT 'model'
        CHECK (kind IN ('model', 'embedding', 'media')),
    tags text[] NOT NULL DEFAULT '{}',
    outcome text CHECK (outcome IN ('succeeded', 'failed')),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    selection_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(selection_snapshot) = 'object'
        AND octet_length(selection_snapshot::text) <= 1000000
    ),
    CHECK ((outcome IS NULL) = (completed_at IS NULL)),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK (cardinality(tags) BETWEEN 0 AND 32),
    CHECK (router.text_array_is_unique(tags)),
    UNIQUE (service_id, workspace_id, id),
    UNIQUE (call_actor, id),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE,
    CHECK (
        (call_actor = 'service' AND service_id IS NOT NULL
         AND workspace_id IS NOT NULL AND administrator_subject IS NULL
         AND configuration_service_api_name IS NULL)
        OR
        (call_actor = 'administrator' AND service_id IS NULL
         AND workspace_id IS NULL AND administrator_subject IS NOT NULL)
    ),
    CHECK (
        call_actor = 'service'
        OR ((assignment_api_name IS NULL) <> (exact_provider_model_api_name IS NULL))
    ),
    CHECK (
        (call_actor = 'administrator' AND assignment_api_name IS NOT NULL)
        = (configuration_service_api_name IS NOT NULL)
    )
);

CREATE INDEX raw_accounting_calls_scope_time
    ON router.raw_accounting_calls(service_id, started_at, id);

ALTER TABLE router.request_logs
    ADD CONSTRAINT request_logs_actor_logical_call_fkey
    FOREIGN KEY (call_actor, logical_call_id)
    REFERENCES router.raw_accounting_calls(call_actor, id) ON DELETE CASCADE;

CREATE TABLE router.raw_accounting_attempts (
    id uuid PRIMARY KEY,
    call_id uuid NOT NULL,
    call_actor text NOT NULL DEFAULT 'service'
        CHECK (call_actor IN ('service', 'administrator')),
    service_id uuid,
    workspace_id uuid,
    position integer NOT NULL CHECK (position BETWEEN 0 AND 15),
    provider_connection_api_name router.api_name NOT NULL,
    provider_model_api_name router.api_name NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    usage jsonb CHECK (usage IS NULL OR jsonb_typeof(usage) = 'array'),
    applied_price jsonb NOT NULL CHECK (jsonb_typeof(applied_price) = 'object'),
    cost numeric(76, 36) CHECK (cost >= 0),
    currency char(3) CHECK (currency ~ '^[A-Z]{3}$'),
    failure_class text CHECK (failure_class IN (
        'authentication', 'rate_limited', 'timeout', 'transport',
        'unavailable', 'refusal', 'incompatible', 'invalid_response',
        'interrupted', 'upstream_failed'
    )),
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (completed_at >= started_at),
    CHECK ((usage IS NULL) = (cost IS NULL)),
    CHECK ((cost IS NULL) = (currency IS NULL)),
    CHECK ((outcome = 'succeeded') = (failure_class IS NULL)),
    UNIQUE (call_id, position),
    FOREIGN KEY (call_id) REFERENCES router.raw_accounting_calls(id)
        ON DELETE CASCADE,
    FOREIGN KEY (call_actor, call_id)
        REFERENCES router.raw_accounting_calls(call_actor, id)
        ON DELETE CASCADE,
    FOREIGN KEY (service_id, workspace_id, call_id)
        REFERENCES router.raw_accounting_calls(service_id, workspace_id, id)
        ON DELETE CASCADE,
    CHECK ((service_id IS NULL) = (workspace_id IS NULL)),
    CHECK (
        (call_actor = 'service' AND service_id IS NOT NULL
         AND workspace_id IS NOT NULL)
        OR
        (call_actor = 'administrator' AND service_id IS NULL
         AND workspace_id IS NULL)
    )
);


CREATE INDEX raw_accounting_attempts_scope_time
    ON router.raw_accounting_attempts(service_id, started_at, id);

CREATE FUNCTION router.reject_raw_accounting_update() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    IF OLD.outcome IS NULL
       AND NEW.outcome IN ('succeeded', 'failed')
       AND NEW.completed_at IS NOT NULL
       AND NEW.id = OLD.id
       AND NEW.call_actor = OLD.call_actor
       AND NEW.service_id IS NOT DISTINCT FROM OLD.service_id
       AND NEW.workspace_id IS NOT DISTINCT FROM OLD.workspace_id
       AND NEW.administrator_subject IS NOT DISTINCT FROM OLD.administrator_subject
       AND NEW.configuration_service_api_name IS NOT DISTINCT FROM OLD.configuration_service_api_name
       AND NEW.assignment_api_name IS NOT DISTINCT FROM OLD.assignment_api_name
       AND NEW.exact_provider_model_api_name IS NOT DISTINCT FROM OLD.exact_provider_model_api_name
       AND NEW.kind = OLD.kind
       AND NEW.tags = OLD.tags
       AND NEW.started_at = OLD.started_at
       AND NEW.selection_snapshot = OLD.selection_snapshot THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Raw accounting facts are immutable.'
        USING ERRCODE = '23514', CONSTRAINT = 'raw_accounting_immutable';
END;
$$;

CREATE TRIGGER raw_accounting_calls_reject_update
BEFORE UPDATE ON router.raw_accounting_calls
FOR EACH ROW EXECUTE FUNCTION router.reject_raw_accounting_update();

CREATE FUNCTION router.reject_raw_accounting_attempt_update() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    RAISE EXCEPTION 'Raw accounting facts are immutable.'
        USING ERRCODE = '23514', CONSTRAINT = 'raw_accounting_immutable';
END;
$$;

CREATE TRIGGER raw_accounting_attempts_reject_update
BEFORE UPDATE ON router.raw_accounting_attempts
FOR EACH ROW EXECUTE FUNCTION router.reject_raw_accounting_attempt_update();

CREATE TABLE router.daily_accounting (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_actor text NOT NULL DEFAULT 'service'
        CHECK (call_actor IN ('service', 'administrator')),
    service_id uuid,
    workspace_id uuid,
    administrator_subject text CHECK (
        administrator_subject IS NULL OR char_length(administrator_subject) BETWEEN 1 AND 500
    ),
    configuration_service_api_name router.api_name,
    day date NOT NULL,
    assignment_api_name router.assignment_name,
    exact_provider_model_api_name router.api_name,
    provider_model_api_name router.api_name,
    outcome text CHECK (outcome IN ('succeeded', 'failed')),
    tags text[] NOT NULL,
    usage_unit text CHECK (usage_unit IN (
        'input_token', 'output_token', 'cached_input_token', 'image',
        'video_second', 'audio_second', 'request', 'provider_unit'
    )),
    currency char(3) CHECK (currency ~ '^[A-Z]{3}$'),
    calls bigint NOT NULL CHECK (calls >= 0),
    attempts bigint NOT NULL CHECK (attempts >= 0),
    quantity numeric(76, 18) NOT NULL CHECK (quantity >= 0),
    cost numeric(112, 36) CHECK (cost >= 0),
    rolled_up_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE NULLS NOT DISTINCT (
        call_actor, service_id, workspace_id, administrator_subject,
        configuration_service_api_name, day, assignment_api_name,
        exact_provider_model_api_name,
        provider_model_api_name, outcome, tags, usage_unit, currency
    ),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE,
    CHECK (
        (call_actor = 'service' AND service_id IS NOT NULL
         AND workspace_id IS NOT NULL AND administrator_subject IS NULL
         AND configuration_service_api_name IS NULL)
        OR
        (call_actor = 'administrator' AND service_id IS NULL
         AND workspace_id IS NULL AND administrator_subject IS NOT NULL)
    )
);

CREATE INDEX daily_accounting_scope_day
    ON router.daily_accounting(service_id, day);

CREATE TABLE router.accounting_rollups (
    day date PRIMARY KEY,
    completed_at timestamptz NOT NULL,
    attempt_count bigint NOT NULL CHECK (attempt_count >= 0),
    call_count bigint NOT NULL DEFAULT 0 CHECK (call_count >= 0)
);

CREATE TABLE router.price_synchronizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempted_at timestamptz NOT NULL,
    run_kind text NOT NULL CHECK (run_kind IN ('scheduled', 'on_demand')),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'array'),
    completed boolean NOT NULL,
    failure_class text CHECK (char_length(failure_class) BETWEEN 1 AND 200)
);

CREATE UNIQUE INDEX price_synchronizations_one_scheduled_utc_day
    ON router.price_synchronizations (
        ((attempted_at AT TIME ZONE 'UTC')::date)
    ) WHERE run_kind = 'scheduled';

CREATE TABLE router.media_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    logical_call_id uuid UNIQUE,
    call_actor text NOT NULL DEFAULT 'service'
        CHECK (call_actor IN ('service', 'administrator')),
    service_id uuid,
    workspace_id uuid,
    administrator_subject text CHECK (
        administrator_subject IS NULL OR char_length(administrator_subject) BETWEEN 1 AND 500
    ),
    configuration_service_api_name router.api_name,
    assignment_api_name router.assignment_name,
    exact_provider_model_api_name router.api_name,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    provider_model_api_name router.api_name NOT NULL,
    kind text NOT NULL DEFAULT 'image' CHECK (kind IN ('image', 'video', 'audio')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(payload) = 'object'
        AND octet_length(payload::text) BETWEEN 2 AND 2097152
    ),
    error_code text CHECK (char_length(error_code) BETWEEN 1 AND 200),
    error_message text CHECK (char_length(error_message) BETWEEN 1 AND 1000),
    attempts jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(attempts) = 'array' AND jsonb_array_length(attempts) <= 16
    ),
    elapsed_ms integer CHECK (elapsed_ms BETWEEN 0 AND 86400000),
    usage jsonb CHECK (usage IS NULL OR jsonb_typeof(usage) = 'object'),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    deadline_at timestamptz NOT NULL DEFAULT transaction_timestamp() + interval '24 hours',
    completed_at timestamptz,
    CHECK (deadline_at > created_at AND deadline_at <= created_at + interval '24 hours'),
    CHECK (completed_at IS NULL OR completed_at >= created_at),
    CHECK ((error_code IS NULL) = (error_message IS NULL)),
    CHECK (state IN ('pending', 'running') OR payload = '{}'::jsonb),
    CHECK (
        (state IN ('pending', 'running') AND error_code IS NULL AND completed_at IS NULL)
        OR (state = 'succeeded' AND error_code IS NULL AND completed_at IS NOT NULL)
        OR (state = 'failed' AND error_code IS NOT NULL AND completed_at IS NOT NULL)
    ),
    UNIQUE (service_id, workspace_id, id),
    UNIQUE (call_actor, id),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE,
    FOREIGN KEY (call_actor, logical_call_id)
        REFERENCES router.raw_accounting_calls(call_actor, id),
    CHECK (
        (call_actor = 'service' AND service_id IS NOT NULL
         AND workspace_id IS NOT NULL AND administrator_subject IS NULL
         AND logical_call_id IS NULL)
        OR
        (call_actor = 'administrator' AND service_id IS NULL
         AND workspace_id IS NULL AND administrator_subject IS NOT NULL
         AND logical_call_id IS NOT NULL)
    ),
    CHECK (
        call_actor = 'service'
        OR ((assignment_api_name IS NULL) <>
            (exact_provider_model_api_name IS NULL))
    ),
    CHECK (
        (call_actor = 'administrator' AND assignment_api_name IS NOT NULL)
        = (configuration_service_api_name IS NOT NULL)
    )
);

CREATE INDEX media_jobs_pending_deadline
    ON router.media_jobs(state, deadline_at, created_at, id);

CREATE FUNCTION router.enforce_media_job_state_transition() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    IF ROW(
        NEW.id, NEW.logical_call_id, NEW.call_actor, NEW.service_id,
        NEW.workspace_id, NEW.administrator_subject,
        NEW.configuration_service_api_name, NEW.assignment_api_name,
        NEW.exact_provider_model_api_name, NEW.kind, NEW.created_at,
        NEW.deadline_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.logical_call_id, OLD.call_actor, OLD.service_id,
        OLD.workspace_id, OLD.administrator_subject,
        OLD.configuration_service_api_name, OLD.assignment_api_name,
        OLD.exact_provider_model_api_name, OLD.kind, OLD.created_at,
        OLD.deadline_at
    ) THEN
        RAISE EXCEPTION 'A media job identity and selection are immutable.'
            USING ERRCODE = '23514', CONSTRAINT = 'media_jobs_identity_immutable';
    END IF;
    IF OLD.state IN ('succeeded', 'failed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'A terminal media job cannot change.'
            USING ERRCODE = '23514', CONSTRAINT = 'media_jobs_terminal_immutable';
    END IF;
    IF NEW.state IN ('pending', 'running') AND ROW(
        NEW.provider_model_api_name, NEW.payload, NEW.error_code,
        NEW.error_message, NEW.attempts, NEW.elapsed_ms, NEW.usage,
        NEW.completed_at
    ) IS DISTINCT FROM ROW(
        OLD.provider_model_api_name, OLD.payload, OLD.error_code,
        OLD.error_message, OLD.attempts, OLD.elapsed_ms, OLD.usage,
        OLD.completed_at
    ) THEN
        RAISE EXCEPTION 'A nonterminal media job can change only its state.'
            USING ERRCODE = '23514', CONSTRAINT = 'media_jobs_nonterminal_immutable';
    END IF;
    IF (NEW.state = 'failed' AND (
            NEW.error_code IS NULL OR NEW.completed_at IS NULL
        )) OR (NEW.state = 'succeeded' AND (
            NEW.error_code IS NOT NULL OR NEW.completed_at IS NULL
        )) OR (NEW.state IN ('pending', 'running') AND (
            NEW.error_code IS NOT NULL OR NEW.completed_at IS NOT NULL
        )) THEN
        RAISE EXCEPTION 'A media job result does not match its state.'
            USING ERRCODE = '23514', CONSTRAINT = 'media_jobs_state_result';
    END IF;
    IF NEW.state = OLD.state
        OR (OLD.state = 'pending'
            AND NEW.state IN ('running', 'succeeded', 'failed'))
        OR (OLD.state = 'running'
            AND NEW.state IN ('succeeded', 'failed')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'A media job state must move forward.'
        USING ERRCODE = '23514', CONSTRAINT = 'media_jobs_state_transition';
END;
$$;

CREATE TRIGGER media_jobs_enforce_state_transition
BEFORE UPDATE ON router.media_jobs
FOR EACH ROW EXECUTE FUNCTION router.enforce_media_job_state_transition();

CREATE TABLE router.media_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_actor text NOT NULL DEFAULT 'service'
        CHECK (call_actor IN ('service', 'administrator')),
    service_id uuid,
    workspace_id uuid,
    media_job_id uuid,
    request_log_id uuid,
    object_key text NOT NULL UNIQUE CHECK (octet_length(object_key) BETWEEN 1 AND 1024),
    media_type text NOT NULL CHECK (char_length(media_type) BETWEEN 1 AND 200),
    role text NOT NULL CHECK (role IN ('input', 'output')),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 1073741824),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE,
    FOREIGN KEY (request_log_id)
        REFERENCES router.request_logs(id) ON DELETE CASCADE,
    FOREIGN KEY (media_job_id)
        REFERENCES router.media_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY (call_actor, request_log_id)
        REFERENCES router.request_logs(call_actor, id) ON DELETE CASCADE,
    FOREIGN KEY (call_actor, media_job_id)
        REFERENCES router.media_jobs(call_actor, id) ON DELETE CASCADE,
    FOREIGN KEY (service_id, workspace_id, request_log_id)
        REFERENCES router.request_logs(service_id, workspace_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (service_id, workspace_id, media_job_id)
        REFERENCES router.media_jobs(service_id, workspace_id, id)
        ON DELETE CASCADE,
    CHECK ((service_id IS NULL) = (workspace_id IS NULL)),
    CHECK (
        (call_actor = 'service' AND service_id IS NOT NULL
         AND workspace_id IS NOT NULL)
        OR
        (call_actor = 'administrator' AND service_id IS NULL
         AND workspace_id IS NULL)
    ),
    CHECK ((media_job_id IS NULL) <> (request_log_id IS NULL))
);

CREATE INDEX media_objects_request_log
    ON router.media_objects(request_log_id, id);
CREATE INDEX media_objects_scope_time
    ON router.media_objects(service_id, workspace_id, created_at, id);
CREATE UNIQUE INDEX media_objects_one_job_output
    ON router.media_objects(media_job_id)
    WHERE media_job_id IS NOT NULL AND role = 'output';

-- Rows remain after public metadata is removed. This lets object deletion
-- finish after a service, workspace, log, or retention delete commits.
CREATE TABLE router.object_deletion_queue (
    object_key text PRIMARY KEY CHECK (octet_length(object_key) BETWEEN 1 AND 1024),
    queued_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    last_attempt_at timestamptz,
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    failure_class text CHECK (char_length(failure_class) BETWEEN 1 AND 200)
);

CREATE INDEX object_deletion_queue_age
    ON router.object_deletion_queue(queued_at, object_key);

CREATE FUNCTION router.queue_media_object_deletion() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    INSERT INTO router.object_deletion_queue (object_key)
    VALUES (OLD.object_key)
    ON CONFLICT (object_key) DO NOTHING;
    RETURN OLD;
END;
$$;

CREATE TRIGGER media_objects_queue_deletion
AFTER DELETE ON router.media_objects
FOR EACH ROW EXECUTE FUNCTION router.queue_media_object_deletion();
