CREATE SCHEMA router;

CREATE FUNCTION router.reject_record_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION router.protect_stable_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id THEN
        RAISE EXCEPTION '% identity is immutable', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_service_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.stable_name <> OLD.stable_name
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'service identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_workspace_creation_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.service_id <> OLD.service_id
       OR NEW.caller_reference <> OLD.caller_reference
       OR NEW.creation_idempotency_key <> OLD.creation_idempotency_key
       OR NEW.creation_fingerprint <> OLD.creation_fingerprint
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'workspace creation identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_catalog_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR (TG_TABLE_NAME = 'canonical_models'
           AND to_jsonb(NEW)->>'stable_name'
               <> to_jsonb(OLD)->>'stable_name') THEN
        RAISE EXCEPTION '% identity is immutable', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_provider_identity_and_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.owner_kind <> OLD.owner_kind
       OR NEW.owner_service_id IS DISTINCT FROM OLD.owner_service_id
       OR (TG_TABLE_NAME = 'encrypted_credentials'
           AND to_jsonb(NEW)->>'credential_kind'
               <> to_jsonb(OLD)->>'credential_kind')
       OR (TG_TABLE_NAME = 'provider_instances'
           AND (to_jsonb(NEW)->>'adapter_type_id'
                    <> to_jsonb(OLD)->>'adapter_type_id'
                OR to_jsonb(NEW)->>'stable_name'
                    <> to_jsonb(OLD)->>'stable_name'))
       OR (TG_TABLE_NAME = 'provider_model_routes'
           AND (to_jsonb(NEW)->>'provider_instance_id'
                    <> to_jsonb(OLD)->>'provider_instance_id'
                OR to_jsonb(NEW)->>'canonical_model_id'
                    <> to_jsonb(OLD)->>'canonical_model_id'
                OR to_jsonb(NEW)->>'provider_lookup_id'
                    <> to_jsonb(OLD)->>'provider_lookup_id')) THEN
        RAISE EXCEPTION '% identity and scope are immutable', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    IF NEW.generation <= OLD.generation THEN
        RAISE EXCEPTION '% generation must increase', TG_TABLE_NAME
            USING ERRCODE = '40001';
    END IF;
    IF OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION '% retirement is terminal', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.protect_terminal_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION '% retirement is terminal', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    IF NEW.state_revision <= OLD.state_revision THEN
        RAISE EXCEPTION '% state revision must increase', TG_TABLE_NAME
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE router.services (
    id uuid PRIMARY KEY,
    parent_service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    stable_name text NOT NULL UNIQUE CHECK (stable_name <> ''),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    state_revision bigint NOT NULL DEFAULT 1 CHECK (state_revision > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retired_at timestamptz,
    CHECK (parent_service_id IS NULL OR parent_service_id <> id),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL)),
    UNIQUE (id, state_revision)
);

CREATE INDEX services_parent_idx
    ON router.services (parent_service_id)
    WHERE parent_service_id IS NOT NULL;

CREATE FUNCTION router.check_service_parent_chain()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        WITH RECURSIVE ancestors AS (
            SELECT parent_service_id
            FROM router.services
            WHERE id = NEW.id
          UNION ALL
            SELECT service.parent_service_id
            FROM router.services AS service
            JOIN ancestors ON service.id = ancestors.parent_service_id
            WHERE service.parent_service_id IS NOT NULL
        )
        SELECT 1 FROM ancestors WHERE parent_service_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'service parent chain contains a cycle'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER services_parent_chain
AFTER INSERT OR UPDATE OF parent_service_id ON router.services
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_service_parent_chain();

CREATE TRIGGER services_stable_identity
BEFORE UPDATE ON router.services
FOR EACH ROW EXECUTE FUNCTION router.protect_service_identity();

CREATE TRIGGER services_terminal_state
BEFORE UPDATE ON router.services
FOR EACH ROW EXECUTE FUNCTION router.protect_terminal_state();

CREATE TABLE router.workspaces (
    id uuid PRIMARY KEY CHECK (id <> '00000000-0000-0000-0000-000000000000'),
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    caller_reference text NOT NULL CHECK (caller_reference <> ''),
    creation_idempotency_key text NOT NULL CHECK (creation_idempotency_key <> ''),
    creation_fingerprint bytea NOT NULL CHECK (octet_length(creation_fingerprint) = 32),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    state_revision bigint NOT NULL DEFAULT 1 CHECK (state_revision > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retired_at timestamptz,
    CHECK ((state = 'retired') = (retired_at IS NOT NULL)),
    UNIQUE (id, service_id),
    UNIQUE (service_id, caller_reference),
    UNIQUE (service_id, creation_idempotency_key)
);

CREATE TRIGGER workspaces_stable_identity
BEFORE UPDATE ON router.workspaces
FOR EACH ROW EXECUTE FUNCTION router.protect_workspace_creation_identity();

CREATE TRIGGER workspaces_terminal_state
BEFORE UPDATE ON router.workspaces
FOR EACH ROW EXECUTE FUNCTION router.protect_terminal_state();

CREATE TABLE router.provider_adapter_types (
    id text PRIMARY KEY CHECK (id <> ''),
    settings_schema_name text NOT NULL CHECK (settings_schema_name <> ''),
    settings_schema_major integer NOT NULL CHECK (settings_schema_major > 0),
    capabilities jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'object'),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE router.canonical_models (
    id uuid PRIMARY KEY,
    stable_name text NOT NULL UNIQUE CHECK (stable_name <> ''),
    capabilities jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'object'),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TRIGGER provider_adapter_types_stable_identity
BEFORE UPDATE ON router.provider_adapter_types
FOR EACH ROW EXECUTE FUNCTION router.protect_catalog_identity();

CREATE TRIGGER canonical_models_stable_identity
BEFORE UPDATE ON router.canonical_models
FOR EACH ROW EXECUTE FUNCTION router.protect_catalog_identity();

CREATE TABLE router.encrypted_credentials (
    id uuid PRIMARY KEY,
    owner_kind text NOT NULL CHECK (owner_kind IN ('global', 'service')),
    owner_service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    credential_kind text NOT NULL CHECK (credential_kind <> ''),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) > 0),
    encrypted_data_key bytea NOT NULL CHECK (octet_length(encrypted_data_key) > 0),
    wrapping_key_id text NOT NULL CHECK (wrapping_key_id <> ''),
    safe_fingerprint text NOT NULL CHECK (safe_fingerprint <> ''),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retired_at timestamptz,
    CHECK (
        (owner_kind = 'global' AND owner_service_id IS NULL)
        OR (owner_kind = 'service' AND owner_service_id IS NOT NULL)
    ),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL)),
    UNIQUE (id, generation)
);

CREATE INDEX encrypted_credentials_owner_idx
    ON router.encrypted_credentials (owner_service_id)
    WHERE owner_service_id IS NOT NULL;

CREATE TRIGGER encrypted_credentials_identity_generation
BEFORE UPDATE ON router.encrypted_credentials
FOR EACH ROW EXECUTE FUNCTION router.protect_provider_identity_and_generation();

CREATE TABLE router.provider_instances (
    id uuid PRIMARY KEY,
    owner_kind text NOT NULL CHECK (owner_kind IN ('global', 'service')),
    owner_service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    adapter_type_id text NOT NULL
        REFERENCES router.provider_adapter_types (id) ON DELETE RESTRICT,
    credential_id uuid NOT NULL
        REFERENCES router.encrypted_credentials (id) ON DELETE RESTRICT,
    stable_name text NOT NULL CHECK (stable_name <> ''),
    endpoint_origin text NOT NULL CHECK (endpoint_origin <> ''),
    settings_schema_name text NOT NULL CHECK (settings_schema_name <> ''),
    settings_schema_major integer NOT NULL CHECK (settings_schema_major > 0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings) = 'object'),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retired_at timestamptz,
    CHECK (
        (owner_kind = 'global' AND owner_service_id IS NULL)
        OR (owner_kind = 'service' AND owner_service_id IS NOT NULL)
    ),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL)),
    UNIQUE NULLS NOT DISTINCT (owner_kind, owner_service_id, stable_name),
    UNIQUE (id, generation)
);

CREATE FUNCTION router.check_provider_instance_owner()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    credential_owner_kind text;
    credential_owner_service_id uuid;
BEGIN
    SELECT owner_kind, owner_service_id
    INTO credential_owner_kind, credential_owner_service_id
    FROM router.encrypted_credentials
    WHERE id = NEW.credential_id;

    IF credential_owner_kind = 'service'
       AND (NEW.owner_kind <> 'service'
            OR NEW.owner_service_id IS DISTINCT FROM credential_owner_service_id) THEN
        RAISE EXCEPTION 'provider instance and credential owners do not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_instances_owner_check
BEFORE INSERT OR UPDATE OF owner_kind, owner_service_id, credential_id
ON router.provider_instances
FOR EACH ROW EXECUTE FUNCTION router.check_provider_instance_owner();

CREATE TRIGGER provider_instances_identity_generation
BEFORE UPDATE ON router.provider_instances
FOR EACH ROW EXECUTE FUNCTION router.protect_provider_identity_and_generation();

CREATE INDEX provider_instances_owner_idx
    ON router.provider_instances (owner_service_id)
    WHERE owner_service_id IS NOT NULL;

CREATE TABLE router.provider_model_routes (
    id uuid PRIMARY KEY,
    owner_kind text NOT NULL CHECK (owner_kind IN ('global', 'service')),
    owner_service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    provider_instance_id uuid NOT NULL
        REFERENCES router.provider_instances (id) ON DELETE RESTRICT,
    canonical_model_id uuid NOT NULL
        REFERENCES router.canonical_models (id) ON DELETE RESTRICT,
    provider_lookup_id text NOT NULL CHECK (provider_lookup_id <> ''),
    settings_schema_name text NOT NULL CHECK (settings_schema_name <> ''),
    settings_schema_major integer NOT NULL CHECK (settings_schema_major > 0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings) = 'object'),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    retired_at timestamptz,
    CHECK (
        (owner_kind = 'global' AND owner_service_id IS NULL)
        OR (owner_kind = 'service' AND owner_service_id IS NOT NULL)
    ),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL)),
    UNIQUE (provider_instance_id, canonical_model_id, provider_lookup_id),
    UNIQUE (id, generation)
);

CREATE FUNCTION router.check_provider_route_owner()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    instance_owner_kind text;
    instance_owner_service_id uuid;
BEGIN
    SELECT owner_kind, owner_service_id
    INTO instance_owner_kind, instance_owner_service_id
    FROM router.provider_instances
    WHERE id = NEW.provider_instance_id;

    IF instance_owner_kind = 'service'
       AND (NEW.owner_kind <> 'service'
            OR NEW.owner_service_id IS DISTINCT FROM instance_owner_service_id) THEN
        RAISE EXCEPTION 'provider route and instance owners do not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_model_routes_owner_check
BEFORE INSERT OR UPDATE OF owner_kind, owner_service_id, provider_instance_id
ON router.provider_model_routes
FOR EACH ROW EXECUTE FUNCTION router.check_provider_route_owner();

CREATE TRIGGER provider_model_routes_identity_generation
BEFORE UPDATE ON router.provider_model_routes
FOR EACH ROW EXECUTE FUNCTION router.protect_provider_identity_and_generation();

CREATE INDEX provider_model_routes_owner_idx
    ON router.provider_model_routes (owner_service_id)
    WHERE owner_service_id IS NOT NULL;

CREATE TABLE router.configuration_revisions (
    id uuid PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'service', 'workspace')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    revision_number bigint NOT NULL CHECK (revision_number > 0),
    restored_from_revision_id uuid
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    created_by_kind text NOT NULL
        CHECK (created_by_kind IN ('system', 'administrator', 'service')),
    created_by_id text NOT NULL CHECK (created_by_id <> ''),
    CHECK (
        (scope_kind = 'global' AND service_id IS NULL AND workspace_id IS NULL)
        OR (scope_kind = 'service' AND service_id IS NOT NULL AND workspace_id IS NULL)
        OR (scope_kind = 'workspace' AND service_id IS NOT NULL AND workspace_id IS NOT NULL)
    ),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    UNIQUE NULLS NOT DISTINCT (scope_kind, service_id, workspace_id, revision_number),
    UNIQUE (id, scope_kind, service_id, workspace_id)
);

CREATE FUNCTION router.check_configuration_revision_sequence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    previous_revision bigint;
BEGIN
    LOCK TABLE router.configuration_revisions IN SHARE ROW EXCLUSIVE MODE;
    SELECT max(revision_number)
    INTO previous_revision
    FROM router.configuration_revisions
    WHERE scope_kind = NEW.scope_kind
      AND service_id IS NOT DISTINCT FROM NEW.service_id
      AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id;
    IF NEW.revision_number <> COALESCE(previous_revision, 0) + 1 THEN
        RAISE EXCEPTION 'configuration revision must follow the active sequence'
            USING ERRCODE = '40001';
    END IF;
    IF NEW.restored_from_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.configuration_revisions
        WHERE id = NEW.restored_from_revision_id
          AND scope_kind = NEW.scope_kind
          AND service_id IS NOT DISTINCT FROM NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'restored revision must use the same configuration scope'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER configuration_revisions_sequence
BEFORE INSERT ON router.configuration_revisions
FOR EACH ROW EXECUTE FUNCTION router.check_configuration_revision_sequence();

CREATE TRIGGER configuration_revisions_append_only
BEFORE UPDATE OR DELETE ON router.configuration_revisions
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.active_configurations (
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'service', 'workspace')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    revision_number bigint NOT NULL CHECK (revision_number > 0),
    activated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (
        (scope_kind = 'global' AND service_id IS NULL AND workspace_id IS NULL)
        OR (scope_kind = 'service' AND service_id IS NOT NULL AND workspace_id IS NULL)
        OR (scope_kind = 'workspace' AND service_id IS NOT NULL AND workspace_id IS NOT NULL)
    ),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    UNIQUE NULLS NOT DISTINCT (scope_kind, service_id, workspace_id),
    UNIQUE (revision_id)
);

CREATE FUNCTION router.check_active_configuration_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.scope_kind <> OLD.scope_kind
        OR NEW.service_id IS DISTINCT FROM OLD.service_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.revision_number <> OLD.revision_number + 1
    ) THEN
        RAISE EXCEPTION 'active configuration update must select the next scope revision'
            USING ERRCODE = '40001';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM router.configuration_revisions AS revision
        WHERE revision.id = NEW.revision_id
          AND revision.scope_kind = NEW.scope_kind
          AND revision.service_id IS NOT DISTINCT FROM NEW.service_id
          AND revision.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND revision.revision_number = NEW.revision_number
    ) THEN
        RAISE EXCEPTION 'active configuration does not match its revision scope'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER active_configurations_revision_check
BEFORE INSERT OR UPDATE ON router.active_configurations
FOR EACH ROW EXECUTE FUNCTION router.check_active_configuration_revision();

CREATE TABLE router.assignment_definitions (
    id uuid PRIMARY KEY,
    configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    stable_name text NOT NULL CHECK (stable_name <> ''),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'retired')),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (configuration_revision_id, stable_name),
    UNIQUE (id, configuration_revision_id)
);

CREATE TABLE router.assignment_candidates (
    assignment_id uuid NOT NULL,
    configuration_revision_id uuid NOT NULL,
    ordinal smallint NOT NULL CHECK (ordinal BETWEEN 1 AND 8),
    provider_model_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    attempt_timeout_seconds integer NOT NULL CHECK (attempt_timeout_seconds BETWEEN 1 AND 120),
    candidate_policy jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(candidate_policy) = 'object'),
    PRIMARY KEY (assignment_id, ordinal),
    UNIQUE (assignment_id, provider_model_route_id),
    FOREIGN KEY (assignment_id, configuration_revision_id)
        REFERENCES router.assignment_definitions (id, configuration_revision_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER assignment_definitions_append_only
BEFORE UPDATE OR DELETE ON router.assignment_definitions
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER assignment_candidates_append_only
BEFORE UPDATE OR DELETE ON router.assignment_candidates
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_assignment_has_candidate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    checked_assignment_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'assignment_definitions' THEN
        checked_assignment_id := COALESCE(NEW.id, OLD.id);
    ELSE
        checked_assignment_id := COALESCE(NEW.assignment_id, OLD.assignment_id);
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.assignment_definitions WHERE id = checked_assignment_id
    ) AND NOT EXISTS (
        SELECT 1 FROM router.assignment_candidates
        WHERE assignment_id = checked_assignment_id
    ) THEN
        RAISE EXCEPTION 'assignment must contain one or more candidates'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER assignment_definitions_have_candidates
AFTER INSERT OR UPDATE ON router.assignment_definitions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_assignment_has_candidate();

CREATE CONSTRAINT TRIGGER assignment_candidates_keep_chain
AFTER DELETE OR UPDATE OF assignment_id ON router.assignment_candidates
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_assignment_has_candidate();

CREATE TABLE router.business_tool_gateways (
    service_id uuid PRIMARY KEY REFERENCES router.services (id) ON DELETE RESTRICT,
    endpoint_origin text NOT NULL CHECK (endpoint_origin <> ''),
    contract_major integer NOT NULL CHECK (contract_major > 0),
    tool_kinds jsonb NOT NULL CHECK (jsonb_typeof(tool_kinds) = 'array'),
    network_policy jsonb NOT NULL CHECK (jsonb_typeof(network_policy) = 'object'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'disabled')),
    updated_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE router.route_price_sources (
    id uuid PRIMARY KEY,
    provider_model_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    authority_kind text NOT NULL CHECK (authority_kind IN ('manual', 'synchronized')),
    source_name text NOT NULL CHECK (source_name <> ''),
    lookup_identifier text NOT NULL CHECK (lookup_identifier <> ''),
    synchronization_schedule text,
    stale_after interval NOT NULL DEFAULT interval '14 days'
        CHECK (stale_after > interval '0 seconds'),
    UNIQUE (provider_model_route_id)
);

CREATE TABLE router.price_source_snapshots (
    id uuid PRIMARY KEY,
    source_name text NOT NULL CHECK (source_name <> ''),
    fetched_at timestamptz NOT NULL,
    source_revision text,
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    http_validator text,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (source_name, content_sha256)
);

CREATE TRIGGER price_source_snapshots_append_only
BEFORE UPDATE OR DELETE ON router.price_source_snapshots
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.route_price_versions (
    id uuid PRIMARY KEY,
    provider_model_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    source_snapshot_id uuid
        REFERENCES router.price_source_snapshots (id) ON DELETE RESTRICT,
    version_number bigint NOT NULL CHECK (version_number > 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status text NOT NULL CHECK (status IN ('current', 'stale', 'missing', 'failed')),
    accepted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (provider_model_route_id, version_number),
    UNIQUE (id, provider_model_route_id)
);

CREATE TRIGGER route_price_versions_append_only
BEFORE UPDATE OR DELETE ON router.route_price_versions
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.route_price_components (
    price_version_id uuid NOT NULL
        REFERENCES router.route_price_versions (id) ON DELETE RESTRICT,
    component_kind text NOT NULL CHECK (component_kind <> ''),
    unit_name text NOT NULL CHECK (unit_name <> ''),
    unit_quantity numeric(38, 18) NOT NULL CHECK (unit_quantity > 0),
    unit_price numeric(38, 18) NOT NULL CHECK (unit_price >= 0),
    raw_source_value text NOT NULL,
    PRIMARY KEY (price_version_id, component_kind, unit_name)
);

CREATE TRIGGER route_price_components_append_only
BEFORE UPDATE OR DELETE ON router.route_price_components
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();
