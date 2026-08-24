"""Bound all PostgreSQL connections opened by one application process."""
# ruff: noqa: ANN401, EM101, TRY003

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Literal, Self

import psycopg

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from llmrouter_backend.metrics import MetricsRegistry


class DatabaseConnectionLimitError(RuntimeError):
    """Report that no configured database connection slot is available."""


class DatabaseConnections:
    """Open no more than one configured number of PostgreSQL connections."""

    def __init__(self, limit: int, metrics: MetricsRegistry | None = None) -> None:
        """Create one process-local connection gate."""
        if type(limit) is not int or limit < 1:
            raise ValueError("The database connection limit is invalid.")
        self._available = threading.BoundedSemaphore(limit)
        self._metrics = metrics
        if metrics is not None:
            metrics.set_database_saturation(0, limit)

    def connect(self, conninfo: str, **kwargs: Any) -> psycopg.Connection[Any]:
        """Open one connection or reject new work without an in-memory wait."""
        return self._open(conninfo, timeout=0.0, **kwargs)

    def waiting_connect(self, conninfo: str, **kwargs: Any) -> psycopg.Connection[Any]:
        """Give already-admitted background or finalization work a short wait."""
        raw_timeout = kwargs.get("connect_timeout", 2)
        timeout = float(raw_timeout) if isinstance(raw_timeout, int | float) else 2.0
        return self._open(conninfo, timeout=max(0.0, timeout), **kwargs)

    def _open(
        self, conninfo: str, *, timeout: float, **kwargs: Any
    ) -> psycopg.Connection[Any]:
        acquired = self._available.acquire(timeout=timeout)
        if not acquired:
            if self._metrics is not None:
                self._metrics.reject_database_connection()
            raise DatabaseConnectionLimitError
        if self._metrics is not None:
            self._metrics.open_database_connection()
        try:
            connection = psycopg.connect(conninfo, **kwargs)
        except BaseException:
            self._release()
            raise
        return _ManagedConnection(connection, self._release)  # type: ignore[return-value]

    def _release(self) -> None:
        if self._metrics is not None:
            self._metrics.close_database_connection()
        self._available.release()


class _ManagedConnection:
    """Release one gate slot exactly once when its connection closes."""

    def __init__(
        self,
        connection: psycopg.Connection[Any],
        release: Callable[[], None],
    ) -> None:
        self._connection = connection
        self._access: Any = connection
        self._release = release
        self._release_lock = threading.Lock()
        self._released = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._access, name)

    def __enter__(self) -> Self:
        self._access = self._connection.__enter__()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            self._connection.__exit__(exception_type, exception, traceback)
            return False
        finally:
            self._release_once()

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            self._release_once()

    def _release_once(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            self._release()
