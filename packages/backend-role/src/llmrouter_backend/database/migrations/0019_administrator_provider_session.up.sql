ALTER TABLE router.administrator_sessions
ADD COLUMN provider_access_token_ciphertext bytea,
ADD COLUMN provider_refresh_token_ciphertext bytea,
ADD COLUMN provider_access_expires_at timestamptz;

UPDATE router.administrator_sessions
SET revoked_at = COALESCE(revoked_at, transaction_timestamp())
WHERE provider_access_token_ciphertext IS NULL;

ALTER TABLE router.administrator_sessions
ADD CONSTRAINT administrator_sessions_provider_tokens
CHECK (
    (revoked_at IS NOT NULL
     AND provider_access_token_ciphertext IS NULL
     AND provider_refresh_token_ciphertext IS NULL
     AND provider_access_expires_at IS NULL)
    OR
    (revoked_at IS NULL
     AND octet_length(provider_access_token_ciphertext) BETWEEN 41 AND 8232
     AND octet_length(provider_refresh_token_ciphertext) BETWEEN 41 AND 8232
     AND provider_access_expires_at IS NOT NULL)
);
