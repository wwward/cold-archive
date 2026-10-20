"""One writer at a time per archive.

Two Protect or Repair runs on the same folder at once would delete and
rebuild each other's parity sets. An advisory ``flock`` on a small lock file
in the archive root prevents that; the kernel releases it when the process
dies, so a crash never leaves a stale lock. Readers (Check) take a shared
lock so they never scan a set that is being rebuilt underneath them.

The lock file is one of the junk names the scanner ignores, so it is never
archived or listed.
"""
from __future__ import annotations

import os
from typing import Optional

from .layout import LOCK_FILE

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]


class ArchiveBusy(Exception):
    pass


class ArchiveLock:
    def __init__(self, root: str, exclusive: bool = True) -> None:
        self.path = os.path.join(root, LOCK_FILE)
        self.exclusive = exclusive
        self._fd: Optional[int] = None
        self.warning: Optional[str] = None

    def __enter__(self) -> "ArchiveLock":
        if fcntl is None:
            self.warning = "file locking is not available on this system; do not run two jobs on the same archive at once"
            return self
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            # read-only medium (a burned disc): checking is still fine, writing would fail later anyway
            self.warning = f"could not create the lock file ({e}); do not run two jobs on the same archive at once"
            return self
        flags = (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        try:
            fcntl.flock(self._fd, flags)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise ArchiveBusy("another Cold Archive job is already working on this folder; wait for it to finish")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
            if self.exclusive:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
