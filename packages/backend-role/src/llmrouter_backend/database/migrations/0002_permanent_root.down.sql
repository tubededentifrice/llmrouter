-- Relax the invariant without deleting or reversing normalized data.
SELECT pg_advisory_xact_lock(4993044345823);
LOCK TABLE router.services IN ACCESS EXCLUSIVE MODE;
DROP TRIGGER services_protect_root_truncate ON router.services;
DROP TRIGGER services_protect_root ON router.services;
DROP FUNCTION router.protect_root_service();
ALTER TABLE router.services DROP CONSTRAINT services_root_parent;
DROP FUNCTION router.validate_service_tree();
