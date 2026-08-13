ALTER TABLE router.services
ADD COLUMN display_name text;

ALTER TABLE router.services DISABLE TRIGGER services_terminal_state;
UPDATE router.services
SET display_name = stable_name;
ALTER TABLE router.services ENABLE TRIGGER services_terminal_state;

ALTER TABLE router.services
ALTER COLUMN display_name SET NOT NULL,
ADD CONSTRAINT services_display_name_length
    CHECK (char_length(display_name) BETWEEN 1 AND 200);

ALTER TABLE router.workspaces
ADD COLUMN display_name text;

ALTER TABLE router.workspaces DISABLE TRIGGER workspaces_terminal_state;
UPDATE router.workspaces
SET display_name = caller_reference;
ALTER TABLE router.workspaces ENABLE TRIGGER workspaces_terminal_state;

ALTER TABLE router.workspaces
ALTER COLUMN display_name SET NOT NULL,
ADD CONSTRAINT workspaces_display_name_length
    CHECK (char_length(display_name) BETWEEN 1 AND 200),
ADD CONSTRAINT workspaces_caller_reference_length
    CHECK (char_length(caller_reference) BETWEEN 1 AND 200);

CREATE FUNCTION router.fill_lifecycle_display_name()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.display_name IS NULL THEN
        IF TG_TABLE_NAME = 'services' THEN
            NEW.display_name := NEW.stable_name;
        ELSE
            NEW.display_name := NEW.caller_reference;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER services_fill_display_name
BEFORE INSERT ON router.services
FOR EACH ROW EXECUTE FUNCTION router.fill_lifecycle_display_name();

CREATE TRIGGER workspaces_fill_display_name
BEFORE INSERT ON router.workspaces
FOR EACH ROW EXECUTE FUNCTION router.fill_lifecycle_display_name();

CREATE FUNCTION router.reject_lifecycle_identity_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% identity cannot be deleted', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER services_no_delete
BEFORE DELETE ON router.services
FOR EACH ROW EXECUTE FUNCTION router.reject_lifecycle_identity_delete();

CREATE TRIGGER workspaces_no_delete
BEFORE DELETE ON router.workspaces
FOR EACH ROW EXECUTE FUNCTION router.reject_lifecycle_identity_delete();

CREATE OR REPLACE FUNCTION router.protect_terminal_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION '% retirement is terminal', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    IF NEW.state_revision <> OLD.state_revision + 1 THEN
        RAISE EXCEPTION '% state revision must increase by one', TG_TABLE_NAME
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE router.service_lifecycle_operations (
    operation_id uuid PRIMARY KEY,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    actor_id text NOT NULL CHECK (actor_id <> ''),
    action text NOT NULL CHECK (action IN (
        'service.create', 'service.parent', 'service.disable',
        'service.restore', 'service.retire'
    )),
    idempotency_key text,
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    resulting_display_name text NOT NULL
        CHECK (char_length(resulting_display_name) BETWEEN 1 AND 200),
    resulting_state text NOT NULL
        CHECK (resulting_state IN ('active', 'disabled', 'retired')),
    resulting_revision bigint NOT NULL CHECK (resulting_revision > 0),
    resulting_parent_service_id uuid
        REFERENCES router.services (id) ON DELETE RESTRICT,
    changed boolean NOT NULL,
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) DEFERRABLE INITIALLY DEFERRED,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (idempotency_key IS NULL OR char_length(idempotency_key) BETWEEN 16 AND 200)
);

CREATE UNIQUE INDEX service_lifecycle_idempotency_idx
ON router.service_lifecycle_operations (actor_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX service_lifecycle_target_idx
ON router.service_lifecycle_operations (service_id, created_at DESC, operation_id DESC);

CREATE TABLE router.workspace_lifecycle_operations (
    operation_id uuid PRIMARY KEY,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid NOT NULL,
    actor_id text NOT NULL CHECK (actor_id <> ''),
    action text NOT NULL CHECK (action IN (
        'workspace.create', 'workspace.disable',
        'workspace.restore', 'workspace.retire'
    )),
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    resulting_caller_reference text NOT NULL
        CHECK (char_length(resulting_caller_reference) BETWEEN 1 AND 200),
    resulting_display_name text NOT NULL
        CHECK (char_length(resulting_display_name) BETWEEN 1 AND 200),
    resulting_state text NOT NULL
        CHECK (resulting_state IN ('active', 'disabled', 'retired')),
    resulting_revision bigint NOT NULL CHECK (resulting_revision > 0),
    changed boolean NOT NULL,
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) DEFERRABLE INITIALLY DEFERRED,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    UNIQUE (service_id, idempotency_key)
);

CREATE INDEX workspace_lifecycle_target_idx
ON router.workspace_lifecycle_operations (
    service_id, workspace_id, created_at DESC, operation_id DESC
);

CREATE TABLE router.workspace_lifecycle_idempotency_bindings (
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    operation_id uuid NOT NULL
        REFERENCES router.workspace_lifecycle_operations (operation_id)
        ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (service_id, idempotency_key)
);

CREATE TRIGGER service_lifecycle_operations_append_only
BEFORE UPDATE OR DELETE ON router.service_lifecycle_operations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER workspace_lifecycle_operations_append_only
BEFORE UPDATE OR DELETE ON router.workspace_lifecycle_operations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER workspace_lifecycle_idempotency_bindings_append_only
BEFORE UPDATE OR DELETE ON router.workspace_lifecycle_idempotency_bindings
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.lifecycle_admission_is_allowed(
    requested_service_id uuid,
    requested_workspace_id uuid DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    service_is_active boolean;
    workspace_is_active boolean;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(hashtextextended('service-parent-tree', 0));
    WITH RECURSIVE ancestors AS (
        SELECT id, parent_service_id, state
        FROM router.services
        WHERE id = requested_service_id
      UNION ALL
        SELECT parent.id, parent.parent_service_id, parent.state
        FROM router.services AS parent
        JOIN ancestors AS child ON parent.id = child.parent_service_id
    )
    SELECT count(*) > 0 AND bool_and(state = 'active')
    INTO service_is_active
    FROM ancestors;

    IF NOT COALESCE(service_is_active, false) THEN
        RETURN false;
    END IF;
    IF requested_workspace_id IS NULL THEN
        RETURN true;
    END IF;
    SELECT state = 'active'
    INTO workspace_is_active
    FROM router.workspaces
    WHERE id = requested_workspace_id AND service_id = requested_service_id
    FOR SHARE;
    RETURN COALESCE(workspace_is_active, false);
END;
$$;
