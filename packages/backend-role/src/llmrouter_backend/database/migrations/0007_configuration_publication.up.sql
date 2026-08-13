ALTER TABLE router.provider_adapter_types
ADD COLUMN display_name text,
ADD COLUMN generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
ADD COLUMN current_revision uuid REFERENCES router.configuration_revisions (id),
ADD COLUMN retired_at timestamptz,
ADD CONSTRAINT provider_adapter_types_retired_at_check
    CHECK ((state = 'retired') = (retired_at IS NOT NULL));

UPDATE router.provider_adapter_types SET display_name = id;
ALTER TABLE router.provider_adapter_types
ALTER COLUMN display_name SET NOT NULL,
ALTER COLUMN display_name SET DEFAULT 'Unlabeled provider';

ALTER TABLE router.canonical_models
ADD COLUMN display_name text,
ADD COLUMN generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
ADD COLUMN current_revision uuid REFERENCES router.configuration_revisions (id),
ADD COLUMN retired_at timestamptz,
ADD CONSTRAINT canonical_models_retired_at_check
    CHECK ((state = 'retired') = (retired_at IS NOT NULL));

UPDATE router.canonical_models SET display_name = stable_name;
ALTER TABLE router.canonical_models
ALTER COLUMN display_name SET NOT NULL,
ALTER COLUMN display_name SET DEFAULT 'Unlabeled model';

DROP TRIGGER provider_adapter_types_stable_identity ON router.provider_adapter_types;
DROP TRIGGER canonical_models_stable_identity ON router.canonical_models;

CREATE FUNCTION router.protect_catalog_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR (TG_TABLE_NAME = 'canonical_models'
           AND NEW.stable_name <> OLD.stable_name) THEN
        RAISE EXCEPTION '% identity is immutable', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION '% retirement is terminal', TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    IF NEW.generation <> OLD.generation + 1
       OR NEW.current_revision IS NULL
       OR NEW.current_revision IS NOT DISTINCT FROM OLD.current_revision THEN
        RAISE EXCEPTION '% generation must increase with a new revision', TG_TABLE_NAME
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_adapter_types_change_guard
BEFORE UPDATE ON router.provider_adapter_types
FOR EACH ROW EXECUTE FUNCTION router.protect_catalog_change();

CREATE TRIGGER canonical_models_change_guard
BEFORE UPDATE ON router.canonical_models
FOR EACH ROW EXECUTE FUNCTION router.protect_catalog_change();

ALTER TABLE router.provider_instances
ADD COLUMN current_revision uuid REFERENCES router.configuration_revisions (id),
ADD COLUMN display_name text,
ADD COLUMN eligible_service_ids uuid[] NOT NULL DEFAULT '{}',
ADD COLUMN last_changed_at timestamptz NOT NULL DEFAULT transaction_timestamp();

UPDATE router.provider_instances SET display_name = stable_name;
ALTER TABLE router.provider_instances
ALTER COLUMN display_name SET NOT NULL,
ALTER COLUMN display_name SET DEFAULT 'Unlabeled provider';

ALTER TABLE router.provider_model_routes
ADD COLUMN current_revision uuid REFERENCES router.configuration_revisions (id),
ADD COLUMN wire_model text,
ADD COLUMN eligible_service_ids uuid[] NOT NULL DEFAULT '{}',
ADD COLUMN capabilities jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(capabilities) = 'array'),
ADD COLUMN embedding_model_space_id text,
ADD COLUMN embedding_dimensions integer CHECK (embedding_dimensions BETWEEN 1 AND 4096),
ADD COLUMN last_changed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
ADD CONSTRAINT provider_model_routes_embedding_check CHECK (
    (embedding_model_space_id IS NULL) = (embedding_dimensions IS NULL)
);

UPDATE router.provider_model_routes SET wire_model = provider_lookup_id;
ALTER TABLE router.provider_model_routes
ALTER COLUMN wire_model SET NOT NULL,
ALTER COLUMN wire_model SET DEFAULT 'unknown';

ALTER TABLE router.assignment_candidates
ADD COLUMN attempt_timeout_ms integer
    CHECK (attempt_timeout_ms BETWEEN 100 AND 120000);

UPDATE router.assignment_candidates
SET attempt_timeout_ms = attempt_timeout_seconds * 1000;

ALTER TABLE router.assignment_candidates
ALTER COLUMN attempt_timeout_ms SET NOT NULL;

CREATE TABLE router.registered_settings_schemas (
    schema_name text NOT NULL CHECK (schema_name ~ '^[a-z][a-z0-9._-]{0,99}$'),
    major_version integer NOT NULL CHECK (major_version > 0),
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    registered_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (schema_name, major_version)
);

CREATE TABLE router.configuration_distribution_states (
    revision_id uuid PRIMARY KEY
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    state text NOT NULL DEFAULT 'distributing'
        CHECK (state IN ('distributing', 'current', 'degraded')),
    current_nodes integer NOT NULL DEFAULT 0 CHECK (current_nodes >= 0),
    total_nodes integer NOT NULL DEFAULT 0 CHECK (total_nodes >= current_nodes),
    published_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    CHECK ((state = 'current') = (total_nodes > 0 AND current_nodes = total_nodes))
);

CREATE TABLE router.configuration_audit_bindings (
    revision_id uuid PRIMARY KEY
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) ON DELETE RESTRICT
);

CREATE TRIGGER registered_settings_schemas_append_only
BEFORE UPDATE OR DELETE ON router.registered_settings_schemas
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER configuration_audit_bindings_append_only
BEFORE UPDATE OR DELETE ON router.configuration_audit_bindings
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();
