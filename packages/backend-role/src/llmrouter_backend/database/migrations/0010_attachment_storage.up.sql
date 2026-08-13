ALTER TABLE router.attachments
ADD CONSTRAINT attachments_positive_byte_length
CHECK (byte_length BETWEEN 1 AND 26214400) NOT VALID;

CREATE INDEX attachments_create_replay_idx
ON router.attachments (
    service_id, workspace_id, media_type, byte_length, content_sha256,
    created_at DESC
);

CREATE TABLE router.attachment_content (
    attachment_id uuid PRIMARY KEY
        REFERENCES router.attachments (id) ON DELETE RESTRICT,
    ciphertext bytea NOT NULL
        CHECK (octet_length(ciphertext) BETWEEN 41 AND 26214440),
    encrypted_data_key bytea NOT NULL
        CHECK (octet_length(encrypted_data_key) = 72),
    wrapping_key_id text NOT NULL CHECK (length(wrapping_key_id) BETWEEN 1 AND 200),
    stored_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TABLE router.attachment_storage_legacy_ready (
    attachment_id uuid PRIMARY KEY
        REFERENCES router.attachments (id) ON DELETE RESTRICT,
    revision bigint NOT NULL CHECK (revision > 0),
    verified_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

INSERT INTO router.attachment_storage_legacy_ready (
    attachment_id, revision, verified_at, updated_at
)
SELECT attachment_id, revision, verified_at, updated_at
FROM router.attachment_status
WHERE state = 'ready';

UPDATE router.attachment_status AS status
SET state = 'failed', revision = status.revision + 1,
    verified_at = NULL, updated_at = transaction_timestamp()
WHERE status.state = 'ready'
  AND NOT EXISTS (
      SELECT 1 FROM router.attachment_content AS content
      WHERE content.attachment_id = status.attachment_id
  );

CREATE FUNCTION router.protect_attachment_status_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'attachment status cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'pending' OR NEW.revision <> 1
           OR NEW.verified_at IS NOT NULL THEN
            RAISE EXCEPTION 'attachment status must start pending'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.attachment_id <> OLD.attachment_id
       OR NEW.revision <> OLD.revision + 1
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'attachment status identity or revision is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state IN ('ready', 'failed', 'expired'))
        OR (OLD.state IN ('ready', 'failed') AND NEW.state = 'expired')
    ) THEN
        RAISE EXCEPTION 'attachment status transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.state = 'ready' AND NOT EXISTS (
        SELECT 1 FROM router.attachment_content
        WHERE attachment_id = NEW.attachment_id
    ) THEN
        RAISE EXCEPTION 'ready attachment content is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER attachment_status_change_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.attachment_status
FOR EACH ROW EXECUTE FUNCTION router.protect_attachment_status_change();

CREATE FUNCTION router.protect_attachment_content_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM router.attachment_status AS status
            JOIN router.attachments AS attachment
              ON attachment.id = status.attachment_id
            WHERE status.attachment_id = NEW.attachment_id
              AND status.state = 'pending'
              AND octet_length(NEW.ciphertext) = attachment.byte_length + 40
        ) THEN
            RAISE EXCEPTION 'attachment content does not match pending metadata'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'ready attachment content is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM router.attachment_status
        WHERE attachment_id = OLD.attachment_id AND state = 'expired'
    ) THEN
        RAISE EXCEPTION 'attachment content can be removed only after expiry'
            USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER attachment_content_change_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.attachment_content
FOR EACH ROW EXECUTE FUNCTION router.protect_attachment_content_change();

CREATE FUNCTION router.check_attachment_storage_invariant()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    checked_attachment_id uuid;
    attachment_state text;
    content_exists boolean;
BEGIN
    checked_attachment_id := CASE
        WHEN TG_OP = 'DELETE' THEN OLD.attachment_id
        ELSE NEW.attachment_id
    END;
    SELECT state INTO attachment_state
    FROM router.attachment_status
    WHERE attachment_id = checked_attachment_id;
    IF attachment_state IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM router.attachment_content
        WHERE attachment_id = checked_attachment_id
    ) INTO content_exists;
    IF (attachment_state = 'ready') <> content_exists THEN
        RAISE EXCEPTION 'attachment state and content are inconsistent'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER attachment_status_storage_invariant
AFTER INSERT OR UPDATE ON router.attachment_status
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_attachment_storage_invariant();

CREATE CONSTRAINT TRIGGER attachment_content_storage_invariant
AFTER INSERT OR DELETE ON router.attachment_content
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_attachment_storage_invariant();

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
        SELECT 1
        FROM router.attachments AS attachment
        JOIN router.attachment_status AS status
          ON status.attachment_id = attachment.id
        JOIN router.attachment_content AS content
          ON content.attachment_id = attachment.id
        WHERE attachment.id = NEW.attachment_id
          AND attachment.service_id = NEW.service_id
          AND attachment.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND attachment.byte_length BETWEEN 1 AND 26214400
          AND attachment.expires_at > transaction_timestamp()
          AND status.state = 'ready'
    ) THEN
        RAISE EXCEPTION 'request attachment scope or readiness does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
