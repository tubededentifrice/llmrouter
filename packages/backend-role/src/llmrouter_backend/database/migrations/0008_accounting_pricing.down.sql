DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM router.accounting_facts)
       OR EXISTS (SELECT 1 FROM router.price_synchronization_runs)
       OR EXISTS (SELECT 1 FROM router.price_synchronization_idempotency)
       OR EXISTS (SELECT 1 FROM router.price_publication_outbox)
       OR EXISTS (SELECT 1 FROM router.price_synchronization_publications)
       OR EXISTS (
           SELECT 1 FROM router.route_price_synchronization_states
           WHERE NOT migration_0008_backfilled
       )
       OR EXISTS (
           SELECT 1 FROM router.configuration_price_bindings
           WHERE NOT migration_0008_backfilled
       )
       OR EXISTS (SELECT 1 FROM router.external_tool_attempt_identities)
       OR EXISTS (SELECT 1 FROM router.business_tool_call_identities)
       OR EXISTS (
           SELECT 1 FROM router.route_price_sources
           WHERE source_name IS NULL OR lookup_identifier IS NULL
       )
       OR EXISTS (
           SELECT 1
           FROM router.price_source_snapshots
           GROUP BY source_name, content_sha256
           HAVING count(*) > 1
       )
       OR EXISTS (SELECT 1 FROM router.daily_accounting_aggregates) THEN
        RAISE EXCEPTION 'accounting pricing migration cannot roll back without data loss'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

DROP TRIGGER daily_accounting_aggregates_write_guard ON router.daily_accounting_aggregates;
DROP TABLE router.daily_accounting_aggregates;
DROP TRIGGER price_synchronization_results_append_only ON router.price_synchronization_results;
DROP TABLE router.price_synchronization_results;
DROP TRIGGER price_synchronization_publications_append_only ON router.price_synchronization_publications;
DROP TABLE router.price_synchronization_publications;
DROP TABLE router.price_publication_outbox;
DROP TABLE router.price_synchronization_idempotency;
DROP TRIGGER price_synchronization_runs_append_only ON router.price_synchronization_runs;
DROP TABLE router.price_synchronization_runs;
DROP FUNCTION router.protect_price_synchronization_run();
DROP TABLE router.route_price_synchronization_states;
DROP TRIGGER configuration_price_bindings_append_only ON router.configuration_price_bindings;
DROP TABLE router.configuration_price_bindings;
ALTER TABLE router.route_price_sources
ALTER COLUMN synchronization_schedule DROP DEFAULT,
ALTER COLUMN synchronization_schedule DROP NOT NULL;
UPDATE router.route_price_sources
SET synchronization_schedule = NULL
WHERE migration_0008_schedule_was_null;
ALTER TABLE router.route_price_sources
DROP COLUMN migration_0008_schedule_was_null;
ALTER TABLE router.route_price_sources
DROP CONSTRAINT route_price_sources_authority_values,
ALTER COLUMN source_name SET NOT NULL,
ALTER COLUMN lookup_identifier SET NOT NULL,
ADD CHECK (source_name <> ''),
ADD CHECK (lookup_identifier <> '');
ALTER TABLE router.price_source_snapshots
DROP CONSTRAINT price_source_snapshots_source_name_bound,
DROP CONSTRAINT price_source_snapshots_source_revision_bound,
DROP CONSTRAINT price_source_snapshots_http_validator_bound,
DROP COLUMN source_available,
ADD UNIQUE (source_name, content_sha256);
DROP TRIGGER accounting_correction_usage_append_only ON router.accounting_correction_usage;
DROP TABLE router.accounting_correction_usage;
DROP TRIGGER accounting_corrections_append_only ON router.accounting_corrections;
DROP TRIGGER accounting_corrections_currency ON router.accounting_corrections;
DROP TABLE router.accounting_corrections;
DROP FUNCTION router.check_correction_currency();
DROP TRIGGER accounting_usage_components_append_only ON router.accounting_usage_components;
DROP TABLE router.accounting_usage_components;
DROP TRIGGER accounting_facts_append_only ON router.accounting_facts;
DROP TRIGGER accounting_facts_canonical_event ON router.accounting_facts;
DROP TRIGGER accounting_facts_budget_scope ON router.accounting_facts;
DROP TRIGGER accounting_facts_budget_ledger_link ON router.accounting_facts;
DROP TRIGGER accounting_facts_subject ON router.accounting_facts;
DROP TABLE router.accounting_facts;
DROP FUNCTION router.check_accounting_canonical_event();
DROP FUNCTION router.check_accounting_budget_scope();
DROP FUNCTION router.check_accounting_budget_ledger_link();
DROP FUNCTION router.check_accounting_subject();
DROP TRIGGER business_tool_call_identities_append_only ON router.business_tool_call_identities;
DROP TABLE router.business_tool_call_identities;
DROP TRIGGER external_tool_attempt_identities_append_only ON router.external_tool_attempt_identities;
DROP TABLE router.external_tool_attempt_identities;
