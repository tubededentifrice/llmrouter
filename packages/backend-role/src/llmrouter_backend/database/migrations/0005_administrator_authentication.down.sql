DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.administrator_oidc_starts)
       OR EXISTS (SELECT 1 FROM router.trusted_administrator_grant_urls)
       OR EXISTS (
           SELECT 1 FROM router.administrator_grant_idempotency_bindings
       )
       OR EXISTS (
           SELECT 1 FROM router.administrators WHERE identity_generation <> 1
       )
       OR EXISTS (
           SELECT 1 FROM router.administrator_sessions
           WHERE recent_authenticated_at IS NOT NULL
              OR identity_generation <> 1
       ) THEN
        RAISE EXCEPTION 'administrator authentication data cannot roll back without data loss';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM router.administrator_grants
        WHERE cardinality(workspace_ids) > 1
           OR (cardinality(workspace_ids) = 1 AND workspace_id IS DISTINCT FROM workspace_ids[1])
    ) THEN
        RAISE EXCEPTION 'administrator grant workspace arrays cannot roll back without data loss';
    END IF;
END;
$$;

UPDATE router.administrator_grants
SET workspace_id = workspace_ids[1]
WHERE cardinality(workspace_ids) = 1;

DROP TRIGGER trusted_administrator_grant_urls_no_delete
ON router.trusted_administrator_grant_urls;
DROP TRIGGER trusted_administrator_grant_urls_protect_update
ON router.trusted_administrator_grant_urls;
DROP TRIGGER administrator_grant_idempotency_bindings_append_only
ON router.administrator_grant_idempotency_bindings;
DROP TRIGGER administrator_oidc_starts_no_delete
ON router.administrator_oidc_starts;
DROP TRIGGER administrator_oidc_starts_protect_update
ON router.administrator_oidc_starts;
DROP TRIGGER administrator_grants_validate_workspace_ids
ON router.administrator_grants;
DROP FUNCTION router.validate_administrator_grant_workspace_ids();
DROP FUNCTION router.protect_administrator_oidc_start();
DROP FUNCTION router.protect_trusted_administrator_grant_url();
DROP TABLE router.administrator_grant_idempotency_bindings;
DROP TABLE router.administrator_oidc_starts;
DROP TABLE router.trusted_administrator_grant_urls;
ALTER TABLE router.administrator_sessions
DROP COLUMN identity_generation,
DROP COLUMN recent_authenticated_at;
ALTER TABLE router.administrator_grants
DROP CONSTRAINT administrator_grants_time_order;
ALTER TABLE router.administrator_grants DROP COLUMN workspace_ids;
ALTER TABLE router.administrators DROP COLUMN identity_generation;
