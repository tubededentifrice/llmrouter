UPDATE router.administrator_sessions
SET revoked_at = COALESCE(revoked_at, transaction_timestamp()),
    provider_access_token_ciphertext = NULL,
    provider_refresh_token_ciphertext = NULL,
    provider_access_expires_at = NULL,
    absolute_expires_at = LEAST(
        absolute_expires_at,
        authenticated_at + interval '8 hours'
    ),
    idle_expires_at = LEAST(
        idle_expires_at,
        last_used_at + interval '15 minutes',
        authenticated_at + interval '8 hours'
    )
WHERE idle_expires_at > last_used_at + interval '15 minutes'
   OR absolute_expires_at > authenticated_at + interval '8 hours';

ALTER TABLE router.administrator_sessions
DROP CONSTRAINT administrator_sessions_idle_lifetime,
DROP CONSTRAINT administrator_sessions_absolute_lifetime;

ALTER TABLE router.administrator_sessions
ADD CONSTRAINT administrator_sessions_check
CHECK (idle_expires_at <= last_used_at + interval '15 minutes'),
ADD CONSTRAINT administrator_sessions_check1
CHECK (absolute_expires_at <= authenticated_at + interval '8 hours');
