DROP TRIGGER budget_allowance_leases_guard ON router.budget_allowance_leases;
DROP FUNCTION router.check_budget_allowance();

CREATE TABLE router.budget_allowance_batches (
    id uuid PRIMARY KEY,
    lineage_id uuid NOT NULL,
    owner_node_id uuid NOT NULL,
    lease_generation bigint NOT NULL CHECK (lease_generation > 0),
    service_id uuid REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    assignment_id uuid REFERENCES router.assignment_definitions (id) ON DELETE RESTRICT,
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    safety_until timestamptz NOT NULL,
    legacy boolean NOT NULL DEFAULT false,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK (safety_until >= expires_at),
    CHECK (service_id IS NOT NULL OR (workspace_id IS NULL AND assignment_id IS NULL)),
    UNIQUE (lineage_id, lease_generation)
);

ALTER TABLE router.budget_allowance_leases
ADD COLUMN batch_id uuid,
ADD COLUMN maximum_correction_risk numeric(38, 18) NOT NULL DEFAULT 0
    CHECK (maximum_correction_risk >= 0);

INSERT INTO router.budget_allowance_batches (
    id, lineage_id, owner_node_id, lease_generation, service_id, workspace_id,
    assignment_id, currency, issued_at, expires_at, safety_until, legacy
)
SELECT lease.id, lease.id, lease.owner_node_id, lease.lease_generation,
       scope.service_id, scope.workspace_id, scope.assignment_id,
       lease.currency, lease.issued_at, lease.expires_at, lease.safety_until, true
FROM router.budget_allowance_leases AS lease
JOIN router.budget_scopes AS scope ON scope.id = lease.budget_scope_id;

UPDATE router.budget_allowance_leases SET batch_id = id;
ALTER TABLE router.budget_allowance_leases
ALTER COLUMN batch_id SET NOT NULL,
ADD CONSTRAINT budget_allowance_leases_batch_fk
    FOREIGN KEY (batch_id) REFERENCES router.budget_allowance_batches (id)
    ON DELETE RESTRICT,
ADD CONSTRAINT budget_allowance_leases_batch_scope_unique
    UNIQUE (batch_id, budget_scope_id);

CREATE TABLE router.budget_allowance_reconciliations (
    reconciliation_id uuid PRIMARY KEY,
    allowance_lease_id uuid NOT NULL UNIQUE
        REFERENCES router.budget_allowance_leases (id) ON DELETE RESTRICT,
    owner_node_id uuid NOT NULL,
    lease_generation bigint NOT NULL CHECK (lease_generation > 0),
    used_amount numeric(38, 18) NOT NULL CHECK (used_amount >= 0),
    returned_amount numeric(38, 18) NOT NULL CHECK (returned_amount >= 0),
    occurred_at timestamptz NOT NULL,
    reclaimed boolean NOT NULL
);

CREATE TABLE router.budget_allowance_corrections (
    correction_id uuid PRIMARY KEY,
    allowance_lease_id uuid NOT NULL
        REFERENCES router.budget_allowance_reconciliations (allowance_lease_id)
        ON DELETE RESTRICT,
    amount_delta numeric(38, 18) NOT NULL,
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    occurred_at timestamptz NOT NULL,
    UNIQUE (allowance_lease_id, correction_id)
);

CREATE TABLE router.budget_allowance_ledger_entries (
    event_id uuid PRIMARY KEY,
    allowance_lease_id uuid NOT NULL
        REFERENCES router.budget_allowance_leases (id) ON DELETE RESTRICT,
    budget_scope_id uuid NOT NULL REFERENCES router.budget_scopes (id) ON DELETE RESTRICT,
    event_kind text NOT NULL CHECK (event_kind IN ('grant', 'usage', 'return', 'correction')),
    amount numeric(38, 18) NOT NULL,
    source_reconciliation_id uuid
        REFERENCES router.budget_allowance_reconciliations (reconciliation_id)
        ON DELETE RESTRICT,
    source_correction_id uuid
        REFERENCES router.budget_allowance_corrections (correction_id)
        ON DELETE RESTRICT,
    occurred_at timestamptz NOT NULL,
    CHECK ((event_kind <> 'correction' AND amount >= 0) OR event_kind = 'correction'),
    CHECK ((event_kind IN ('usage', 'return')) = (source_reconciliation_id IS NOT NULL)),
    CHECK ((event_kind = 'correction') = (source_correction_id IS NOT NULL))
);

CREATE UNIQUE INDEX budget_allowance_ledger_one_event_idx
ON router.budget_allowance_ledger_entries (allowance_lease_id, event_kind)
WHERE event_kind IN ('grant', 'usage', 'return');

INSERT INTO router.budget_allowance_ledger_entries (
    event_id, allowance_lease_id, budget_scope_id, event_kind, amount, occurred_at
)
SELECT id, id, budget_scope_id, 'grant', issued_amount, issued_at
FROM router.budget_allowance_leases;

CREATE FUNCTION router.allowance_scope_consumed(
    requested_scope uuid, requested_at timestamptz
)
RETURNS numeric LANGUAGE sql STABLE AS $$
    SELECT
        COALESCE((SELECT sum(CASE event_kind
            WHEN 'reservation' THEN amount WHEN 'release' THEN -amount ELSE 0 END)
          FROM router.budget_ledger_entries
          WHERE budget_scope_id = requested_scope), 0)
      + greatest(COALESCE((SELECT sum(CASE event_kind
            WHEN 'usage' THEN amount WHEN 'correction' THEN amount ELSE 0 END)
          FROM router.budget_ledger_entries AS entry
          JOIN router.budget_scopes AS scope ON scope.id = entry.budget_scope_id
          WHERE entry.budget_scope_id = requested_scope
            AND (scope.reset_period = 'none'
              OR (scope.reset_period = 'daily' AND entry.occurred_at >=
                  date_trunc('day', requested_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
              OR (scope.reset_period = 'monthly' AND entry.occurred_at >=
                  date_trunc('month', requested_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'))), 0), 0)
      + COALESCE((SELECT sum(lease.issued_amount)
          FROM router.budget_allowance_leases AS lease
          JOIN router.budget_scopes AS scope ON scope.id = lease.budget_scope_id
          WHERE lease.budget_scope_id = requested_scope
            AND NOT EXISTS (SELECT 1 FROM router.budget_allowance_reconciliations AS final
                            WHERE final.allowance_lease_id = lease.id)
            AND (scope.reset_period = 'none'
              OR (scope.reset_period = 'daily' AND lease.issued_at >=
                  date_trunc('day', requested_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
              OR (scope.reset_period = 'monthly' AND lease.issued_at >=
                  date_trunc('month', requested_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'))), 0)
      + greatest(COALESCE((SELECT sum(CASE ledger.event_kind
            WHEN 'usage' THEN ledger.amount
            WHEN 'correction' THEN ledger.amount ELSE 0 END)
          FROM router.budget_allowance_ledger_entries AS ledger
          JOIN router.budget_scopes AS scope ON scope.id = ledger.budget_scope_id
          WHERE ledger.budget_scope_id = requested_scope
            AND (scope.reset_period = 'none'
              OR (scope.reset_period = 'daily' AND ledger.occurred_at >=
                  date_trunc('day', requested_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
              OR (scope.reset_period = 'monthly' AND ledger.occurred_at >=
                  date_trunc('month', requested_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'))), 0), 0)
$$;

CREATE FUNCTION router.check_budget_allowance_batch()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    highest_generation bigint;
    prior_owner uuid;
    prior_safety timestamptz;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'budget allowance batches are append only' USING ERRCODE = '55000';
    END IF;
    IF NEW.legacy THEN
        RAISE EXCEPTION 'new budget allowance batches cannot use legacy mode'
            USING ERRCODE = '23514';
    END IF;
    SELECT lease_generation, owner_node_id, safety_until
    INTO highest_generation, prior_owner, prior_safety
    FROM router.budget_allowance_batches
    WHERE lineage_id = NEW.lineage_id
    ORDER BY lease_generation DESC LIMIT 1 FOR UPDATE;
    IF highest_generation IS NOT NULL AND NEW.lease_generation <= highest_generation THEN
        RAISE EXCEPTION 'budget allowance generation is stale' USING ERRCODE = '40001';
    END IF;
    IF highest_generation IS NULL AND NEW.lease_generation <> 1 THEN
        RAISE EXCEPTION 'a budget allowance lineage must start at generation one'
            USING ERRCODE = '23514';
    END IF;
    IF highest_generation IS NOT NULL AND EXISTS (
        SELECT 1 FROM router.budget_allowance_batches AS prior
        WHERE prior.lineage_id = NEW.lineage_id
          AND prior.lease_generation = highest_generation
          AND (prior.service_id IS DISTINCT FROM NEW.service_id
            OR prior.workspace_id IS DISTINCT FROM NEW.workspace_id
            OR prior.assignment_id IS DISTINCT FROM NEW.assignment_id
            OR prior.currency <> NEW.currency)
    ) THEN
        RAISE EXCEPTION 'budget allowance lineage scope is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF prior_owner IS NOT NULL AND prior_owner <> NEW.owner_node_id
       AND NEW.issued_at < prior_safety THEN
        RAISE EXCEPTION 'budget allowance owner remains inside its safety window'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_allowance_batches_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.budget_allowance_batches
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance_batch();

CREATE FUNCTION router.check_budget_allowance_lease()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    batch router.budget_allowance_batches%ROWTYPE;
    scope_limit numeric(38, 18);
    reset_boundary timestamptz;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'budget allowance leases are append only' USING ERRCODE = '55000';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    SELECT * INTO batch FROM router.budget_allowance_batches WHERE id = NEW.batch_id FOR UPDATE;
    IF batch.id IS NULL OR NEW.owner_node_id <> batch.owner_node_id
       OR NEW.lease_generation <> batch.lease_generation
       OR NEW.currency <> batch.currency OR NEW.issued_at <> batch.issued_at
       OR NEW.expires_at <> batch.expires_at OR NEW.safety_until <> batch.safety_until
       OR NEW.consumed_amount <> 0 THEN
        RAISE EXCEPTION 'budget allowance lease does not match its batch'
            USING ERRCODE = '23514';
    END IF;
    SELECT hard_limit,
           CASE reset_period
             WHEN 'daily' THEN date_trunc('day', NEW.issued_at AT TIME ZONE 'UTC')
                 AT TIME ZONE 'UTC' + interval '1 day'
             WHEN 'monthly' THEN date_trunc('month', NEW.issued_at AT TIME ZONE 'UTC')
                 AT TIME ZONE 'UTC' + interval '1 month'
             ELSE NULL
           END
    INTO scope_limit, reset_boundary FROM router.budget_scopes
    WHERE id = NEW.budget_scope_id AND currency = NEW.currency FOR UPDATE;
    IF scope_limit IS NULL THEN
        RAISE EXCEPTION 'budget allowance scope or currency does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF router.allowance_scope_consumed(NEW.budget_scope_id, NEW.issued_at)
       + NEW.issued_amount > scope_limit THEN
        RAISE EXCEPTION 'budget allowance grant exceeds the hard limit'
            USING ERRCODE = '23514';
    END IF;
    IF reset_boundary IS NOT NULL AND NEW.expires_at > reset_boundary THEN
        RAISE EXCEPTION 'budget allowance cannot cross a reset boundary'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_allowance_leases_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.budget_allowance_leases
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance_lease();

CREATE FUNCTION router.budget_allowance_batch_is_complete(requested_batch uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS (
        WITH RECURSIVE batch AS (
            SELECT * FROM router.budget_allowance_batches WHERE id = requested_batch
        ), ancestors AS (
            SELECT service.id, service.parent_service_id
            FROM router.services AS service
            JOIN batch ON batch.service_id = service.id
          UNION ALL
            SELECT service.id, service.parent_service_id FROM router.services AS service
            JOIN ancestors ON ancestors.parent_service_id = service.id
        ), applicable AS (
            SELECT scope.id FROM router.budget_scopes AS scope CROSS JOIN batch
            WHERE scope.scope_kind = 'global'
               OR (scope.scope_kind = 'service' AND scope.service_id IN (SELECT id FROM ancestors))
               OR (scope.scope_kind IN ('workspace', 'host_ceiling')
                   AND scope.service_id = batch.service_id
                   AND scope.workspace_id IS NOT DISTINCT FROM batch.workspace_id)
               OR (scope.scope_kind = 'assignment' AND scope.service_id = batch.service_id
                   AND scope.assignment_id IS NOT DISTINCT FROM batch.assignment_id
                   AND (scope.workspace_id IS NULL OR scope.workspace_id IS NOT DISTINCT FROM batch.workspace_id))
        ), granted AS (
            SELECT budget_scope_id AS id FROM router.budget_allowance_leases
            WHERE batch_id = requested_batch
        )
        (SELECT id FROM applicable EXCEPT SELECT id FROM granted)
        UNION ALL (SELECT id FROM granted EXCEPT SELECT id FROM applicable)
    )
$$;

CREATE FUNCTION router.check_complete_budget_allowance_batch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT router.budget_allowance_batch_is_complete(NEW.id) THEN
        RAISE EXCEPTION 'budget allowance batch scopes are incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER budget_allowance_batches_complete
AFTER INSERT ON router.budget_allowance_batches DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_allowance_batch();

CREATE FUNCTION router.check_outstanding_budget_allowance_topology()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.budget_allowance_batches AS batch
        WHERE NOT batch.legacy
          AND EXISTS (
              SELECT 1 FROM router.budget_allowance_leases AS lease
              WHERE lease.batch_id = batch.id
                AND NOT EXISTS (
                    SELECT 1 FROM router.budget_allowance_reconciliations AS final
                    WHERE final.allowance_lease_id = lease.id
                )
          )
          AND NOT router.budget_allowance_batch_is_complete(batch.id)
    ) THEN
        RAISE EXCEPTION 'budget topology would invalidate an outstanding allowance'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER budget_scopes_allowance_topology
AFTER INSERT OR UPDATE ON router.budget_scopes DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_outstanding_budget_allowance_topology();
CREATE CONSTRAINT TRIGGER services_allowance_topology
AFTER UPDATE OF parent_service_id ON router.services DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_outstanding_budget_allowance_topology();

CREATE FUNCTION router.check_budget_allowance_reconciliation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE lease router.budget_allowance_leases%ROWTYPE;
BEGIN
    SELECT * INTO lease FROM router.budget_allowance_leases
    WHERE id = NEW.allowance_lease_id FOR UPDATE;
    IF lease.id IS NULL OR NEW.owner_node_id <> lease.owner_node_id
       OR NEW.lease_generation <> lease.lease_generation
       OR NEW.used_amount + NEW.returned_amount <> lease.issued_amount THEN
        RAISE EXCEPTION 'budget allowance reconciliation does not match its lease'
            USING ERRCODE = '40001';
    END IF;
    IF NEW.reclaimed AND NEW.occurred_at < lease.safety_until THEN
        RAISE EXCEPTION 'budget allowance reclaim precedes its safety window'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.reclaimed AND NEW.returned_amount <> 0 THEN
        RAISE EXCEPTION 'a reclaimed allowance must remain conservatively used'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_allowance_reconciliations_guard
BEFORE INSERT ON router.budget_allowance_reconciliations
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance_reconciliation();
CREATE TRIGGER budget_allowance_reconciliations_append_only
BEFORE UPDATE OR DELETE ON router.budget_allowance_reconciliations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_budget_allowance_ledger()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE lease router.budget_allowance_leases%ROWTYPE;
BEGIN
    SELECT * INTO lease FROM router.budget_allowance_leases WHERE id = NEW.allowance_lease_id;
    IF lease.id IS NULL OR NEW.budget_scope_id <> lease.budget_scope_id THEN
        RAISE EXCEPTION 'budget allowance ledger scope does not match' USING ERRCODE = '23514';
    END IF;
    IF NEW.event_kind = 'grant' AND (NEW.amount <> lease.issued_amount OR NEW.occurred_at <> lease.issued_at) THEN
        RAISE EXCEPTION 'budget allowance grant ledger does not match' USING ERRCODE = '23514';
    END IF;
    IF NEW.event_kind IN ('usage', 'return') AND NOT EXISTS (
        SELECT 1 FROM router.budget_allowance_reconciliations AS final
        JOIN router.budget_allowance_leases AS source_lease
          ON source_lease.id = final.allowance_lease_id
        WHERE final.reconciliation_id = NEW.source_reconciliation_id
          AND final.allowance_lease_id = NEW.allowance_lease_id
          AND NEW.amount = CASE WHEN NEW.event_kind = 'usage' THEN final.used_amount ELSE final.returned_amount END
          AND NEW.occurred_at = source_lease.issued_at
    ) THEN RAISE EXCEPTION 'budget allowance final ledger does not match' USING ERRCODE = '23514'; END IF;
    IF NEW.event_kind = 'correction' AND NOT EXISTS (
        SELECT 1 FROM router.budget_allowance_corrections AS correction
        WHERE correction.correction_id = NEW.source_correction_id
          AND correction.allowance_lease_id = NEW.allowance_lease_id
          AND correction.amount_delta = NEW.amount
          AND NEW.occurred_at = lease.issued_at
    ) THEN RAISE EXCEPTION 'budget allowance correction ledger does not match' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_allowance_ledger_guard
BEFORE INSERT ON router.budget_allowance_ledger_entries
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance_ledger();
CREATE TRIGGER budget_allowance_ledger_append_only
BEFORE UPDATE OR DELETE ON router.budget_allowance_ledger_entries
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_complete_budget_allowance_reconciliation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM router.budget_allowance_ledger_entries
       WHERE allowance_lease_id = NEW.allowance_lease_id AND event_kind = 'usage'
         AND source_reconciliation_id = NEW.reconciliation_id AND amount = NEW.used_amount
         AND occurred_at = (SELECT issued_at FROM router.budget_allowance_leases
                            WHERE id = NEW.allowance_lease_id))
       OR NOT EXISTS (SELECT 1 FROM router.budget_allowance_ledger_entries
       WHERE allowance_lease_id = NEW.allowance_lease_id AND event_kind = 'return'
         AND source_reconciliation_id = NEW.reconciliation_id AND amount = NEW.returned_amount
         AND occurred_at = (SELECT issued_at FROM router.budget_allowance_leases
                            WHERE id = NEW.allowance_lease_id)) THEN
        RAISE EXCEPTION 'budget allowance reconciliation ledger is incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER budget_allowance_reconciliations_complete
AFTER INSERT ON router.budget_allowance_reconciliations DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_allowance_reconciliation();

CREATE FUNCTION router.check_budget_allowance_correction()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE total numeric(38,18); risk numeric(38,18); used numeric(38,18); final_time timestamptz;
BEGIN
    SELECT lease.maximum_correction_risk, final.used_amount, final.occurred_at
    INTO risk, used, final_time
    FROM router.budget_allowance_leases AS lease
    JOIN router.budget_allowance_reconciliations AS final ON final.allowance_lease_id = lease.id
    WHERE lease.id = NEW.allowance_lease_id FOR UPDATE OF lease;
    SELECT COALESCE(sum(amount_delta), 0) + NEW.amount_delta INTO total
    FROM router.budget_allowance_corrections WHERE allowance_lease_id = NEW.allowance_lease_id;
    IF NEW.occurred_at < final_time OR total > risk OR used + total < 0 THEN
        RAISE EXCEPTION 'budget allowance correction exceeds its bound'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER budget_allowance_corrections_guard
BEFORE INSERT ON router.budget_allowance_corrections
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance_correction();
CREATE TRIGGER budget_allowance_corrections_append_only
BEFORE UPDATE OR DELETE ON router.budget_allowance_corrections
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_complete_budget_allowance_correction()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM router.budget_allowance_ledger_entries
       WHERE allowance_lease_id = NEW.allowance_lease_id AND event_kind = 'correction'
         AND source_correction_id = NEW.correction_id AND amount = NEW.amount_delta
         AND occurred_at = (SELECT issued_at FROM router.budget_allowance_leases
                            WHERE id = NEW.allowance_lease_id)) THEN
        RAISE EXCEPTION 'budget allowance correction ledger is incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER budget_allowance_corrections_complete
AFTER INSERT ON router.budget_allowance_corrections DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_allowance_correction();

CREATE FUNCTION router.check_allowance_bound_after_budget_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF router.allowance_scope_consumed(NEW.id, NEW.effective_at) > NEW.hard_limit THEN
        RAISE EXCEPTION 'budget hard limit is below committed allowance and use'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER budget_scopes_allowance_bound
AFTER INSERT OR UPDATE ON router.budget_scopes DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_allowance_bound_after_budget_change();

CREATE FUNCTION router.check_allowance_bound_after_candidate()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE row record;
BEGIN
    FOR row IN SELECT scope.* FROM router.budget_reservation_allocations AS allocation
      JOIN router.budget_scopes AS scope ON scope.id = allocation.budget_scope_id
      WHERE allocation.reservation_id = NEW.id LOOP
        IF router.allowance_scope_consumed(row.id, NEW.created_at) > row.hard_limit THEN
            RAISE EXCEPTION 'candidate reservation and allowances exceed hard budget'
                USING ERRCODE = '23514';
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER budget_candidate_allowance_bound
AFTER INSERT ON router.budget_candidate_reservations DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_allowance_bound_after_candidate();

CREATE INDEX budget_allowance_unreconciled_idx
ON router.budget_allowance_leases (budget_scope_id, safety_until, id);
CREATE INDEX budget_allowance_ledger_scope_time_idx
ON router.budget_allowance_ledger_entries (budget_scope_id, occurred_at, event_id);
