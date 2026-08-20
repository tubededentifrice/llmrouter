ALTER TABLE router.embed_sessions
ADD COLUMN recent_auth_at timestamptz,
ADD COLUMN theme_mode text NOT NULL DEFAULT 'system',
ADD COLUMN theme_density text NOT NULL DEFAULT 'comfortable',
ADD COLUMN theme_corner_style text NOT NULL DEFAULT 'rounded',
ADD COLUMN frame_nonce_digest bytea,
ADD COLUMN session_token_digest bytea UNIQUE,
ADD CONSTRAINT embed_sessions_one_workspace CHECK (cardinality(workspace_ids) <= 1),
ADD CONSTRAINT embed_sessions_permissions_closed CHECK (
    permitted_actions <@ ARRAY[
        'configuration.read', 'configuration.write', 'budget.read',
        'budget.write', 'accounting.read', 'request_status.read',
        'health.read', 'diagnostic.run'
    ]::text[]
),
ADD CONSTRAINT embed_sessions_origin_bounds CHECK (
    char_length(host_subject) BETWEEN 1 AND 200
    AND
    char_length(host_origin) BETWEEN 1 AND 2000
    AND char_length(frame_origin) BETWEEN 1 AND 2000
),
ADD CONSTRAINT embed_sessions_theme_closed CHECK (
    theme_mode IN ('light', 'dark', 'system')
    AND theme_density IN ('comfortable', 'compact')
    AND theme_corner_style IN ('square', 'rounded')
),
ADD CONSTRAINT embed_sessions_lifetime CHECK (
    expires_at > created_at
    AND expires_at <= created_at + interval '5 minutes'
    AND (
        recent_auth_at IS NULL
        OR (
            recent_auth_at <= created_at
            AND expires_at <= recent_auth_at + interval '5 minutes'
        )
    )
),
ADD CONSTRAINT embed_sessions_sensitive_recent_auth CHECK (
    NOT (permitted_actions && ARRAY[
        'configuration.write', 'budget.write', 'diagnostic.run'
    ]::text[])
    OR recent_auth_at IS NOT NULL
),
ADD CONSTRAINT embed_sessions_redemption_state CHECK (
    (
        (
            redeemed_at IS NULL
            AND frame_nonce_digest IS NULL
            AND session_token_digest IS NULL
        ) OR (
            redeemed_at IS NOT NULL
            AND frame_nonce_digest IS NOT NULL
            AND session_token_digest IS NOT NULL
        )
    )
    AND (frame_nonce_digest IS NULL OR octet_length(frame_nonce_digest) = 32)
    AND (session_token_digest IS NULL OR octet_length(session_token_digest) = 32)
    AND (
        redeemed_at IS NULL
        OR (redeemed_at >= created_at AND redeemed_at < expires_at)
    )
    AND (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX embed_sessions_expiry_idx
ON router.embed_sessions (expires_at)
WHERE revoked_at IS NULL;
