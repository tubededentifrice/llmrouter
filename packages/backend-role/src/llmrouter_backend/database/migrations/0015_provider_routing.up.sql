DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM router.logical_requests
        WHERE state IN ('admitted', 'running', 'waiting_for_tool', 'cancel_requested')
    ) THEN
        RAISE EXCEPTION
            'migration 0015 cannot prove the historical route chain for active logical requests'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM router.provider_attempts AS attempt
        GROUP BY attempt.request_row_id, COALESCE((
            SELECT candidate.ordinal
            FROM router.assignment_candidates AS candidate
            WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
              AND candidate.provider_model_route_id = attempt.provider_model_route_id
            LIMIT 1
        ), 1)
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'migration 0015 cannot prove one admitted snapshot for repeated legacy candidates'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM router.provider_attempts AS attempt
        WHERE 1 < (
            SELECT count(*) FROM router.assignment_candidates AS candidate
            WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
              AND candidate.provider_model_route_id = attempt.provider_model_route_id
        )
    ) THEN
        RAISE EXCEPTION
            'migration 0015 cannot prove an ambiguous legacy candidate ordinal'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE FUNCTION router.active_request_scope(target_service_id uuid, target_workspace_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
WITH RECURSIVE ancestors AS (
    SELECT id, parent_service_id, state
    FROM router.services WHERE id = target_service_id
  UNION ALL
    SELECT service.id, service.parent_service_id, service.state
    FROM router.services AS service
    JOIN ancestors ON ancestors.parent_service_id = service.id
)
SELECT EXISTS (SELECT 1 FROM ancestors)
   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE state <> 'active')
   AND (
       target_workspace_id IS NULL
       OR EXISTS (
           SELECT 1 FROM router.workspaces
           WHERE id = target_workspace_id
             AND service_id = target_service_id
             AND state = 'active'
       )
   );
$$;

CREATE FUNCTION router.provider_route_is_eligible(route_id uuid, target_service_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
WITH RECURSIVE ancestors AS (
    SELECT service.id, service.parent_service_id, 0 AS depth
    FROM router.services AS service WHERE service.id = target_service_id
  UNION ALL
    SELECT parent.id, parent.parent_service_id, child.depth + 1
    FROM router.services AS parent
    JOIN ancestors AS child ON child.parent_service_id = parent.id
), resources AS (
    SELECT route.state AS route_state, route.owner_kind AS route_owner_kind,
           route.owner_service_id AS route_owner_service_id,
           route.eligible_service_ids AS route_eligible_service_ids,
           instance.state AS instance_state,
           instance.owner_kind AS instance_owner_kind,
           instance.owner_service_id AS instance_owner_service_id,
           instance.eligible_service_ids AS instance_eligible_service_ids
    FROM router.provider_model_routes AS route
    JOIN router.provider_instances AS instance ON instance.id = route.provider_instance_id
    WHERE route.id = route_id
)
SELECT COALESCE((
    SELECT route_state = 'active' AND instance_state = 'active'
       AND (route_owner_kind = 'global' OR EXISTS (
           SELECT 1 FROM ancestors WHERE id = route_owner_service_id
       ))
       AND (instance_owner_kind = 'global' OR EXISTS (
           SELECT 1 FROM ancestors WHERE id = instance_owner_service_id
       ))
       AND (cardinality(route_eligible_service_ids) = 0 OR EXISTS (
           SELECT 1 FROM ancestors
           WHERE id = ANY(route_eligible_service_ids)
             AND (route_owner_service_id IS NULL OR depth < (
                 SELECT depth FROM ancestors WHERE id = route_owner_service_id
             ))
       ) OR route_owner_service_id = target_service_id)
       AND (cardinality(instance_eligible_service_ids) = 0 OR EXISTS (
           SELECT 1 FROM ancestors
           WHERE id = ANY(instance_eligible_service_ids)
             AND (instance_owner_service_id IS NULL OR depth < (
                 SELECT depth FROM ancestors WHERE id = instance_owner_service_id
             ))
       ) OR instance_owner_service_id = target_service_id)
    FROM resources
), false);
$$;

CREATE FUNCTION router.provider_resource_is_enabled(
    target_kind text,
    target_resource_id uuid,
    target_service_id uuid,
    target_workspace_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
WITH RECURSIVE service_chain AS (
    SELECT id, parent_service_id, 1 AS depth
    FROM router.services WHERE id = target_service_id
  UNION ALL
    SELECT parent.id, parent.parent_service_id, child.depth + 1
    FROM router.services AS parent
    JOIN service_chain AS child ON child.parent_service_id = parent.id
), scope_revisions AS (
    SELECT active.revision_id, revision.content, 0 AS priority
    FROM router.active_configurations AS active
    JOIN router.configuration_revisions AS revision ON revision.id = active.revision_id
    WHERE active.scope_kind = 'workspace'
      AND active.service_id = target_service_id
      AND active.workspace_id = target_workspace_id
  UNION ALL
    SELECT active.revision_id, revision.content, chain.depth
    FROM service_chain AS chain
    JOIN router.active_configurations AS active
      ON active.scope_kind = 'service' AND active.service_id = chain.id
    JOIN router.configuration_revisions AS revision ON revision.id = active.revision_id
  UNION ALL
    SELECT active.revision_id, revision.content, 1000000
    FROM router.active_configurations AS active
    JOIN router.configuration_revisions AS revision ON revision.id = active.revision_id
    WHERE active.scope_kind = 'global'
), resource AS (
    SELECT owner_kind, owner_service_id
    FROM router.provider_model_routes
    WHERE target_kind = 'provider_model_route' AND id = target_resource_id
  UNION ALL
    SELECT owner_kind, owner_service_id
    FROM router.provider_instances
    WHERE target_kind = 'provider_instance' AND id = target_resource_id
), owner AS (
    SELECT 1000000 AS priority
    FROM resource WHERE owner_kind = 'global'
  UNION ALL
    SELECT chain.depth AS priority
    FROM resource
    JOIN service_chain AS chain ON chain.id = resource.owner_service_id
    WHERE resource.owner_kind = 'service'
)
SELECT target_kind IN ('provider_model_route', 'provider_instance')
   AND EXISTS (SELECT 1 FROM owner)
   AND NOT EXISTS (
       SELECT 1 FROM scope_revisions AS child, owner
       WHERE child.priority < owner.priority
         AND child.content->'inherited_disables' @> jsonb_build_array(
             jsonb_build_object(
                 'resource_kind', target_kind,
                 'resource_id', target_resource_id::text
             )
         )
   );
$$;

CREATE TABLE router.provider_route_execution_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_row_id uuid NOT NULL
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    candidate_ordinal smallint NOT NULL CHECK (candidate_ordinal BETWEEN 1 AND 8),
    assignment_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    attempt_timeout_ms integer NOT NULL CHECK (attempt_timeout_ms BETWEEN 100 AND 120000),
    candidate_policy jsonb NOT NULL CHECK (jsonb_typeof(candidate_policy) = 'object'),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    route_configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    provider_model_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    route_generation bigint NOT NULL CHECK (route_generation > 0),
    provider_instance_id uuid NOT NULL
        REFERENCES router.provider_instances (id) ON DELETE RESTRICT,
    provider_instance_generation bigint NOT NULL
        CHECK (provider_instance_generation > 0),
    instance_configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    credential_id uuid NOT NULL
        REFERENCES router.encrypted_credentials (id) ON DELETE RESTRICT,
    credential_generation bigint NOT NULL CHECK (credential_generation > 0),
    credential_revision_id uuid NOT NULL,
    price_version_id uuid NOT NULL,
    adapter_type_id text NOT NULL
        REFERENCES router.provider_adapter_types (id) ON DELETE RESTRICT,
    endpoint_origin text NOT NULL CHECK (char_length(endpoint_origin) BETWEEN 1 AND 2000),
    wire_model text NOT NULL CHECK (char_length(wire_model) BETWEEN 1 AND 500),
    capabilities jsonb NOT NULL CHECK (jsonb_typeof(capabilities) = 'array'),
    instance_settings jsonb NOT NULL CHECK (jsonb_typeof(instance_settings) = 'object'),
    route_settings jsonb NOT NULL CHECK (jsonb_typeof(route_settings) = 'object'),
    typed_prices jsonb NOT NULL CHECK (
        jsonb_typeof(typed_prices) = 'array' AND jsonb_array_length(typed_prices) > 0
    ),
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    migration_0015_backfilled boolean NOT NULL DEFAULT false,
    FOREIGN KEY (price_version_id, provider_model_route_id)
        REFERENCES router.route_price_versions (id, provider_model_route_id)
        ON DELETE RESTRICT,
    UNIQUE (request_row_id, candidate_ordinal),
    UNIQUE (id, provider_model_route_id),
    UNIQUE (
        id, provider_model_route_id, route_generation,
        provider_instance_id, provider_instance_generation,
        credential_id, credential_generation, price_version_id
    )
);

CREATE TRIGGER provider_route_execution_snapshots_append_only
BEFORE UPDATE OR DELETE ON router.provider_route_execution_snapshots
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.validate_provider_route_execution_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_prices jsonb;
    expected_document jsonb;
BEGIN
    SELECT jsonb_agg(jsonb_build_object(
               'unit', component.unit_name,
               'price', component.unit_price::text,
               'currency', version.currency,
               'raw_source_value', component.raw_source_value,
               'unit_quantity', component.unit_quantity::text
           ) ORDER BY component.unit_name)
    INTO expected_prices
    FROM router.route_price_components AS component
    JOIN router.route_price_versions AS version
      ON version.id = component.price_version_id
    WHERE component.price_version_id = NEW.price_version_id;

    IF NEW.migration_0015_backfilled THEN
        IF NOT EXISTS (
            SELECT 1 FROM router.provider_attempts AS attempt
            WHERE attempt.id = NEW.id
              AND attempt.provider_model_route_id = NEW.provider_model_route_id
              AND attempt.route_generation = NEW.route_generation
              AND attempt.price_version_id = NEW.price_version_id
        ) THEN
            RAISE EXCEPTION 'backfilled route snapshot has no matching legacy attempt'
                USING ERRCODE = '23514';
        END IF;
    ELSIF expected_prices IS NULL OR NEW.typed_prices <> expected_prices OR NOT EXISTS (

        SELECT 1
        FROM router.provider_model_routes AS route
        JOIN router.provider_instances AS instance
          ON instance.id = route.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = instance.credential_id
        JOIN router.configuration_price_bindings AS price
          ON price.configuration_revision_id = NEW.assignment_revision_id
         AND price.provider_model_route_id = route.id
         AND price.price_version_id = NEW.price_version_id
        JOIN router.logical_requests AS request
          ON request.row_id = NEW.request_row_id
        WHERE route.id = NEW.provider_model_route_id
          AND route.state = 'active'
          AND route.generation = NEW.route_generation
          AND route.current_revision = NEW.route_configuration_revision_id
          AND route.wire_model = NEW.wire_model
          AND route.capabilities = NEW.capabilities
          AND route.settings = NEW.route_settings
          AND instance.id = NEW.provider_instance_id
          AND instance.state = 'active'
          AND instance.generation = NEW.provider_instance_generation
          AND COALESCE(instance.current_revision, route.current_revision)
              = NEW.instance_configuration_revision_id
          AND instance.adapter_type_id = NEW.adapter_type_id
          AND instance.endpoint_origin = NEW.endpoint_origin
          AND instance.settings = NEW.instance_settings
          AND credential.id = NEW.credential_id
          AND credential.state = 'active'
          AND credential.generation = NEW.credential_generation
          AND credential.current_revision = NEW.credential_revision_id
          AND request.configuration_revision_id = NEW.assignment_revision_id
          AND router.provider_route_is_eligible(route.id, request.service_id)
          AND router.provider_resource_is_enabled(
              'provider_model_route', route.id,
              request.service_id, request.workspace_id
          )
          AND router.provider_resource_is_enabled(
              'provider_instance', instance.id,
              request.service_id, request.workspace_id
          )
          AND (
              (request.assignment_id IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM router.assignment_candidates AS candidate
                   WHERE candidate.assignment_id = request.assignment_id
                     AND candidate.configuration_revision_id = NEW.assignment_revision_id
                     AND candidate.ordinal = NEW.candidate_ordinal
                     AND candidate.provider_model_route_id = NEW.provider_model_route_id
                     AND candidate.attempt_timeout_ms = NEW.attempt_timeout_ms
                     AND candidate.candidate_policy = NEW.candidate_policy
               ))
              OR
              (request.assignment_id IS NULL
               AND request.exact_route_id = NEW.provider_model_route_id
               AND NEW.route_configuration_revision_id = request.configuration_revision_id
               AND NEW.candidate_ordinal = 1
               AND NEW.attempt_timeout_ms = 120000
               AND NEW.candidate_policy = '{}'::jsonb)
          )
    ) THEN
        RAISE EXCEPTION 'provider route execution snapshot does not match current configuration'
            USING ERRCODE = '23514';
    END IF;

    expected_document := jsonb_build_object(
        'request_row_id', NEW.request_row_id,
        'candidate_ordinal', NEW.candidate_ordinal,
        'assignment_revision_id', NEW.assignment_revision_id,
        'attempt_timeout_ms', NEW.attempt_timeout_ms,
        'candidate_policy', NEW.candidate_policy,
        'route_configuration_revision_id', NEW.route_configuration_revision_id,
        'provider_model_route_id', NEW.provider_model_route_id,
        'route_generation', NEW.route_generation,
        'provider_instance_id', NEW.provider_instance_id,
        'provider_instance_generation', NEW.provider_instance_generation,
        'instance_configuration_revision_id', NEW.instance_configuration_revision_id,
        'credential_id', NEW.credential_id,
        'credential_generation', NEW.credential_generation,
        'credential_revision_id', NEW.credential_revision_id,
        'price_version_id', NEW.price_version_id,
        'adapter_type_id', NEW.adapter_type_id,
        'endpoint_origin', NEW.endpoint_origin,
        'wire_model', NEW.wire_model,
        'capabilities', NEW.capabilities,
        'instance_settings', NEW.instance_settings,
        'route_settings', NEW.route_settings,
        'typed_prices', NEW.typed_prices
    );
    IF NEW.content_sha256 <> sha256(convert_to(expected_document::text, 'UTF8')) THEN
        RAISE EXCEPTION 'provider route execution snapshot digest does not match'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_route_execution_snapshots_insert_guard
BEFORE INSERT ON router.provider_route_execution_snapshots
FOR EACH ROW EXECUTE FUNCTION router.validate_provider_route_execution_snapshot();

CREATE TABLE router.routing_attempt_claims (
    request_row_id uuid PRIMARY KEY,
    claim_id uuid NOT NULL UNIQUE,
    claim_generation bigint NOT NULL CHECK (claim_generation > 0),
    owner_id text NOT NULL CHECK (char_length(owner_id) BETWEEN 1 AND 500),
    attempt_id uuid NOT NULL UNIQUE,
    attempt_number smallint NOT NULL CHECK (attempt_number BETWEEN 1 AND 8),
    candidate_ordinal smallint NOT NULL CHECK (candidate_ordinal BETWEEN 1 AND 8),
    assignment_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    route_snapshot_id uuid NOT NULL
        REFERENCES router.provider_route_execution_snapshots (id) ON DELETE RESTRICT,
    candidate_policy jsonb NOT NULL CHECK (jsonb_typeof(candidate_policy) = 'object'),
    connect_timeout_ms integer NOT NULL CHECK (connect_timeout_ms BETWEEN 1 AND 120000),
    first_byte_timeout_ms integer NOT NULL CHECK (first_byte_timeout_ms BETWEEN 1 AND 120000),
    idle_timeout_ms integer NOT NULL CHECK (idle_timeout_ms BETWEEN 1 AND 120000),
    execution_timeout_ms integer NOT NULL CHECK (execution_timeout_ms BETWEEN 100 AND 120000),
    logical_deadline timestamptz NOT NULL,
    attempt_deadline timestamptz NOT NULL,
    claimed_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    CHECK (connect_timeout_ms <= execution_timeout_ms),
    CHECK (first_byte_timeout_ms <= execution_timeout_ms),
    CHECK (idle_timeout_ms <= execution_timeout_ms),
    CHECK (attempt_deadline <= logical_deadline),
    CHECK (lease_expires_at > claimed_at AND lease_expires_at <= claimed_at + interval '30 seconds'),
    FOREIGN KEY (request_row_id)
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT
);

CREATE FUNCTION router.protect_routing_claim()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.claimed_at > transaction_timestamp()
           OR NEW.lease_expires_at > transaction_timestamp() + interval '30 seconds' THEN
            RAISE EXCEPTION 'routing claim time is outside the transaction lease'
                USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM router.logical_requests AS request
            JOIN router.provider_route_execution_snapshots AS snapshot
              ON snapshot.request_row_id = request.row_id
             AND snapshot.id = NEW.route_snapshot_id
            JOIN router.provider_model_routes AS route
              ON route.id = snapshot.provider_model_route_id
            JOIN router.provider_instances AS instance
              ON instance.id = snapshot.provider_instance_id
            JOIN router.encrypted_credentials AS credential
              ON credential.id = snapshot.credential_id
            WHERE request.row_id = NEW.request_row_id
              AND request.state = 'running'
              AND NOT request.committed_effect
              AND NOT request.partial_output
              AND snapshot.candidate_ordinal = NEW.candidate_ordinal
              AND snapshot.assignment_revision_id = NEW.assignment_revision_id
              AND snapshot.candidate_policy = NEW.candidate_policy
              AND (
                  request.assignment_id IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM router.diagnostic_route_authorizations AS diagnostic_use
                      WHERE diagnostic_use.request_id = request.request_id
                        AND diagnostic_use.service_id = request.service_id
                        AND diagnostic_use.workspace_id IS NOT DISTINCT FROM request.workspace_id
                        AND diagnostic_use.exact_route_id = snapshot.provider_model_route_id
                        AND diagnostic_use.route_configuration_revision_id =
                            snapshot.route_configuration_revision_id
                  )
              )
              AND NEW.logical_deadline = request.admitted_at + interval '15 minutes'
              AND NEW.execution_timeout_ms = LEAST(
                  snapshot.attempt_timeout_ms,
                  floor(extract(epoch FROM (
                      request.admitted_at + interval '15 minutes' - NEW.claimed_at
                  )) * 1000)::integer
              )
              AND NEW.connect_timeout_ms = LEAST(10000, NEW.execution_timeout_ms)
              AND NEW.first_byte_timeout_ms = LEAST(30000, NEW.execution_timeout_ms)
              AND NEW.idle_timeout_ms = LEAST(30000, NEW.execution_timeout_ms)
              AND NEW.attempt_deadline = NEW.claimed_at
                  + NEW.execution_timeout_ms * interval '1 millisecond'
              AND (
                  (NOT EXISTS (
                      SELECT 1 FROM router.routing_candidate_decisions AS prior
                      WHERE prior.request_row_id = request.row_id
                  ) AND NEW.candidate_ordinal = 1)
                  OR EXISTS (
                      SELECT 1 FROM router.routing_candidate_decisions AS prior
                      WHERE prior.request_row_id = request.row_id
                        AND prior.fallback_decision = 'next_candidate'
                        AND (
                            (prior.affected_scope = 'attempt'
                             AND prior.candidate_ordinal = NEW.candidate_ordinal
                             AND EXISTS (
                                 SELECT 1 FROM router.provider_attempts AS prior_attempt
                                 WHERE prior_attempt.id = prior.attempt_id
                                   AND prior_attempt.request_row_id = request.row_id
                             ))
                            OR
                            ((prior.affected_scope <> 'attempt' OR NOT EXISTS (
                                 SELECT 1 FROM router.provider_attempts AS prior_attempt
                                 WHERE prior_attempt.id = prior.attempt_id
                                   AND prior_attempt.request_row_id = request.row_id
                             ))
                             AND prior.candidate_ordinal < NEW.candidate_ordinal
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM router.provider_route_execution_snapshots AS intermediate
                                 WHERE intermediate.request_row_id = request.row_id
                                   AND intermediate.candidate_ordinal > prior.candidate_ordinal
                                   AND intermediate.candidate_ordinal < NEW.candidate_ordinal
                                   AND NOT EXISTS (
                                       SELECT 1
                                       FROM router.routing_candidate_decisions AS excluded
                                       WHERE excluded.request_row_id = request.row_id
                                         AND (
                                             excluded.affected_scope = 'logical_request'
                                             OR (excluded.affected_scope = 'provider_model_route'
                                                 AND excluded.affected_scope_id = intermediate.provider_model_route_id::text)
                                             OR (excluded.affected_scope = 'provider_instance'
                                                 AND excluded.affected_scope_id = intermediate.provider_instance_id::text)
                                             OR (excluded.affected_scope = 'credential'
                                                 AND excluded.affected_scope_id = intermediate.credential_id::text)
                                             OR (excluded.affected_scope = 'assignment_candidate'
                                                 AND excluded.affected_scope_id = COALESCE(
                                                     request.assignment_id::text,
                                                     'exact:' || request.exact_route_id::text
                                                 ) || ':' || intermediate.candidate_ordinal::text)
                                         )
                                   )
                             ))
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM router.routing_candidate_decisions AS later
                            WHERE later.request_row_id = request.row_id
                              AND later.decision_sequence > prior.decision_sequence
                        )
                  )
              )
              AND NEW.attempt_number = 1 + (
                  SELECT count(*) FROM router.provider_attempts AS old_attempt
                  WHERE old_attempt.request_row_id = request.row_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM router.routing_candidate_decisions AS exclusion
                  WHERE exclusion.request_row_id = request.row_id
                    AND exclusion.attempt_state <> 'succeeded'
                    AND (
                        exclusion.affected_scope = 'logical_request'
                        OR (exclusion.affected_scope = 'provider_model_route'
                            AND exclusion.affected_scope_id = snapshot.provider_model_route_id::text)
                        OR (exclusion.affected_scope = 'provider_instance'
                            AND exclusion.affected_scope_id = snapshot.provider_instance_id::text)
                        OR (exclusion.affected_scope = 'credential'
                            AND exclusion.affected_scope_id = snapshot.credential_id::text)
                        OR (exclusion.affected_scope = 'assignment_candidate'
                            AND exclusion.affected_scope_id =
                                COALESCE(request.assignment_id::text,
                                         'exact:' || request.exact_route_id::text)
                                || ':' || snapshot.candidate_ordinal::text)
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM router.provider_attempts AS prior_attempt
                  WHERE prior_attempt.request_row_id = request.row_id
                    AND prior_attempt.state <> 'started'
                    AND NOT prior_attempt.migration_0015_backfilled
                    AND NOT EXISTS (
                        SELECT 1
                        FROM router.accounting_facts AS fact
                        LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
                          ON reconciliation.accounting_event_id = fact.event_id
                         AND reconciliation.reservation_id =
                             prior_attempt.budget_reservation_id
                        WHERE fact.request_row_id = request.row_id
                          AND fact.subject_kind = 'provider_attempt'
                          AND fact.subject_id = prior_attempt.id
                          AND fact.outcome = CASE prior_attempt.state
                              WHEN 'succeeded' THEN 'succeeded'
                              WHEN 'failed' THEN 'failed'
                              WHEN 'interrupted' THEN 'interrupted'
                              WHEN 'uncertain' THEN 'uncertain'
                              WHEN 'cancelled' THEN 'failed'
                          END
                          AND fact.occurred_at >= prior_attempt.finished_at
                          AND (
                              prior_attempt.budget_reservation_id IS NULL
                              OR reconciliation.reservation_id IS NOT NULL
                          )
                    )
              )
        ) THEN
            RAISE EXCEPTION 'routing claim does not match the admitted chain and live controls'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM router.logical_requests AS request
        JOIN router.provider_route_execution_snapshots AS snapshot
          ON snapshot.id = NEW.route_snapshot_id
         AND snapshot.request_row_id = request.row_id
        JOIN router.provider_model_routes AS route
          ON route.id = snapshot.provider_model_route_id
        JOIN router.provider_instances AS instance
          ON instance.id = snapshot.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = snapshot.credential_id
        WHERE request.row_id = NEW.request_row_id
          AND request.state = 'running'
          AND NOT request.partial_output AND NOT request.committed_effect
          AND credential.state = 'active'
          AND credential.generation = snapshot.credential_generation
          AND credential.current_revision = snapshot.credential_revision_id
          AND (
              request.assignment_id IS NOT NULL OR EXISTS (
                  SELECT 1 FROM router.diagnostic_route_authorizations AS diagnostic_use
                  WHERE diagnostic_use.request_id = request.request_id
                    AND diagnostic_use.service_id = request.service_id
                    AND diagnostic_use.workspace_id IS NOT DISTINCT FROM request.workspace_id
                    AND diagnostic_use.exact_route_id = snapshot.provider_model_route_id
                    AND diagnostic_use.route_configuration_revision_id =
                        snapshot.route_configuration_revision_id
              )
          )
    ) AND NOT EXISTS (
        SELECT 1 FROM router.routing_attempt_starts AS attempt_start
        WHERE attempt_start.claim_id = NEW.claim_id
          AND attempt_start.attempt_id = NEW.attempt_id
          AND attempt_start.request_row_id = NEW.request_row_id
    ) THEN
        RAISE EXCEPTION 'routing claim recovery is blocked by live controls'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.request_row_id <> OLD.request_row_id
       OR NEW.claim_id <> OLD.claim_id
       OR NEW.attempt_id <> OLD.attempt_id
       OR NEW.attempt_number <> OLD.attempt_number
       OR NEW.candidate_ordinal <> OLD.candidate_ordinal
       OR NEW.assignment_revision_id <> OLD.assignment_revision_id
       OR NEW.route_snapshot_id <> OLD.route_snapshot_id
       OR NEW.candidate_policy <> OLD.candidate_policy
       OR NEW.connect_timeout_ms <> OLD.connect_timeout_ms
       OR NEW.first_byte_timeout_ms <> OLD.first_byte_timeout_ms
       OR NEW.idle_timeout_ms <> OLD.idle_timeout_ms
       OR NEW.execution_timeout_ms <> OLD.execution_timeout_ms
       OR NEW.logical_deadline <> OLD.logical_deadline
       OR NEW.attempt_deadline <> OLD.attempt_deadline
       OR NEW.claim_generation <> OLD.claim_generation + 1
       OR transaction_timestamp() < OLD.lease_expires_at
       OR NEW.claimed_at < OLD.lease_expires_at
       OR NEW.claimed_at > transaction_timestamp()
       OR NEW.lease_expires_at <= NEW.claimed_at THEN
        RAISE EXCEPTION 'routing claim takeover can change its owner and lease only'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER routing_attempt_claims_change_guard
BEFORE INSERT OR UPDATE ON router.routing_attempt_claims
FOR EACH ROW EXECUTE FUNCTION router.protect_routing_claim();

CREATE FUNCTION router.guard_routing_claim_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM router.routing_attempt_starts
        WHERE claim_id = OLD.claim_id
          AND attempt_id = OLD.attempt_id
          AND request_row_id = OLD.request_row_id
    ) AND NOT EXISTS (
        SELECT 1 FROM router.routing_candidate_decisions
        WHERE request_row_id = OLD.request_row_id AND attempt_id = OLD.attempt_id
    ) THEN
        RAISE EXCEPTION 'routing claim requires a durable start or decision before delete'
            USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER routing_attempt_claims_delete_guard
BEFORE DELETE ON router.routing_attempt_claims
FOR EACH ROW EXECUTE FUNCTION router.guard_routing_claim_delete();

CREATE TABLE router.routing_attempt_starts (
    attempt_id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL,
    claim_id uuid NOT NULL UNIQUE,
    claim_generation bigint NOT NULL CHECK (claim_generation > 0),
    candidate_ordinal smallint NOT NULL CHECK (candidate_ordinal BETWEEN 1 AND 8),
    route_snapshot_id uuid NOT NULL
        REFERENCES router.provider_route_execution_snapshots (id) ON DELETE RESTRICT,
    budget_reservation_id uuid NOT NULL UNIQUE
        REFERENCES router.budget_candidate_reservations (id) ON DELETE RESTRICT,
    reservation_key text NOT NULL CHECK (char_length(reservation_key) BETWEEN 1 AND 200),
    started_at timestamptz NOT NULL
);

CREATE TRIGGER routing_attempt_starts_append_only
BEFORE UPDATE OR DELETE ON router.routing_attempt_starts
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.validate_routing_attempt_start()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM router.routing_attempt_claims AS claim
        JOIN router.provider_route_execution_snapshots AS snapshot
          ON snapshot.id = claim.route_snapshot_id
        JOIN router.budget_candidate_reservations AS reservation
          ON reservation.id = NEW.budget_reservation_id
        JOIN router.logical_request_budget_sets AS budget_set
          ON budget_set.id = reservation.budget_set_id
        WHERE claim.request_row_id = NEW.request_row_id
          AND claim.claim_id = NEW.claim_id
          AND claim.claim_generation = NEW.claim_generation
          AND claim.attempt_id = NEW.attempt_id
          AND claim.candidate_ordinal = NEW.candidate_ordinal
          AND claim.route_snapshot_id = NEW.route_snapshot_id
          AND claim.owner_id <> ''
          AND transaction_timestamp() < claim.lease_expires_at
          AND NEW.started_at = transaction_timestamp()
          AND NEW.started_at >= claim.claimed_at
          AND NEW.started_at + interval '100 milliseconds' <= claim.attempt_deadline
          AND budget_set.request_row_id = NEW.request_row_id
          AND reservation.candidate_kind = 'provider_route'
          AND reservation.candidate_id = snapshot.provider_model_route_id
          AND reservation.reservation_key = NEW.reservation_key
          AND NEW.reservation_key = claim.claim_id::text
          AND NOT EXISTS (
              SELECT 1 FROM router.routing_candidate_decisions AS decision
              WHERE decision.request_row_id = claim.request_row_id
                AND decision.attempt_id = claim.attempt_id
          )
    ) THEN
        RAISE EXCEPTION 'attempt start does not match its claim and budget reservation'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER routing_attempt_starts_guard
BEFORE INSERT ON router.routing_attempt_starts
FOR EACH ROW EXECUTE FUNCTION router.validate_routing_attempt_start();

CREATE TABLE router.routing_attempt_dispatches (
    attempt_id uuid PRIMARY KEY
        REFERENCES router.routing_attempt_starts (attempt_id) ON DELETE RESTRICT,
    claim_id uuid NOT NULL,
    claim_generation bigint NOT NULL CHECK (claim_generation > 0),
    owner_id text NOT NULL CHECK (char_length(owner_id) BETWEEN 1 AND 500),
    dispatched_at timestamptz NOT NULL
);

CREATE TRIGGER routing_attempt_dispatches_append_only
BEFORE UPDATE OR DELETE ON router.routing_attempt_dispatches
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.validate_routing_attempt_dispatch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.dispatched_at <> transaction_timestamp() OR NOT EXISTS (
        SELECT 1
        FROM router.routing_attempt_claims AS claim
        JOIN router.routing_attempt_starts AS attempt_start
          ON attempt_start.claim_id = claim.claim_id
         AND attempt_start.attempt_id = claim.attempt_id
        JOIN router.provider_attempts AS attempt
          ON attempt.id = attempt_start.attempt_id AND attempt.state = 'started'
        JOIN router.logical_requests AS request
          ON request.row_id = attempt.request_row_id
        JOIN router.provider_route_execution_snapshots AS snapshot
          ON snapshot.id = attempt.route_snapshot_id
        JOIN router.provider_model_routes AS route
          ON route.id = snapshot.provider_model_route_id
        JOIN router.provider_instances AS instance
          ON instance.id = snapshot.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = snapshot.credential_id
        WHERE claim.attempt_id = NEW.attempt_id
          AND claim.claim_id = NEW.claim_id
          AND claim.claim_generation = NEW.claim_generation
          AND claim.owner_id = NEW.owner_id
          AND transaction_timestamp() < claim.lease_expires_at
          AND transaction_timestamp() < claim.attempt_deadline
          AND request.state = 'running'
          AND NOT request.partial_output AND NOT request.committed_effect
          AND credential.state = 'active'
          AND credential.generation = snapshot.credential_generation
          AND credential.current_revision = snapshot.credential_revision_id
          AND (
              request.assignment_id IS NOT NULL OR EXISTS (
                  SELECT 1 FROM router.diagnostic_route_authorizations AS diagnostic_use
                  WHERE diagnostic_use.request_id = request.request_id
                    AND diagnostic_use.service_id = request.service_id
                    AND diagnostic_use.workspace_id IS NOT DISTINCT FROM request.workspace_id
                    AND diagnostic_use.exact_route_id = snapshot.provider_model_route_id
                    AND diagnostic_use.route_configuration_revision_id =
                        snapshot.route_configuration_revision_id
              )
          )
    ) THEN
        RAISE EXCEPTION 'routing dispatch does not match its live execution claim'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER routing_attempt_dispatches_guard
BEFORE INSERT ON router.routing_attempt_dispatches
FOR EACH ROW EXECUTE FUNCTION router.validate_routing_attempt_dispatch();

CREATE TABLE router.routing_attempt_usage_reports (
    attempt_id uuid PRIMARY KEY
        REFERENCES router.provider_attempts (id) ON DELETE RESTRICT,
    usage_components jsonb NOT NULL CHECK (
        jsonb_typeof(usage_components) = 'array'
        AND jsonb_array_length(usage_components) BETWEEN 1 AND 9
    ),
    reported_at timestamptz NOT NULL
);

CREATE FUNCTION router.validate_routing_attempt_usage()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    component jsonb;
    units text[] := '{}';
BEGIN
    IF NEW.reported_at <> transaction_timestamp() THEN
        RAISE EXCEPTION 'routing usage time must match its transaction'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM router.provider_attempts AS attempt
        JOIN router.routing_candidate_decisions AS decision
          ON decision.attempt_id = attempt.id
         AND decision.request_row_id = attempt.request_row_id
         AND decision.attempt_state = attempt.state::text
        WHERE attempt.id = NEW.attempt_id
          AND attempt.state <> 'started'
    ) THEN
        RAISE EXCEPTION 'routing usage requires an exact terminal attempt decision'
            USING ERRCODE = '23514';
    END IF;
    FOR component IN SELECT value FROM jsonb_array_elements(NEW.usage_components)
    LOOP
        IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(component) AS key)
               IS DISTINCT FROM ARRAY['quantity', 'unit']
           OR component->>'unit' NOT IN (
               'input_token', 'output_token', 'cached_token', 'request', 'image',
               'audio_second', 'search', 'tool_unit', 'other'
           )
           OR jsonb_typeof(component->'quantity') <> 'string'
           OR component->>'quantity' !~ '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR (component->>'quantity')::numeric >
              99999999999999999999.999999999999999999 THEN
            RAISE EXCEPTION 'routing usage component is invalid'
                USING ERRCODE = '23514';
        END IF;
        IF component->>'unit' = ANY(units) THEN
            RAISE EXCEPTION 'routing usage units must be unique'
                USING ERRCODE = '23514';
        END IF;
        units := array_append(units, component->>'unit');
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER routing_attempt_usage_guard
BEFORE INSERT ON router.routing_attempt_usage_reports
FOR EACH ROW EXECUTE FUNCTION router.validate_routing_attempt_usage();

CREATE TRIGGER routing_attempt_usage_append_only
BEFORE UPDATE OR DELETE ON router.routing_attempt_usage_reports
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.valid_redacted_routing_evidence(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    keys text[];
    valid boolean;
BEGIN
    IF jsonb_typeof(value) <> 'object' THEN
        RETURN false;
    END IF;
    SELECT array_agg(key ORDER BY key) INTO keys FROM jsonb_object_keys(value) AS key;
    IF keys IS DISTINCT FROM ARRAY['detail_code', 'provider_status', 'retry_after_ms'] THEN
        RETURN false;
    END IF;
    valid := (value->'provider_status' = 'null'::jsonb OR (
               jsonb_typeof(value->'provider_status') = 'number'
               AND value->>'provider_status' ~ '^[0-9]{3}$'
               AND (value->>'provider_status')::integer BETWEEN 100 AND 599
           ))
       AND (value->'retry_after_ms' = 'null'::jsonb OR (
               jsonb_typeof(value->'retry_after_ms') = 'number'
               AND value->>'retry_after_ms' ~ '^[0-9]{1,6}$'
               AND (value->>'retry_after_ms')::integer BETWEEN 0 AND 900000
           ))
       AND (value->'detail_code' = 'null'::jsonb OR (
               jsonb_typeof(value->'detail_code') = 'string'
               AND char_length(value->>'detail_code') BETWEEN 1 AND 100
               AND value->>'detail_code' ~ '^[ -~]+$'
           ));
    RETURN COALESCE(valid, false);
EXCEPTION
    WHEN numeric_value_out_of_range OR invalid_text_representation THEN
        RETURN false;
END;
$$;

CREATE TABLE router.routing_candidate_decisions (
    decision_id uuid PRIMARY KEY,
    request_row_id uuid NOT NULL,
    decision_sequence smallint NOT NULL CHECK (decision_sequence BETWEEN 1 AND 16),
    attempt_id uuid NOT NULL,
    claim_id uuid,
    claim_generation bigint CHECK (claim_generation > 0),
    attempt_number smallint NOT NULL CHECK (attempt_number BETWEEN 1 AND 8),
    candidate_ordinal smallint NOT NULL CHECK (candidate_ordinal BETWEEN 1 AND 8),
    route_snapshot_id uuid NOT NULL
        REFERENCES router.provider_route_execution_snapshots (id) ON DELETE RESTRICT,
    connect_timeout_ms integer CHECK (connect_timeout_ms BETWEEN 1 AND 120000),
    first_byte_timeout_ms integer CHECK (first_byte_timeout_ms BETWEEN 1 AND 120000),
    idle_timeout_ms integer CHECK (idle_timeout_ms BETWEEN 1 AND 120000),
    execution_timeout_ms integer CHECK (execution_timeout_ms BETWEEN 100 AND 120000),
    logical_deadline timestamptz,
    attempt_deadline timestamptz,
    attempt_state text NOT NULL CHECK (attempt_state IN (
        'succeeded', 'failed', 'interrupted', 'cancelled', 'uncertain'
    )),
    normalized_error_class text CHECK (normalized_error_class IN (
        'authentication', 'policy', 'budget', 'rate_limit', 'timeout', 'transport',
        'provider_unavailable', 'invalid_provider_response', 'incompatible_request',
        'cancelled', 'uncertain_effect', 'router_internal'
    )),
    affected_scope text CHECK (affected_scope IN (
        'attempt', 'provider_model_route', 'provider_instance', 'credential',
        'assignment_candidate', 'logical_request'
    )),
    affected_scope_id text CHECK (char_length(affected_scope_id) BETWEEN 1 AND 500),
    fallback_decision text NOT NULL CHECK (fallback_decision IN (
        'succeeded', 'next_candidate', 'stop_request', 'commit_boundary', 'cancelled'
    )),
    safe_provider_code text CHECK (
        safe_provider_code IS NULL OR (
            char_length(safe_provider_code) BETWEEN 1 AND 200
            AND safe_provider_code ~ '^[ -~]+$'
        )
    ),
    redacted_evidence jsonb CHECK (
        redacted_evidence IS NULL
        OR router.valid_redacted_routing_evidence(redacted_evidence)
    ),
    occurred_at timestamptz NOT NULL,
    migration_0015_backfilled boolean NOT NULL DEFAULT false,
    FOREIGN KEY (request_row_id)
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    UNIQUE (request_row_id, attempt_id),
    UNIQUE (request_row_id, decision_sequence),
    CHECK (
        migration_0015_backfilled OR (
            claim_id IS NOT NULL
            AND claim_generation IS NOT NULL
            AND connect_timeout_ms IS NOT NULL
            AND first_byte_timeout_ms IS NOT NULL
            AND idle_timeout_ms IS NOT NULL
            AND execution_timeout_ms IS NOT NULL
            AND logical_deadline IS NOT NULL
            AND attempt_deadline IS NOT NULL
            AND connect_timeout_ms <= execution_timeout_ms
            AND first_byte_timeout_ms <= execution_timeout_ms
            AND idle_timeout_ms <= execution_timeout_ms
            AND attempt_deadline <= logical_deadline
        )
    ),
    CHECK (
        (attempt_state = 'succeeded'
         AND normalized_error_class IS NULL
         AND affected_scope IS NULL
         AND affected_scope_id IS NULL
         AND fallback_decision = 'succeeded'
         AND safe_provider_code IS NULL
         AND redacted_evidence IS NULL)
        OR
        (attempt_state <> 'succeeded'
         AND normalized_error_class IS NOT NULL
         AND affected_scope IS NOT NULL
         AND affected_scope_id IS NOT NULL
         AND fallback_decision <> 'succeeded'
         AND redacted_evidence IS NOT NULL)
    )
);

CREATE TRIGGER routing_candidate_decisions_append_only
BEFORE UPDATE OR DELETE ON router.routing_candidate_decisions
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE TABLE router.routing_request_terminal_decisions (
    request_row_id uuid PRIMARY KEY
        REFERENCES router.logical_requests (row_id) ON DELETE RESTRICT,
    decision_id uuid NOT NULL UNIQUE,
    attempt_id uuid NOT NULL UNIQUE,
    claim_id uuid NOT NULL UNIQUE,
    attempt_number smallint NOT NULL CHECK (attempt_number BETWEEN 1 AND 8),
    candidate_ordinal smallint NOT NULL CHECK (candidate_ordinal BETWEEN 1 AND 8),
    route_snapshot_id uuid NOT NULL
        REFERENCES router.provider_route_execution_snapshots (id) ON DELETE RESTRICT,
    connect_timeout_ms integer NOT NULL CHECK (connect_timeout_ms BETWEEN 1 AND 120000),
    first_byte_timeout_ms integer NOT NULL CHECK (first_byte_timeout_ms BETWEEN 1 AND 120000),
    idle_timeout_ms integer NOT NULL CHECK (idle_timeout_ms BETWEEN 1 AND 120000),
    execution_timeout_ms integer NOT NULL CHECK (execution_timeout_ms BETWEEN 100 AND 120000),
    logical_deadline timestamptz NOT NULL,
    attempt_deadline timestamptz NOT NULL,
    attempt_state text NOT NULL CHECK (attempt_state = 'failed'),
    normalized_error_class text NOT NULL CHECK (normalized_error_class = 'timeout'),
    affected_scope text NOT NULL CHECK (affected_scope = 'logical_request'),
    affected_scope_id text NOT NULL CHECK (char_length(affected_scope_id) BETWEEN 1 AND 500),
    fallback_decision text NOT NULL CHECK (fallback_decision = 'stop_request'),
    safe_provider_code text CHECK (safe_provider_code IS NULL),
    redacted_evidence jsonb NOT NULL CHECK (
        router.valid_redacted_routing_evidence(redacted_evidence)
    ),
    occurred_at timestamptz NOT NULL,
    CHECK (connect_timeout_ms <= execution_timeout_ms),
    CHECK (first_byte_timeout_ms <= execution_timeout_ms),
    CHECK (idle_timeout_ms <= execution_timeout_ms),
    CHECK (attempt_deadline = logical_deadline)
);

CREATE TRIGGER routing_request_terminal_decisions_append_only
BEFORE UPDATE OR DELETE ON router.routing_request_terminal_decisions
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.validate_routing_request_terminal_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM router.logical_requests
    WHERE row_id = NEW.request_row_id FOR UPDATE;
    IF NEW.occurred_at <> transaction_timestamp() OR NOT EXISTS (
        SELECT 1
        FROM router.logical_requests AS request
        JOIN router.provider_route_execution_snapshots AS snapshot
          ON snapshot.request_row_id = request.row_id
         AND snapshot.id = NEW.route_snapshot_id
        WHERE request.row_id = NEW.request_row_id
          AND request.state = 'running'
          AND NOT request.partial_output
          AND NOT request.committed_effect
          AND NEW.candidate_ordinal = snapshot.candidate_ordinal
          AND NEW.attempt_number = 1 + (
              SELECT count(*) FROM router.provider_attempts AS attempt
              WHERE attempt.request_row_id = request.row_id
          )
          AND NEW.logical_deadline = request.admitted_at + interval '15 minutes'
          AND transaction_timestamp() + interval '100 milliseconds'
              >= NEW.logical_deadline
          AND NEW.affected_scope_id = request.request_id::text
          AND NEW.redacted_evidence = jsonb_build_object(
              'provider_status', NULL, 'retry_after_ms', NULL,
              'detail_code', 'logical_deadline'
          )
          AND NOT EXISTS (
              SELECT 1 FROM router.routing_attempt_claims AS claim
              WHERE claim.request_row_id = request.row_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM router.provider_attempts AS prior_attempt
              WHERE prior_attempt.request_row_id = request.row_id
                AND prior_attempt.state <> 'started'
                AND NOT prior_attempt.migration_0015_backfilled
                AND NOT EXISTS (
                    SELECT 1
                    FROM router.accounting_facts AS fact
                    LEFT JOIN router.budget_reservation_reconciliations AS reconciliation
                      ON reconciliation.accounting_event_id = fact.event_id
                     AND reconciliation.reservation_id =
                         prior_attempt.budget_reservation_id
                    WHERE fact.request_row_id = request.row_id
                      AND fact.subject_kind = 'provider_attempt'
                      AND fact.subject_id = prior_attempt.id
                      AND fact.outcome = CASE prior_attempt.state
                          WHEN 'succeeded' THEN 'succeeded'
                          WHEN 'failed' THEN 'failed'
                          WHEN 'interrupted' THEN 'interrupted'
                          WHEN 'uncertain' THEN 'uncertain'
                          WHEN 'cancelled' THEN 'failed'
                      END
                      AND fact.occurred_at >= prior_attempt.finished_at
                      AND (
                          prior_attempt.budget_reservation_id IS NULL
                          OR reconciliation.reservation_id IS NOT NULL
                      )
                )
          )
    ) THEN
        RAISE EXCEPTION 'request terminal decision does not match an expired admitted request'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER routing_request_terminal_decisions_guard
BEFORE INSERT ON router.routing_request_terminal_decisions
FOR EACH ROW EXECUTE FUNCTION router.validate_routing_request_terminal_decision();

CREATE TABLE router.diagnostic_route_grants (
    grant_id uuid PRIMARY KEY,
    grant_sha256 bytea NOT NULL UNIQUE CHECK (octet_length(grant_sha256) = 32),
    service_id uuid NOT NULL REFERENCES router.services (id) ON DELETE RESTRICT,
    workspace_id uuid,
    exact_route_id uuid NOT NULL
        REFERENCES router.provider_model_routes (id) ON DELETE RESTRICT,
    route_configuration_revision_id uuid NOT NULL
        REFERENCES router.configuration_revisions (id) ON DELETE RESTRICT,
    credential_id uuid NOT NULL
        REFERENCES router.encrypted_credentials (id) ON DELETE RESTRICT,
    credential_generation bigint NOT NULL CHECK (credential_generation > 0),
    credential_revision_id uuid NOT NULL,
    created_by_kind text NOT NULL CHECK (created_by_kind IN ('service', 'administrator')),
    created_by_id text NOT NULL CHECK (char_length(created_by_id) BETWEEN 1 AND 500),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    creation_audit_event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '5 minutes')
);

CREATE TRIGGER diagnostic_route_grants_append_only
BEFORE UPDATE OR DELETE ON router.diagnostic_route_grants
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

ALTER TABLE router.diagnostic_route_grants
ADD CONSTRAINT diagnostic_route_grants_complete_identity UNIQUE NULLS NOT DISTINCT (
    grant_id, service_id, workspace_id, exact_route_id,
    route_configuration_revision_id
);

CREATE TABLE router.diagnostic_route_authorizations (
    authorization_id uuid PRIMARY KEY,
    grant_id uuid NOT NULL
        REFERENCES router.diagnostic_route_grants (grant_id) ON DELETE RESTRICT,
    request_id uuid NOT NULL,
    service_id uuid NOT NULL,
    workspace_id uuid,
    exact_route_id uuid NOT NULL,
    route_configuration_revision_id uuid NOT NULL,
    authorized_by_kind text NOT NULL
        CHECK (authorized_by_kind IN ('service', 'administrator')),
    authorized_by_id text NOT NULL CHECK (char_length(authorized_by_id) BETWEEN 1 AND 500),
    authorized_at timestamptz NOT NULL,
    use_audit_event_id uuid NOT NULL UNIQUE
        REFERENCES router.audit_events (event_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, service_id)
        REFERENCES router.workspaces (id, service_id) ON DELETE RESTRICT,
    UNIQUE (grant_id),
    FOREIGN KEY (grant_id, service_id, workspace_id, exact_route_id,
                 route_configuration_revision_id)
        REFERENCES router.diagnostic_route_grants (
            grant_id, service_id, workspace_id, exact_route_id,
            route_configuration_revision_id
        ) ON DELETE RESTRICT
);

CREATE TRIGGER diagnostic_route_authorizations_append_only
BEFORE UPDATE OR DELETE ON router.diagnostic_route_authorizations
FOR EACH ROW EXECUTE FUNCTION router.reject_record_change();

CREATE FUNCTION router.check_diagnostic_route_authorization()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.authorized_at <> transaction_timestamp() THEN
        RAISE EXCEPTION 'diagnostic authorization time must match its transaction'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM router.diagnostic_route_grants AS diagnostic_grant
        JOIN router.provider_model_routes AS route
          ON route.id = diagnostic_grant.exact_route_id
        JOIN router.provider_instances AS instance
          ON instance.id = route.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = instance.credential_id
        WHERE diagnostic_grant.grant_id = NEW.grant_id
          AND diagnostic_grant.service_id = NEW.service_id
          AND diagnostic_grant.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND diagnostic_grant.exact_route_id = NEW.exact_route_id
          AND diagnostic_grant.route_configuration_revision_id = NEW.route_configuration_revision_id
          AND credential.id = diagnostic_grant.credential_id
          AND credential.generation = diagnostic_grant.credential_generation
          AND credential.current_revision = diagnostic_grant.credential_revision_id
          AND NEW.authorized_at >= diagnostic_grant.created_at
          AND NEW.authorized_at < diagnostic_grant.expires_at
          AND route.current_revision = diagnostic_grant.route_configuration_revision_id
          AND route.state = 'active'
          AND instance.state = 'active'
          AND credential.state = 'active'
          AND router.provider_route_is_eligible(route.id, NEW.service_id)
          AND router.active_request_scope(NEW.service_id, NEW.workspace_id)
          AND router.provider_resource_is_enabled(
              'provider_model_route', route.id, NEW.service_id, NEW.workspace_id
          )
          AND router.provider_resource_is_enabled(
              'provider_instance', instance.id, NEW.service_id, NEW.workspace_id
          )
    ) THEN
        RAISE EXCEPTION 'diagnostic route authorization does not match its grant'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM router.audit_events AS audit
        WHERE audit.event_id = NEW.use_audit_event_id
          AND audit.actor_kind = NEW.authorized_by_kind
          AND audit.actor_id = NEW.authorized_by_id
          AND audit.authority_class = CASE NEW.authorized_by_kind
              WHEN 'administrator' THEN 'global_administrator'
              ELSE 'service'
          END
          AND audit.service_id = NEW.service_id
          AND audit.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND audit.action = 'diagnostic.route.use'
          AND audit.permission_result = 'permitted'
          AND audit.occurred_at = NEW.authorized_at
          AND audit.safe_details = jsonb_build_object(
              'diagnostic_grant_id', NEW.grant_id,
              'exact_route_id', NEW.exact_route_id,
              'request_id', NEW.request_id,
              'route_configuration_revision_id', NEW.route_configuration_revision_id
          )
    ) THEN
        RAISE EXCEPTION 'diagnostic route use audit does not match its authorization'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER diagnostic_route_authorizations_guard
BEFORE INSERT ON router.diagnostic_route_authorizations
FOR EACH ROW EXECUTE FUNCTION router.check_diagnostic_route_authorization();

CREATE FUNCTION router.check_diagnostic_route_grant_audit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.created_at <> transaction_timestamp() THEN
        RAISE EXCEPTION 'diagnostic grant time must match its transaction'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM router.provider_model_routes AS route
        JOIN router.provider_instances AS instance
          ON instance.id = route.provider_instance_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = instance.credential_id
        WHERE route.id = NEW.exact_route_id
          AND route.current_revision = NEW.route_configuration_revision_id
          AND route.state = 'active'
          AND instance.state = 'active'
          AND credential.state = 'active'
          AND credential.id = NEW.credential_id
          AND credential.generation = NEW.credential_generation
          AND credential.current_revision = NEW.credential_revision_id
          AND router.provider_route_is_eligible(route.id, NEW.service_id)
          AND router.active_request_scope(NEW.service_id, NEW.workspace_id)
          AND router.provider_resource_is_enabled(
              'provider_model_route', route.id, NEW.service_id, NEW.workspace_id
          )
          AND router.provider_resource_is_enabled(
              'provider_instance', instance.id, NEW.service_id, NEW.workspace_id
          )
    ) THEN
        RAISE EXCEPTION 'diagnostic grant route is not active and eligible'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM router.audit_events AS audit
        WHERE audit.event_id = NEW.creation_audit_event_id
          AND audit.actor_kind = NEW.created_by_kind
          AND audit.actor_id = NEW.created_by_id
          AND audit.authority_class = CASE NEW.created_by_kind
              WHEN 'administrator' THEN 'global_administrator'
              ELSE 'service'
          END
          AND audit.service_id = NEW.service_id
          AND audit.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND audit.action = 'diagnostic.grant.create'
          AND audit.permission_result = 'permitted'
          AND audit.occurred_at = NEW.created_at
          AND audit.safe_details = jsonb_build_object(
              'diagnostic_grant_id', NEW.grant_id,
              'exact_route_id', NEW.exact_route_id,
              'route_configuration_revision_id', NEW.route_configuration_revision_id,
              'reason', NEW.reason,
              'expires_at', NEW.expires_at
          )
    ) THEN
        RAISE EXCEPTION 'diagnostic grant creation audit does not match its grant'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER diagnostic_route_grants_audit_guard
BEFORE INSERT ON router.diagnostic_route_grants
FOR EACH ROW EXECUTE FUNCTION router.check_diagnostic_route_grant_audit();

ALTER TABLE router.provider_attempts
DROP CONSTRAINT provider_attempts_provider_model_route_id_route_generation_fkey,
ADD COLUMN route_snapshot_id uuid,
ADD COLUMN candidate_ordinal smallint,
ADD COLUMN provider_instance_id uuid,
ADD COLUMN provider_instance_generation bigint,
ADD COLUMN credential_id uuid,
ADD COLUMN credential_generation bigint,
ADD COLUMN connect_timeout_ms integer,
ADD COLUMN first_byte_timeout_ms integer,
ADD COLUMN idle_timeout_ms integer,
ADD COLUMN execution_timeout_ms integer,
ADD COLUMN logical_deadline timestamptz,
ADD COLUMN attempt_deadline timestamptz,
ADD COLUMN affected_scope_id text,
ADD COLUMN safe_provider_code text,
ADD COLUMN redacted_evidence jsonb,
ADD COLUMN budget_reservation_id uuid,
ADD COLUMN migration_0015_backfilled boolean NOT NULL DEFAULT false;

INSERT INTO router.provider_route_execution_snapshots (
    id, request_row_id, candidate_ordinal, assignment_revision_id,
    attempt_timeout_ms, candidate_policy,
    content_sha256, route_configuration_revision_id,
    provider_model_route_id, route_generation, provider_instance_id,
    provider_instance_generation, instance_configuration_revision_id,
    credential_id, credential_generation, credential_revision_id,
    price_version_id, adapter_type_id, endpoint_origin, wire_model,
    capabilities, instance_settings, route_settings, typed_prices,
    created_at, migration_0015_backfilled
)
SELECT attempt.id, attempt.request_row_id, COALESCE((
           SELECT candidate.ordinal FROM router.assignment_candidates AS candidate
           WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
             AND candidate.provider_model_route_id = attempt.provider_model_route_id
           LIMIT 1
       ), 1), attempt.assignment_revision_id, COALESCE((
           SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
           WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
             AND candidate.provider_model_route_id = attempt.provider_model_route_id
           LIMIT 1
       ), 120000), COALESCE((
           SELECT candidate.candidate_policy FROM router.assignment_candidates AS candidate
           WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
             AND candidate.provider_model_route_id = attempt.provider_model_route_id
           LIMIT 1
       ), '{}'::jsonb),
       sha256(convert_to(jsonb_build_object(
           'request_row_id', attempt.request_row_id,
           'candidate_ordinal', COALESCE((
               SELECT candidate.ordinal FROM router.assignment_candidates AS candidate
               WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
                 AND candidate.provider_model_route_id = attempt.provider_model_route_id
               LIMIT 1
           ), 1),
           'assignment_revision_id', attempt.assignment_revision_id,
           'attempt_timeout_ms', COALESCE((
               SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
               WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
                 AND candidate.provider_model_route_id = attempt.provider_model_route_id
               LIMIT 1
           ), 120000),
           'candidate_policy', COALESCE((
               SELECT candidate.candidate_policy FROM router.assignment_candidates AS candidate
               WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
                 AND candidate.provider_model_route_id = attempt.provider_model_route_id
               LIMIT 1
           ), '{}'::jsonb),
           'route_configuration_revision_id', route.current_revision,
           'provider_model_route_id', route.id,
           'route_generation', attempt.route_generation,
           'provider_instance_id', instance.id,
           'provider_instance_generation', instance.generation,
           'instance_configuration_revision_id', COALESCE(instance.current_revision, route.current_revision),
           'credential_id', credential.id,
           'credential_generation', credential.generation,
           'credential_revision_id', credential.current_revision,
           'price_version_id', attempt.price_version_id,
           'adapter_type_id', instance.adapter_type_id,
           'endpoint_origin', instance.endpoint_origin,
           'wire_model', route.wire_model,
           'capabilities', route.capabilities,
           'instance_settings', instance.settings,
           'route_settings', route.settings,
           'typed_prices', COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'unit', component.unit_name,
                   'price', component.unit_price::text,
                   'currency', version.currency,
                   'raw_source_value', component.raw_source_value,
                   'unit_quantity', component.unit_quantity::text
               ) ORDER BY component.unit_name)
               FROM router.route_price_components AS component
               JOIN router.route_price_versions AS version
                 ON version.id = component.price_version_id
               WHERE component.price_version_id = attempt.price_version_id
           ), '[{"unit":"request","price":"0","currency":"USD","raw_source_value":"migration","unit_quantity":"1"}]'::jsonb)
       )::text, 'UTF8')),
       route.current_revision, route.id, attempt.route_generation,
       instance.id, instance.generation, COALESCE(instance.current_revision, route.current_revision),
       credential.id, credential.generation, credential.current_revision,
       attempt.price_version_id, instance.adapter_type_id, instance.endpoint_origin,
       route.wire_model, route.capabilities, instance.settings, route.settings,
       COALESCE((
           SELECT jsonb_agg(jsonb_build_object(
               'unit', component.unit_name,
               'price', component.unit_price::text,
               'currency', version.currency,
               'raw_source_value', component.raw_source_value,
               'unit_quantity', component.unit_quantity::text
           ) ORDER BY component.unit_name)
           FROM router.route_price_components AS component
           JOIN router.route_price_versions AS version
             ON version.id = component.price_version_id
           WHERE component.price_version_id = attempt.price_version_id
       ), '[{"unit":"request","price":"0","currency":"USD","raw_source_value":"migration","unit_quantity":"1"}]'::jsonb),
       attempt.started_at, true
FROM router.provider_attempts AS attempt
JOIN router.provider_model_routes AS route
  ON route.id = attempt.provider_model_route_id
 AND route.generation = attempt.route_generation
JOIN router.provider_instances AS instance
  ON instance.id = route.provider_instance_id
JOIN router.encrypted_credentials AS credential
  ON credential.id = instance.credential_id;

UPDATE router.provider_attempts AS attempt
SET route_snapshot_id = snapshot.id,
    candidate_ordinal = COALESCE((
        SELECT candidate.ordinal FROM router.assignment_candidates AS candidate
        WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
          AND candidate.provider_model_route_id = attempt.provider_model_route_id
        LIMIT 1
    ), 1),
    provider_instance_id = snapshot.provider_instance_id,
    provider_instance_generation = snapshot.provider_instance_generation,
    credential_id = snapshot.credential_id,
    credential_generation = snapshot.credential_generation,
    connect_timeout_ms = LEAST(10000, COALESCE((
        SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
        WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
          AND candidate.provider_model_route_id = attempt.provider_model_route_id
        LIMIT 1
    ), 30000)),
    first_byte_timeout_ms = LEAST(30000, COALESCE((
        SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
        WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
          AND candidate.provider_model_route_id = attempt.provider_model_route_id
        LIMIT 1
    ), 30000)),
    idle_timeout_ms = LEAST(30000, COALESCE((
        SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
        WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
          AND candidate.provider_model_route_id = attempt.provider_model_route_id
        LIMIT 1
    ), 30000)),
    execution_timeout_ms = COALESCE((
        SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
        WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
          AND candidate.provider_model_route_id = attempt.provider_model_route_id
        LIMIT 1
    ), 30000),
    logical_deadline = request.admitted_at + interval '15 minutes',
    attempt_deadline = LEAST(
        attempt.started_at + COALESCE((
            SELECT candidate.attempt_timeout_ms FROM router.assignment_candidates AS candidate
            WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
              AND candidate.provider_model_route_id = attempt.provider_model_route_id
            LIMIT 1
        ), 30000) * interval '1 millisecond',
        request.admitted_at + interval '15 minutes'
    ),
    normalized_error_class = CASE
        WHEN attempt.state IN ('started', 'succeeded') THEN NULL
        WHEN attempt.state = 'cancelled' THEN 'cancelled'
        WHEN attempt.state = 'uncertain' THEN 'uncertain_effect'
        ELSE COALESCE(attempt.normalized_error_class, 'router_internal')
    END,
    affected_scope = CASE
        WHEN attempt.state IN ('started', 'succeeded') THEN NULL
        WHEN attempt.state IN ('cancelled', 'uncertain') THEN 'logical_request'
        ELSE COALESCE(attempt.affected_scope, 'attempt')
    END,
    retry_decision = CASE
        WHEN attempt.state = 'started' THEN NULL
        WHEN attempt.state = 'succeeded' THEN 'succeeded'
        WHEN attempt.state = 'cancelled' THEN 'cancelled'
        WHEN attempt.state = 'uncertain' THEN 'commit_boundary'
        ELSE COALESCE(attempt.retry_decision, 'stop_request')
    END,
    affected_scope_id = CASE WHEN attempt.state IN ('started', 'succeeded') THEN NULL
      WHEN attempt.state IN ('cancelled', 'uncertain') THEN request.request_id::text ELSE
      CASE COALESCE(attempt.affected_scope, 'attempt')
        WHEN 'attempt' THEN attempt.id::text
        WHEN 'provider_model_route' THEN snapshot.provider_model_route_id::text
        WHEN 'provider_instance' THEN snapshot.provider_instance_id::text
        WHEN 'credential' THEN snapshot.credential_id::text
        WHEN 'assignment_candidate' THEN COALESCE(
            request.assignment_id::text,
            'exact:' || request.exact_route_id::text
        ) || ':' || COALESCE((
            SELECT candidate.ordinal FROM router.assignment_candidates AS candidate
            WHERE candidate.configuration_revision_id = attempt.assignment_revision_id
              AND candidate.provider_model_route_id = attempt.provider_model_route_id
            LIMIT 1
        ), 1)::text
        WHEN 'logical_request' THEN request.request_id::text
        ELSE NULL
      END
    END,
    safe_provider_code = NULL,
    redacted_evidence = CASE WHEN attempt.state IN ('started', 'succeeded') THEN NULL ELSE
        jsonb_build_object(
            'provider_status', NULL,
            'retry_after_ms', NULL,
            'detail_code', NULL
        ) END,
    migration_0015_backfilled = true
FROM router.provider_route_execution_snapshots AS snapshot,
     router.logical_requests AS request
WHERE snapshot.id = attempt.id
  AND request.row_id = attempt.request_row_id;

INSERT INTO router.routing_candidate_decisions (
    decision_id, request_row_id, decision_sequence, attempt_id, attempt_number, candidate_ordinal,
    route_snapshot_id, attempt_state, normalized_error_class, affected_scope,
    affected_scope_id, fallback_decision, safe_provider_code, redacted_evidence,
    occurred_at, migration_0015_backfilled
)
SELECT attempt.id, attempt.request_row_id, attempt.attempt_number, attempt.id, attempt.attempt_number,
       attempt.candidate_ordinal, attempt.route_snapshot_id, attempt.state::text,
       attempt.normalized_error_class, attempt.affected_scope,
       attempt.affected_scope_id, attempt.retry_decision,
       attempt.safe_provider_code, attempt.redacted_evidence,
       COALESCE(attempt.finished_at, attempt.started_at), true
FROM router.provider_attempts AS attempt
WHERE attempt.state <> 'started';

ALTER TABLE router.provider_attempts
ALTER COLUMN route_snapshot_id SET NOT NULL,
ALTER COLUMN candidate_ordinal SET NOT NULL,
ALTER COLUMN provider_instance_id SET NOT NULL,
ALTER COLUMN provider_instance_generation SET NOT NULL,
ALTER COLUMN credential_id SET NOT NULL,
ALTER COLUMN credential_generation SET NOT NULL,
ALTER COLUMN connect_timeout_ms SET NOT NULL,
ALTER COLUMN first_byte_timeout_ms SET NOT NULL,
ALTER COLUMN idle_timeout_ms SET NOT NULL,
ALTER COLUMN execution_timeout_ms SET NOT NULL,
ALTER COLUMN logical_deadline SET NOT NULL,
ALTER COLUMN attempt_deadline SET NOT NULL,
ADD CONSTRAINT provider_attempts_route_snapshot_fk
    FOREIGN KEY (
        route_snapshot_id, provider_model_route_id, route_generation,
        provider_instance_id, provider_instance_generation,
        credential_id, credential_generation, price_version_id
    ) REFERENCES router.provider_route_execution_snapshots (
        id, provider_model_route_id, route_generation,
        provider_instance_id, provider_instance_generation,
        credential_id, credential_generation, price_version_id
    ) ON DELETE RESTRICT,
ADD CONSTRAINT provider_attempts_budget_reservation_fk
    FOREIGN KEY (budget_reservation_id)
    REFERENCES router.budget_candidate_reservations (id) ON DELETE RESTRICT,
ADD CONSTRAINT provider_attempts_timeout_bounds CHECK (
    connect_timeout_ms BETWEEN 1 AND execution_timeout_ms
    AND first_byte_timeout_ms BETWEEN 1 AND execution_timeout_ms
    AND idle_timeout_ms BETWEEN 1 AND execution_timeout_ms
    AND execution_timeout_ms BETWEEN 100 AND 120000
    AND attempt_deadline <= logical_deadline
),
ADD CONSTRAINT provider_attempts_failure_evidence CHECK (
    (state = 'started' AND normalized_error_class IS NULL
     AND affected_scope IS NULL AND affected_scope_id IS NULL
     AND retry_decision IS NULL AND safe_provider_code IS NULL
     AND redacted_evidence IS NULL)
    OR (state = 'succeeded' AND normalized_error_class IS NULL
        AND affected_scope IS NULL AND affected_scope_id IS NULL
        AND retry_decision = 'succeeded' AND safe_provider_code IS NULL
        AND redacted_evidence IS NULL)
    OR (state IN ('failed', 'interrupted', 'cancelled', 'uncertain')
        AND normalized_error_class IN (
            'authentication', 'policy', 'budget', 'rate_limit', 'timeout', 'transport',
            'provider_unavailable', 'invalid_provider_response', 'incompatible_request',
            'cancelled', 'uncertain_effect', 'router_internal'
        )
        AND affected_scope IN (
            'attempt', 'provider_model_route', 'provider_instance', 'credential',
            'assignment_candidate', 'logical_request'
        )
        AND affected_scope_id IS NOT NULL
        AND char_length(affected_scope_id) BETWEEN 1 AND 500
        AND retry_decision IN (
            'next_candidate', 'stop_request', 'commit_boundary', 'cancelled'
        )
        AND redacted_evidence IS NOT NULL
        AND router.valid_redacted_routing_evidence(redacted_evidence))
),
ADD CONSTRAINT provider_attempts_safe_provider_code_bound CHECK (
    safe_provider_code IS NULL OR (
        char_length(safe_provider_code) BETWEEN 1 AND 200
        AND safe_provider_code ~ '^[ -~]+$'
    )
),
ADD CONSTRAINT provider_attempts_new_work_has_budget CHECK (
    migration_0015_backfilled OR budget_reservation_id IS NOT NULL
);

CREATE OR REPLACE FUNCTION router.protect_provider_attempt()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id
       OR NEW.request_row_id <> OLD.request_row_id
       OR NEW.service_id <> OLD.service_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.attempt_number <> OLD.attempt_number
       OR NEW.provider_model_route_id <> OLD.provider_model_route_id
       OR NEW.route_generation <> OLD.route_generation
       OR NEW.assignment_revision_id <> OLD.assignment_revision_id
       OR NEW.price_version_id <> OLD.price_version_id
       OR NEW.route_snapshot_id <> OLD.route_snapshot_id
       OR NEW.candidate_ordinal <> OLD.candidate_ordinal
       OR NEW.provider_instance_id <> OLD.provider_instance_id
       OR NEW.provider_instance_generation <> OLD.provider_instance_generation
       OR NEW.credential_id <> OLD.credential_id
       OR NEW.credential_generation <> OLD.credential_generation
       OR NEW.connect_timeout_ms <> OLD.connect_timeout_ms
       OR NEW.first_byte_timeout_ms <> OLD.first_byte_timeout_ms
       OR NEW.idle_timeout_ms <> OLD.idle_timeout_ms
       OR NEW.execution_timeout_ms <> OLD.execution_timeout_ms
       OR NEW.logical_deadline <> OLD.logical_deadline
       OR NEW.attempt_deadline <> OLD.attempt_deadline
       OR NEW.budget_reservation_id IS DISTINCT FROM OLD.budget_reservation_id
       OR NEW.migration_0015_backfilled <> OLD.migration_0015_backfilled
       OR NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION 'provider attempt identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state <> 'started' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal provider attempt is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'started' AND NEW.state = 'started'
       AND NEW.finished_at IS NOT NULL THEN
        RAISE EXCEPTION 'started provider attempt cannot have finished_at'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.check_provider_attempt_sequence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_number integer;
BEGIN
    PERFORM 1 FROM router.logical_requests
    WHERE row_id = NEW.request_row_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM router.provider_attempts
        WHERE request_row_id = NEW.request_row_id AND state = 'started'
    ) THEN
        RAISE EXCEPTION 'one provider attempt is already active'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.provider_attempts
        WHERE request_row_id = NEW.request_row_id AND state = 'succeeded'
    ) THEN
        RAISE EXCEPTION 'a successful request cannot start another provider attempt'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM router.provider_attempts AS attempt
        WHERE attempt.request_row_id = NEW.request_row_id
          AND attempt.state <> 'started'
          AND NOT EXISTS (
              SELECT 1 FROM router.routing_candidate_decisions AS decision
              WHERE decision.request_row_id = attempt.request_row_id
                AND decision.attempt_id = attempt.id
                AND decision.attempt_state = attempt.state::text
          )
    ) THEN
        RAISE EXCEPTION 'a prior provider attempt has no durable routing decision'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.routing_candidate_decisions AS decision
        WHERE decision.request_row_id = NEW.request_row_id
          AND decision.fallback_decision <> 'next_candidate'
          AND NOT EXISTS (
              SELECT 1 FROM router.routing_candidate_decisions AS later
              WHERE later.request_row_id = decision.request_row_id
                AND later.candidate_ordinal > decision.candidate_ordinal
          )
    ) THEN
        RAISE EXCEPTION 'the last routing decision does not permit another attempt'
            USING ERRCODE = '55000';
    END IF;
    SELECT COALESCE(max(attempt_number), 0) + 1 INTO expected_number
    FROM router.provider_attempts WHERE request_row_id = NEW.request_row_id;
    IF NEW.attempt_number <> expected_number THEN
        RAISE EXCEPTION 'provider attempts must use the next sequence number'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION router.validate_provider_attempt_start()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.migration_0015_backfilled THEN
        RETURN NEW;
    END IF;
    IF NEW.state <> 'started' OR NEW.finished_at IS NOT NULL THEN
        RAISE EXCEPTION 'a provider attempt must start before it can finish'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM router.routing_attempt_starts AS attempt_start
        JOIN router.routing_attempt_claims AS claim
          ON claim.claim_id = attempt_start.claim_id
         AND claim.claim_generation = attempt_start.claim_generation
         AND claim.request_row_id = attempt_start.request_row_id
        JOIN router.provider_route_execution_snapshots AS snapshot
          ON snapshot.id = attempt_start.route_snapshot_id
        JOIN router.logical_requests AS request
          ON request.row_id = attempt_start.request_row_id
        JOIN router.encrypted_credentials AS credential
          ON credential.id = snapshot.credential_id
        JOIN router.provider_model_routes AS live_route
          ON live_route.id = snapshot.provider_model_route_id
        JOIN router.provider_instances AS live_instance
          ON live_instance.id = snapshot.provider_instance_id
        WHERE attempt_start.attempt_id = NEW.id
          AND attempt_start.request_row_id = NEW.request_row_id
          AND attempt_start.candidate_ordinal = NEW.candidate_ordinal
          AND attempt_start.route_snapshot_id = NEW.route_snapshot_id
          AND attempt_start.budget_reservation_id = NEW.budget_reservation_id
          AND attempt_start.started_at = NEW.started_at
          AND claim.attempt_id = NEW.id
          AND claim.attempt_number = NEW.attempt_number
          AND claim.assignment_revision_id = NEW.assignment_revision_id
          AND claim.logical_deadline = NEW.logical_deadline
          AND claim.attempt_deadline = NEW.attempt_deadline
          AND claim.connect_timeout_ms = NEW.connect_timeout_ms
          AND claim.first_byte_timeout_ms = NEW.first_byte_timeout_ms
          AND claim.idle_timeout_ms = NEW.idle_timeout_ms
          AND claim.execution_timeout_ms = NEW.execution_timeout_ms
          AND request.service_id = NEW.service_id
          AND request.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND snapshot.provider_model_route_id = NEW.provider_model_route_id
          AND snapshot.route_generation = NEW.route_generation
          AND snapshot.provider_instance_id = NEW.provider_instance_id
          AND snapshot.provider_instance_generation = NEW.provider_instance_generation
          AND snapshot.credential_id = NEW.credential_id
          AND snapshot.credential_generation = NEW.credential_generation
          AND snapshot.price_version_id = NEW.price_version_id
          AND credential.state = 'active'
          AND credential.generation = snapshot.credential_generation
          AND credential.current_revision = snapshot.credential_revision_id
          AND request.state = 'running'
          AND NOT request.partial_output AND NOT request.committed_effect
          AND (
              (request.assignment_id IS NOT NULL
               AND request.exact_route_id IS NULL
               AND request.configuration_revision_id = NEW.assignment_revision_id
               AND EXISTS (
                   SELECT 1 FROM router.assignment_candidates AS candidate
                   WHERE candidate.assignment_id = request.assignment_id
                     AND candidate.configuration_revision_id = NEW.assignment_revision_id
                     AND candidate.ordinal = NEW.candidate_ordinal
                     AND candidate.provider_model_route_id = NEW.provider_model_route_id
               ))
              OR
              (request.assignment_id IS NULL
               AND request.exact_route_id = NEW.provider_model_route_id
               AND request.configuration_revision_id = NEW.assignment_revision_id
               AND NEW.candidate_ordinal = 1)
          )
    ) THEN
        RAISE EXCEPTION 'provider attempt does not match its durable route start'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_attempts_routing_start_guard
BEFORE INSERT ON router.provider_attempts
FOR EACH ROW EXECUTE FUNCTION router.validate_provider_attempt_start();

CREATE TRIGGER provider_attempts_sequence
BEFORE INSERT ON router.provider_attempts
FOR EACH ROW EXECUTE FUNCTION router.check_provider_attempt_sequence();

CREATE FUNCTION router.validate_routing_candidate_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_scope_id text;
BEGIN
    PERFORM 1 FROM router.logical_requests
    WHERE row_id = NEW.request_row_id FOR UPDATE;
    IF NEW.decision_sequence <> 1 + COALESCE((
        SELECT max(decision_sequence)
        FROM router.routing_candidate_decisions
        WHERE request_row_id = NEW.request_row_id
    ), 0) THEN
        RAISE EXCEPTION 'routing decisions must use the next sequence number'
            USING ERRCODE = '40001';
    END IF;
    SELECT CASE NEW.affected_scope
        WHEN 'attempt' THEN NEW.attempt_id::text
        WHEN 'provider_model_route' THEN snapshot.provider_model_route_id::text
        WHEN 'provider_instance' THEN snapshot.provider_instance_id::text
        WHEN 'credential' THEN snapshot.credential_id::text
        WHEN 'assignment_candidate' THEN
            COALESCE(request.assignment_id::text, 'exact:' || request.exact_route_id::text)
            || ':' || NEW.candidate_ordinal::text
        WHEN 'logical_request' THEN request.request_id::text
        ELSE NULL
    END
    INTO expected_scope_id
    FROM router.logical_requests AS request
    JOIN router.provider_route_execution_snapshots AS snapshot
      ON snapshot.id = NEW.route_snapshot_id
    WHERE request.row_id = NEW.request_row_id;

    IF NEW.attempt_state <> 'succeeded'
       AND NEW.affected_scope_id IS DISTINCT FROM expected_scope_id THEN
        RAISE EXCEPTION 'routing decision affected identity is not canonical'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.affected_scope = 'logical_request'
       AND NEW.fallback_decision = 'next_candidate' THEN
        RAISE EXCEPTION 'request-wide failures cannot use candidate fallback'
            USING ERRCODE = '23514';
    END IF;
    IF (NEW.attempt_state = 'cancelled' OR NEW.normalized_error_class = 'cancelled')
       AND NEW.fallback_decision <> 'cancelled' THEN
        RAISE EXCEPTION 'cancelled work must stop as cancelled'
            USING ERRCODE = '23514';
    END IF;
    IF (NEW.attempt_state = 'uncertain' OR NEW.normalized_error_class = 'uncertain_effect')
       AND NEW.fallback_decision <> 'commit_boundary' THEN
        RAISE EXCEPTION 'uncertain work must stop at the commit boundary'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.logical_requests AS request
        WHERE request.row_id = NEW.request_row_id
          AND (request.partial_output OR request.committed_effect)
    ) AND (
        NEW.attempt_state NOT IN ('interrupted', 'cancelled', 'uncertain')
        OR NEW.fallback_decision NOT IN ('commit_boundary', 'cancelled')
    ) THEN
        RAISE EXCEPTION 'a committed request must stop at its commit boundary'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM router.provider_attempts AS attempt
        WHERE attempt.id = NEW.attempt_id
    ) THEN
        IF NOT EXISTS (
            SELECT 1 FROM router.provider_attempts AS attempt
            LEFT JOIN router.routing_attempt_starts AS attempt_start
              ON attempt_start.attempt_id = attempt.id
            LEFT JOIN router.routing_attempt_claims AS claim
              ON claim.attempt_id = attempt.id
             AND claim.claim_id = attempt_start.claim_id
            WHERE attempt.id = NEW.attempt_id
              AND attempt.request_row_id = NEW.request_row_id
              AND attempt.migration_0015_backfilled = NEW.migration_0015_backfilled
              AND attempt.attempt_number = NEW.attempt_number
              AND attempt.candidate_ordinal = NEW.candidate_ordinal
              AND attempt.route_snapshot_id = NEW.route_snapshot_id
              AND (
                  NEW.migration_0015_backfilled OR (
                      claim.claim_id = NEW.claim_id
                      AND claim.claim_generation = NEW.claim_generation
                      AND attempt_start.claim_generation <= NEW.claim_generation
                      AND attempt.connect_timeout_ms = NEW.connect_timeout_ms
                      AND attempt.first_byte_timeout_ms = NEW.first_byte_timeout_ms
                      AND attempt.idle_timeout_ms = NEW.idle_timeout_ms
                      AND attempt.execution_timeout_ms = NEW.execution_timeout_ms
                      AND attempt.logical_deadline = NEW.logical_deadline
                      AND attempt.attempt_deadline = NEW.attempt_deadline
                  )
              )
              AND attempt.state::text = NEW.attempt_state
              AND attempt.normalized_error_class IS NOT DISTINCT FROM NEW.normalized_error_class
              AND attempt.affected_scope IS NOT DISTINCT FROM NEW.affected_scope
              AND attempt.affected_scope_id IS NOT DISTINCT FROM NEW.affected_scope_id
              AND attempt.retry_decision IS NOT DISTINCT FROM NEW.fallback_decision
              AND attempt.safe_provider_code IS NOT DISTINCT FROM NEW.safe_provider_code
              AND attempt.redacted_evidence IS NOT DISTINCT FROM NEW.redacted_evidence
              AND attempt.finished_at = NEW.occurred_at
        ) THEN
            RAISE EXCEPTION 'routing decision does not match its provider attempt'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM router.routing_attempt_claims AS claim
        WHERE claim.request_row_id = NEW.request_row_id
          AND claim.attempt_id = NEW.attempt_id
          AND claim.claim_id = NEW.claim_id
          AND claim.claim_generation = NEW.claim_generation
          AND claim.attempt_number = NEW.attempt_number
          AND claim.candidate_ordinal = NEW.candidate_ordinal
          AND claim.route_snapshot_id = NEW.route_snapshot_id
          AND claim.connect_timeout_ms = NEW.connect_timeout_ms
          AND claim.first_byte_timeout_ms = NEW.first_byte_timeout_ms
          AND claim.idle_timeout_ms = NEW.idle_timeout_ms
          AND claim.execution_timeout_ms = NEW.execution_timeout_ms
          AND claim.logical_deadline = NEW.logical_deadline
          AND claim.attempt_deadline = NEW.attempt_deadline
          AND NEW.attempt_state <> 'succeeded'
    ) THEN
        RAISE EXCEPTION 'pre-attempt routing decision does not match its claim'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER routing_candidate_decisions_guard
BEFORE INSERT ON router.routing_candidate_decisions
FOR EACH ROW EXECUTE FUNCTION router.validate_routing_candidate_decision();

CREATE FUNCTION router.require_terminal_attempt_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state <> 'started' AND NOT NEW.migration_0015_backfilled AND NOT EXISTS (
        SELECT 1 FROM router.routing_candidate_decisions AS decision
        WHERE decision.request_row_id = NEW.request_row_id
          AND decision.attempt_id = NEW.id
          AND decision.attempt_state = NEW.state::text
    ) THEN
        RAISE EXCEPTION 'terminal provider attempt requires one routing decision'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER provider_attempts_terminal_decision
AFTER INSERT OR UPDATE OF state ON router.provider_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION router.require_terminal_attempt_decision();
