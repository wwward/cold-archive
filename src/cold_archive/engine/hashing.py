"""SHA-256 hashing of files, sequential and parallel, with progress and cancel.

Threads rather than processes: ``hashlib`` releases the GIL for buffers larger
than 2 KiB, so threads reach full disk speed, and threads work inside a
PyInstaller bundle without the ``multiprocessing`` bootstrap traps.
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Iterable, NamedTuple, Optional, Tuple, Union

from .observer import Cancelled, NullObserver, Observer

CHUNK = 1 << 20  # 1 MiB reads: 30 % faster than 4 KiB here, far fewer syscalls on USB drives


class Digests(NamedTuple):
    sha256: str
    md5: str   # par2's own per-file identity (FileDesc packet); "" when not computed


def set_nocache(fd: int) -> bool:
    """Ask the kernel not to serve or keep this descriptor's data in the page
    cache (macOS F_NOCACHE). Used for read-back after a write, so the bytes
    come from the medium and not from the copy that was just written into
    RAM. Best effort; returns whether it took."""
    try:
        import fcntl
        flag = getattr(fcntl, "F_NOCACHE", None)
        if flag is None:
            return False
        fcntl.fcntl(fd, flag, 1)
        return True
    except (OSError, ImportError, ValueError):
        return False


def digest_file(path: str, *, chunk: int = CHUNK, observer: Optional[Observer] = None,
                on_bytes: Optional[Callable[[int], None]] = None, with_md5: bool = True,
                nocache: bool = False) -> Digests:
    """Hash one file in a single pass. ``on_bytes`` receives byte counts as read.
    ``nocache`` reads past the page cache where the OS allows it."""
    h = hashlib.sha256()
    m = hashlib.md5() if with_md5 else None
    with open(path, "rb", buffering=0) as f:
        if nocache:
            set_nocache(f.fileno())
        while True:
            if observer is not None:
                observer.check_cancel()
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            if m is not None:
                m.update(b)
            if on_bytes is not None:
                on_bytes(len(b))
    return Digests(h.hexdigest(), m.hexdigest() if m is not None else "")


def sha256_file(path: str, *, chunk: int = CHUNK, observer: Optional[Observer] = None,
                on_bytes: Optional[Callable[[int], None]] = None, nocache: bool = False) -> str:
    return digest_file(path, chunk=chunk, observer=observer, on_bytes=on_bytes, with_md5=False, nocache=nocache).sha256


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


HashOutcome = Union[Digests, BaseException]


def hash_many(items: Iterable[Tuple[str, str, int]], *, workers: int = 4,
              observer: Optional[Observer] = None, with_md5: bool = True) -> Dict[str, HashOutcome]:
    """Hash many files concurrently.

    ``items`` yields ``(key, abspath, size)``. Returns ``{key: Digests}`` or
    ``{key: exception}`` for files that could not be read. Progress is reported
    in bytes against the sum of sizes. Raises :class:`Cancelled` promptly.
    """
    obs = observer or NullObserver()
    items = list(items)
    total = sum(max(0, s) for _, _, s in items)
    done = 0
    results: Dict[str, HashOutcome] = {}
    workers = max(1, workers)

    def work(key: str, path: str) -> Tuple[str, HashOutcome]:
        try:
            return key, digest_file(path, observer=obs, with_md5=with_md5)
        except Cancelled:
            raise
        except BaseException as e:  # noqa: BLE001 - every failure must be attributed, never dropped
            return key, e

    obs.progress(0, total)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sha256") as pool:
        futures = {pool.submit(work, key, path): (key, size) for key, path, size in items}
        try:
            for fut in as_completed(futures):
                key, size = futures[fut]
                k, outcome = fut.result()
                results[k] = outcome
                done += max(0, size)
                obs.progress(done, total, os.path.basename(key))
                obs.check_cancel()
        except Cancelled:
            for f in futures:
                f.cancel()
            raise
    return results
