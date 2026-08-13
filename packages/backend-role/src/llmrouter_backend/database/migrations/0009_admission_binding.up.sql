ALTER TABLE router.logical_requests
ADD COLUMN exact_route_id uuid
    REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
ADD COLUMN operation_name text,
ADD COLUMN contract_major integer,
ADD COLUMN status_location text,
ADD COLUMN cancel_location text,
ADD COLUMN events_location text;

UPDATE router.logical_requests
SET operation_name = CASE request_kind
        WHEN 'model' THEN 'model.create'
        ELSE 'tool.create'
    END,
    contract_major = 1,
    status_location = CASE request_kind
        WHEN 'model' THEN '/v1/model-requests/' || request_id::text
        ELSE '/v1/shared-tool-requests/' || request_id::text
    END,
    cancel_location = CASE request_kind
        WHEN 'model' THEN '/v1/model-requests/' || request_id::text || '/cancel'
        ELSE '/v1/shared-tool-requests/' || request_id::text || '/cancel'
    END,
    events_location = CASE request_kind
        WHEN 'model' THEN '/v1/model-requests/' || request_id::text || '/events'
        ELSE NULL
    END;

ALTER TABLE router.logical_requests
ALTER COLUMN operation_name SET NOT NULL,
ALTER COLUMN operation_name SET DEFAULT 'model.create',
ALTER COLUMN contract_major SET NOT NULL,
ALTER COLUMN contract_major SET DEFAULT 1,
ALTER COLUMN status_location SET NOT NULL,
ALTER COLUMN status_location SET DEFAULT '/v1/model-requests/legacy-binding',
ADD CONSTRAINT logical_requests_operation_check CHECK (
    (request_kind = 'model' AND operation_name = 'model.create')
    OR (request_kind = 'model' AND operation_name IN (
        'openai.chat.completions.create', 'openai.responses.create'
    ))
    OR (request_kind = 'shared_tool' AND operation_name = 'tool.create')
),
ADD CONSTRAINT logical_requests_contract_major_check CHECK (contract_major > 0),
ADD CONSTRAINT logical_requests_location_check CHECK (
    status_location ~ '^/v1/' AND length(status_location) <= 1000
    AND (cancel_location IS NULL OR
         (cancel_location ~ '^/v1/' AND length(cancel_location) <= 1000))
    AND (events_location IS NULL OR
         (events_location ~ '^/v1/' AND length(events_location) <= 1000))
);

ALTER TABLE router.logical_requests
DISABLE TRIGGER logical_requests_stable_identity;

UPDATE router.logical_requests
SET expires_at = terminal_at + interval '24 hours'
WHERE terminal_at IS NOT NULL
  AND (expires_at IS NULL OR expires_at < terminal_at + interval '24 hours');

ALTER TABLE router.logical_requests
ENABLE TRIGGER logical_requests_stable_identity;

ALTER TABLE router.logical_requests
ADD CONSTRAINT logical_requests_terminal_expiry_required CHECK (
    terminal_at IS NULL
    OR (expires_at IS NOT NULL AND expires_at >= terminal_at + interval '24 hours')
);

CREATE OR REPLACE FUNCTION router.protect_logical_request_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.row_id <> OLD.row_id
       OR NEW.request_id <> OLD.request_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.request_kind <> OLD.request_kind
       OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
       OR NEW.exact_route_id IS DISTINCT FROM OLD.exact_route_id
       OR NEW.configuration_revision_id <> OLD.configuration_revision_id
       OR NEW.operation_name <> OLD.operation_name
       OR NEW.contract_major <> OLD.contract_major
       OR NEW.fingerprint_version <> OLD.fingerprint_version
       OR NEW.fingerprint_sha256 <> OLD.fingerprint_sha256
       OR NEW.data_profile <> OLD.data_profile
       OR NEW.admitted_at <> OLD.admitted_at
       OR NEW.status_location <> OLD.status_location
       OR NEW.cancel_location IS DISTINCT FROM OLD.cancel_location
       OR NEW.events_location IS DISTINCT FROM OLD.events_location THEN
        RAISE EXCEPTION 'logical request admission identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state IN ('succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain')
       AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal logical request is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.check_admission_target()
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

CREATE TRIGGER logical_requests_admission_target
BEFORE INSERT OR UPDATE OF assignment_id, exact_route_id, configuration_revision_id
ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.check_admission_target();

CREATE OR REPLACE FUNCTION router.check_execution_configuration_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        WITH RECURSIVE service_ancestors AS (
            SELECT id, parent_service_id
            FROM router.services WHERE id = NEW.service_id
          UNION ALL
            SELECT parent.id, parent.parent_service_id
            FROM router.services AS parent
            JOIN service_ancestors AS child
              ON child.parent_service_id = parent.id
        )
        SELECT 1 FROM router.configuration_revisions
        WHERE id = NEW.configuration_revision_id
          AND (
              scope_kind = 'global'
              OR (scope_kind = 'service'
                  AND service_id IN (SELECT id FROM service_ancestors))
              OR (scope_kind = 'workspace'
                  AND service_id = NEW.service_id
                  AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
          )
    ) THEN
        RAISE EXCEPTION 'execution configuration does not match its scope'
            USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'logical_requests'
       AND to_jsonb(NEW)->>'assignment_id' IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM router.assignment_definitions
           WHERE id = (to_jsonb(NEW)->>'assignment_id')::uuid
             AND configuration_revision_id = NEW.configuration_revision_id
       ) THEN
        RAISE EXCEPTION 'request assignment does not match its configuration revision'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
