"""One writer at a time per archive, and no writer while a check runs.

Two Protect or Repair runs on the same folder at once would delete and
rebuild each other's parity sets, and a Check that scans a set being rebuilt
reports damage that is not there. Advisory ``flock`` locks prevent both; the
kernel releases them when the process dies, so a crash never leaves a stale
lock.

The lock is taken on the archive's top folder itself (shared for Check,
exclusive for Protect and Repair). That needs no file: a Check must change
nothing on the drive it checks. MEASURED 2026-10-19 on a real archive: until
0.3.0 a Check created an empty ``.cold-archive.lock`` in the top folder to
hold its shared lock, and never removed it, which changed the folder's date.

Writers also lock ``.cold-archive.lock`` (created for the purpose and removed
afterwards), because releases before 0.3.0 only know that file: an older
version running at the same moment is still kept out. A reader takes that
file's lock too, but only if a writer has already made the file. The file is
one of the junk names the scanner ignores, so it is never archived or listed.
"""
from __future__ import annotations

import os
from typing import Optional

from .layout import LOCK_FILE

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

BUSY = "another Cold Archive job is already working on this folder; wait for it to finish"


class ArchiveBusy(Exception):
    pass


class ArchiveLock:
    def __init__(self, root: str, exclusive: bool = True) -> None:
        self.root = root
        self.path = os.path.join(root, LOCK_FILE)
        self.exclusive = exclusive
        self._dir_fd: Optional[int] = None
        self._fd: Optional[int] = None
        self.warning: Optional[str] = None

    def _flock(self, fd: int) -> bool:
        """True when locked; ArchiveBusy when someone else holds it; False
        when this filesystem does not support locks (the job goes ahead)."""
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            raise ArchiveBusy(BUSY) from None
        except OSError:
            return False

    def __enter__(self) -> "ArchiveLock":
        if fcntl is None:
            self.warning = "file locking is not available on this system; do not run two jobs on the same archive at once"
            return self
        try:
            # 1. the archive folder itself: nothing is created or changed
            try:
                self._dir_fd = os.open(self.root, os.O_RDONLY)
                if not self._flock(self._dir_fd):
                    os.close(self._dir_fd)
                    self._dir_fd = None
            except OSError:
                self._dir_fd = None
            # 2. the lock file older releases use: writers create it, readers only join it
            try:
                if self.exclusive:
                    self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
                else:
                    self._fd = os.open(self.path, os.O_RDONLY)
            except FileNotFoundError:
                self._fd = None                     # no writer has made it: nothing to join
            except OSError as e:
                self._fd = None
                if self.exclusive:
                    # read-only medium (a burned disc): writing would fail later anyway
                    self.warning = f"could not create the lock file ({e}); do not run two jobs on the same archive at once"
            if self._fd is not None and not self._flock(self._fd):
                os.close(self._fd)
                self._fd = None
            if self._dir_fd is None and self._fd is None and self.warning is None:
                self.warning = "this drive does not support locking; do not run two jobs on the same archive at once"
        except ArchiveBusy:
            self._release(remove=False)
            raise
        return self

    def _release(self, remove: bool) -> None:
        for name in ("_fd", "_dir_fd"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
                setattr(self, name, None)
        if remove:
            try:
                os.remove(self.path)
            except OSError:
                pass

    def __exit__(self, *exc: object) -> None:
        if fcntl is not None:
            self._release(remove=self.exclusive)
