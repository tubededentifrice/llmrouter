DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.captured_content)
       OR EXISTS (SELECT 1 FROM router.protected_exports)
       OR EXISTS (SELECT 1 FROM router.content_lifecycle_jobs)
       OR EXISTS (
           SELECT 1 FROM router.capture_policies
           WHERE id <> '00000000-0000-7000-8000-000000000013'::uuid
              OR scope_kind <> 'global' OR policy <> 'complete'
              OR minimum_policy <> 'disabled' OR maximum_policy <> 'complete'
              OR revision <> 1
       )
       OR EXISTS (
           SELECT 1
           FROM router.retention_limits AS retention_limit
           JOIN (VALUES
               ('diagnostic_logs', 1, 36500, NULL::integer, NULL::integer),
               ('captured_content', 1, 36500, NULL::integer, NULL::integer),
               ('raw_accounting', 1, 36500, NULL::integer, NULL::integer),
               ('agent_tool_audit', 7, 365, NULL::integer, NULL::integer),
               ('daily_accounting', 1, 36500, NULL::integer, NULL::integer),
               ('security_audit', 1, 36500, NULL::integer, NULL::integer),
               ('configuration_revisions', 1, 36500, 1, 1000000)
           ) AS expected(
               data_class, minimum_days, maximum_days,
               allowed_minimum_count, allowed_maximum_count
           ) USING (data_class)
           WHERE (
               retention_limit.minimum_days,
               retention_limit.maximum_days,
               retention_limit.allowed_minimum_count,
               retention_limit.allowed_maximum_count,
               retention_limit.revision
           ) IS DISTINCT FROM (
               expected.minimum_days,
               expected.maximum_days,
               expected.allowed_minimum_count,
               expected.allowed_maximum_count,
               1::bigint
           )
       )
       OR (SELECT count(*) FROM router.retention_limits) <> 7
       OR EXISTS (
           SELECT 1 FROM router.logical_requests
           WHERE capture_policy = 'metadata_only'
              OR (capture_policy = 'disabled' AND capture_reason = 'configured')
       ) THEN
        RAISE EXCEPTION 'cannot roll back without content lifecycle data loss';
    END IF;
END;
$$;

DROP TABLE router.export_redemptions;
DROP TABLE router.protected_exports;
DROP TABLE router.captured_content;
DROP TABLE router.content_segments;
DROP TABLE router.content_manifests;
DROP TRIGGER content_manifest_cleanup_authorizations_guard
ON router.content_manifest_cleanup_authorizations;
DROP FUNCTION router.protect_content_manifest_cleanup_authorization();
DROP TABLE router.content_manifest_cleanup_authorizations;
DROP FUNCTION router.protect_export_redemption();
DROP FUNCTION router.protect_export_record();
DROP FUNCTION router.protect_content_record();
DROP FUNCTION router.has_current_content_manifest_fence(uuid, boolean);

DROP TRIGGER content_lifecycle_jobs_fenced ON router.content_lifecycle_jobs;
DROP FUNCTION router.protect_content_lifecycle_job();
DROP TABLE router.content_lifecycle_jobs;

DROP TRIGGER daily_accounting_aggregates_delete_guard
ON router.daily_accounting_aggregates;
DROP TRIGGER accounting_events_append_only ON router.accounting_events;
DROP TRIGGER audit_events_append_only ON router.audit_events;
DROP TRIGGER configuration_revisions_append_only
ON router.configuration_revisions;

CREATE TRIGGER accounting_events_append_only
BEFORE UPDATE OR DELETE ON router.accounting_events
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON router.audit_events
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TRIGGER configuration_revisions_append_only
BEFORE UPDATE OR DELETE ON router.configuration_revisions
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

DROP FUNCTION router.protect_retained_record();
DROP FUNCTION router.has_current_content_lifecycle_fence(text[], text);

DROP TRIGGER logical_requests_configured_capture_snapshot ON router.logical_requests;
DROP FUNCTION router.apply_configured_capture_snapshot();

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

ALTER TABLE router.logical_requests
DROP CONSTRAINT logical_requests_capture_snapshot_check,
DROP CONSTRAINT logical_requests_capture_reason_check,
DROP CONSTRAINT logical_requests_capture_pressure_check,
DROP CONSTRAINT logical_requests_capture_policy_check,
DROP COLUMN captured_content_expires_at,
DROP COLUMN capture_reason,
DROP COLUMN capture_policy;

ALTER TABLE router.logical_requests
ADD CONSTRAINT logical_requests_legacy_capture_pressure_check
CHECK (capture_enabled OR capture_pressure_reason IS NOT NULL);

DROP TABLE router.capture_policies;
DROP TABLE router.retention_previews;
DROP TABLE router.retention_limits;

DELETE FROM router.retention_policies
WHERE id IN (
    '00000000-0000-7000-8000-000000000020',
    '00000000-0000-7000-8000-000000000021',
    '00000000-0000-7000-8000-000000000022',
    '00000000-0000-7000-8000-000000000023',
    '00000000-0000-7000-8000-000000000024',
    '00000000-0000-7000-8000-000000000025',
    '00000000-0000-7000-8000-000000000026'
);

ALTER TABLE router.retention_policies
DROP CONSTRAINT retention_policies_agent_tool_range,
DROP CONSTRAINT retention_policies_data_class_check;

UPDATE router.retention_policies
SET data_class = CASE data_class
    WHEN 'agent_tool_audit' THEN 'agent_business_audit'
    WHEN 'security_audit' THEN 'security_global_audit'
    ELSE data_class
END;

ALTER TABLE router.retention_policies
ADD CONSTRAINT retention_policies_data_class_check CHECK (data_class IN (
    'diagnostic_logs', 'captured_content', 'raw_accounting',
    'agent_business_audit', 'daily_accounting', 'security_global_audit',
    'configuration_revisions'
)),
ADD CONSTRAINT retention_policies_check2 CHECK (
    data_class <> 'agent_business_audit' OR retention_days BETWEEN 7 AND 365
);
