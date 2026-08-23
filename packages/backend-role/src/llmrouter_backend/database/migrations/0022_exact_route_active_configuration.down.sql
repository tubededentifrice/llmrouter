DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM router.logical_requests AS request
        JOIN router.provider_model_routes AS route
          ON route.id = request.exact_route_id
        WHERE request.exact_route_id IS NOT NULL
          AND request.configuration_revision_id <> route.current_revision
    ) THEN
        RAISE EXCEPTION 'cannot roll back without data loss from exact-route requests'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION router.validate_provider_route_execution_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_prices jsonb;
    expected_document jsonb;
BEGIN
    SELECT jsonb_agg(jsonb_build_object(
               'unit', component.unit_name,
               'price', component.unit_price::text,
               'currency', version.currency,
               'raw_source_value', component.raw_source_value,
               'unit_quantity', component.unit_quantity::text
           ) ORDER BY component.unit_name)
    INTO expected_prices
    FROM router.route_price_components AS component
    JOIN router.route_price_versions AS version
      ON version.id = component.price_version_id
    WHERE component.price_version_id = NEW.price_version_id;

    IF NEW.migration_0015_backfilled THEN
        IF NOT EXISTS (
            SELECT 1 FROM router.provider_attempts AS attempt
            WHERE attempt.id = NEW.id
              AND attempt.provider_model_route_id = NEW.provider_model_route_id
              AND attempt.route_generation = NEW.route_generation
              AND attempt.price_version_id = NEW.price_version_id
        ) THEN
            RAISE EXCEPTION 'backfilled route snapshot has no matching legacy attempt'
                USING ERRCODE = '23514';
        END IF;
    ELSIF expected_prices IS NULL OR NEW.typed_prices <> expected_prices OR NOT EXISTS (
        SELECT 1
        FROM router.provider_model_routes AS route
        JOIN router.provider_instances AS instance
          ON instance.id = route.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = instance.credential_id
        JOIN router.configuration_price_bindings AS price
          ON price.configuration_revision_id = NEW.assignment_revision_id
         AND price.provider_model_route_id = route.id
         AND price.price_version_id = NEW.price_version_id
        JOIN router.logical_requests AS request
          ON request.row_id = NEW.request_row_id
        WHERE route.id = NEW.provider_model_route_id
          AND route.state = 'active'
          AND route.generation = NEW.route_generation
          AND route.current_revision = NEW.route_configuration_revision_id
          AND route.wire_model = NEW.wire_model
          AND route.capabilities = NEW.capabilities
          AND route.settings = NEW.route_settings
          AND instance.id = NEW.provider_instance_id
          AND instance.state = 'active'
          AND instance.generation = NEW.provider_instance_generation
          AND COALESCE(instance.current_revision, route.current_revision)
              = NEW.instance_configuration_revision_id
          AND instance.adapter_type_id = NEW.adapter_type_id
          AND instance.endpoint_origin = NEW.endpoint_origin
          AND instance.settings = NEW.instance_settings
          AND credential.id = NEW.credential_id
          AND credential.state = 'active'
          AND credential.generation = NEW.credential_generation
          AND credential.current_revision = NEW.credential_revision_id
          AND request.configuration_revision_id = NEW.assignment_revision_id
          AND router.provider_route_is_eligible(route.id, request.service_id)
          AND router.provider_resource_is_enabled(
              'provider_model_route', route.id,
              request.service_id, request.workspace_id
          )
          AND router.provider_resource_is_enabled(
              'provider_instance', instance.id,
              request.service_id, request.workspace_id
          )
          AND (
              (request.assignment_id IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM router.assignment_candidates AS candidate
                   WHERE candidate.assignment_id = request.assignment_id
                     AND candidate.configuration_revision_id = NEW.assignment_revision_id
                     AND candidate.ordinal = NEW.candidate_ordinal
                     AND candidate.provider_model_route_id = NEW.provider_model_route_id
                     AND candidate.attempt_timeout_ms = NEW.attempt_timeout_ms
                     AND candidate.candidate_policy = NEW.candidate_policy
               ))
              OR
              (request.assignment_id IS NULL
               AND request.exact_route_id = NEW.provider_model_route_id
               AND NEW.route_configuration_revision_id = request.configuration_revision_id
               AND NEW.candidate_ordinal = 1
               AND NEW.attempt_timeout_ms = 120000
               AND NEW.candidate_policy = '{}'::jsonb)
          )
    ) THEN
        RAISE EXCEPTION 'provider route execution snapshot does not match current configuration'
            USING ERRCODE = '23514';
    END IF;

    expected_document := jsonb_build_object(
        'request_row_id', NEW.request_row_id,
        'candidate_ordinal', NEW.candidate_ordinal,
        'assignment_revision_id', NEW.assignment_revision_id,
        'attempt_timeout_ms', NEW.attempt_timeout_ms,
        'candidate_policy', NEW.candidate_policy,
        'route_configuration_revision_id', NEW.route_configuration_revision_id,
        'provider_model_route_id', NEW.provider_model_route_id,
        'route_generation', NEW.route_generation,
        'provider_instance_id', NEW.provider_instance_id,
        'provider_instance_generation', NEW.provider_instance_generation,
        'instance_configuration_revision_id', NEW.instance_configuration_revision_id,
        'credential_id', NEW.credential_id,
        'credential_generation', NEW.credential_generation,
        'credential_revision_id', NEW.credential_revision_id,
        'price_version_id', NEW.price_version_id,
        'adapter_type_id', NEW.adapter_type_id,
        'endpoint_origin', NEW.endpoint_origin,
        'wire_model', NEW.wire_model,
        'capabilities', NEW.capabilities,
        'instance_settings', NEW.instance_settings,
        'route_settings', NEW.route_settings,
        'typed_prices', NEW.typed_prices
    );
    IF NEW.content_sha256 <> sha256(convert_to(expected_document::text, 'UTF8')) THEN
        RAISE EXCEPTION 'provider route execution snapshot digest does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION router.check_admission_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.assignment_id IS NULL) = (NEW.exact_route_id IS NULL) THEN
        RAISE EXCEPTION 'request must select exactly one admission target'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.assignment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.assignment_definitions
        WHERE id = NEW.assignment_id
          AND configuration_revision_id = NEW.configuration_revision_id
          AND state = 'active'
    ) THEN
        RAISE EXCEPTION 'request assignment is not active in its configuration'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.exact_route_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.provider_model_routes
        WHERE id = NEW.exact_route_id AND state = 'active'
          AND current_revision = NEW.configuration_revision_id
    ) THEN
        RAISE EXCEPTION 'request exact route is not active in its configuration'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
