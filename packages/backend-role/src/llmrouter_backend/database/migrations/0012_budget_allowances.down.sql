DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.budget_allowance_batches WHERE NOT legacy)
       OR EXISTS (SELECT 1 FROM router.budget_allowance_reconciliations)
       OR EXISTS (SELECT 1 FROM router.budget_allowance_corrections) THEN
        RAISE EXCEPTION 'budget allowance migration cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;
DROP TRIGGER budget_candidate_allowance_bound ON router.budget_candidate_reservations;
DROP FUNCTION router.check_allowance_bound_after_candidate();
DROP TRIGGER budget_scopes_allowance_bound ON router.budget_scopes;
DROP FUNCTION router.check_allowance_bound_after_budget_change();
DROP TRIGGER budget_allowance_corrections_complete ON router.budget_allowance_corrections;
DROP FUNCTION router.check_complete_budget_allowance_correction();
DROP TRIGGER budget_allowance_corrections_append_only ON router.budget_allowance_corrections;
DROP TRIGGER budget_allowance_corrections_guard ON router.budget_allowance_corrections;
DROP FUNCTION router.check_budget_allowance_correction();
DROP TRIGGER budget_allowance_reconciliations_complete ON router.budget_allowance_reconciliations;
DROP FUNCTION router.check_complete_budget_allowance_reconciliation();
DROP TRIGGER budget_allowance_ledger_append_only ON router.budget_allowance_ledger_entries;
DROP TRIGGER budget_allowance_ledger_guard ON router.budget_allowance_ledger_entries;
DROP FUNCTION router.check_budget_allowance_ledger();
DROP TRIGGER budget_allowance_reconciliations_append_only ON router.budget_allowance_reconciliations;
DROP TRIGGER budget_allowance_reconciliations_guard ON router.budget_allowance_reconciliations;
DROP FUNCTION router.check_budget_allowance_reconciliation();
DROP TRIGGER budget_allowance_batches_complete ON router.budget_allowance_batches;
DROP FUNCTION router.check_complete_budget_allowance_batch();
DROP TRIGGER services_allowance_topology ON router.services;
DROP TRIGGER budget_scopes_allowance_topology ON router.budget_scopes;
DROP FUNCTION router.check_outstanding_budget_allowance_topology();
DROP FUNCTION router.budget_allowance_batch_is_complete(uuid);
DROP TRIGGER budget_allowance_leases_guard ON router.budget_allowance_leases;
DROP FUNCTION router.check_budget_allowance_lease();
DROP TRIGGER budget_allowance_batches_guard ON router.budget_allowance_batches;
DROP FUNCTION router.check_budget_allowance_batch();
DROP FUNCTION router.allowance_scope_consumed(uuid, timestamptz);
DROP TABLE router.budget_allowance_ledger_entries;
DROP TABLE router.budget_allowance_corrections;
DROP TABLE router.budget_allowance_reconciliations;
ALTER TABLE router.budget_allowance_leases
DROP CONSTRAINT budget_allowance_leases_batch_scope_unique,
DROP CONSTRAINT budget_allowance_leases_batch_fk,
DROP COLUMN maximum_correction_risk,
DROP COLUMN batch_id;
DROP TABLE router.budget_allowance_batches;
CREATE FUNCTION router.check_budget_allowance()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    scope_limit numeric(38, 18);
    live_issued numeric(38, 18);
    central_reserved numeric(38, 18);
BEGIN
    SELECT hard_limit
    INTO scope_limit
    FROM router.budget_scopes
    WHERE id = NEW.budget_scope_id AND currency = NEW.currency
    FOR UPDATE;
    IF scope_limit IS NULL THEN
        RAISE EXCEPTION 'budget allowance scope or currency does not exist'
            USING ERRCODE = '23503';
    END IF;
    SELECT COALESCE(sum(issued_amount), 0)
    INTO live_issued
    FROM router.budget_allowance_leases
    WHERE budget_scope_id = NEW.budget_scope_id
      AND expires_at > transaction_timestamp()
      AND id <> NEW.id;
    SELECT COALESCE(sum(reserved_amount - released_amount), 0)
    INTO central_reserved
    FROM router.budget_reservations
    WHERE budget_scope_id = NEW.budget_scope_id
      AND allowance_lease_id IS NULL
      AND reconciled_at IS NULL;
    IF NEW.expires_at > transaction_timestamp()
       AND live_issued + NEW.issued_amount + central_reserved > scope_limit THEN
        RAISE EXCEPTION 'live budget allowances exceed the hard limit'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id OR NEW.budget_scope_id <> OLD.budget_scope_id
           OR NEW.currency <> OLD.currency OR NEW.owner_node_id <> OLD.owner_node_id
           OR NEW.issued_amount <> OLD.issued_amount OR NEW.issued_at <> OLD.issued_at
           OR NEW.safety_until <> OLD.safety_until THEN
            RAISE EXCEPTION 'budget allowance identity and issued amount are immutable'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.lease_generation <= OLD.lease_generation THEN
            RAISE EXCEPTION 'budget allowance generation must increase'
                USING ERRCODE = '40001';
        END IF;
        IF NEW.consumed_amount < OLD.consumed_amount THEN
            RAISE EXCEPTION 'budget allowance consumption cannot decrease'
                USING ERRCODE = '40001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER budget_allowance_leases_guard
BEFORE INSERT OR UPDATE ON router.budget_allowance_leases
FOR EACH ROW EXECUTE FUNCTION router.check_budget_allowance();
