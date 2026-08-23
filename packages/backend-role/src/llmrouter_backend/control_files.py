"""Safe reads for bounded deployment control files."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class ControlFileError(Exception):
    """Hide the reason that a deployment control file is not usable."""


def read_control_file(path: Path, *, maximum: int) -> bytes:
    """Read one bounded regular file without following links."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ControlFileError
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as error:
        raise ControlFileError from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not 1 <= len(raw) <= maximum:
        raise ControlFileError
    return raw
