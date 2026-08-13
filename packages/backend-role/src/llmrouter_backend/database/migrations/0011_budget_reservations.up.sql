ALTER TABLE router.budget_scopes
ADD COLUMN reset_period text NOT NULL DEFAULT 'none'
    CHECK (reset_period IN ('none', 'daily', 'monthly')),
ADD COLUMN effective_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
ADD COLUMN host_ceiling_revision uuid;

ALTER TABLE router.budget_scopes
DROP CONSTRAINT budget_scopes_scope_kind_check,
DROP CONSTRAINT budget_scopes_check1,
ADD CONSTRAINT budget_scopes_scope_kind_check CHECK (scope_kind IN (
    'global', 'service', 'workspace', 'assignment', 'host_ceiling'
)),
ADD CONSTRAINT budget_scopes_check1 CHECK (
    (scope_kind = 'global' AND service_id IS NULL
        AND workspace_id IS NULL AND assignment_id IS NULL
        AND host_ceiling_revision IS NULL)
    OR (scope_kind = 'service' AND service_id IS NOT NULL
        AND workspace_id IS NULL AND assignment_id IS NULL
        AND host_ceiling_revision IS NULL)
    OR (scope_kind = 'workspace' AND service_id IS NOT NULL
        AND workspace_id IS NOT NULL AND assignment_id IS NULL
        AND host_ceiling_revision IS NULL)
    OR (scope_kind = 'assignment' AND service_id IS NOT NULL
        AND assignment_id IS NOT NULL AND host_ceiling_revision IS NULL)
    OR (scope_kind = 'host_ceiling' AND service_id IS NOT NULL
        AND workspace_id IS NOT NULL AND assignment_id IS NULL
        AND host_ceiling_revision IS NOT NULL)
);

CREATE FUNCTION router.lock_budget_hierarchy_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_scopes_hierarchy_lock
BEFORE INSERT OR UPDATE ON router.budget_scopes
FOR EACH ROW EXECUTE FUNCTION router.lock_budget_hierarchy_mutation();

CREATE TRIGGER budget_scopes_no_delete
BEFORE DELETE ON router.budget_scopes
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

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
                  AND EXISTS (
                      WITH RECURSIVE ancestors AS (
                          SELECT id, parent_service_id
                          FROM router.services WHERE id = NEW.service_id
                        UNION ALL
                          SELECT service.id, service.parent_service_id
                          FROM router.services AS service
                          JOIN ancestors
                            ON ancestors.parent_service_id = service.id
                      )
                      SELECT 1 FROM ancestors
                      WHERE ancestors.id = budget.service_id
                  ))
              OR (budget.scope_kind IN ('workspace', 'host_ceiling')
                  AND budget.service_id = NEW.service_id
                  AND budget.workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
              OR (budget.scope_kind = 'assignment'
                  AND NEW.subject_kind = 'provider_attempt'
                  AND budget.service_id = NEW.service_id
                  AND NEW.assignment_id = budget.assignment_id
                  AND EXISTS (
                      SELECT 1 FROM router.logical_requests AS request
                      WHERE request.row_id = NEW.request_row_id
                        AND request.assignment_id = budget.assignment_id
                  )
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

CREATE TABLE router.workspace_budget_ceilings (
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid NOT NULL,
    budget_scope_id uuid NOT NULL UNIQUE,
    amount numeric(38, 18) NOT NULL CHECK (amount >= 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    revision uuid NOT NULL UNIQUE,
    operation_id uuid NOT NULL UNIQUE,
    effective_at timestamptz NOT NULL,
    PRIMARY KEY (service_id, workspace_id),
    FOREIGN KEY (budget_scope_id, currency)
        REFERENCES router.budget_scopes (id, currency) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT
);

CREATE FUNCTION router.lock_workspace_budget_ceiling_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'host-ceiling:' || NEW.service_id::text || ':' || NEW.workspace_id::text,
        0
    ));
    RETURN NEW;
END;
$$;

CREATE TRIGGER workspace_budget_ceilings_lock
BEFORE INSERT OR UPDATE ON router.workspace_budget_ceilings
FOR EACH ROW EXECUTE FUNCTION router.lock_workspace_budget_ceiling_mutation();

CREATE FUNCTION router.protect_workspace_budget_ceiling()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workspace budget ceiling cannot be removed'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.service_id <> OLD.service_id
        OR NEW.workspace_id <> OLD.workspace_id
        OR NEW.budget_scope_id <> OLD.budget_scope_id
        OR NEW.revision = OLD.revision
        OR NEW.operation_id = OLD.operation_id
        OR NEW.effective_at < OLD.effective_at
    ) THEN
        RAISE EXCEPTION 'workspace budget ceiling identity or revision is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.currency <> OLD.currency AND EXISTS (
        SELECT 1
        FROM router.logical_request_budget_sets AS budget_set
        JOIN router.logical_requests AS request
          ON request.row_id = budget_set.request_row_id
        WHERE request.service_id = NEW.service_id
          AND request.workspace_id = NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'workspace budget currency with financial history is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.budget_scopes AS budget
        WHERE budget.service_id = NEW.service_id
          AND budget.scope_kind IN ('workspace', 'assignment')
          AND (budget.workspace_id = NEW.workspace_id
               OR (budget.scope_kind = 'assignment'
                   AND budget.workspace_id IS NULL))
          AND budget.currency <> NEW.currency
    ) THEN
        RAISE EXCEPTION 'workspace budget ceiling currency does not match Router limits'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM router.budget_scopes AS budget
        WHERE budget.id = NEW.budget_scope_id
          AND budget.scope_kind = 'host_ceiling'
          AND budget.service_id = NEW.service_id
          AND budget.workspace_id = NEW.workspace_id
          AND budget.currency = NEW.currency
          AND budget.hard_limit = NEW.amount
          AND budget.host_ceiling_revision = NEW.revision
          AND budget.effective_at = NEW.effective_at
    ) THEN
        RAISE EXCEPTION 'workspace budget ceiling accounting scope does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workspace_budget_ceilings_guard
BEFORE INSERT OR UPDATE OR DELETE ON router.workspace_budget_ceilings
FOR EACH ROW EXECUTE FUNCTION router.protect_workspace_budget_ceiling();

CREATE TABLE router.budget_ceiling_operations (
    operation_id uuid PRIMARY KEY,
    service_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    actor_id text NOT NULL CHECK (actor_id <> ''),
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    expected_revision uuid,
    resulting_revision uuid NOT NULL UNIQUE,
    amount numeric(38, 18) NOT NULL CHECK (amount >= 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) DEFERRABLE INITIALLY DEFERRED,
    effective_at timestamptz NOT NULL,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    UNIQUE (service_id, workspace_id, actor_id, idempotency_key)
);

CREATE TRIGGER budget_ceiling_operations_append_only
BEFORE UPDATE OR DELETE ON router.budget_ceiling_operations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_complete_workspace_budget_ceiling()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.budget_ceiling_operations AS operation
        WHERE operation.operation_id = NEW.operation_id
          AND operation.service_id = NEW.service_id
          AND operation.workspace_id = NEW.workspace_id
          AND operation.expected_revision IS NOT DISTINCT FROM
              CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.revision END
          AND operation.resulting_revision = NEW.revision
          AND operation.amount = NEW.amount
          AND operation.currency = NEW.currency
          AND operation.effective_at = NEW.effective_at
    ) THEN
        RAISE EXCEPTION 'workspace budget ceiling operation is incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER workspace_budget_ceilings_complete
AFTER INSERT OR UPDATE ON router.workspace_budget_ceilings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_workspace_budget_ceiling();

CREATE TABLE router.budget_limit_operations (
    operation_id uuid PRIMARY KEY,
    budget_scope_id uuid NOT NULL REFERENCES router.budget_scopes (id) ON DELETE RESTRICT,
    actor_id text NOT NULL CHECK (actor_id <> ''),
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 16 AND 200),
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 32),
    expected_revision bigint NOT NULL CHECK (expected_revision >= 0),
    resulting_revision bigint NOT NULL CHECK (resulting_revision > 0),
    hard_limit numeric(38, 18) NOT NULL CHECK (hard_limit >= 0),
    warning_threshold numeric(38, 18),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    reset_period text NOT NULL CHECK (reset_period IN ('none', 'daily', 'monthly')),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) DEFERRABLE INITIALLY DEFERRED,
    effective_at timestamptz NOT NULL,
    CHECK (warning_threshold IS NULL OR warning_threshold BETWEEN 0 AND hard_limit),
    CHECK (resulting_revision = expected_revision + 1),
    UNIQUE (actor_id, idempotency_key),
    UNIQUE (budget_scope_id, resulting_revision)
);

CREATE TRIGGER budget_limit_operations_append_only
BEFORE UPDATE OR DELETE ON router.budget_limit_operations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_complete_budget_limit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    prior_revision bigint;
BEGIN
    IF NEW.scope_kind = 'host_ceiling' THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.hard_limit IS NOT DISTINCT FROM OLD.hard_limit
       AND NEW.warning_threshold IS NOT DISTINCT FROM OLD.warning_threshold
       AND NEW.currency IS NOT DISTINCT FROM OLD.currency
       AND NEW.reset_period IS NOT DISTINCT FROM OLD.reset_period
       AND NEW.revision IS NOT DISTINCT FROM OLD.revision
       AND NEW.effective_at IS NOT DISTINCT FROM OLD.effective_at THEN
        RETURN NULL;
    END IF;
    prior_revision := CASE
        WHEN TG_OP = 'INSERT' THEN 0 ELSE OLD.revision
    END;
    IF NOT EXISTS (
        SELECT 1 FROM router.budget_limit_operations AS operation
        WHERE operation.budget_scope_id = NEW.id
          AND operation.expected_revision = prior_revision
          AND operation.resulting_revision = NEW.revision
          AND operation.hard_limit = NEW.hard_limit
          AND operation.warning_threshold IS NOT DISTINCT FROM
              NEW.warning_threshold
          AND operation.currency = NEW.currency
          AND operation.reset_period = NEW.reset_period
          AND operation.effective_at = NEW.effective_at
    ) THEN
        RAISE EXCEPTION 'budget limit operation is incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER budget_scopes_limit_complete
AFTER INSERT OR UPDATE ON router.budget_scopes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_limit();

CREATE TABLE router.budget_scope_parent_backfill (
    budget_scope_id uuid PRIMARY KEY,
    prior_parent_budget_scope_id uuid
);

CREATE TRIGGER budget_scope_parent_backfill_append_only
BEFORE UPDATE OR DELETE ON router.budget_scope_parent_backfill
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

INSERT INTO router.budget_scope_parent_backfill (
    budget_scope_id, prior_parent_budget_scope_id
)
SELECT id, parent_budget_scope_id FROM router.budget_scopes;

CREATE FUNCTION router.expected_budget_parent(
    requested_kind text,
    requested_service_id uuid,
    requested_workspace_id uuid,
    requested_scope_id uuid
)
RETURNS uuid LANGUAGE sql STABLE AS $$
    WITH RECURSIVE service_ancestors AS (
        SELECT id, parent_service_id, 0 AS depth
        FROM router.services WHERE id = requested_service_id
      UNION ALL
        SELECT service.id, service.parent_service_id, ancestors.depth + 1
        FROM router.services AS service
        JOIN service_ancestors AS ancestors
          ON ancestors.parent_service_id = service.id
    ), candidates AS (
        SELECT budget.id, 3 AS priority, 0 AS depth
        FROM router.budget_scopes AS budget
        WHERE requested_kind IN ('service', 'workspace', 'assignment')
          AND budget.scope_kind = 'global'
          AND budget.id <> requested_scope_id
      UNION ALL
        SELECT budget.id, 2 AS priority, ancestors.depth
        FROM router.budget_scopes AS budget
        JOIN service_ancestors AS ancestors ON ancestors.id = budget.service_id
        WHERE requested_kind IN ('service', 'workspace', 'assignment')
          AND budget.scope_kind = 'service'
          AND budget.id <> requested_scope_id
          AND (requested_kind <> 'service'
               OR budget.service_id <> requested_service_id)
      UNION ALL
        SELECT budget.id, 1 AS priority, 0 AS depth
        FROM router.budget_scopes AS budget
        WHERE requested_kind = 'assignment'
          AND budget.scope_kind = 'workspace'
          AND budget.service_id = requested_service_id
          AND budget.workspace_id IS NOT DISTINCT FROM requested_workspace_id
          AND budget.id <> requested_scope_id
    )
    SELECT id FROM candidates ORDER BY priority, depth, id LIMIT 1
$$;

CREATE FUNCTION router.check_complete_budget_hierarchy()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_currency char(3);
    parent_limit numeric(38, 18);
    expected_parent uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.scope_kind IS DISTINCT FROM OLD.scope_kind
        OR NEW.service_id IS DISTINCT FROM OLD.service_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
    ) THEN
        RAISE EXCEPTION 'budget scope identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.currency <> OLD.currency AND EXISTS (
        SELECT 1 FROM router.budget_ledger_entries
        WHERE budget_scope_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'budget currency with financial history is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.scope_kind = 'host_ceiling' THEN
        IF NEW.parent_budget_scope_id IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM router.workspace_budget_ceilings AS ceiling
            WHERE ceiling.budget_scope_id = NEW.id
              AND ceiling.service_id = NEW.service_id
              AND ceiling.workspace_id = NEW.workspace_id
              AND ceiling.currency = NEW.currency
              AND ceiling.amount = NEW.hard_limit
              AND ceiling.revision = NEW.host_ceiling_revision
              AND ceiling.effective_at = NEW.effective_at
        ) THEN
            RAISE EXCEPTION 'host ceiling accounting scope does not match ceiling'
                USING ERRCODE = '23514';
        END IF;
        RETURN NULL;
    END IF;
    IF (
        NEW.scope_kind = 'workspace'
        AND EXISTS (
            SELECT 1 FROM router.budget_scopes AS budget
            WHERE budget.id <> NEW.id AND budget.scope_kind = 'assignment'
              AND budget.service_id = NEW.service_id
              AND budget.workspace_id IS NULL
              AND budget.currency <> NEW.currency
        )
    ) OR (
        NEW.scope_kind = 'assignment' AND NEW.workspace_id IS NULL
        AND EXISTS (
            SELECT 1 FROM router.budget_scopes AS budget
            WHERE budget.id <> NEW.id AND budget.scope_kind = 'workspace'
              AND budget.service_id = NEW.service_id
              AND budget.currency <> NEW.currency
        )
    ) THEN
        RAISE EXCEPTION 'co-applicable budget scopes must use one currency'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.parent_budget_scope_id IS NOT NULL THEN
        SELECT currency, hard_limit INTO parent_currency, parent_limit
        FROM router.budget_scopes WHERE id = NEW.parent_budget_scope_id;
        IF parent_currency IS NULL
           OR parent_currency IS DISTINCT FROM NEW.currency
           OR parent_limit < NEW.hard_limit THEN
            RAISE EXCEPTION 'child budget must use its parent currency and limit'
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_budget_scope_id
                FROM router.budget_scopes WHERE id = NEW.parent_budget_scope_id
              UNION ALL
                SELECT budget.id, budget.parent_budget_scope_id
                FROM router.budget_scopes AS budget
                JOIN ancestors ON budget.id = ancestors.parent_budget_scope_id
                WHERE ancestors.parent_budget_scope_id IS NOT NULL
            )
            SELECT 1 FROM ancestors WHERE id = NEW.id
        ) THEN
            RAISE EXCEPTION 'budget parent chain contains a cycle'
                USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
            WITH RECURSIVE service_ancestors AS (
                SELECT id, parent_service_id
                FROM router.services WHERE id = NEW.service_id
              UNION ALL
                SELECT service.id, service.parent_service_id
                FROM router.services AS service
                JOIN service_ancestors
                  ON service_ancestors.parent_service_id = service.id
            )
            SELECT 1 FROM router.budget_scopes AS parent
            WHERE parent.id = NEW.parent_budget_scope_id
              AND (
                (NEW.scope_kind = 'service' AND (
                    parent.scope_kind = 'global'
                    OR (parent.scope_kind = 'service'
                        AND parent.service_id IN (
                            SELECT id FROM service_ancestors WHERE id <> NEW.service_id
                        ))
                ))
                OR (NEW.scope_kind = 'workspace' AND (
                    parent.scope_kind = 'global'
                    OR (parent.scope_kind = 'service'
                        AND parent.service_id IN (SELECT id FROM service_ancestors))
                ))
                OR (NEW.scope_kind = 'assignment' AND (
                    parent.scope_kind = 'global'
                    OR (parent.scope_kind = 'service'
                        AND parent.service_id IN (SELECT id FROM service_ancestors))
                    OR (parent.scope_kind = 'workspace'
                        AND parent.service_id = NEW.service_id
                        AND parent.workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
                ))
              )
        ) THEN
            RAISE EXCEPTION 'budget parent is not an applicable structural ancestor'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    expected_parent := router.expected_budget_parent(
        NEW.scope_kind, NEW.service_id, NEW.workspace_id, NEW.id
    );
    IF NEW.parent_budget_scope_id IS DISTINCT FROM expected_parent THEN
        RAISE EXCEPTION 'budget must use its nearest applicable parent'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.budget_scopes AS child
        WHERE child.parent_budget_scope_id = NEW.id
          AND (child.currency <> NEW.currency OR child.hard_limit > NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'parent budget cannot exclude an existing child budget'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.scope_kind <> 'global' AND EXISTS (
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_service_id FROM router.services WHERE id = NEW.service_id
          UNION ALL
            SELECT service.id, service.parent_service_id
            FROM router.services AS service
            JOIN ancestors ON ancestors.parent_service_id = service.id
        )
        SELECT 1 FROM router.budget_scopes AS budget
        WHERE budget.id <> NEW.id
          AND (
            budget.scope_kind = 'global'
            OR (budget.scope_kind = 'service'
                AND budget.service_id IN (SELECT id FROM ancestors))
            OR (budget.scope_kind = 'workspace'
                AND budget.service_id = NEW.service_id
                AND budget.workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
          )
          AND (budget.currency <> NEW.currency OR budget.hard_limit < NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'budget limit exceeds an applicable ancestor'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.workspace_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM router.workspace_budget_ceilings AS ceiling
        WHERE ceiling.service_id = NEW.service_id
          AND ceiling.workspace_id = NEW.workspace_id
          AND (ceiling.currency <> NEW.currency OR ceiling.amount < NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'budget limit exceeds the host workspace ceiling'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.scope_kind = 'assignment' AND NEW.workspace_id IS NULL AND EXISTS (
        SELECT 1 FROM router.workspace_budget_ceilings AS ceiling
        WHERE ceiling.service_id = NEW.service_id
          AND (ceiling.currency <> NEW.currency
               OR ceiling.amount < NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'service assignment budget exceeds a host workspace ceiling'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.scope_kind = 'global' AND EXISTS (
        SELECT 1 FROM router.budget_scopes AS child
        WHERE child.id <> NEW.id
          AND child.scope_kind <> 'host_ceiling'
          AND (child.currency <> NEW.currency OR child.hard_limit > NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'global budget excludes an existing descendant'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.scope_kind = 'service' AND EXISTS (
        WITH RECURSIVE descendants AS (
            SELECT id FROM router.services WHERE id = NEW.service_id
          UNION ALL
            SELECT service.id FROM router.services AS service
            JOIN descendants ON service.parent_service_id = descendants.id
        )
        SELECT 1 FROM router.budget_scopes AS child
        WHERE child.id <> NEW.id
          AND child.scope_kind <> 'host_ceiling'
          AND child.service_id IN (SELECT id FROM descendants)
          AND (child.currency <> NEW.currency OR child.hard_limit > NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'service budget excludes an existing descendant'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.scope_kind = 'workspace' AND EXISTS (
        SELECT 1 FROM router.budget_scopes AS child
        WHERE child.id <> NEW.id AND child.scope_kind = 'assignment'
          AND child.service_id = NEW.service_id
          AND child.workspace_id = NEW.workspace_id
          AND (child.currency <> NEW.currency OR child.hard_limit > NEW.hard_limit)
    ) THEN
        RAISE EXCEPTION 'workspace budget excludes an existing assignment'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.budget_scopes AS budget
        WHERE budget.id <> NEW.id
          AND budget.parent_budget_scope_id IS DISTINCT FROM
              router.expected_budget_parent(
                  budget.scope_kind, budget.service_id,
                  budget.workspace_id, budget.id
              )
    ) THEN
        RAISE EXCEPTION 'a descendant budget does not use its nearest parent'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER budget_scopes_hierarchy ON router.budget_scopes;
DROP FUNCTION router.check_budget_hierarchy();
UPDATE router.budget_scopes AS budget
SET parent_budget_scope_id = router.expected_budget_parent(
    budget.scope_kind, budget.service_id, budget.workspace_id, budget.id
);
CREATE CONSTRAINT TRIGGER budget_scopes_hierarchy
AFTER INSERT OR UPDATE ON router.budget_scopes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_hierarchy();

CREATE FUNCTION router.lock_budget_hierarchy_before_service_move()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    RETURN NULL;
END;
$$;

CREATE TRIGGER services_budget_hierarchy_lock
BEFORE UPDATE OF parent_service_id ON router.services
FOR EACH STATEMENT
EXECUTE FUNCTION router.lock_budget_hierarchy_before_service_move();

CREATE FUNCTION router.reparent_budgets_after_service_move()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_service_id IS NOT DISTINCT FROM OLD.parent_service_id THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    UPDATE router.budget_scopes AS budget
    SET parent_budget_scope_id = router.expected_budget_parent(
        budget.scope_kind, budget.service_id, budget.workspace_id, budget.id
    )
    WHERE budget.scope_kind <> 'host_ceiling';
    RETURN NEW;
END;
$$;

CREATE TRIGGER services_reparent_budgets
AFTER UPDATE OF parent_service_id ON router.services
FOR EACH ROW EXECUTE FUNCTION router.reparent_budgets_after_service_move();

CREATE TABLE router.logical_request_budget_sets (
    id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL UNIQUE
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    maximum_cost numeric(38, 18) CHECK (maximum_cost IS NULL OR maximum_cost >= 0),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);

CREATE TRIGGER logical_request_budget_sets_append_only
BEFORE UPDATE OR DELETE ON router.logical_request_budget_sets
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.budget_candidate_reservations (
    id uuid PRIMARY KEY,
    budget_set_id uuid NOT NULL
        REFERENCES router.logical_request_budget_sets (id) ON DELETE RESTRICT,
    reservation_key text NOT NULL
        CHECK (char_length(reservation_key) BETWEEN 1 AND 200),
    candidate_id uuid NOT NULL,
    candidate_kind text NOT NULL CHECK (candidate_kind IN (
        'provider_route', 'external_tool', 'business_tool'
    )),
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    estimated_amount numeric(38, 18) NOT NULL CHECK (estimated_amount >= 0),
    reserved_amount numeric(38, 18) NOT NULL
        CHECK (reserved_amount >= estimated_amount),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (budget_set_id, reservation_key)
);

CREATE TRIGGER budget_candidate_reservations_append_only
BEFORE UPDATE OR DELETE ON router.budget_candidate_reservations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_budget_reservation_time()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    request_service uuid;
    request_workspace uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('budget-hierarchy', 0));
    SELECT request.service_id, request.workspace_id
    INTO request_service, request_workspace
    FROM router.logical_request_budget_sets AS budget_set
    JOIN router.logical_requests AS request
      ON request.row_id = budget_set.request_row_id
    WHERE budget_set.id = NEW.budget_set_id;
    IF request_workspace IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'host-ceiling:' || request_service::text || ':' ||
            request_workspace::text,
            0
        ));
    END IF;
    IF NEW.created_at < (
        SELECT request.admitted_at
        FROM router.logical_request_budget_sets AS budget_set
        JOIN router.logical_requests AS request
          ON request.row_id = budget_set.request_row_id
        WHERE budget_set.id = NEW.budget_set_id
    ) THEN
        RAISE EXCEPTION 'budget reservation cannot precede request admission'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_candidate_reservations_time
BEFORE INSERT ON router.budget_candidate_reservations
FOR EACH ROW EXECUTE FUNCTION router.check_budget_reservation_time();

CREATE FUNCTION router.check_complete_candidate_reservation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    request_service uuid;
    request_workspace uuid;
    request_assignment uuid;
    request_currency char(3);
    request_maximum numeric(38, 18);
    logical_consumed numeric(38, 18);
    scope_row record;
    scope_start timestamptz;
    scope_reserved numeric(38, 18);
    scope_used numeric(38, 18);
    scope_corrected numeric(38, 18);
    ceiling_amount numeric(38, 18);
    ceiling_currency char(3);
    workspace_consumed numeric(38, 18);
BEGIN
    SELECT request.service_id, request.workspace_id, request.assignment_id,
           budget_set.currency, budget_set.maximum_cost
    INTO request_service, request_workspace, request_assignment,
         request_currency, request_maximum
    FROM router.logical_request_budget_sets AS budget_set
    JOIN router.logical_requests AS request
      ON request.row_id = budget_set.request_row_id
    WHERE budget_set.id = NEW.budget_set_id;

    IF EXISTS (
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_service_id FROM router.services
            WHERE id = request_service
          UNION ALL
            SELECT service.id, service.parent_service_id
            FROM router.services AS service
            JOIN ancestors ON ancestors.parent_service_id = service.id
        ), applicable AS (
            SELECT budget.id
            FROM router.budget_scopes AS budget
            WHERE budget.scope_kind = 'global'
               OR (budget.scope_kind = 'service'
                   AND budget.service_id IN (SELECT id FROM ancestors))
               OR (budget.scope_kind = 'workspace'
                   AND budget.service_id = request_service
                   AND budget.workspace_id IS NOT DISTINCT FROM request_workspace)
               OR (budget.scope_kind = 'host_ceiling'
                   AND budget.service_id = request_service
                   AND budget.workspace_id IS NOT DISTINCT FROM request_workspace)
               OR (budget.scope_kind = 'assignment'
                   AND budget.service_id = request_service
                   AND budget.assignment_id IS NOT DISTINCT FROM request_assignment
                   AND (budget.workspace_id IS NULL
                        OR budget.workspace_id IS NOT DISTINCT FROM request_workspace))
        ), allocated AS (
            SELECT budget_scope_id AS id
            FROM router.budget_reservation_allocations
            WHERE reservation_id = NEW.id
        )
        (SELECT id FROM applicable EXCEPT SELECT id FROM allocated)
        UNION ALL
        (SELECT id FROM allocated EXCEPT SELECT id FROM applicable)
    ) THEN
        RAISE EXCEPTION 'candidate reservation allocations are incomplete'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM router.budget_reservation_allocations AS allocation
        WHERE allocation.reservation_id = NEW.id
          AND NOT EXISTS (
              SELECT 1
              FROM router.budget_ledger_entries AS ledger
              WHERE ledger.reservation_id = allocation.reservation_id
                AND ledger.budget_scope_id = allocation.budget_scope_id
                AND ledger.event_kind = 'reservation'
                AND ledger.amount = allocation.reserved_amount
                AND ledger.occurred_at = NEW.created_at
          )
    ) THEN
        RAISE EXCEPTION 'candidate reservation ledger is incomplete'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(sum(greatest(
        CASE WHEN reconciliation.reservation_id IS NULL
             THEN reservation.reserved_amount
             ELSE reconciliation.actual_amount END
        + COALESCE(correction.amount, 0), 0
    )), 0)
    INTO logical_consumed
    FROM router.budget_candidate_reservations AS reservation
    LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
      ON reconciliation.reservation_id = reservation.id
    LEFT JOIN LATERAL (
        SELECT sum(amount_delta) AS amount
        FROM router.budget_reservation_corrections
        WHERE reservation_id = reservation.id
    ) AS correction ON true
    WHERE reservation.budget_set_id = NEW.budget_set_id;
    IF request_maximum IS NOT NULL
       AND logical_consumed > request_maximum THEN
        RAISE EXCEPTION 'logical request reservations exceed maximum cost'
            USING ERRCODE = '23514';
    END IF;

    FOR scope_row IN
        SELECT budget.id, budget.currency, budget.hard_limit,
               budget.reset_period
        FROM router.budget_reservation_allocations AS allocation
        JOIN router.budget_scopes AS budget
          ON budget.id = allocation.budget_scope_id
        WHERE allocation.reservation_id = NEW.id
        ORDER BY budget.id FOR UPDATE OF budget
    LOOP
        IF scope_row.currency <> request_currency THEN
            RAISE EXCEPTION 'candidate reservation currency does not match budget'
                USING ERRCODE = '23514';
        END IF;
        scope_start := CASE scope_row.reset_period
            WHEN 'daily' THEN date_trunc('day', NEW.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            WHEN 'monthly' THEN date_trunc('month', NEW.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            ELSE NULL
        END;
        SELECT
            COALESCE(sum(CASE
                WHEN event_kind = 'reservation' THEN amount
                WHEN event_kind = 'release' THEN -amount ELSE 0 END), 0),
            COALESCE(sum(CASE WHEN event_kind = 'usage'
                AND (scope_start IS NULL OR occurred_at >= scope_start)
                THEN amount ELSE 0 END), 0),
            COALESCE(sum(CASE WHEN event_kind = 'correction'
                AND (scope_start IS NULL OR occurred_at >= scope_start)
                THEN amount ELSE 0 END), 0)
        INTO scope_reserved, scope_used, scope_corrected
        FROM router.budget_ledger_entries
        WHERE budget_scope_id = scope_row.id;
        IF scope_reserved + greatest(scope_used + scope_corrected, 0)
           > scope_row.hard_limit THEN
            RAISE EXCEPTION 'candidate reservations exceed hard budget'
                USING ERRCODE = '23514';
        END IF;
    END LOOP;

    IF request_workspace IS NOT NULL THEN
        SELECT amount, currency INTO ceiling_amount, ceiling_currency
        FROM router.workspace_budget_ceilings
        WHERE service_id = request_service AND workspace_id = request_workspace
        FOR UPDATE;
        IF ceiling_amount IS NOT NULL THEN
            IF ceiling_currency <> request_currency THEN
                RAISE EXCEPTION 'candidate reservation currency does not match ceiling'
                    USING ERRCODE = '23514';
            END IF;
            SELECT COALESCE(sum(greatest(
                CASE WHEN reconciliation.reservation_id IS NULL
                     THEN reservation.reserved_amount
                     ELSE reconciliation.actual_amount END
                + COALESCE(correction.amount, 0), 0
            )), 0)
            INTO workspace_consumed
            FROM router.logical_request_budget_sets AS budget_set
            JOIN router.logical_requests AS request
              ON request.row_id = budget_set.request_row_id
            JOIN router.budget_candidate_reservations AS reservation
              ON reservation.budget_set_id = budget_set.id
            LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
              ON reconciliation.reservation_id = reservation.id
            LEFT JOIN LATERAL (
                SELECT sum(amount_delta) AS amount
                FROM router.budget_reservation_corrections
                WHERE reservation_id = reservation.id
            ) AS correction ON true
            WHERE request.service_id = request_service
              AND request.workspace_id = request_workspace;
            IF workspace_consumed > ceiling_amount THEN
                RAISE EXCEPTION 'candidate reservations exceed host ceiling'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER budget_candidate_reservations_complete
AFTER INSERT ON router.budget_candidate_reservations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_candidate_reservation();

CREATE TABLE router.budget_rejections (
    id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    candidate_id uuid NOT NULL,
    reservation_key text NOT NULL
        CHECK (char_length(reservation_key) BETWEEN 1 AND 200),
    request_fingerprint bytea NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    rejected_scope text NOT NULL CHECK (rejected_scope IN (
        'global', 'service', 'workspace', 'assignment',
        'host_ceiling', 'logical_request'
    )),
    exhausted boolean NOT NULL,
    occurred_at timestamptz NOT NULL,
    UNIQUE (request_row_id, reservation_key)
);

CREATE TRIGGER budget_rejections_append_only
BEFORE UPDATE OR DELETE ON router.budget_rejections
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.budget_reservation_reconciliations (
    reservation_id uuid PRIMARY KEY
        REFERENCES router.budget_candidate_reservations (id) ON DELETE RESTRICT,
    accounting_event_id uuid NOT NULL UNIQUE
        REFERENCES router.accounting_facts (event_id) ON DELETE RESTRICT,
    actual_amount numeric(38, 18) NOT NULL CHECK (actual_amount >= 0),
    occurred_at timestamptz NOT NULL
);

CREATE TRIGGER budget_reservation_reconciliations_append_only
BEFORE UPDATE OR DELETE ON router.budget_reservation_reconciliations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.budget_reservation_corrections (
    correction_id uuid PRIMARY KEY,
    reservation_id uuid NOT NULL
        REFERENCES router.budget_reservation_reconciliations (reservation_id)
        ON DELETE RESTRICT,
    accounting_correction_id uuid NOT NULL UNIQUE
        REFERENCES router.accounting_corrections (correction_id) ON DELETE RESTRICT,
    amount_delta numeric(38, 18) NOT NULL,
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    occurred_at timestamptz NOT NULL
);

CREATE TRIGGER budget_reservation_corrections_append_only
BEFORE UPDATE OR DELETE ON router.budget_reservation_corrections
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_budget_reconciliation_source()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    reservation_request uuid;
    reservation_currency char(3);
    reservation_created timestamptz;
    reservation_candidate uuid;
    reservation_candidate_kind text;
    fact_request uuid;
    fact_currency char(3);
    fact_amount numeric(38, 18);
    fact_subject_kind text;
    fact_subject_id uuid;
    fact_budget_scope uuid;
    fact_occurred_at timestamptz;
BEGIN
    SELECT budget_set.request_row_id, budget_set.currency, reservation.created_at,
           reservation.candidate_id, reservation.candidate_kind
    INTO reservation_request, reservation_currency, reservation_created,
         reservation_candidate, reservation_candidate_kind
    FROM router.budget_candidate_reservations AS reservation
    JOIN router.logical_request_budget_sets AS budget_set
      ON budget_set.id = reservation.budget_set_id
    WHERE reservation.id = NEW.reservation_id;
    SELECT request_row_id, currency, amount, subject_kind, subject_id,
           budget_scope_id, occurred_at
    INTO fact_request, fact_currency, fact_amount, fact_subject_kind,
         fact_subject_id, fact_budget_scope, fact_occurred_at
    FROM router.accounting_facts WHERE event_id = NEW.accounting_event_id;
    IF fact_request IS DISTINCT FROM reservation_request
       OR fact_currency IS DISTINCT FROM reservation_currency
       OR fact_amount IS DISTINCT FROM NEW.actual_amount
       OR fact_occurred_at IS DISTINCT FROM NEW.occurred_at
       OR NOT EXISTS (
           SELECT 1 FROM router.budget_reservation_allocations
           WHERE reservation_id = NEW.reservation_id
             AND budget_scope_id = fact_budget_scope
       ) THEN
        RAISE EXCEPTION 'budget reconciliation source does not match accounting'
            USING ERRCODE = '23514';
    END IF;
    IF reservation_candidate_kind = 'provider_route' AND (
       fact_subject_kind <> 'provider_attempt'
       OR NOT EXISTS (
        SELECT 1 FROM router.provider_attempts
        WHERE id = fact_subject_id
          AND request_row_id = reservation_request
          AND provider_model_route_id = reservation_candidate
          AND price_version_id IS NOT NULL
       )) THEN
        RAISE EXCEPTION 'budget candidate does not match provider attempt accounting'
            USING ERRCODE = '23514';
    END IF;
    IF reservation_candidate_kind = 'external_tool'
       AND (fact_subject_kind <> 'external_tool_attempt'
            OR fact_subject_id <> reservation_candidate) THEN
        RAISE EXCEPTION 'budget candidate does not match external tool accounting'
            USING ERRCODE = '23514';
    END IF;
    IF reservation_candidate_kind = 'business_tool'
       AND (fact_subject_kind <> 'business_tool_call'
            OR fact_subject_id <> reservation_candidate) THEN
        RAISE EXCEPTION 'budget candidate does not match business tool accounting'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.occurred_at < reservation_created THEN
        RAISE EXCEPTION 'budget reconciliation cannot precede reservation'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_reservation_reconciliations_source
BEFORE INSERT ON router.budget_reservation_reconciliations
FOR EACH ROW EXECUTE FUNCTION router.check_budget_reconciliation_source();

CREATE FUNCTION router.check_budget_correction_source()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    reconciliation_event uuid;
    reconciliation_time timestamptz;
    reconciliation_amount numeric(38, 18);
    source_event uuid;
    source_currency char(3);
    source_delta numeric(38, 18);
    source_reason text;
    source_time timestamptz;
BEGIN
    PERFORM 1
    FROM router.budget_candidate_reservations
    WHERE id = NEW.reservation_id
    FOR UPDATE;
    SELECT accounting_event_id, occurred_at, actual_amount
    INTO reconciliation_event, reconciliation_time, reconciliation_amount
    FROM router.budget_reservation_reconciliations
    WHERE reservation_id = NEW.reservation_id;
    SELECT source_event_id, currency, amount_delta, reason, occurred_at
    INTO source_event, source_currency, source_delta, source_reason, source_time
    FROM router.accounting_corrections
    WHERE correction_id = NEW.accounting_correction_id;
    IF source_event IS DISTINCT FROM reconciliation_event
       OR source_delta IS DISTINCT FROM NEW.amount_delta
       OR source_reason IS DISTINCT FROM NEW.reason
       OR source_time IS DISTINCT FROM NEW.occurred_at
       OR source_currency IS DISTINCT FROM (
           SELECT budget_set.currency
           FROM router.budget_candidate_reservations AS reservation
           JOIN router.logical_request_budget_sets AS budget_set
             ON budget_set.id = reservation.budget_set_id
           WHERE reservation.id = NEW.reservation_id
       ) THEN
        RAISE EXCEPTION 'budget correction source does not match accounting'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.occurred_at < reconciliation_time THEN
        RAISE EXCEPTION 'budget correction cannot precede reconciliation'
            USING ERRCODE = '23514';
    END IF;
    IF reconciliation_amount + NEW.amount_delta + COALESCE((
        SELECT sum(amount_delta)
        FROM router.budget_reservation_corrections
        WHERE reservation_id = NEW.reservation_id
    ), 0) < 0 THEN
        RAISE EXCEPTION 'budget corrections cannot make use negative'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_reservation_corrections_source
BEFORE INSERT ON router.budget_reservation_corrections
FOR EACH ROW EXECUTE FUNCTION router.check_budget_correction_source();

CREATE TABLE router.budget_reservation_allocations (
    reservation_id uuid NOT NULL
        REFERENCES router.budget_candidate_reservations (id) ON DELETE RESTRICT,
    budget_scope_id uuid NOT NULL
        REFERENCES router.budget_scopes (id) ON DELETE RESTRICT,
    reserved_amount numeric(38, 18) NOT NULL CHECK (reserved_amount >= 0),
    PRIMARY KEY (reservation_id, budget_scope_id)
);

CREATE TRIGGER budget_reservation_allocations_append_only
BEFORE UPDATE OR DELETE ON router.budget_reservation_allocations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_budget_reservation_allocation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.reserved_amount IS DISTINCT FROM (
        SELECT reserved_amount FROM router.budget_candidate_reservations
        WHERE id = NEW.reservation_id
    ) THEN
        RAISE EXCEPTION 'budget allocation must equal candidate reservation'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        WITH RECURSIVE request_scope AS (
            SELECT request.service_id, request.workspace_id,
                   request.assignment_id
            FROM router.budget_candidate_reservations AS reservation
            JOIN router.logical_request_budget_sets AS budget_set
              ON budget_set.id = reservation.budget_set_id
            JOIN router.logical_requests AS request
              ON request.row_id = budget_set.request_row_id
            WHERE reservation.id = NEW.reservation_id
        ), service_ancestors AS (
            SELECT service.id, service.parent_service_id
            FROM router.services AS service
            JOIN request_scope ON request_scope.service_id = service.id
          UNION ALL
            SELECT service.id, service.parent_service_id
            FROM router.services AS service
            JOIN service_ancestors AS ancestor
              ON ancestor.parent_service_id = service.id
        )
        SELECT 1
        FROM router.budget_scopes AS budget
        CROSS JOIN request_scope AS request
        WHERE budget.id = NEW.budget_scope_id
          AND (
              budget.scope_kind = 'global'
              OR (budget.scope_kind = 'service'
                  AND budget.service_id IN (
                      SELECT id FROM service_ancestors
                  ))
              OR (budget.scope_kind = 'workspace'
                  AND budget.service_id = request.service_id
                  AND budget.workspace_id IS NOT DISTINCT FROM
                      request.workspace_id)
              OR (budget.scope_kind = 'host_ceiling'
                  AND budget.service_id = request.service_id
                  AND budget.workspace_id IS NOT DISTINCT FROM
                      request.workspace_id)
              OR (budget.scope_kind = 'assignment'
                  AND budget.service_id = request.service_id
                  AND budget.assignment_id IS NOT DISTINCT FROM
                      request.assignment_id
                  AND (budget.workspace_id IS NULL
                       OR budget.workspace_id IS NOT DISTINCT FROM
                          request.workspace_id))
          )
    ) THEN
        RAISE EXCEPTION 'budget allocation is not applicable to its request'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_reservation_allocations_amount
BEFORE INSERT ON router.budget_reservation_allocations
FOR EACH ROW EXECUTE FUNCTION router.check_budget_reservation_allocation();

CREATE TABLE router.budget_ledger_entries (
    event_id uuid PRIMARY KEY,
    reservation_id uuid NOT NULL
        REFERENCES router.budget_candidate_reservations (id) ON DELETE RESTRICT,
    budget_scope_id uuid NOT NULL,
    event_kind text NOT NULL CHECK (event_kind IN (
        'reservation', 'usage', 'release', 'correction'
    )),
    amount numeric(38, 18) NOT NULL,
    source_event_id uuid REFERENCES router.budget_ledger_entries (event_id) ON DELETE RESTRICT,
    source_correction_id uuid
        REFERENCES router.budget_reservation_corrections (correction_id)
        ON DELETE RESTRICT,
    occurred_at timestamptz NOT NULL,
    FOREIGN KEY (reservation_id, budget_scope_id)
        REFERENCES router.budget_reservation_allocations (
            reservation_id, budget_scope_id
        ) ON DELETE RESTRICT,
    CHECK (
        (event_kind IN ('reservation', 'usage', 'release') AND amount >= 0)
        OR event_kind = 'correction'
    ),
    CHECK ((event_kind = 'correction') = (source_event_id IS NOT NULL)),
    CHECK ((event_kind = 'correction') = (source_correction_id IS NOT NULL))
);

CREATE UNIQUE INDEX budget_ledger_one_reservation_event_idx
ON router.budget_ledger_entries (reservation_id, budget_scope_id, event_kind)
WHERE event_kind IN ('reservation', 'usage', 'release');

CREATE UNIQUE INDEX budget_ledger_one_correction_source_idx
ON router.budget_ledger_entries (
    reservation_id, budget_scope_id, source_correction_id
)
WHERE event_kind = 'correction';

CREATE FUNCTION router.check_budget_ledger_entry()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    allocated numeric(38, 18);
BEGIN
    SELECT reserved_amount INTO allocated
    FROM router.budget_reservation_allocations
    WHERE reservation_id = NEW.reservation_id
      AND budget_scope_id = NEW.budget_scope_id;
    IF allocated IS NULL THEN
        RAISE EXCEPTION 'budget ledger entry has no matching allocation'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.event_kind = 'reservation' AND NEW.amount <> allocated THEN
        RAISE EXCEPTION 'budget reservation ledger amount does not match allocation'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_kind = 'usage' AND NOT EXISTS (
        SELECT 1 FROM router.budget_reservation_reconciliations
        WHERE reservation_id = NEW.reservation_id
          AND actual_amount = NEW.amount
          AND occurred_at = NEW.occurred_at
    ) THEN
        RAISE EXCEPTION 'budget usage does not match reconciliation'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_kind = 'release' AND NEW.amount <> allocated THEN
        RAISE EXCEPTION 'budget release must return the complete reservation'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_kind = 'release' AND NOT EXISTS (
        SELECT 1 FROM router.budget_ledger_entries
        WHERE reservation_id = NEW.reservation_id
          AND budget_scope_id = NEW.budget_scope_id
          AND event_kind = 'usage'
    ) THEN
        RAISE EXCEPTION 'budget release cannot precede usage'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.event_kind = 'release' AND NEW.occurred_at IS DISTINCT FROM (
        SELECT occurred_at FROM router.budget_reservation_reconciliations
        WHERE reservation_id = NEW.reservation_id
    ) THEN
        RAISE EXCEPTION 'budget release time does not match reconciliation'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.source_event_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.budget_ledger_entries AS source
        WHERE source.event_id = NEW.source_event_id
          AND source.reservation_id = NEW.reservation_id
          AND source.budget_scope_id = NEW.budget_scope_id
          AND source.event_kind = 'usage'
    ) THEN
        RAISE EXCEPTION 'budget correction source does not match its usage'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.source_correction_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM router.budget_reservation_corrections
        WHERE correction_id = NEW.source_correction_id
          AND reservation_id = NEW.reservation_id
          AND amount_delta = NEW.amount
    ) THEN
        RAISE EXCEPTION 'budget ledger correction does not match its source'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_ledger_entries_guard
BEFORE INSERT ON router.budget_ledger_entries
FOR EACH ROW EXECUTE FUNCTION router.check_budget_ledger_entry();

CREATE TRIGGER budget_ledger_entries_append_only
BEFORE UPDATE OR DELETE ON router.budget_ledger_entries
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE INDEX budget_ledger_scope_time_idx
ON router.budget_ledger_entries (budget_scope_id, occurred_at, event_id);

CREATE FUNCTION router.check_complete_budget_reconciliation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.budget_reservation_allocations AS allocation
        WHERE allocation.reservation_id = NEW.reservation_id
          AND (
            NOT EXISTS (
                SELECT 1 FROM router.budget_ledger_entries AS usage
                WHERE usage.reservation_id = allocation.reservation_id
                  AND usage.budget_scope_id = allocation.budget_scope_id
                  AND usage.event_kind = 'usage'
                  AND usage.amount = NEW.actual_amount
                  AND usage.occurred_at = NEW.occurred_at
            )
            OR NOT EXISTS (
                SELECT 1 FROM router.budget_ledger_entries AS release
                WHERE release.reservation_id = allocation.reservation_id
                  AND release.budget_scope_id = allocation.budget_scope_id
                  AND release.event_kind = 'release'
                  AND release.amount = allocation.reserved_amount
                  AND release.occurred_at = NEW.occurred_at
            )
          )
    ) THEN
        RAISE EXCEPTION 'budget reconciliation ledger is incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER budget_reservation_reconciliations_complete
AFTER INSERT ON router.budget_reservation_reconciliations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_reconciliation();

CREATE FUNCTION router.check_complete_budget_correction()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.budget_reservation_allocations AS allocation
        WHERE allocation.reservation_id = NEW.reservation_id
          AND NOT EXISTS (
              SELECT 1 FROM router.budget_ledger_entries AS ledger
              WHERE ledger.reservation_id = allocation.reservation_id
                AND ledger.budget_scope_id = allocation.budget_scope_id
                AND ledger.event_kind = 'correction'
                AND ledger.source_correction_id = NEW.correction_id
                AND ledger.amount = NEW.amount_delta
                AND ledger.occurred_at = NEW.occurred_at
          )
    ) THEN
        RAISE EXCEPTION 'budget correction ledger is incomplete'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER budget_reservation_corrections_complete
AFTER INSERT ON router.budget_reservation_corrections
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.check_complete_budget_correction();
