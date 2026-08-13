DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.attachment_content) THEN
        RAISE EXCEPTION 'attachment storage cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION router.check_request_attachment_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.logical_requests
        WHERE row_id = NEW.request_row_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) OR NOT EXISTS (
        SELECT 1 FROM router.attachments
        WHERE id = NEW.attachment_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'request attachment scope does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER attachment_content_storage_invariant ON router.attachment_content;
DROP TRIGGER attachment_status_storage_invariant ON router.attachment_status;
DROP FUNCTION router.check_attachment_storage_invariant();

DROP TRIGGER attachment_content_change_guard ON router.attachment_content;
DROP FUNCTION router.protect_attachment_content_change();
DROP TABLE router.attachment_content;

DROP TRIGGER attachment_status_change_guard ON router.attachment_status;
DROP FUNCTION router.protect_attachment_status_change();

UPDATE router.attachment_status AS status
SET state = 'ready', revision = legacy.revision,
    verified_at = legacy.verified_at, updated_at = legacy.updated_at
FROM router.attachment_storage_legacy_ready AS legacy
WHERE status.attachment_id = legacy.attachment_id
  AND status.state = 'failed'
  AND status.revision = legacy.revision + 1
  AND status.verified_at IS NULL;

DROP TABLE router.attachment_storage_legacy_ready;

DROP INDEX router.attachments_create_replay_idx;
ALTER TABLE router.attachments
DROP CONSTRAINT attachments_positive_byte_length;
