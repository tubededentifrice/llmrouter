ALTER TABLE router.administrators
ADD COLUMN identity_generation bigint NOT NULL DEFAULT 1
    CHECK (identity_generation > 0);

ALTER TABLE router.administrator_grants
ADD COLUMN workspace_ids uuid[] NOT NULL DEFAULT '{}',
ADD CONSTRAINT administrator_grants_workspace_ids_limit
    CHECK (cardinality(workspace_ids) <= 1000);

UPDATE router.administrator_grants
SET workspace_ids = ARRAY[workspace_id]
WHERE workspace_id IS NOT NULL;

ALTER TABLE router.administrator_sessions
ADD COLUMN recent_authenticated_at timestamptz,
ADD COLUMN identity_generation bigint NOT NULL DEFAULT 1
    CHECK (identity_generation > 0);

CREATE TABLE router.administrator_oidc_starts (
    id uuid PRIMARY KEY,
    state_digest bytea NOT NULL UNIQUE CHECK (octet_length(state_digest) = 32),
    nonce_digest bytea NOT NULL CHECK (octet_length(nonce_digest) = 32),
    pkce_verifier_ciphertext bytea NOT NULL
        CHECK (octet_length(pkce_verifier_ciphertext) = 83),
    purpose text NOT NULL CHECK (purpose IN ('login', 'recent_authentication')),
    return_path text NOT NULL CHECK (
        return_path ~ '^/[A-Za-z0-9._~!$&''()*+,;=:@%/?-]*$'
        AND char_length(return_path) <= 2000
    ),
    session_id uuid REFERENCES router.administrator_sessions (id) ON DELETE RESTRICT,
    trusted_grant_url_id uuid,
    exact_redirect_uri text NOT NULL CHECK (exact_redirect_uri <> ''),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz,
    CHECK (expires_at > created_at),
    CHECK (redeemed_at IS NULL OR redeemed_at BETWEEN created_at AND expires_at),
    CHECK (
        (purpose = 'login' AND session_id IS NULL)
        OR (purpose = 'recent_authentication' AND session_id IS NOT NULL)
    ),
    CHECK (trusted_grant_url_id IS NULL OR purpose = 'login')
);

CREATE TABLE router.trusted_administrator_grant_urls (
    id uuid PRIMARY KEY,
    verifier_digest bytea NOT NULL UNIQUE CHECK (octet_length(verifier_digest) = 32),
    purpose text NOT NULL CHECK (purpose IN ('initial', 'recovery')),
    operations text[] NOT NULL CHECK (cardinality(operations) > 0),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz,
    redeemed_administrator_id uuid
        REFERENCES router.administrators (id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    CHECK (redeemed_at IS NULL OR redeemed_at BETWEEN created_at AND expires_at),
    CHECK ((redeemed_at IS NULL) = (redeemed_administrator_id IS NULL))
);

CREATE TABLE router.administrator_grant_idempotency_bindings (
    administrator_id uuid NOT NULL
        REFERENCES router.administrators (id) ON DELETE RESTRICT,
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    grant_id uuid NOT NULL
        REFERENCES router.administrator_grants (id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (administrator_id, idempotency_key)
);

ALTER TABLE router.administrator_oidc_starts
ADD CONSTRAINT administrator_oidc_starts_trusted_url_fk
FOREIGN KEY (trusted_grant_url_id)
REFERENCES router.trusted_administrator_grant_urls (id) ON DELETE RESTRICT;

CREATE INDEX administrator_oidc_starts_expiry_idx
ON router.administrator_oidc_starts (expires_at)
WHERE redeemed_at IS NULL;

CREATE INDEX trusted_administrator_grant_urls_expiry_idx
ON router.trusted_administrator_grant_urls (expires_at)
WHERE redeemed_at IS NULL;

CREATE FUNCTION router.validate_administrator_grant_workspace_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF cardinality(NEW.workspace_ids) > 1000 THEN
        RAISE EXCEPTION 'an administrator grant cannot contain more than 1000 workspaces';
    END IF;
    IF cardinality(NEW.operations) <> (
        SELECT count(DISTINCT operation)
        FROM unnest(NEW.operations) AS requested(operation)
    ) THEN
        RAISE EXCEPTION 'an administrator grant cannot contain duplicate operations';
    END IF;
    IF cardinality(NEW.workspace_ids) <> (
        SELECT count(DISTINCT workspace_id)
        FROM unnest(NEW.workspace_ids) AS requested(workspace_id)
    ) THEN
        RAISE EXCEPTION 'an administrator grant cannot contain duplicate workspaces';
    END IF;
    IF NEW.authority_class = 'global' AND cardinality(NEW.workspace_ids) <> 0 THEN
        RAISE EXCEPTION 'a global administrator grant cannot contain workspaces';
    END IF;
    IF (cardinality(NEW.workspace_ids) = 1
            AND NEW.workspace_id IS DISTINCT FROM NEW.workspace_ids[1])
       OR (cardinality(NEW.workspace_ids) <> 1
            AND NEW.workspace_id IS NOT NULL) THEN
        RAISE EXCEPTION 'an administrator grant legacy workspace must match its workspace array';
    END IF;
    IF NEW.authority_class = 'service' AND EXISTS (
        SELECT 1
        FROM unnest(NEW.workspace_ids) AS requested(workspace_id)
        LEFT JOIN router.workspaces AS workspace
          ON workspace.id = requested.workspace_id
         AND workspace.service_id = NEW.service_id
        WHERE workspace.id IS NULL
    ) THEN
        RAISE EXCEPTION 'an administrator grant workspace must belong to its service';
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE router.administrator_grants
ADD CONSTRAINT administrator_grants_time_order
CHECK (
    (expires_at IS NULL OR expires_at > created_at)
    AND (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE FUNCTION router.protect_administrator_oidc_start()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.state_digest <> OLD.state_digest
       OR NEW.nonce_digest <> OLD.nonce_digest
       OR NEW.pkce_verifier_ciphertext <> OLD.pkce_verifier_ciphertext
       OR NEW.purpose <> OLD.purpose
       OR NEW.return_path <> OLD.return_path
       OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.trusted_grant_url_id IS DISTINCT FROM OLD.trusted_grant_url_id
       OR NEW.exact_redirect_uri <> OLD.exact_redirect_uri
       OR NEW.created_at <> OLD.created_at
       OR NEW.expires_at <> OLD.expires_at
       OR OLD.redeemed_at IS NOT NULL
       OR NEW.redeemed_at IS NULL THEN
        RAISE EXCEPTION 'administrator OIDC start fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_trusted_administrator_grant_url()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.verifier_digest <> OLD.verifier_digest
       OR NEW.purpose <> OLD.purpose
       OR NEW.operations <> OLD.operations
       OR NEW.created_at <> OLD.created_at
       OR NEW.expires_at <> OLD.expires_at
       OR OLD.redeemed_at IS NOT NULL
       OR OLD.redeemed_administrator_id IS NOT NULL
       OR NEW.redeemed_at IS NULL
       OR NEW.redeemed_administrator_id IS NULL THEN
        RAISE EXCEPTION 'trusted administrator grant URL fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER administrator_grants_validate_workspace_ids
BEFORE INSERT OR UPDATE ON router.administrator_grants
FOR EACH ROW EXECUTE FUNCTION router.validate_administrator_grant_workspace_ids();

CREATE TRIGGER administrator_oidc_starts_no_delete
BEFORE DELETE ON router.administrator_oidc_starts
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER administrator_oidc_starts_protect_update
BEFORE UPDATE ON router.administrator_oidc_starts
FOR EACH ROW EXECUTE FUNCTION router.protect_administrator_oidc_start();

CREATE TRIGGER trusted_administrator_grant_urls_no_delete
BEFORE DELETE ON router.trusted_administrator_grant_urls
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER trusted_administrator_grant_urls_protect_update
BEFORE UPDATE ON router.trusted_administrator_grant_urls
FOR EACH ROW EXECUTE FUNCTION router.protect_trusted_administrator_grant_url();

CREATE TRIGGER administrator_grant_idempotency_bindings_append_only
BEFORE UPDATE OR DELETE ON router.administrator_grant_idempotency_bindings
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();
