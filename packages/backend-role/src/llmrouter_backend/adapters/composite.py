"""Compose operation-specific adapters behind one registered provider kind."""
# ruff: noqa: D107, EM101, TRY003

from __future__ import annotations

from typing import TYPE_CHECKING

from llmrouter_backend.calls import (
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderFailureError,
    ProviderOperation,
    ProviderOutput,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from llmrouter_backend.calls import ProviderAdapter


class CompositeProviderAdapter:
    """Dispatch one call kind to its closed provider protocol adapter."""

    def __init__(self, adapters: Mapping[str, ProviderAdapter]) -> None:
        self._adapters = dict(adapters)
        if not self._adapters or set(self._adapters) - {"model", "embedding", "media"}:
            raise ValueError("The provider adapter operation map is invalid.")
        self.usage_units = frozenset(
            unit for adapter in self._adapters.values() for unit in adapter.usage_units
        )

    def usage_units_for(self, operation: ProviderOperation, /) -> frozenset[str]:
        """Declare units from only the selected operation adapter."""
        adapter = self._adapters.get(operation.kind)
        return (
            adapter.usage_units_for(operation) if adapter is not None else frozenset()
        )

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        """Run only one selected operation adapter without a hidden fallback."""
        adapter = self._adapters.get(request.kind)
        if adapter is None:
            raise ProviderFailureError("incompatible")
        async for event in adapter.attempt(request):
            yield event
