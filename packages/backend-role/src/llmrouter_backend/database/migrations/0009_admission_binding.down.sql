DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.logical_requests
        WHERE exact_route_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'cannot roll back without data loss: new admission targets exist';
    END IF;
END;
$$;

DROP TRIGGER logical_requests_admission_target ON router.logical_requests;
DROP FUNCTION router.check_admission_target();

CREATE OR REPLACE FUNCTION router.check_execution_configuration_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.configuration_revisions
        WHERE id = NEW.configuration_revision_id
          AND (
              scope_kind = 'global'
              OR (scope_kind = 'service'
                  AND service_id = NEW.service_id)
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
       OR NEW.configuration_revision_id <> OLD.configuration_revision_id
       OR NEW.fingerprint_version <> OLD.fingerprint_version
       OR NEW.fingerprint_sha256 <> OLD.fingerprint_sha256
       OR NEW.data_profile <> OLD.data_profile
       OR NEW.admitted_at <> OLD.admitted_at THEN
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

ALTER TABLE router.logical_requests
DROP CONSTRAINT logical_requests_location_check,
DROP CONSTRAINT logical_requests_terminal_expiry_required,
DROP CONSTRAINT logical_requests_contract_major_check,
DROP CONSTRAINT logical_requests_operation_check,
DROP COLUMN events_location,
DROP COLUMN cancel_location,
DROP COLUMN status_location,
DROP COLUMN contract_major,
DROP COLUMN operation_name,
DROP COLUMN exact_route_id;
