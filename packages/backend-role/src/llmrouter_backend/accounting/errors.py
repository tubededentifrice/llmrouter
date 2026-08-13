"""Safe accounting errors."""

from __future__ import annotations


class AccountingError(RuntimeError):
    """A safe accounting, pricing, or authority failure."""
