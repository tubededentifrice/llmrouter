"""Bounded zeroizable cache for route-scoped credential delivery."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from threading import RLock
from typing import TYPE_CHECKING
from weakref import WeakSet

from llmrouter_backend.credential_store.model import SecretLease

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

MAXIMUM_CACHE_ENTRIES = 1_024
MAXIMUM_CACHE_LIFETIME = timedelta(minutes=5)
MAXIMUM_OUTSTANDING_LEASES = 4_096


@dataclass(slots=True)
class _CacheEntry:
    credential_id: str
    generation: int
    expires_at: datetime
    value: bytearray

    def erase(self) -> None:
        """Erase the mutable cached buffer."""
        self.value[:] = bytes(len(self.value))


class BoundedCredentialCache:
    """Keep no more than a fixed number of short-lived decrypted values."""

    def __init__(
        self,
        *,
        maximum_entries: int,
        lifetime: timedelta,
        maximum_outstanding_leases: int = MAXIMUM_OUTSTANDING_LEASES,
    ) -> None:
        """Require a positive cache bound and no more than five minutes."""
        if not 1 <= maximum_entries <= MAXIMUM_CACHE_ENTRIES:
            msg = "The credential cache must contain from 1 to 1024 entries."
            raise ValueError(msg)
        if not timedelta(0) < lifetime <= MAXIMUM_CACHE_LIFETIME:
            msg = "The credential cache lifetime must be from zero to five minutes."
            raise ValueError(msg)
        if not 1 <= maximum_outstanding_leases <= MAXIMUM_OUTSTANDING_LEASES:
            msg = "The credential cache must have from 1 to 4096 active leases."
            raise ValueError(msg)
        self._maximum_entries = maximum_entries
        self._lifetime = lifetime
        self._maximum_outstanding_leases = maximum_outstanding_leases
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._leases: dict[str, WeakSet[SecretLease]] = {}
        self._lock = RLock()

    @property
    def entry_count(self) -> int:
        """Return the current safe entry count."""
        with self._lock:
            return len(self._entries)

    def acquire(
        self,
        route_id: str,
        *,
        now: datetime,
        loader: Callable[[], tuple[str, int, bytearray]],
    ) -> SecretLease:
        """Return one copied bounded lease and keep one bounded cache entry."""
        with self._lock:
            self._expire(now)
            self._expire_leases(now)
            if self._active_lease_count() >= self._maximum_outstanding_leases:
                msg = "The credential lease capacity is not available."
                raise RuntimeError(msg)
            entry = self._entries.get(route_id)
            if entry is None:
                credential_id, generation, value = loader()
                entry = _CacheEntry(
                    credential_id,
                    generation,
                    now + self._lifetime,
                    value,
                )
                self._entries[route_id] = entry
                self._evict_to_bound()
            else:
                self._entries.move_to_end(route_id)
            lease = SecretLease(
                entry.credential_id,
                entry.generation,
                entry.expires_at,
                bytearray(entry.value),
            )
            self._leases.setdefault(route_id, WeakSet()).add(lease)
            return lease

    def invalidate(self, credential_id: str) -> None:
        """Erase all route values for one changed credential."""
        with self._lock:
            for route_id, entry in tuple(self._entries.items()):
                if entry.credential_id == credential_id:
                    entry.erase()
                    del self._entries[route_id]
            for route_id, leases in tuple(self._leases.items()):
                for lease in tuple(leases):
                    if lease.credential_id == credential_id:
                        lease.close()
                if not leases:
                    del self._leases[route_id]

    def retain_routes(self, active_route_ids: frozenset[str]) -> None:
        """Erase values for routes that are no longer active on this node."""
        with self._lock:
            for route_id, entry in tuple(self._entries.items()):
                if route_id not in active_route_ids:
                    entry.erase()
                    del self._entries[route_id]
            for route_id, leases in tuple(self._leases.items()):
                if route_id not in active_route_ids:
                    for lease in tuple(leases):
                        lease.close()
                    del self._leases[route_id]

    def close(self) -> None:
        """Erase every cached value."""
        with self._lock:
            for entry in self._entries.values():
                entry.erase()
            self._entries.clear()
            for leases in self._leases.values():
                for lease in tuple(leases):
                    lease.close()
            self._leases.clear()

    def _expire(self, now: datetime) -> None:
        for route_id, entry in tuple(self._entries.items()):
            if now >= entry.expires_at:
                entry.erase()
                del self._entries[route_id]

    def _expire_leases(self, now: datetime) -> None:
        for route_id, leases in tuple(self._leases.items()):
            for lease in tuple(leases):
                if lease.closed or now >= lease.expires_at:
                    lease.close()
                    leases.discard(lease)
            if not leases:
                del self._leases[route_id]

    def _active_lease_count(self) -> int:
        return sum(len(leases) for leases in self._leases.values())

    def _evict_to_bound(self) -> None:
        while len(self._entries) > self._maximum_entries:
            _route_id, entry = self._entries.popitem(last=False)
            entry.erase()
