-- Refuse rollback while unused manual observations exist. Keep their data.
ALTER TABLE router.assignment_usage
    ALTER COLUMN last_used_at SET NOT NULL;
