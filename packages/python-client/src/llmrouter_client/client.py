"""Define the placeholder Python client."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Client:
    """Store the configured Router endpoint until transport work starts."""

    endpoint: str
