ALTER TABLE router.provider_models
    DROP COLUMN default_reasoning_level,
    DROP COLUMN reasoning_strategy;

ALTER TABLE router.canonical_models
    DROP COLUMN default_reasoning_level,
    DROP COLUMN reasoning_strategy;
