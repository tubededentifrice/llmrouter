ALTER TABLE router.administrator_sessions
DROP CONSTRAINT administrator_sessions_check,
DROP CONSTRAINT administrator_sessions_check1;

ALTER TABLE router.administrator_sessions
ADD CONSTRAINT administrator_sessions_idle_lifetime
CHECK (idle_expires_at <= last_used_at + interval '7 days'),
ADD CONSTRAINT administrator_sessions_absolute_lifetime
CHECK (absolute_expires_at <= authenticated_at + interval '7 days');
