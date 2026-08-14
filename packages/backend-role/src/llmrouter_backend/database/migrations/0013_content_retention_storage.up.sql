-- Durable capture, retention, protected export, and fenced lifecycle storage.

ALTER TABLE router.retention_policies
DROP CONSTRAINT retention_policies_data_class_check;

UPDATE router.retention_policies
SET data_class = CASE data_class
    WHEN 'agent_business_audit' THEN 'agent_tool_audit'
    WHEN 'security_global_audit' THEN 'security_audit'
    ELSE data_class
END;

ALTER TABLE router.retention_policies
ADD CONSTRAINT retention_policies_data_class_check CHECK (data_class IN (
    'diagnostic_logs', 'captured_content', 'raw_accounting',
    'agent_tool_audit', 'daily_accounting', 'security_audit',
    'configuration_revisions'
)),
DROP CONSTRAINT retention_policies_check2,
ADD CONSTRAINT retention_policies_agent_tool_range CHECK (
    data_class <> 'agent_tool_audit' OR retention_days BETWEEN 7 AND 365
);

CREATE TABLE router.retention_limits (
    data_class text PRIMARY KEY CHECK (data_class IN (
        'diagnostic_logs', 'captured_content', 'raw_accounting',
        'agent_tool_audit', 'daily_accounting', 'security_audit',
        'configuration_revisions'
    )),
    minimum_days integer NOT NULL CHECK (minimum_days BETWEEN 1 AND 36500),
    maximum_days integer NOT NULL CHECK (maximum_days BETWEEN 1 AND 36500),
    allowed_minimum_count integer,
    allowed_maximum_count integer,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at timestamptz NOT NULL,
    CHECK (minimum_days <= maximum_days),
    CHECK (
        (data_class = 'configuration_revisions'
         AND allowed_minimum_count BETWEEN 1 AND 1000000
         AND allowed_maximum_count BETWEEN allowed_minimum_count AND 1000000)
        OR (data_class <> 'configuration_revisions'
            AND allowed_minimum_count IS NULL
            AND allowed_maximum_count IS NULL)
    ),
    CHECK (
        data_class <> 'agent_tool_audit'
        OR (minimum_days >= 7 AND maximum_days <= 365)
    )
);

INSERT INTO router.retention_limits (
    data_class, minimum_days, maximum_days,
    allowed_minimum_count, allowed_maximum_count, updated_at
) VALUES
    ('diagnostic_logs', 1, 36500, NULL, NULL, transaction_timestamp()),
    ('captured_content', 1, 36500, NULL, NULL, transaction_timestamp()),
    ('raw_accounting', 1, 36500, NULL, NULL, transaction_timestamp()),
    ('agent_tool_audit', 7, 365, NULL, NULL, transaction_timestamp()),
    ('daily_accounting', 1, 36500, NULL, NULL, transaction_timestamp()),
    ('security_audit', 1, 36500, NULL, NULL, transaction_timestamp()),
    ('configuration_revisions', 1, 36500, 1, 1000000, transaction_timestamp());

WITH defaults(id, data_class, retention_days, minimum_revision_count) AS (
    VALUES
      ('00000000-0000-7000-8000-000000000020'::uuid, 'diagnostic_logs', 7, NULL),
      ('00000000-0000-7000-8000-000000000021'::uuid, 'captured_content', 7, NULL),
      ('00000000-0000-7000-8000-000000000022'::uuid, 'raw_accounting', 90, NULL),
      ('00000000-0000-7000-8000-000000000023'::uuid, 'agent_tool_audit', 30, NULL),
      ('00000000-0000-7000-8000-000000000024'::uuid, 'daily_accounting', 730, NULL),
      ('00000000-0000-7000-8000-000000000025'::uuid, 'security_audit', 730, NULL),
      ('00000000-0000-7000-8000-000000000026'::uuid, 'configuration_revisions', 730, 100)
)
INSERT INTO router.retention_policies (
    id, scope_kind, data_class, retention_days,
    minimum_revision_count, revision, effective_at
)
SELECT defaults.id, 'global', defaults.data_class, defaults.retention_days,
       defaults.minimum_revision_count, 1, timestamptz '2000-01-01 00:00:00+00'
FROM defaults
WHERE NOT EXISTS (
    SELECT 1 FROM router.retention_policies AS policy
    WHERE policy.scope_kind = 'global'
      AND policy.data_class = defaults.data_class
);

CREATE TABLE router.capture_policies (
    id uuid PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'service', 'workspace')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    policy text NOT NULL CHECK (policy IN ('disabled', 'metadata_only', 'complete')),
    minimum_policy text CHECK (minimum_policy IN ('disabled', 'metadata_only', 'complete')),
    maximum_policy text CHECK (maximum_policy IN ('disabled', 'metadata_only', 'complete')),
    revision bigint NOT NULL CHECK (revision > 0),
    effective_at timestamptz NOT NULL,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (
        (scope_kind = 'global' AND service_id IS NULL AND workspace_id IS NULL
         AND minimum_policy IS NOT NULL AND maximum_policy IS NOT NULL)
        OR (scope_kind = 'service' AND service_id IS NOT NULL AND workspace_id IS NULL
            AND minimum_policy IS NULL AND maximum_policy IS NULL)
        OR (scope_kind = 'workspace' AND service_id IS NOT NULL AND workspace_id IS NOT NULL
            AND minimum_policy IS NULL AND maximum_policy IS NULL)
    ),
    CHECK (
        minimum_policy IS NULL OR maximum_policy IS NULL
        OR CASE minimum_policy WHEN 'disabled' THEN 0 WHEN 'metadata_only' THEN 1 ELSE 2 END
           <= CASE maximum_policy WHEN 'disabled' THEN 0 WHEN 'metadata_only' THEN 1 ELSE 2 END
    ),
    UNIQUE NULLS NOT DISTINCT (scope_kind, service_id, workspace_id, revision)
);

CREATE TABLE router.retention_previews (
    id uuid PRIMARY KEY,
    actor_id text NOT NULL CHECK (actor_id <> ''),
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'service', 'workspace')),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    expected_revision text NOT NULL CHECK (expected_revision <> ''),
    selection_fingerprint bytea NOT NULL CHECK (octet_length(selection_fingerprint) = 32),
    effects jsonb NOT NULL CHECK (jsonb_typeof(effects) = 'array'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    confirmed_at timestamptz,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    CHECK (confirmed_at IS NULL OR confirmed_at BETWEEN created_at AND expires_at),
    CHECK (
        (scope_kind = 'global' AND service_id IS NULL AND workspace_id IS NULL)
        OR (scope_kind = 'service' AND service_id IS NOT NULL AND workspace_id IS NULL)
        OR (scope_kind = 'workspace' AND service_id IS NOT NULL AND workspace_id IS NOT NULL)
    )
);

INSERT INTO router.capture_policies (
    id, scope_kind, policy, minimum_policy, maximum_policy, revision, effective_at
) VALUES (
    '00000000-0000-7000-8000-000000000013', 'global', 'complete',
    'disabled', 'complete', 1, timestamptz '2000-01-01 00:00:00+00'
);

ALTER TABLE router.logical_requests
ADD COLUMN capture_policy text,
ADD COLUMN capture_reason text,
ADD COLUMN captured_content_expires_at timestamptz;

DO $$
DECLARE
    old_constraint text;
BEGIN
    SELECT constraint_name
    INTO old_constraint
    FROM information_schema.check_constraints
    WHERE constraint_schema = 'router'
      AND check_clause LIKE '%capture_enabled%capture_pressure_reason%'
    LIMIT 1;
    IF old_constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE router.logical_requests DROP CONSTRAINT %I',
            old_constraint
        );
    END IF;
END;
$$;

ALTER TABLE router.logical_requests DISABLE TRIGGER logical_requests_stable_identity;

UPDATE router.logical_requests
SET capture_policy = CASE WHEN capture_enabled THEN 'complete' ELSE 'disabled' END,
    capture_reason = CASE WHEN capture_enabled THEN 'configured' ELSE 'spool_pressure' END,
    captured_content_expires_at = CASE
        WHEN capture_enabled THEN admitted_at + make_interval(days => (
            SELECT policy.retention_days
            FROM router.retention_policies AS policy
            WHERE policy.data_class = 'captured_content'
              AND policy.effective_at <= router.logical_requests.admitted_at
              AND (
                policy.scope_kind = 'global'
                OR (policy.scope_kind = 'service'
                    AND policy.service_id = router.logical_requests.service_id)
                OR (policy.scope_kind = 'workspace'
                    AND policy.service_id = router.logical_requests.service_id
                    AND policy.workspace_id = router.logical_requests.workspace_id)
              )
            ORDER BY CASE policy.scope_kind
                WHEN 'workspace' THEN 3 WHEN 'service' THEN 2 ELSE 1 END DESC,
                policy.revision DESC
            LIMIT 1
        ))
        ELSE NULL
    END;

ALTER TABLE router.logical_requests ENABLE TRIGGER logical_requests_stable_identity;

ALTER TABLE router.logical_requests
ALTER COLUMN capture_policy SET NOT NULL,
ALTER COLUMN capture_reason SET NOT NULL,
ADD CONSTRAINT logical_requests_capture_policy_check CHECK (
    capture_policy IN ('complete', 'metadata_only', 'disabled')
),
ADD CONSTRAINT logical_requests_capture_reason_check CHECK (
    capture_reason IN ('configured', 'spool_pressure')
),
ADD CONSTRAINT logical_requests_capture_pressure_check CHECK (
    (capture_reason = 'spool_pressure') = (capture_pressure_reason IS NOT NULL)
    AND (capture_pressure_reason IS NULL OR capture_pressure_reason <> '')
),
ADD CONSTRAINT logical_requests_capture_snapshot_check CHECK (
    capture_enabled = (capture_policy <> 'disabled')
    AND (capture_reason <> 'spool_pressure' OR capture_policy = 'disabled')
    AND ((capture_policy = 'disabled') = (captured_content_expires_at IS NULL))
    AND (captured_content_expires_at IS NULL OR captured_content_expires_at > admitted_at)
);

CREATE FUNCTION router.apply_configured_capture_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    configured_policy text;
    configured_retention integer;
BEGIN
    IF NEW.capture_reason = 'spool_pressure' THEN
        NEW.capture_enabled := false;
        NEW.capture_policy := 'disabled';
        NEW.capture_reason := 'spool_pressure';
        NEW.captured_content_expires_at := NULL;
        RETURN NEW;
    END IF;
    SELECT policy.policy
    INTO configured_policy
    FROM router.capture_policies AS policy
    WHERE policy.effective_at <= NEW.admitted_at AND (
        policy.scope_kind = 'global'
        OR (policy.scope_kind = 'service' AND policy.service_id = NEW.service_id)
        OR (policy.scope_kind = 'workspace' AND policy.service_id = NEW.service_id
            AND policy.workspace_id = NEW.workspace_id)
    )
    ORDER BY CASE policy.scope_kind
        WHEN 'workspace' THEN 3 WHEN 'service' THEN 2 ELSE 1 END DESC,
        policy.revision DESC
    LIMIT 1;
    IF configured_policy IS NULL THEN
        RAISE EXCEPTION 'configured capture policy is missing' USING ERRCODE = '23514';
    END IF;
    NEW.capture_policy := configured_policy;
    NEW.capture_reason := 'configured';
    NEW.capture_enabled := configured_policy <> 'disabled';
    IF configured_policy = 'disabled' THEN
        NEW.captured_content_expires_at := NULL;
        RETURN NEW;
    END IF;
    SELECT policy.retention_days
    INTO configured_retention
    FROM router.retention_policies AS policy
    WHERE policy.data_class = 'captured_content'
      AND policy.effective_at <= NEW.admitted_at AND (
        policy.scope_kind = 'global'
        OR (policy.scope_kind = 'service' AND policy.service_id = NEW.service_id)
        OR (policy.scope_kind = 'workspace' AND policy.service_id = NEW.service_id
            AND policy.workspace_id = NEW.workspace_id)
    )
    ORDER BY CASE policy.scope_kind
        WHEN 'workspace' THEN 3 WHEN 'service' THEN 2 ELSE 1 END DESC,
        policy.revision DESC
    LIMIT 1;
    IF configured_retention IS NULL THEN
        RAISE EXCEPTION 'configured capture retention is missing' USING ERRCODE = '23514';
    END IF;
    NEW.captured_content_expires_at :=
        NEW.admitted_at + make_interval(days => configured_retention);
    RETURN NEW;
END;
$$;

CREATE TRIGGER logical_requests_configured_capture_snapshot
BEFORE INSERT ON router.logical_requests
FOR EACH ROW EXECUTE FUNCTION router.apply_configured_capture_snapshot();

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
       OR NEW.capture_enabled <> OLD.capture_enabled
       OR NEW.capture_pressure_reason IS DISTINCT FROM OLD.capture_pressure_reason
       OR NEW.capture_policy <> OLD.capture_policy
       OR NEW.capture_reason <> OLD.capture_reason
       OR NEW.captured_content_expires_at IS DISTINCT FROM OLD.captured_content_expires_at
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

CREATE TABLE router.content_manifests (
    id uuid PRIMARY KEY,
    manifest_version smallint NOT NULL DEFAULT 1 CHECK (manifest_version = 1),
    segment_count integer NOT NULL CHECK (segment_count BETWEEN 1 AND 10000),
    ciphertext_bytes bigint NOT NULL CHECK (ciphertext_bytes > 0),
    manifest_sha256 bytea NOT NULL CHECK (octet_length(manifest_sha256) = 32),
    created_at timestamptz NOT NULL
);

CREATE TABLE router.content_segments (
    manifest_id uuid NOT NULL REFERENCES router.content_manifests (id) ON DELETE RESTRICT,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    object_key text NOT NULL UNIQUE CHECK (object_key <> '' AND length(object_key) <= 1000),
    ciphertext_bytes bigint NOT NULL CHECK (ciphertext_bytes > 0),
    ciphertext_sha256 bytea NOT NULL CHECK (octet_length(ciphertext_sha256) = 32),
    encrypted_data_key bytea NOT NULL CHECK (octet_length(encrypted_data_key) > 24),
    wrapping_key_id text NOT NULL CHECK (wrapping_key_id <> ''),
    PRIMARY KEY (manifest_id, ordinal)
);

CREATE TRIGGER content_manifests_append_only
BEFORE UPDATE OR DELETE ON router.content_manifests
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER content_segments_append_only
BEFORE UPDATE OR DELETE ON router.content_segments
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.captured_content (
    id uuid PRIMARY KEY,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    request_row_id uuid NOT NULL,
    request_id uuid NOT NULL,
    capture_policy text NOT NULL CHECK (capture_policy IN ('complete', 'metadata_only')),
    content_type text NOT NULL CHECK (content_type <> '' AND length(content_type) <= 200),
    manifest_id uuid REFERENCES router.content_manifests (id) ON DELETE RESTRICT,
    plaintext_sha256 bytea CHECK (plaintext_sha256 IS NULL OR octet_length(plaintext_sha256) = 32),
    plaintext_bytes bigint CHECK (plaintext_bytes IS NULL OR plaintext_bytes > 0),
    admitted_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    lifecycle_state text NOT NULL DEFAULT 'live'
        CHECK (lifecycle_state IN ('live', 'deleting')),
    deletion_started_at timestamptz,
    deleted_at timestamptz,
    FOREIGN KEY (request_row_id)
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK ((capture_policy = 'complete') = (manifest_id IS NOT NULL)),
    CHECK ((manifest_id IS NULL) = (plaintext_sha256 IS NULL)),
    CHECK ((manifest_id IS NULL) = (plaintext_bytes IS NULL)),
    CHECK (expires_at > admitted_at),
    CHECK ((lifecycle_state = 'deleting') = (deletion_started_at IS NOT NULL)),
    CHECK (deleted_at IS NULL OR deleted_at >= created_at),
    UNIQUE (id, service_id, workspace_id)
);

CREATE INDEX captured_content_discovery_idx
ON router.captured_content (created_at DESC, id DESC)
WHERE deleted_at IS NULL;

CREATE INDEX captured_content_expiry_idx
ON router.captured_content (expires_at, id)
WHERE deleted_at IS NULL;

CREATE TABLE router.protected_exports (
    id uuid PRIMARY KEY,
    actor_id text NOT NULL CHECK (actor_id <> ''),
    administrator_session_id text NOT NULL CHECK (administrator_session_id <> ''),
    data_class text NOT NULL CHECK (data_class IN (
        'accounting', 'audit', 'configuration', 'captured_content'
    )),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    range_start timestamptz NOT NULL,
    range_end timestamptz NOT NULL,
    export_format text NOT NULL CHECK (export_format IN ('jsonl', 'csv')),
    idempotency_key_digest bytea NOT NULL CHECK (octet_length(idempotency_key_digest) = 32),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'running', 'completed', 'failed', 'expired'
    )),
    manifest_id uuid REFERENCES router.content_manifests (id) ON DELETE RESTRICT,
    content_sha256 bytea CHECK (content_sha256 IS NULL OR octet_length(content_sha256) = 32),
    safe_error text CHECK (safe_error IS NULL OR length(safe_error) <= 500),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deletion_started_at timestamptz,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (range_start < range_end),
    CHECK (expires_at > created_at),
    CHECK (
        (state = 'completed' AND manifest_id IS NOT NULL AND content_sha256 IS NOT NULL)
        OR (state = 'expired')
        OR (state NOT IN ('completed', 'expired')
            AND manifest_id IS NULL AND content_sha256 IS NULL)
    ),
    CHECK ((manifest_id IS NULL) = (content_sha256 IS NULL)),
    UNIQUE (actor_id, idempotency_key_digest)
);

CREATE FUNCTION router.protect_content_record()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF TG_TABLE_NAME = 'captured_content' AND NOT EXISTS (
            SELECT 1 FROM router.logical_requests AS request
            WHERE request.row_id = NEW.request_row_id
              AND request.request_id = NEW.request_id
              AND request.service_id = NEW.service_id
              AND request.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
        ) THEN
            RAISE EXCEPTION 'captured content request scope does not match'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF current_setting('llmrouter.lifecycle_cleanup', true) <> 'on' THEN
            RAISE EXCEPTION 'content records require fenced lifecycle cleanup'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_TABLE_NAME <> 'captured_content' THEN
        RAISE EXCEPTION 'content manifests and segments are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_TABLE_NAME = 'captured_content' THEN
        IF NEW.id <> OLD.id OR NEW.service_id <> OLD.service_id
           OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.request_row_id <> OLD.request_row_id OR NEW.request_id <> OLD.request_id
           OR NEW.capture_policy <> OLD.capture_policy OR NEW.content_type <> OLD.content_type
           OR NEW.manifest_id IS DISTINCT FROM OLD.manifest_id
           OR NEW.plaintext_sha256 IS DISTINCT FROM OLD.plaintext_sha256
           OR NEW.plaintext_bytes IS DISTINCT FROM OLD.plaintext_bytes
           OR NEW.admitted_at <> OLD.admitted_at OR NEW.expires_at <> OLD.expires_at
           OR NEW.created_at <> OLD.created_at OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
           OR OLD.lifecycle_state <> 'live' OR NEW.lifecycle_state <> 'deleting'
           OR NEW.deletion_started_at IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM router.content_lifecycle_jobs AS job
               WHERE job.job_kind IN ('expiry', 'delete')
                 AND job.scope_key = OLD.id::text AND job.state = 'running'
                 AND job.lease_expires_at > NEW.deletion_started_at
           ) THEN
            RAISE EXCEPTION 'invalid captured-content lifecycle transition'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER content_manifests_append_only ON router.content_manifests;
DROP TRIGGER content_segments_append_only ON router.content_segments;

CREATE TRIGGER content_manifests_append_only
BEFORE UPDATE OR DELETE ON router.content_manifests
FOR EACH ROW EXECUTE FUNCTION router.protect_content_record();

CREATE TRIGGER content_segments_append_only
BEFORE UPDATE OR DELETE ON router.content_segments
FOR EACH ROW EXECUTE FUNCTION router.protect_content_record();

CREATE TRIGGER captured_content_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.captured_content
FOR EACH ROW EXECUTE FUNCTION router.protect_content_record();

CREATE FUNCTION router.protect_export_record()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allowed boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'queued' OR NEW.manifest_id IS NOT NULL
           OR NEW.content_sha256 IS NOT NULL OR NEW.safe_error IS NOT NULL
           OR NEW.deletion_started_at IS NOT NULL OR NEW.updated_at <> NEW.created_at THEN
            RAISE EXCEPTION 'protected export must start queued'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF current_setting('llmrouter.lifecycle_cleanup', true) <> 'on' THEN
            RAISE EXCEPTION 'exports require fenced lifecycle cleanup'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.id <> OLD.id OR NEW.actor_id <> OLD.actor_id
       OR NEW.administrator_session_id <> OLD.administrator_session_id
       OR NEW.data_class <> OLD.data_class OR NEW.service_id IS DISTINCT FROM OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.range_start <> OLD.range_start OR NEW.range_end <> OLD.range_end
       OR NEW.export_format <> OLD.export_format
       OR NEW.idempotency_key_digest <> OLD.idempotency_key_digest
       OR NEW.request_fingerprint <> OLD.request_fingerprint
       OR NEW.created_at <> OLD.created_at OR NEW.expires_at <> OLD.expires_at
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'protected export identity is immutable' USING ERRCODE = '55000';
    END IF;
    allowed := CASE OLD.state
        WHEN 'queued' THEN NEW.state IN ('running', 'expired')
        WHEN 'running' THEN NEW.state IN ('completed', 'failed', 'expired')
        WHEN 'completed' THEN NEW.state = 'expired'
        WHEN 'failed' THEN NEW.state = 'expired'
        WHEN 'expired' THEN NEW.state = 'expired'
        ELSE false
    END;
    IF NOT allowed OR (NEW.state = 'completed' AND NEW.manifest_id IS NULL)
       OR (NEW.state <> 'completed' AND OLD.manifest_id IS NULL AND NEW.manifest_id IS NOT NULL)
       OR (OLD.manifest_id IS NOT NULL AND NEW.manifest_id IS DISTINCT FROM OLD.manifest_id)
       OR (OLD.content_sha256 IS NOT NULL
           AND NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256)
       OR (NEW.deletion_started_at IS NOT NULL AND NEW.state <> 'expired')
       OR (OLD.state = 'expired' AND (
           OLD.deletion_started_at IS NOT NULL
           OR NEW.deletion_started_at IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM router.content_lifecycle_jobs AS job
               WHERE job.job_kind = 'export_expiry'
                 AND job.scope_key = OLD.id::text AND job.state = 'running'
                 AND job.lease_expires_at > NEW.deletion_started_at
           )
       )) THEN
        RAISE EXCEPTION 'invalid protected export transition' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER protected_exports_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.protected_exports
FOR EACH ROW EXECUTE FUNCTION router.protect_export_record();

CREATE TABLE router.export_redemptions (
    export_id uuid PRIMARY KEY REFERENCES router.protected_exports (id) ON DELETE RESTRICT,
    token_digest bytea NOT NULL UNIQUE CHECK (octet_length(token_digest) = 32),
    administrator_session_id text NOT NULL CHECK (administrator_session_id <> ''),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz,
    CHECK (expires_at > issued_at AND expires_at <= issued_at + interval '5 minutes'),
    CHECK (redeemed_at IS NULL OR redeemed_at BETWEEN issued_at AND expires_at)
);

CREATE FUNCTION router.protect_export_redemption()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rotated boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1 FROM router.protected_exports AS export
            WHERE export.id = NEW.export_id AND export.state = 'completed'
              AND export.administrator_session_id = NEW.administrator_session_id
              AND export.expires_at >= NEW.expires_at
        ) THEN
            RAISE EXCEPTION 'redemption does not match a live completed export'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF current_setting('llmrouter.lifecycle_cleanup', true) <> 'on' THEN
            RAISE EXCEPTION 'redemptions require fenced lifecycle cleanup'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;
    rotated := NEW.token_digest <> OLD.token_digest;
    IF NEW.export_id <> OLD.export_id
       OR NEW.administrator_session_id <> OLD.administrator_session_id
       OR NEW.expires_at < NEW.issued_at
       OR NEW.expires_at > NEW.issued_at + interval '5 minutes'
       OR (rotated AND (
           NEW.issued_at < OLD.issued_at OR NEW.redeemed_at IS NOT NULL
           OR NOT EXISTS (
               SELECT 1 FROM router.protected_exports AS export
               WHERE export.id = OLD.export_id AND export.state = 'completed'
                 AND export.expires_at > transaction_timestamp()
           )
       ))
       OR (NOT rotated AND (
           NEW.issued_at <> OLD.issued_at OR NEW.expires_at <> OLD.expires_at
           OR OLD.redeemed_at IS NOT NULL
           OR NEW.redeemed_at IS NULL
           OR NEW.redeemed_at < NEW.issued_at OR NEW.redeemed_at > NEW.expires_at
       )) THEN
        RAISE EXCEPTION 'invalid export redemption transition' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER export_redemptions_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.export_redemptions
FOR EACH ROW EXECUTE FUNCTION router.protect_export_redemption();

CREATE TABLE router.content_lifecycle_jobs (
    id uuid PRIMARY KEY,
    job_kind text NOT NULL CHECK (job_kind IN (
        'expiry', 'delete', 'export', 'export_expiry', 'archive', 'retention'
    )),
    scope_key text NOT NULL CHECK (scope_key <> ''),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    state text NOT NULL DEFAULT 'ready' CHECK (state IN (
        'ready', 'running', 'retry_wait', 'succeeded', 'failed'
    )),
    owner_node_id uuid,
    lease_generation bigint NOT NULL DEFAULT 1 CHECK (lease_generation > 0),
    lease_expires_at timestamptz,
    available_at timestamptz NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    safe_error text CHECK (safe_error IS NULL OR length(safe_error) <= 500),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (job_kind, scope_key),
    CHECK (
        (state = 'running' AND owner_node_id IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'running' AND owner_node_id IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE INDEX content_lifecycle_jobs_due_idx
ON router.content_lifecycle_jobs (available_at, id)
WHERE state IN ('ready', 'retry_wait', 'running');

CREATE FUNCTION router.protect_content_lifecycle_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allowed boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'ready' OR NEW.owner_node_id IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL OR NEW.lease_generation <> 1
           OR NEW.attempt_count <> 0 OR NEW.safe_error IS NOT NULL
           OR NEW.updated_at <> NEW.created_at THEN
            RAISE EXCEPTION 'content lifecycle job must start ready'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.job_kind <> OLD.job_kind
       OR NEW.scope_key <> OLD.scope_key OR NEW.payload <> OLD.payload
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'content lifecycle job identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'terminal content lifecycle job is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.lease_generation <> OLD.lease_generation + 1 THEN
        RAISE EXCEPTION 'content lifecycle job generation must increase'
            USING ERRCODE = '40001';
    END IF;
    IF OLD.state = 'running' AND NEW.state = 'running'
       AND OLD.lease_expires_at > transaction_timestamp()
       AND NEW.owner_node_id IS DISTINCT FROM OLD.owner_node_id THEN
        RAISE EXCEPTION 'content lifecycle job does not have the live owner'
            USING ERRCODE = '40001';
    END IF;
    IF OLD.state = 'running' AND NEW.state <> 'running'
       AND OLD.lease_expires_at <= NEW.updated_at THEN
        RAISE EXCEPTION 'expired content lifecycle lease cannot complete work'
            USING ERRCODE = '40001';
    END IF;
    allowed := CASE OLD.state
        WHEN 'ready' THEN NEW.state = 'running'
        WHEN 'retry_wait' THEN NEW.state = 'running'
        WHEN 'running' THEN NEW.state IN ('running', 'retry_wait', 'succeeded', 'failed')
        ELSE false
    END;
    IF NOT allowed THEN
        RAISE EXCEPTION 'invalid content lifecycle job state transition'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state = 'running' AND (
        NEW.owner_node_id IS NULL OR NEW.lease_expires_at IS NULL
        OR NEW.lease_expires_at <= NEW.updated_at
    ) THEN
        RAISE EXCEPTION 'running content lifecycle job needs a live lease'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state <> 'running' AND (
        NEW.owner_node_id IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'non-running content lifecycle job cannot keep a lease'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.updated_at < OLD.updated_at
       OR (NEW.state = 'running' AND NEW.attempt_count <> OLD.attempt_count + 1)
       OR (NEW.state <> 'running' AND NEW.attempt_count <> OLD.attempt_count)
       OR (NEW.state <> 'retry_wait' AND NEW.available_at <> OLD.available_at)
       OR (NEW.state NOT IN ('retry_wait', 'failed')
           AND NEW.safe_error IS DISTINCT FROM OLD.safe_error) THEN
        RAISE EXCEPTION 'invalid content lifecycle job mutable fields'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER content_lifecycle_jobs_fenced
BEFORE INSERT OR UPDATE ON router.content_lifecycle_jobs
FOR EACH ROW EXECUTE FUNCTION router.protect_content_lifecycle_job();

CREATE FUNCTION router.protect_retained_record()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('llmrouter.retention_cleanup', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'retained records require the retention worker'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER accounting_events_append_only ON router.accounting_events;
DROP TRIGGER audit_events_append_only ON router.audit_events;
DROP TRIGGER configuration_revisions_append_only ON router.configuration_revisions;

CREATE TRIGGER accounting_events_append_only
BEFORE UPDATE OR DELETE ON router.accounting_events
FOR EACH ROW EXECUTE FUNCTION router.protect_retained_record();

CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON router.audit_events
FOR EACH ROW EXECUTE FUNCTION router.protect_retained_record();

CREATE TRIGGER configuration_revisions_append_only
BEFORE UPDATE OR DELETE ON router.configuration_revisions
FOR EACH ROW EXECUTE FUNCTION router.protect_retained_record();

CREATE TRIGGER daily_accounting_aggregates_delete_guard
BEFORE DELETE ON router.daily_accounting_aggregates
FOR EACH ROW EXECUTE FUNCTION router.protect_retained_record();
