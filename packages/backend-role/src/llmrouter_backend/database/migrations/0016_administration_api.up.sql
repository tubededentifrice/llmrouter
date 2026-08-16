CREATE TABLE router.configuration_write_idempotency_bindings (
    actor_id text NOT NULL,
    operation text NOT NULL,
    scope_key text NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    resource_id text NOT NULL,
    active_revision uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    distribution_state text NOT NULL
        CHECK (distribution_state IN ('distributing', 'current', 'degraded')),
    operation_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (actor_id, operation, scope_key, idempotency_key)
);

CREATE TRIGGER configuration_write_idempotency_bindings_append_only
BEFORE UPDATE OR DELETE ON router.configuration_write_idempotency_bindings
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();
