-- Keep service and assignment writes out of the complete normalization.
SELECT pg_advisory_xact_lock(4993044345823);
LOCK TABLE router.services, router.assignment_definitions,
    router.assignment_candidates IN ACCESS EXCLUSIVE MODE;
SELECT pg_advisory_xact_lock(4993044345822);

CREATE FUNCTION router.validate_service_tree() RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    IF (SELECT count(*) FROM router.services
        WHERE api_name = 'root' AND parent_service_id IS NULL) <> 1
       OR EXISTS (
           SELECT 1 FROM router.services AS child
           LEFT JOIN router.services AS parent ON parent.id = child.parent_service_id
           WHERE child.api_name <> 'root'
             AND (child.parent_service_id IS NULL OR parent.id IS NULL)
       ) OR EXISTS (
           WITH RECURSIVE chain(id, parent_service_id, path, cycle) AS (
               SELECT id, parent_service_id, ARRAY[id], false FROM router.services
             UNION ALL
               SELECT parent.id, parent.parent_service_id,
                      chain.path || parent.id, parent.id = ANY(chain.path)
               FROM router.services AS parent
               JOIN chain ON parent.id = chain.parent_service_id
               WHERE NOT chain.cycle
           ) SELECT 1 FROM chain WHERE cycle
       ) THEN
        RAISE EXCEPTION 'The stored service tree is invalid.'
            USING ERRCODE = '23514', CONSTRAINT = 'services_tree';
    END IF;
END;
$$;

-- The old trigger walks an unbounded legacy chain. Validate the entire
-- normalized graph before this transaction enables it again.
ALTER TABLE router.services DISABLE TRIGGER services_reject_parent_cycle;
DO $$
DECLARE
    root_id uuid;
BEGIN
    SELECT id INTO root_id FROM router.services WHERE api_name = 'root';
    IF root_id IS NULL THEN
        IF EXISTS (SELECT 1 FROM router.services)
           AND NOT EXISTS (SELECT 1 FROM router.services WHERE parent_service_id IS NULL)
        THEN
            RAISE EXCEPTION 'The stored service tree has no root.'
                USING ERRCODE = '23514', CONSTRAINT = 'services_tree';
        END IF;
        INSERT INTO router.services (api_name, display_name)
        VALUES ('root', 'Root') RETURNING id INTO root_id;
    ELSE
        UPDATE router.services SET parent_service_id = NULL
        WHERE id = root_id AND parent_service_id IS NOT NULL;
    END IF;
    UPDATE router.services SET parent_service_id = root_id
    WHERE id <> root_id AND parent_service_id IS NULL;
    PERFORM router.validate_service_tree();
END;
$$;
ALTER TABLE router.services ENABLE TRIGGER services_reject_parent_cycle;

ALTER TABLE router.services ADD CONSTRAINT services_root_parent
    CHECK ((api_name = 'root') = (parent_service_id IS NULL));

CREATE FUNCTION router.protect_root_service() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, router
AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'The root service cannot be deleted.'
            USING ERRCODE = '23514', CONSTRAINT = 'services_permanent_root';
    END IF;
    IF OLD.api_name = 'root' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'The root service cannot be deleted.'
                USING ERRCODE = '23514', CONSTRAINT = 'services_permanent_root';
        END IF;
        IF NEW.api_name IS DISTINCT FROM OLD.api_name
           OR NEW.id IS DISTINCT FROM OLD.id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.parent_service_id IS NOT NULL THEN
            RAISE EXCEPTION 'The root service identity and parent must not change.'
                USING ERRCODE = '23514', CONSTRAINT = 'services_permanent_root';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER services_protect_root
BEFORE UPDATE OR DELETE ON router.services
FOR EACH ROW EXECUTE FUNCTION router.protect_root_service();
CREATE TRIGGER services_protect_root_truncate
BEFORE TRUNCATE ON router.services
FOR EACH STATEMENT EXECUTE FUNCTION router.protect_root_service();
