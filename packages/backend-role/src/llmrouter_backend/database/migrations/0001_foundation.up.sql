CREATE SCHEMA router;

COMMENT ON SCHEMA router IS 'LLM Router application data';

CREATE DOMAIN router.api_name AS text
    CHECK (VALUE ~ '^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$');

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

-- These service and workspace ownership roots make later call, accounting,
-- log, and media migrations deletion-safe. A late result cannot recreate data
-- after its service or workspace has been removed because each insert needs the
-- live composite workspace relationship.
CREATE TABLE router.assignment_definitions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL REFERENCES router.services(id) ON DELETE CASCADE,
    api_name text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (service_id, api_name)
);

CREATE TABLE router.request_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE
);

CREATE TABLE router.raw_accounting (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE
);

CREATE TABLE router.daily_accounting (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    day date NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE
);

CREATE TABLE router.media_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (service_id, workspace_id, id),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE
);

CREATE TABLE router.media_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    media_job_id uuid,
    object_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (service_id, workspace_id)
        REFERENCES router.workspaces(service_id, id) ON DELETE CASCADE,
    FOREIGN KEY (service_id, workspace_id, media_job_id)
        REFERENCES router.media_jobs(service_id, workspace_id, id) ON DELETE CASCADE
);
