CREATE INDEX audit_events_global_discovery_idx
    ON router.audit_events (occurred_at DESC, event_id DESC);
