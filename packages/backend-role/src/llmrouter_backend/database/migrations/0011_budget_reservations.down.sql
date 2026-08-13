DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.workspace_budget_ceilings)
       OR EXISTS (SELECT 1 FROM router.budget_ceiling_operations)
       OR EXISTS (SELECT 1 FROM router.budget_limit_operations)
       OR EXISTS (SELECT 1 FROM router.logical_request_budget_sets)
       OR EXISTS (SELECT 1 FROM router.budget_candidate_reservations)
       OR EXISTS (SELECT 1 FROM router.budget_rejections)
       OR EXISTS (SELECT 1 FROM router.budget_reservation_reconciliations)
       OR EXISTS (SELECT 1 FROM router.budget_reservation_corrections)
       OR EXISTS (SELECT 1 FROM router.budget_reservation_allocations)
       OR EXISTS (SELECT 1 FROM router.budget_ledger_entries)
       OR EXISTS (
           SELECT 1 FROM router.budget_scopes
           WHERE reset_period <> 'none'
       ) THEN
        RAISE EXCEPTION 'budget reservation migration cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP TRIGGER budget_ledger_entries_append_only ON router.budget_ledger_entries;
DROP TRIGGER budget_ledger_entries_guard ON router.budget_ledger_entries;
DROP TRIGGER budget_reservation_corrections_complete ON router.budget_reservation_corrections;
DROP FUNCTION router.check_complete_budget_correction();
DROP TRIGGER budget_reservation_reconciliations_complete ON router.budget_reservation_reconciliations;
DROP FUNCTION router.check_complete_budget_reconciliation();
DROP TABLE router.budget_ledger_entries;
DROP FUNCTION router.check_budget_ledger_entry();
DROP TRIGGER budget_reservation_allocations_append_only ON router.budget_reservation_allocations;
DROP TRIGGER budget_reservation_allocations_amount ON router.budget_reservation_allocations;
DROP TABLE router.budget_reservation_allocations;
DROP FUNCTION router.check_budget_reservation_allocation();
DROP TRIGGER budget_reservation_corrections_source ON router.budget_reservation_corrections;
DROP TRIGGER budget_reservation_corrections_append_only ON router.budget_reservation_corrections;
DROP TABLE router.budget_reservation_corrections;
DROP FUNCTION router.check_budget_correction_source();
DROP TRIGGER budget_reservation_reconciliations_source ON router.budget_reservation_reconciliations;
DROP TRIGGER budget_reservation_reconciliations_append_only ON router.budget_reservation_reconciliations;
DROP TABLE router.budget_reservation_reconciliations;
DROP FUNCTION router.check_budget_reconciliation_source();
DROP TRIGGER budget_rejections_append_only ON router.budget_rejections;
DROP TABLE router.budget_rejections;
DROP TRIGGER budget_candidate_reservations_complete ON router.budget_candidate_reservations;
DROP FUNCTION router.check_complete_candidate_reservation();
DROP TRIGGER budget_candidate_reservations_append_only ON router.budget_candidate_reservations;
DROP TRIGGER budget_candidate_reservations_time ON router.budget_candidate_reservations;
DROP TABLE router.budget_candidate_reservations;
DROP FUNCTION router.check_budget_reservation_time();
DROP TRIGGER logical_request_budget_sets_append_only ON router.logical_request_budget_sets;
DROP TABLE router.logical_request_budget_sets;
DROP TRIGGER services_reparent_budgets ON router.services;
DROP FUNCTION router.reparent_budgets_after_service_move();
DROP TRIGGER services_budget_hierarchy_lock ON router.services;
DROP FUNCTION router.lock_budget_hierarchy_before_service_move();
DROP TRIGGER budget_scopes_hierarchy ON router.budget_scopes;
DROP FUNCTION router.check_complete_budget_hierarchy();
DROP TRIGGER budget_scopes_limit_complete ON router.budget_scopes;
DROP FUNCTION router.check_complete_budget_limit();
DROP TRIGGER budget_scope_parent_backfill_append_only
ON router.budget_scope_parent_backfill;
UPDATE router.budget_scopes AS budget
SET parent_budget_scope_id = backfill.prior_parent_budget_scope_id
FROM router.budget_scope_parent_backfill AS backfill
WHERE backfill.budget_scope_id = budget.id;
DROP TABLE router.budget_scope_parent_backfill;
DROP FUNCTION router.expected_budget_parent(text, uuid, uuid, uuid);
CREATE FUNCTION router.check_budget_hierarchy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_currency char(3);
    parent_limit numeric(38, 18);
BEGIN
    IF NEW.parent_budget_scope_id IS NOT NULL THEN
        SELECT currency, hard_limit INTO parent_currency, parent_limit
        FROM router.budget_scopes WHERE id = NEW.parent_budget_scope_id;
        IF parent_currency IS DISTINCT FROM NEW.currency OR parent_limit < NEW.hard_limit THEN
            RAISE EXCEPTION 'child budget must use its parent currency and limit'
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            WITH RECURSIVE ancestors AS (
                SELECT parent_budget_scope_id
                FROM router.budget_scopes WHERE id = NEW.parent_budget_scope_id
              UNION ALL
                SELECT budget.parent_budget_scope_id
                FROM router.budget_scopes AS budget
                JOIN ancestors ON budget.id = ancestors.parent_budget_scope_id
                WHERE budget.parent_budget_scope_id IS NOT NULL
            )
            SELECT 1 FROM ancestors WHERE parent_budget_scope_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'budget parent chain contains a cycle'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.budget_scopes AS child
        WHERE child.parent_budget_scope_id = NEW.id
          AND (child.currency <> NEW.currency OR child.hard_limit > NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'parent budget cannot exclude an existing child budget'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER budget_scopes_hierarchy
AFTER INSERT OR UPDATE ON router.budget_scopes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_budget_hierarchy();
DROP TRIGGER budget_limit_operations_append_only ON router.budget_limit_operations;
DROP TABLE router.budget_limit_operations;
DROP TRIGGER workspace_budget_ceilings_complete
ON router.workspace_budget_ceilings;
DROP FUNCTION router.check_complete_workspace_budget_ceiling();
DROP TRIGGER budget_ceiling_operations_append_only ON router.budget_ceiling_operations;
DROP TABLE router.budget_ceiling_operations;
DROP TRIGGER workspace_budget_ceilings_guard ON router.workspace_budget_ceilings;
DROP TRIGGER workspace_budget_ceilings_lock ON router.workspace_budget_ceilings;
DROP FUNCTION router.lock_workspace_budget_ceiling_mutation();
DROP TABLE router.workspace_budget_ceilings;
DROP FUNCTION router.protect_workspace_budget_ceiling();
DROP TRIGGER accounting_facts_budget_scope ON router.accounting_facts;
DROP FUNCTION router.check_accounting_budget_scope();
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
                        ON candidate.configuration_revision_id =
                           attempt.assignment_revision_id
                       AND candidate.provider_model_route_id =
                           attempt.provider_model_route_id
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
CREATE TRIGGER accounting_facts_budget_scope
BEFORE INSERT ON router.accounting_facts
FOR EACH ROW EXECUTE FUNCTION router.check_accounting_budget_scope();
ALTER TABLE router.budget_scopes
DROP CONSTRAINT budget_scopes_scope_kind_check,
DROP CONSTRAINT budget_scopes_check1,
ADD CONSTRAINT budget_scopes_scope_kind_check CHECK (scope_kind IN (
    'global', 'service', 'workspace', 'assignment'
)),
ADD CONSTRAINT budget_scopes_check1 CHECK (
    (scope_kind = 'global' AND service_id IS NULL
        AND workspace_id IS NULL AND assignment_id IS NULL)
    OR (scope_kind = 'service' AND service_id IS NOT NULL
        AND workspace_id IS NULL AND assignment_id IS NULL)
    OR (scope_kind = 'workspace' AND service_id IS NOT NULL
        AND workspace_id IS NOT NULL AND assignment_id IS NULL)
    OR (scope_kind = 'assignment' AND service_id IS NOT NULL
        AND assignment_id IS NOT NULL)
);
DROP TRIGGER budget_scopes_hierarchy_lock ON router.budget_scopes;
DROP TRIGGER budget_scopes_no_delete ON router.budget_scopes;
DROP FUNCTION router.lock_budget_hierarchy_mutation();
ALTER TABLE router.budget_scopes
DROP COLUMN reset_period,
DROP COLUMN effective_at,
DROP COLUMN host_ceiling_revision;
