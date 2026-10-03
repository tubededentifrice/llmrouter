ALTER TABLE router.canonical_models
    ADD COLUMN reasoning_strategy text NOT NULL DEFAULT 'auto'
        CHECK (reasoning_strategy IN ('auto', 'none', 'effort', 'nested_effort',
                                     'thinking_type', 'system_token', 'native')),
    ADD COLUMN default_reasoning_level text
        CHECK (default_reasoning_level IN ('none', 'low', 'medium', 'high'));

ALTER TABLE router.provider_models
    ADD COLUMN reasoning_strategy text
        CHECK (reasoning_strategy IN ('auto', 'none', 'effort', 'nested_effort',
                                     'thinking_type', 'system_token', 'native')),
    ADD COLUMN default_reasoning_level text
        CHECK (default_reasoning_level IN ('none', 'low', 'medium', 'high'));
