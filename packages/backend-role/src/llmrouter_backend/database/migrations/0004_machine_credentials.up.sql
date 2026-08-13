ALTER TABLE router.service_bootstrap_generations
ADD COLUMN issuer text,
ADD COLUMN allowed_audiences text[],
ADD COLUMN workspace_limit text;

UPDATE router.service_bootstrap_generations
SET issuer = 'llmrouter',
    allowed_audiences = ARRAY['service_management']::text[],
    workspace_limit = 'all_service_workspaces';

ALTER TABLE router.service_bootstrap_generations
ALTER COLUMN issuer SET NOT NULL,
ALTER COLUMN allowed_audiences SET NOT NULL,
ALTER COLUMN workspace_limit SET NOT NULL,
ADD CONSTRAINT bootstrap_issuer_not_empty CHECK (issuer <> ''),
ADD CONSTRAINT bootstrap_audiences_not_empty
    CHECK (cardinality(allowed_audiences) > 0),
ADD CONSTRAINT bootstrap_audiences_closed CHECK (
    allowed_audiences <@ ARRAY[
        'data_plane', 'service_management', 'host_backend', 'accounting',
        'configuration', 'budget_authority'
    ]::text[]
),
ADD CONSTRAINT bootstrap_operations_match_audiences CHECK (
    allowed_operations <@ (
        (CASE WHEN 'data_plane' = ANY(allowed_audiences) THEN ARRAY[
            'model.create', 'model.read', 'model.cancel', 'run.create',
            'run.read', 'run.cancel', 'tool.create', 'tool.read', 'tool.cancel',
            'attachment.create', 'attachment.read', 'embedding.create',
            'embedding.read'
        ]::text[] ELSE ARRAY[]::text[] END)
        || (CASE WHEN 'service_management' = ANY(allowed_audiences) THEN ARRAY[
            'workspace.create', 'workspace.read', 'workspace.disable',
            'workspace.restore', 'workspace.retire'
        ]::text[] ELSE ARRAY[]::text[] END)
        || (CASE WHEN 'host_backend' = ANY(allowed_audiences)
            THEN ARRAY['admin_embed.create']::text[] ELSE ARRAY[]::text[] END)
        || (CASE WHEN 'accounting' = ANY(allowed_audiences)
            THEN ARRAY['accounting.read']::text[] ELSE ARRAY[]::text[] END)
        || (CASE WHEN 'configuration' = ANY(allowed_audiences) THEN ARRAY[
            'configuration.read', 'configuration.write',
            'diagnostic.grant.create', 'retention.read', 'retention.preview',
            'retention.write', 'budget.read', 'budget.write'
        ]::text[] ELSE ARRAY[]::text[] END)
        || (CASE WHEN 'budget_authority' = ANY(allowed_audiences) THEN ARRAY[
            'budget_ceiling.read', 'budget_ceiling.write'
        ]::text[] ELSE ARRAY[]::text[] END)
    )
),
ADD CONSTRAINT bootstrap_workspace_limit_valid
    CHECK (workspace_limit IN ('all_service_workspaces', 'explicit_only'));

ALTER TABLE router.service_access_tokens
ADD COLUMN issuer text,
ADD COLUMN digest_key_id text,
ADD COLUMN workspace_restricted boolean;

UPDATE router.service_access_tokens
SET issuer = 'llmrouter',
    digest_key_id = 'legacy',
    workspace_restricted = false,
    expires_at = issued_at + interval '5 minutes',
    revoked_at = COALESCE(revoked_at, transaction_timestamp());

UPDATE router.service_bootstrap_generations
SET valid_until = GREATEST(valid_until, created_at)
WHERE valid_until < created_at;

ALTER TABLE router.service_access_tokens
ALTER COLUMN issuer SET NOT NULL,
ALTER COLUMN digest_key_id SET NOT NULL,
ALTER COLUMN workspace_restricted SET NOT NULL,
ADD CONSTRAINT service_token_issuer_not_empty CHECK (issuer <> ''),
ADD CONSTRAINT service_token_digest_key_not_empty CHECK (digest_key_id <> ''),
ADD CONSTRAINT service_token_audience_closed CHECK (
    audience IN (
        'data_plane', 'service_management', 'host_backend', 'accounting',
        'configuration', 'budget_authority'
    )
),
ADD CONSTRAINT service_token_operations_match_audience CHECK (
    operations <@ CASE audience
        WHEN 'data_plane' THEN ARRAY[
            'model.create', 'model.read', 'model.cancel', 'run.create',
            'run.read', 'run.cancel', 'tool.create', 'tool.read', 'tool.cancel',
            'attachment.create', 'attachment.read', 'embedding.create',
            'embedding.read'
        ]::text[]
        WHEN 'service_management' THEN ARRAY[
            'workspace.create', 'workspace.read', 'workspace.disable',
            'workspace.restore', 'workspace.retire'
        ]::text[]
        WHEN 'host_backend' THEN ARRAY['admin_embed.create']::text[]
        WHEN 'accounting' THEN ARRAY['accounting.read']::text[]
        WHEN 'configuration' THEN ARRAY[
            'configuration.read', 'configuration.write',
            'diagnostic.grant.create', 'retention.read', 'retention.preview',
            'retention.write', 'budget.read', 'budget.write'
        ]::text[]
        WHEN 'budget_authority' THEN ARRAY[
            'budget_ceiling.read', 'budget_ceiling.write'
        ]::text[]
        ELSE ARRAY[]::text[]
    END
),
ADD CONSTRAINT service_token_lifetime_exact
    CHECK (expires_at = issued_at + interval '5 minutes'),
ADD CONSTRAINT service_token_workspace_count CHECK (cardinality(workspace_ids) <= 1000);

ALTER TABLE router.service_bootstrap_generations
ADD CONSTRAINT bootstrap_validity_after_creation
    CHECK (valid_until IS NULL OR valid_until >= created_at);

CREATE TABLE router.service_machine_tls_policies (
    service_id uuid PRIMARY KEY REFERENCES router.services (id) ON DELETE RESTRICT,
    required boolean NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE FUNCTION router.protect_machine_tls_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.service_id IS DISTINCT FROM OLD.service_id
       OR NEW.revision <> OLD.revision + 1
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'machine TLS policy is protected'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER service_machine_tls_policy_protected
BEFORE UPDATE OR DELETE ON router.service_machine_tls_policies
FOR EACH ROW EXECUTE FUNCTION router.protect_machine_tls_policy();

CREATE TABLE router.service_machine_tls_identities (
    certificate_identity text PRIMARY KEY CHECK (certificate_identity <> ''),
    service_id uuid NOT NULL,
    bootstrap_generation bigint NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (service_id, bootstrap_generation)
        REFERENCES router.service_bootstrap_generations (service_id, generation)
        ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK (expires_at <= issued_at + interval '24 hours')
);

CREATE INDEX service_machine_tls_identity_scope_idx
ON router.service_machine_tls_identities (service_id, bootstrap_generation)
WHERE revoked_at IS NULL;

CREATE FUNCTION router.protect_machine_tls_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.certificate_identity <> OLD.certificate_identity
       OR NEW.service_id <> OLD.service_id
       OR NEW.bootstrap_generation <> OLD.bootstrap_generation
       OR NEW.issued_at <> OLD.issued_at
       OR NEW.expires_at <> OLD.expires_at
       OR NEW.created_at <> OLD.created_at
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'machine TLS identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER service_machine_tls_identity_immutable
BEFORE UPDATE OR DELETE ON router.service_machine_tls_identities
FOR EACH ROW EXECUTE FUNCTION router.protect_machine_tls_identity();

CREATE FUNCTION router.protect_bootstrap_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.service_id IS DISTINCT FROM OLD.service_id
       OR NEW.generation IS DISTINCT FROM OLD.generation
       OR NEW.argon2id_verifier IS DISTINCT FROM OLD.argon2id_verifier
       OR NEW.allowed_operations IS DISTINCT FROM OLD.allowed_operations
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.allowed_audiences IS DISTINCT FROM OLD.allowed_audiences
       OR NEW.workspace_limit IS DISTINCT FROM OLD.workspace_limit
       OR (OLD.valid_until IS NOT NULL AND (
           NEW.valid_until IS NULL OR NEW.valid_until > OLD.valid_until
       ))
       OR (OLD.valid_until IS NULL AND NEW.valid_until IS NOT NULL
           AND NEW.valid_until > transaction_timestamp() + interval '24 hours')
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'bootstrap generation authority is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER service_bootstrap_generation_protected
BEFORE UPDATE OR DELETE ON router.service_bootstrap_generations
FOR EACH ROW EXECUTE FUNCTION router.protect_bootstrap_generation();

CREATE FUNCTION router.protect_service_access_token()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.token_id IS DISTINCT FROM OLD.token_id
       OR NEW.token_digest IS DISTINCT FROM OLD.token_digest
       OR NEW.service_id IS DISTINCT FROM OLD.service_id
       OR NEW.bootstrap_generation IS DISTINCT FROM OLD.bootstrap_generation
       OR NEW.audience IS DISTINCT FROM OLD.audience
       OR NEW.operations IS DISTINCT FROM OLD.operations
       OR NEW.workspace_ids IS DISTINCT FROM OLD.workspace_ids
       OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.digest_key_id IS DISTINCT FROM OLD.digest_key_id
       OR NEW.workspace_restricted IS DISTINCT FROM OLD.workspace_restricted
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'service access token authority is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER service_access_token_protected
BEFORE UPDATE ON router.service_access_tokens
FOR EACH ROW EXECUTE FUNCTION router.protect_service_access_token();

CREATE FUNCTION router.check_service_access_token_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    generation_audiences text[];
    generation_operations text[];
    generation_workspace_limit text;
    owned_workspace_count bigint;
BEGIN
    SELECT allowed_audiences, allowed_operations, workspace_limit
    INTO generation_audiences, generation_operations, generation_workspace_limit
    FROM router.service_bootstrap_generations
    WHERE service_id = NEW.service_id
      AND generation = NEW.bootstrap_generation;

    IF generation_audiences IS NULL
       OR NOT NEW.audience = ANY(generation_audiences)
       OR NOT NEW.operations <@ generation_operations
       OR (NOT NEW.workspace_restricted AND cardinality(NEW.workspace_ids) <> 0)
       OR (generation_workspace_limit = 'explicit_only' AND (
           NOT NEW.workspace_restricted OR 'workspace.create' = ANY(NEW.operations)
       )) THEN
        RAISE EXCEPTION 'service token exceeds bootstrap scope'
            USING ERRCODE = '23514';
    END IF;

    SELECT count(*) INTO owned_workspace_count
    FROM unnest(NEW.workspace_ids) AS requested(workspace_id)
    JOIN router.workspaces AS workspace
      ON workspace.id = requested.workspace_id
     AND workspace.service_id = NEW.service_id;
    IF owned_workspace_count <> cardinality(NEW.workspace_ids) THEN
        RAISE EXCEPTION 'service token workspace is outside service scope'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER service_access_token_scope_checked
BEFORE INSERT ON router.service_access_tokens
FOR EACH ROW EXECUTE FUNCTION router.check_service_access_token_scope();
