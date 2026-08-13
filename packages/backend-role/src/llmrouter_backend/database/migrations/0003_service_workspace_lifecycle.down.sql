DROP TABLE router.workspace_lifecycle_idempotency_bindings;
DROP TABLE router.workspace_lifecycle_operations;
DROP TABLE router.service_lifecycle_operations;
DROP FUNCTION router.lifecycle_admission_is_allowed(uuid, uuid);

DROP TRIGGER workspaces_no_delete ON router.workspaces;
DROP TRIGGER services_no_delete ON router.services;
DROP FUNCTION router.reject_lifecycle_identity_delete();

DROP TRIGGER workspaces_fill_display_name ON router.workspaces;
DROP TRIGGER services_fill_display_name ON router.services;
DROP FUNCTION router.fill_lifecycle_display_name();

CREATE OR REPLACE FUNCTION router.protect_terminal_state()
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

ALTER TABLE router.workspaces
DROP CONSTRAINT workspaces_caller_reference_length,
DROP CONSTRAINT workspaces_display_name_length,
DROP COLUMN display_name;

ALTER TABLE router.services
DROP CONSTRAINT services_display_name_length,
DROP COLUMN display_name;
