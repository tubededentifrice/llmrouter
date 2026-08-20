UPDATE router.administrator_sessions
SET revoked_at = COALESCE(revoked_at, transaction_timestamp()),
    provider_access_token_ciphertext = NULL,
    provider_refresh_token_ciphertext = NULL,
    provider_access_expires_at = NULL;

ALTER TABLE router.administrator_sessions
DROP CONSTRAINT administrator_sessions_provider_tokens,
DROP COLUMN provider_access_token_ciphertext,
DROP COLUMN provider_refresh_token_ciphertext,
DROP COLUMN provider_access_expires_at;
