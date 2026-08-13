ALTER TABLE router.encrypted_credentials
ADD COLUMN current_revision uuid,
ADD COLUMN safe_label text,
ADD COLUMN last_changed_at timestamptz;

UPDATE router.encrypted_credentials
SET current_revision = id,
    last_changed_at = created_at;

ALTER TABLE router.encrypted_credentials
ALTER COLUMN current_revision SET NOT NULL,
ALTER COLUMN last_changed_at SET NOT NULL,
ADD CONSTRAINT encrypted_credentials_safe_label_bound
    CHECK (safe_label IS NULL OR char_length(safe_label) <= 200);

DROP TRIGGER encrypted_credentials_identity_generation
ON router.encrypted_credentials;

CREATE FUNCTION router.protect_encrypted_credential_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.owner_kind <> OLD.owner_kind
       OR NEW.owner_service_id IS DISTINCT FROM OLD.owner_service_id
       OR NEW.credential_kind <> OLD.credential_kind
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'encrypted credential identity and scope are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'encrypted credential retirement is terminal'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.generation = OLD.generation THEN
        IF NEW.state <> OLD.state
           OR NEW.ciphertext <> OLD.ciphertext
           OR NEW.safe_fingerprint <> OLD.safe_fingerprint
           OR NEW.current_revision <> OLD.current_revision
           OR NEW.safe_label IS DISTINCT FROM OLD.safe_label
           OR NEW.last_changed_at <> OLD.last_changed_at
           OR NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
            RAISE EXCEPTION 'credential wrapping-key changes cannot change public state'
                USING ERRCODE = '40001';
        END IF;
    ELSIF NEW.generation <> OLD.generation + 1
          OR NEW.current_revision = OLD.current_revision
          OR NEW.last_changed_at < OLD.last_changed_at THEN
        RAISE EXCEPTION 'encrypted credential generation must increase by one'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER encrypted_credentials_change_guard
BEFORE UPDATE ON router.encrypted_credentials
FOR EACH ROW EXECUTE FUNCTION router.protect_encrypted_credential_change();

CREATE TABLE router.credential_idempotency_bindings (
    actor_id text NOT NULL CHECK (actor_id <> ''),
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    credential_id uuid NOT NULL
        REFERENCES router.encrypted_credentials (id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (actor_id, idempotency_key)
);

CREATE TABLE router.credential_urgent_invalidations (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    credential_id uuid NOT NULL
        REFERENCES router.encrypted_credentials (id) ON DELETE RESTRICT,
    generation bigint NOT NULL CHECK (generation > 1),
    action text NOT NULL CHECK (action IN ('rotate', 'disable', 'retire')),
    occurred_at timestamptz NOT NULL,
    UNIQUE (credential_id, generation)
);

CREATE INDEX credential_urgent_invalidations_credential_idx
ON router.credential_urgent_invalidations (credential_id, sequence);

CREATE TRIGGER credential_idempotency_bindings_append_only
BEFORE UPDATE OR DELETE ON router.credential_idempotency_bindings
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER credential_urgent_invalidations_append_only
BEFORE UPDATE OR DELETE ON router.credential_urgent_invalidations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();
