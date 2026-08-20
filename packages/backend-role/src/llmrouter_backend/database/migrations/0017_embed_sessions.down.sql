DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.embed_sessions) THEN
        RAISE EXCEPTION 'embed session rollback would cause data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP INDEX router.embed_sessions_expiry_idx;

ALTER TABLE router.embed_sessions
DROP CONSTRAINT embed_sessions_redemption_state,
DROP CONSTRAINT embed_sessions_lifetime,
DROP CONSTRAINT embed_sessions_theme_closed,
DROP CONSTRAINT embed_sessions_origin_bounds,
DROP CONSTRAINT embed_sessions_permissions_closed,
DROP CONSTRAINT embed_sessions_one_workspace,
DROP COLUMN session_token_digest,
DROP COLUMN frame_nonce_digest,
DROP COLUMN theme_corner_style,
DROP COLUMN theme_density,
DROP COLUMN theme_mode,
DROP COLUMN recent_auth_at;
