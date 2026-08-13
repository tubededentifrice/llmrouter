DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.credential_idempotency_bindings)
       OR EXISTS (SELECT 1 FROM router.credential_urgent_invalidations) THEN
        RAISE EXCEPTION 'credential store data exists; cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP TRIGGER credential_urgent_invalidations_append_only
ON router.credential_urgent_invalidations;
DROP TRIGGER credential_idempotency_bindings_append_only
ON router.credential_idempotency_bindings;
DROP TABLE router.credential_urgent_invalidations;
DROP TABLE router.credential_idempotency_bindings;

DROP TRIGGER encrypted_credentials_change_guard
ON router.encrypted_credentials;
DROP FUNCTION router.protect_encrypted_credential_change();

ALTER TABLE router.encrypted_credentials
DROP CONSTRAINT encrypted_credentials_safe_label_bound,
DROP COLUMN last_changed_at,
DROP COLUMN safe_label,
DROP COLUMN current_revision;

CREATE TRIGGER encrypted_credentials_identity_generation
BEFORE UPDATE ON router.encrypted_credentials
FOR EACH ROW EXECUTE FUNCTION router.protect_provider_identity_and_generation();
