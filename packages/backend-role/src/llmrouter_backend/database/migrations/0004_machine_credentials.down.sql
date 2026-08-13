DROP FUNCTION router.check_service_access_token_scope() CASCADE;
DROP FUNCTION router.protect_service_access_token() CASCADE;
DROP FUNCTION router.protect_bootstrap_generation() CASCADE;
DROP TABLE router.service_machine_tls_identities;
DROP FUNCTION router.protect_machine_tls_identity();
DROP TABLE router.service_machine_tls_policies;
DROP FUNCTION router.protect_machine_tls_policy();

ALTER TABLE router.service_access_tokens
DROP CONSTRAINT service_token_workspace_count,
DROP CONSTRAINT service_token_lifetime_exact,
DROP CONSTRAINT service_token_operations_match_audience,
DROP CONSTRAINT service_token_audience_closed,
DROP CONSTRAINT service_token_digest_key_not_empty,
DROP CONSTRAINT service_token_issuer_not_empty,
DROP COLUMN workspace_restricted,
DROP COLUMN digest_key_id,
DROP COLUMN issuer;

ALTER TABLE router.service_bootstrap_generations
DROP CONSTRAINT bootstrap_validity_after_creation,
DROP CONSTRAINT bootstrap_workspace_limit_valid,
DROP CONSTRAINT bootstrap_operations_match_audiences,
DROP CONSTRAINT bootstrap_audiences_closed,
DROP CONSTRAINT bootstrap_audiences_not_empty,
DROP CONSTRAINT bootstrap_issuer_not_empty,
DROP COLUMN workspace_limit,
DROP COLUMN allowed_audiences,
DROP COLUMN issuer;
