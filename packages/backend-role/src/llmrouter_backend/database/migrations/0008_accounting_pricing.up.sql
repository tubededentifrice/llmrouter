CREATE TABLE router.external_tool_attempt_identities (
    id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL,
    service_id uuid NOT NULL,
    workspace_id uuid,
    FOREIGN KEY (request_row_id, service_id, workspace_id)
        REFERENCES router.logical_requests (row_id, service_id, workspace_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER external_tool_attempt_identities_append_only
BEFORE UPDATE OR DELETE ON router.external_tool_attempt_identities
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.business_tool_call_identities (
    id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL,
    service_id uuid NOT NULL,
    workspace_id uuid,
    FOREIGN KEY (request_row_id, service_id, workspace_id)
        REFERENCES router.logical_requests (row_id, service_id, workspace_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER business_tool_call_identities_append_only
BEFORE UPDATE OR DELETE ON router.business_tool_call_identities
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.accounting_facts (
    event_id uuid PRIMARY KEY,
    canonical_event_id uuid NOT NULL UNIQUE
        REFERENCES router.canonical_events (event_id) ON DELETE RESTRICT,
    request_row_id uuid NOT NULL,
    service_id uuid NOT NULL,
    workspace_id uuid,
    budget_scope_id uuid NOT NULL,
    assignment_id uuid REFERENCES router.assignment_definitions (id) ON DELETE RESTRICT,
    budget_ledger_event_id uuid
        REFERENCES router.accounting_events (event_id) ON DELETE RESTRICT,
    subject_kind text NOT NULL CHECK (subject_kind IN (
        'logical_request', 'provider_attempt', 'external_tool_attempt',
        'business_tool_call'
    )),
    subject_id uuid NOT NULL,
    outcome text NOT NULL CHECK (outcome IN (
        'succeeded', 'failed', 'refused', 'interrupted', 'uncertain'
    )),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    price_version_id uuid REFERENCES router.route_price_versions (id) ON DELETE RESTRICT,
    amount numeric(38, 18) NOT NULL CHECK (amount >= 0),
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    canonical_payload_sha256 bytea NOT NULL CHECK (octet_length(canonical_payload_sha256) = 32),
    FOREIGN KEY (request_row_id, service_id, workspace_id)
        REFERENCES router.logical_requests (row_id, service_id, workspace_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (budget_scope_id, currency)
        REFERENCES router.budget_scopes (id, currency) ON DELETE RESTRICT
);

CREATE FUNCTION router.check_accounting_subject()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.subject_kind = 'logical_request' AND NEW.subject_id <> NEW.request_row_id THEN
        RAISE EXCEPTION 'logical request accounting subject does not match'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.subject_kind = 'provider_attempt' AND NOT EXISTS (
        SELECT 1 FROM router.provider_attempts
        WHERE id = NEW.subject_id AND request_row_id = NEW.request_row_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND price_version_id = NEW.price_version_id
    ) THEN
        RAISE EXCEPTION 'provider attempt accounting subject does not match'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.subject_kind = 'external_tool_attempt' AND NOT EXISTS (
        SELECT 1 FROM router.external_tool_attempt_identities
        WHERE id = NEW.subject_id AND request_row_id = NEW.request_row_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'external tool accounting subject does not match'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.subject_kind = 'business_tool_call' AND NOT EXISTS (
        SELECT 1 FROM router.business_tool_call_identities
        WHERE id = NEW.subject_id AND request_row_id = NEW.request_row_id
          AND service_id = NEW.service_id
          AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'business tool accounting subject does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.check_accounting_budget_ledger_link()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.budget_ledger_event_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.accounting_events AS event
        WHERE event.event_id = NEW.budget_ledger_event_id
          AND event.request_row_id = NEW.request_row_id
          AND event.budget_scope_id = NEW.budget_scope_id
          AND event.currency = NEW.currency
          AND event.amount = NEW.amount
    ) THEN
        RAISE EXCEPTION 'budget ledger event does not match accounting fact'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.check_accounting_canonical_event()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.canonical_events
        WHERE event_id = NEW.canonical_event_id
          AND event_class = 'accounting'
          AND occurred_at = NEW.occurred_at
          AND payload_sha256 = NEW.canonical_payload_sha256
    ) THEN
        RAISE EXCEPTION 'canonical accounting event does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.check_accounting_budget_scope()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.budget_scopes AS budget
        WHERE budget.id = NEW.budget_scope_id
          AND budget.currency = NEW.currency
          AND (
              budget.scope_kind = 'global'
              OR (budget.scope_kind = 'service'
                  AND budget.service_id = NEW.service_id)
              OR (budget.scope_kind = 'workspace'
                  AND budget.service_id = NEW.service_id
                  AND budget.workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
              OR (budget.scope_kind = 'assignment'
                  AND NEW.subject_kind = 'provider_attempt'
                  AND budget.service_id = NEW.service_id
                  AND NEW.assignment_id = budget.assignment_id
                  AND EXISTS (
                      SELECT 1 FROM router.provider_attempts AS attempt
                      JOIN router.assignment_candidates AS candidate
                        ON candidate.configuration_revision_id = attempt.assignment_revision_id
                       AND candidate.provider_model_route_id = attempt.provider_model_route_id
                      WHERE attempt.id = NEW.subject_id
                        AND candidate.assignment_id = NEW.assignment_id
                  ))
          )
    ) THEN
        RAISE EXCEPTION 'accounting budget scope does not apply to the event'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER accounting_facts_subject
BEFORE INSERT ON router.accounting_facts
FOR EACH ROW EXECUTE FUNCTION router.check_accounting_subject();

CREATE TRIGGER accounting_facts_canonical_event
BEFORE INSERT ON router.accounting_facts
FOR EACH ROW EXECUTE FUNCTION router.check_accounting_canonical_event();

CREATE TRIGGER accounting_facts_budget_scope
BEFORE INSERT ON router.accounting_facts
FOR EACH ROW EXECUTE FUNCTION router.check_accounting_budget_scope();

CREATE TRIGGER accounting_facts_budget_ledger_link
BEFORE INSERT ON router.accounting_facts
FOR EACH ROW EXECUTE FUNCTION router.check_accounting_budget_ledger_link();

CREATE TRIGGER accounting_facts_append_only
BEFORE UPDATE OR DELETE ON router.accounting_facts
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE INDEX accounting_facts_scope_time_idx
    ON router.accounting_facts (service_id, workspace_id, occurred_at, event_id);

CREATE TABLE router.accounting_usage_components (
    event_id uuid NOT NULL REFERENCES router.accounting_facts (event_id) ON DELETE RESTRICT,
    unit_name text NOT NULL CHECK (unit_name IN (
        'input_token', 'output_token', 'cached_token', 'request', 'image',
        'audio_second', 'search', 'tool_unit', 'other'
    )),
    quantity numeric(38, 18) NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (event_id, unit_name)
);

CREATE TRIGGER accounting_usage_components_append_only
BEFORE UPDATE OR DELETE ON router.accounting_usage_components
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.accounting_corrections (
    correction_id uuid PRIMARY KEY,
    source_event_id uuid NOT NULL REFERENCES router.accounting_facts (event_id) ON DELETE RESTRICT,
    correction_kind text NOT NULL CHECK (correction_kind IN (
        'price', 'provider_usage', 'invoice'
    )),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    amount_delta numeric(38, 18) NOT NULL,
    source_name text NOT NULL CHECK (source_name <> ''),
    reason text NOT NULL CHECK (reason <> ''),
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE FUNCTION router.check_correction_currency()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.accounting_facts
        WHERE event_id = NEW.source_event_id AND currency = NEW.currency
    ) THEN
        RAISE EXCEPTION 'correction currency does not match its source event'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER accounting_corrections_currency
BEFORE INSERT ON router.accounting_corrections
FOR EACH ROW EXECUTE FUNCTION router.check_correction_currency();

CREATE TRIGGER accounting_corrections_append_only
BEFORE UPDATE OR DELETE ON router.accounting_corrections
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.accounting_correction_usage (
    correction_id uuid NOT NULL
        REFERENCES router.accounting_corrections (correction_id) ON DELETE RESTRICT,
    unit_name text NOT NULL CHECK (unit_name IN (
        'input_token', 'output_token', 'cached_token', 'request', 'image',
        'audio_second', 'search', 'tool_unit', 'other'
    )),
    quantity_delta numeric(38, 18) NOT NULL,
    PRIMARY KEY (correction_id, unit_name)
);

CREATE TRIGGER accounting_correction_usage_append_only
BEFORE UPDATE OR DELETE ON router.accounting_correction_usage
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

ALTER TABLE router.route_price_sources
ADD COLUMN migration_0008_schedule_was_null boolean NOT NULL DEFAULT false;

UPDATE router.route_price_sources
SET synchronization_schedule = '0 0 * * 0',
    migration_0008_schedule_was_null = true
WHERE synchronization_schedule IS NULL;

ALTER TABLE router.route_price_sources
ALTER COLUMN synchronization_schedule SET DEFAULT '0 0 * * 0',
ALTER COLUMN synchronization_schedule SET NOT NULL;

ALTER TABLE router.route_price_sources
ALTER COLUMN source_name DROP NOT NULL,
ALTER COLUMN lookup_identifier DROP NOT NULL,
DROP CONSTRAINT route_price_sources_source_name_check,
DROP CONSTRAINT route_price_sources_lookup_identifier_check;

ALTER TABLE router.route_price_sources
ADD CONSTRAINT route_price_sources_authority_values CHECK (
    (authority_kind = 'manual'
     )
    OR (authority_kind = 'synchronized'
        AND source_name IS NOT NULL AND source_name <> ''
        AND lookup_identifier IS NOT NULL AND lookup_identifier <> '')
);

ALTER TABLE router.price_source_snapshots
DROP CONSTRAINT price_source_snapshots_source_name_content_sha256_key;

ALTER TABLE router.price_source_snapshots
ADD COLUMN source_available boolean NOT NULL DEFAULT true,
ADD CONSTRAINT price_source_snapshots_source_name_bound CHECK (
    source_name ~ '^[a-z][a-z0-9._-]{0,99}$'
),
ADD CONSTRAINT price_source_snapshots_source_revision_bound CHECK (
    source_revision IS NULL OR length(source_revision) <= 500
),
ADD CONSTRAINT price_source_snapshots_http_validator_bound CHECK (
    http_validator IS NULL OR length(http_validator) <= 500
);

CREATE TABLE router.route_price_synchronization_states (
    provider_model_route_id uuid PRIMARY KEY
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    synchronization_state text NOT NULL CHECK (synchronization_state IN (
        'manual', 'current', 'stale', 'missing', 'failed'
    )),
    last_price_version_id uuid
        REFERENCES router.route_price_versions (id) ON DELETE RESTRICT,
    last_error_class text CHECK (last_error_class IS NULL OR last_error_class IN (
        'source_unavailable', 'missing_row', 'invalid_value',
        'unsupported_unit', 'currency_mismatch'
    )),
    observed_at timestamptz NOT NULL,
    migration_0008_backfilled boolean NOT NULL DEFAULT false
);

CREATE TABLE router.configuration_price_bindings (
    configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    provider_model_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    price_version_id uuid NOT NULL,
    migration_0008_backfilled boolean NOT NULL DEFAULT false,
    PRIMARY KEY (configuration_revision_id, provider_model_route_id),
    FOREIGN KEY (price_version_id, provider_model_route_id)
        REFERENCES router.route_price_versions (id, provider_model_route_id)
        ON DELETE RESTRICT
);

INSERT INTO router.configuration_price_bindings (
    configuration_revision_id, provider_model_route_id, price_version_id,
    migration_0008_backfilled
)
SELECT revision.id, route.provider_model_route_id, version.id, true
FROM router.configuration_revisions AS revision
CROSS JOIN LATERAL jsonb_array_elements(
    COALESCE(revision.content -> 'provider_model_routes', '[]'::jsonb)
) AS item
CROSS JOIN LATERAL (
    SELECT (item ->> 'provider_model_route_id')::uuid AS provider_model_route_id
) AS route
CROSS JOIN LATERAL (
    SELECT candidate.id
    FROM router.route_price_versions AS candidate
    WHERE candidate.provider_model_route_id = route.provider_model_route_id
    ORDER BY candidate.version_number DESC
    LIMIT 1
) AS version;

INSERT INTO router.route_price_synchronization_states (
    provider_model_route_id, synchronization_state,
    last_price_version_id, observed_at, migration_0008_backfilled
)
SELECT source.provider_model_route_id,
       CASE WHEN source.authority_kind = 'manual' THEN 'manual'
            WHEN version.id IS NULL THEN 'missing' ELSE 'current' END,
       version.id,
       COALESCE(version.accepted_at, transaction_timestamp()), true
FROM router.route_price_sources AS source
LEFT JOIN LATERAL (
    SELECT candidate.id, candidate.accepted_at
    FROM router.route_price_versions AS candidate
    WHERE candidate.provider_model_route_id = source.provider_model_route_id
    ORDER BY candidate.version_number DESC
    LIMIT 1
) AS version ON true;

CREATE TRIGGER configuration_price_bindings_append_only
BEFORE UPDATE OR DELETE ON router.configuration_price_bindings
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.price_synchronization_runs (
    id uuid PRIMARY KEY,
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    source_name text NOT NULL CHECK (source_name <> ''),
    source_snapshot_id uuid REFERENCES router.price_source_snapshots (id) ON DELETE RESTRICT,
    dry_run boolean NOT NULL,
    state text NOT NULL CHECK (state IN (
        'previewed', 'queued', 'running', 'completed', 'failed'
    )),
    resulting_configuration_revision_id uuid
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    CHECK ((state IN ('completed', 'failed')) = (completed_at IS NOT NULL)),
    CHECK (source_snapshot_id IS NOT NULL)
);

CREATE FUNCTION router.protect_price_synchronization_run()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.id <> NEW.id OR OLD.service_id IS DISTINCT FROM NEW.service_id
       OR OLD.source_name <> NEW.source_name OR OLD.source_snapshot_id <> NEW.source_snapshot_id
       OR OLD.dry_run <> NEW.dry_run OR OLD.state <> NEW.state
       OR OLD.started_at <> NEW.started_at OR OLD.completed_at IS DISTINCT FROM NEW.completed_at
       OR OLD.resulting_configuration_revision_id IS NOT NULL
       OR NEW.resulting_configuration_revision_id IS NULL THEN
        RAISE EXCEPTION 'price synchronization run is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER price_synchronization_runs_append_only
BEFORE UPDATE OR DELETE ON router.price_synchronization_runs
FOR EACH ROW EXECUTE FUNCTION router.protect_price_synchronization_run();

CREATE TABLE router.price_synchronization_idempotency (
    actor_id text NOT NULL CHECK (actor_id <> ''),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    run_id uuid NOT NULL UNIQUE
        REFERENCES router.price_synchronization_runs (id) ON DELETE RESTRICT,
    PRIMARY KEY (actor_id, idempotency_key)
);

CREATE TABLE router.price_publication_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    synchronization_run_id uuid NOT NULL
        REFERENCES router.price_synchronization_runs (id) ON DELETE RESTRICT,
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    state text NOT NULL CHECK (state IN ('pending', 'published')),
    resulting_configuration_revision_id uuid
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    accepted_at timestamptz NOT NULL,
    published_at timestamptz,
    UNIQUE NULLS NOT DISTINCT (synchronization_run_id, service_id),
    CHECK (
        (state = 'pending' AND resulting_configuration_revision_id IS NULL
                           AND published_at IS NULL)
        OR (state = 'published' AND resulting_configuration_revision_id IS NOT NULL
                             AND published_at IS NOT NULL)
    )
);

CREATE TABLE router.price_synchronization_publications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    synchronization_run_id uuid NOT NULL
        REFERENCES router.price_synchronization_runs (id) ON DELETE RESTRICT,
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    configuration_revision_id uuid NOT NULL UNIQUE
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    UNIQUE NULLS NOT DISTINCT (synchronization_run_id, service_id)
);

CREATE TRIGGER price_synchronization_publications_append_only
BEFORE UPDATE OR DELETE ON router.price_synchronization_publications
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.price_synchronization_results (
    run_id uuid NOT NULL REFERENCES router.price_synchronization_runs (id) ON DELETE RESTRICT,
    provider_model_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    lookup_identifier text NOT NULL CHECK (lookup_identifier <> ''),
    status text NOT NULL CHECK (status IN (
        'updated', 'unchanged', 'skipped', 'missing', 'failed'
    )),
    synchronization_state text NOT NULL CHECK (synchronization_state IN (
        'manual', 'current', 'stale', 'missing', 'failed'
    )),
    old_prices jsonb NOT NULL CHECK (jsonb_typeof(old_prices) = 'array'),
    new_prices jsonb NOT NULL CHECK (jsonb_typeof(new_prices) = 'array'),
    price_version_id uuid
        REFERENCES router.route_price_versions (id) ON DELETE RESTRICT,
    error_class text CHECK (error_class IS NULL OR error_class IN (
        'source_unavailable', 'missing_row', 'invalid_value',
        'unsupported_unit', 'currency_mismatch'
    )),
    synchronized_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, provider_model_route_id)
);

CREATE TRIGGER price_synchronization_results_append_only
BEFORE UPDATE OR DELETE ON router.price_synchronization_results
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.daily_accounting_aggregates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    accounting_day date NOT NULL,
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    logical_requests bigint NOT NULL CHECK (logical_requests >= 0),
    attempts bigint NOT NULL CHECK (attempts >= 0),
    cost numeric(38, 18) NOT NULL CHECK (cost >= 0),
    corrections numeric(38, 18) NOT NULL,
    usage jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(usage) = 'object'),
    rebuilt_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    UNIQUE NULLS NOT DISTINCT (accounting_day, service_id, workspace_id, currency)
);

CREATE TRIGGER daily_accounting_aggregates_write_guard
BEFORE UPDATE ON router.daily_accounting_aggregates
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();
