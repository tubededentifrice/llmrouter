ALTER TABLE router.service_lifecycle_operations
DROP CONSTRAINT service_lifecycle_operations_action_check;

ALTER TABLE router.service_lifecycle_operations
ADD CONSTRAINT service_lifecycle_operations_action_check
CHECK (action IN (
    'service.create', 'service.parent', 'service.disable',
    'service.restore', 'service.retire'
));
