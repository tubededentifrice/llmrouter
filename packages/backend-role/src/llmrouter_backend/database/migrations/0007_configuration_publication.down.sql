DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.configuration_distribution_states)
       OR EXISTS (SELECT 1 FROM router.configuration_audit_bindings)
       OR EXISTS (SELECT 1 FROM router.registered_settings_schemas)
       OR EXISTS (SELECT 1 FROM router.provider_adapter_types WHERE current_revision IS NOT NULL)
       OR EXISTS (SELECT 1 FROM router.canonical_models WHERE current_revision IS NOT NULL)
       OR EXISTS (SELECT 1 FROM router.provider_instances WHERE current_revision IS NOT NULL)
       OR EXISTS (SELECT 1 FROM router.provider_model_routes WHERE current_revision IS NOT NULL) THEN
        RAISE EXCEPTION 'configuration publication migration cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP TRIGGER configuration_audit_bindings_append_only ON router.configuration_audit_bindings;
DROP TRIGGER registered_settings_schemas_append_only ON router.registered_settings_schemas;
DROP TABLE router.configuration_audit_bindings;
DROP TABLE router.configuration_distribution_states;
DROP TABLE router.registered_settings_schemas;

ALTER TABLE router.assignment_candidates
DROP COLUMN attempt_timeout_ms;

ALTER TABLE router.provider_model_routes
DROP CONSTRAINT provider_model_routes_embedding_check,
DROP COLUMN last_changed_at,
DROP COLUMN embedding_dimensions,
DROP COLUMN embedding_model_space_id,
DROP COLUMN capabilities,
DROP COLUMN eligible_service_ids,
DROP COLUMN wire_model,
DROP COLUMN current_revision;

ALTER TABLE router.provider_instances
DROP COLUMN last_changed_at,
DROP COLUMN eligible_service_ids,
DROP COLUMN display_name,
DROP COLUMN current_revision;

DROP TRIGGER canonical_models_change_guard ON router.canonical_models;
DROP TRIGGER provider_adapter_types_change_guard ON router.provider_adapter_types;
DROP FUNCTION router.protect_catalog_change();

ALTER TABLE router.canonical_models
DROP CONSTRAINT canonical_models_retired_at_check,
DROP COLUMN retired_at,
DROP COLUMN current_revision,
DROP COLUMN generation,
DROP COLUMN display_name;

ALTER TABLE router.provider_adapter_types
DROP CONSTRAINT provider_adapter_types_retired_at_check,
DROP COLUMN retired_at,
DROP COLUMN current_revision,
DROP COLUMN generation,
DROP COLUMN display_name;

CREATE TRIGGER provider_adapter_types_stable_identity
BEFORE UPDATE ON router.provider_adapter_types
FOR EACH ROW EXECUTE FUNCTION router.protect_catalog_identity();

CREATE TRIGGER canonical_models_stable_identity
BEFORE UPDATE ON router.canonical_models
FOR EACH ROW EXECUTE FUNCTION router.protect_catalog_identity();
