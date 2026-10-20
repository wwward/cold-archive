"""Verified copy of a directory tree onto the archive medium.

Every file is written to a temporary name, flushed and fsync'ed, renamed into
place, and then read back and hashed. A file only counts as copied when the
read-back hash equals the hash computed while reading the source. On macOS
the write and the read-back bypass the page cache (F_NOCACHE), so the bytes
compared come from the medium rather than from the copy the kernel still
holds in RAM; that is what catches a flaky cable or a USB bridge that
acknowledged data it did not store. (A drive's own write cache is asked to
flush once at the end, F_FULLFSYNC.) Nothing can catch a medium that returns
the right bytes today and wrong ones next year: that is what Check is for.
The hashes are returned so the following protect step does not have to read
the files a third time.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .hashing import CHUNK, Digests, digest_file, set_nocache, sha256_file
from .layout import MANIFEST_FILE, TMP_SUFFIX, human_bytes, remove_any_spelling
from .observer import Cancelled, NullObserver, Observer
from .preflight import destination_folds_names, name_collisions
from .scan import scan_archive

# A file being written gets a short hidden name in its own folder (covered by
# layout.is_junk), so a long source name never pushes the temporary past the
# filesystem's name limit, and an interrupted copy never leaves a
# plausible-looking partial file behind.
PARTIAL_PREFIX = ".cold-archive-partial-"


def partial_name(name: str) -> str:
    return PARTIAL_PREFIX + hashlib.sha1(name.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _is_partial(name: str) -> bool:
    return name.startswith(PARTIAL_PREFIX) or name.endswith(TMP_SUFFIX)


def _full_fsync(directory: str) -> None:
    """Ask the drive itself to commit its write cache (macOS F_FULLFSYNC);
    plain fsync only reaches the drive's cache. Best effort."""
    try:
        import fcntl
        flag = getattr(fcntl, "F_FULLFSYNC", None)
        if flag is None:
            return
        fd = os.open(directory, os.O_RDONLY)
        try:
            fcntl.fcntl(fd, flag)
        finally:
            os.close(fd)
    except (OSError, ImportError, ValueError):
        pass


@dataclass
class CopyOptions:
    verify_after_write: bool = True
    fsync: bool = True
    skip_identical: bool = True
    include_archive_files: bool = False


@dataclass
class CopyResult:
    source: str
    target: str
    ok: bool = False
    files_total: int = 0
    bytes_total: int = 0
    files_copied: int = 0
    bytes_copied: int = 0
    files_skipped_identical: int = 0
    failures: List[Tuple[str, str]] = field(default_factory=list)
    symlinks_skipped: List[str] = field(default_factory=list)
    junk_skipped: int = 0
    partials_removed: int = 0       # leftovers of an interrupted copy found on the destination and removed
    hashes: Dict[str, Digests] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    def summary(self) -> str:
        lines = [("COPIED: " if self.ok else "COPY INCOMPLETE: ") + f"{self.source} -> {self.target}",
                 f"  {self.files_copied} files copied ({human_bytes(self.bytes_copied)}), "
                 f"{self.files_skipped_identical} already identical, {len(self.failures)} failed"]
        for rel, err in self.failures[:20]:
            lines.append(f"    FAILED {rel}: {err}")
        if self.symlinks_skipped:
            lines.append(f"  {len(self.symlinks_skipped)} symbolic links skipped")
        for w in self.warnings:
            lines.append("  warning: " + w)
        lines.append(f"  took {self.duration_s:.0f} s")
        return "\n".join(lines)


def _clear_immutable(path: str) -> None:
    """copystat carries the Finder 'Locked' flag (uchg) across; a locked file
    cannot be rebuilt by par2 later, so the archive copy must not be locked."""
    chflags = getattr(os, "chflags", None)
    if chflags is None:
        return
    try:
        import stat as _stat
        st = os.lstat(path)
        bad = getattr(_stat, "UF_IMMUTABLE", 0) | getattr(_stat, "SF_IMMUTABLE", 0) | getattr(_stat, "UF_APPEND", 0) | getattr(_stat, "SF_APPEND", 0)
        flags = getattr(st, "st_flags", 0)
        if flags & bad:
            chflags(path, flags & ~bad)
    except OSError:
        pass


def check_nesting(source: str, target: str) -> None:
    s = os.path.realpath(source)
    t = os.path.realpath(target)
    if s == t:
        raise ValueError("source and destination are the same folder")
    if t.startswith(s + os.sep):
        raise ValueError("destination is inside the source folder")
    if s.startswith(t + os.sep):
        raise ValueError("source is inside the destination folder")


def copy_tree(source: str, target: str, options: Optional[CopyOptions] = None,
              observer: Optional[Observer] = None) -> CopyResult:
    opts = options or CopyOptions()
    obs = observer or NullObserver()
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    t0 = time.time()
    res = CopyResult(source=source, target=target)
    if not os.path.isdir(source):
        raise NotADirectoryError(source)
    os.makedirs(target, exist_ok=True)
    check_nesting(source, target)

    scan = scan_archive(source, obs, include_reserved=opts.include_archive_files)
    res.files_total = len(scan.files)
    res.bytes_total = scan.total_bytes
    res.symlinks_skipped = list(scan.symlinks)
    res.junk_skipped = len(scan.junk_skipped)
    for rel, err in scan.errors:
        res.failures.append((rel or "(top folder)", f"could not read: {err}"))
    if scan.symlinks:
        res.warnings.append(f"{len(scan.symlinks)} symbolic links were not copied")
    # Source files that carry one of the archive's own names (SHA256SUMS,
    # READ-ME-FIRST, PARITY*.par2 ...) are not payload the scanner sees. When
    # the source is itself an archive that is expected: fresh records and
    # parity are made on the destination. Otherwise the owner's file would
    # silently vanish from the copy, so it is a failure they must resolve.
    if scan.reserved:
        if os.path.isfile(os.path.join(source, MANIFEST_FILE)):
            res.warnings.append(f"the source is itself a Cold Archive: its {len(scan.reserved)} record and parity files are not copied; "
                                "fresh ones are made on the destination")
        else:
            for rel in scan.reserved:
                res.failures.append((rel, "this name is reserved for the archive's own files; rename it before archiving"))

    # Two source files that fold to one name on this destination (a.txt and
    # A.txt from a Linux disk onto APFS or exFAT) would silently become one
    # file; refuse the second and say so.
    colliding: Dict[str, str] = {}
    if destination_folds_names(target):
        for a, b in name_collisions(scan):
            colliding[b] = a

    # recreate the directory structure, including empty folders; a folder
    # that cannot be made fails every file under it, attributed, instead of
    # stopping the whole copy with a traceback
    bad_dirs: Set[str] = set()
    for sd in scan.dirs:
        if any(sd.rel == b or sd.rel.startswith(b + "/") for b in bad_dirs):
            continue
        dpath = os.path.join(target, os.path.relpath(sd.abspath, source)) if sd.rel else target
        try:
            os.makedirs(dpath, exist_ok=True)
        except OSError as e:
            bad_dirs.add(sd.rel)
            res.failures.append((sd.rel or "(top folder)", f"folder could not be created: {e}"))
            continue
        # leftovers of an interrupted copy are ours: remove them
        try:
            for n in os.listdir(dpath):
                if _is_partial(n):
                    os.remove(os.path.join(dpath, n))
                    res.partials_removed += 1
        except OSError:
            pass
    if res.partials_removed:
        res.warnings.append(f"{res.partials_removed} unfinished files from an interrupted copy were removed from the destination")

    passes = 2 if opts.verify_after_write else 1
    total_work = res.bytes_total * passes
    done = 0
    obs.phase("Copying files", total=total_work, unit="bytes")

    for f in scan.files:
        obs.check_cancel()
        if f.rel in colliding:
            res.failures.append((f.rel, f"would overwrite {colliding[f.rel]!r}: this destination does not distinguish the two names"))
            done += f.size * passes
            continue
        if bad_dirs and any(f.rel.startswith(b + "/") or b == "" for b in bad_dirs):
            res.failures.append((f.rel, "its folder could not be created"))
            done += f.size * passes
            continue
        rel_os = os.path.relpath(f.abspath, source)
        dst = os.path.join(target, rel_os)
        tmp = os.path.join(os.path.dirname(dst), partial_name(f.name))
        try:
            digest: Optional[Digests] = None
            if opts.skip_identical and os.path.isfile(dst) and os.path.getsize(dst) == f.size:
                src_digest = digest_file(f.abspath, observer=obs)
                dst_digest = digest_file(dst, observer=obs, nocache=True)
                done += f.size * passes
                obs.progress(done, total_work, f.name)
                if src_digest == dst_digest:
                    _clear_immutable(dst)     # an identical file counts as copied only unlocked (par2 must be able to rewrite it)
                    res.files_skipped_identical += 1
                    res.hashes[f.rel] = src_digest
                    continue
            if os.path.lexists(dst):
                _clear_immutable(dst)         # a locked destination file makes the rename below fail with a bare EPERM
            h = hashlib.sha256()
            m = hashlib.md5()
            with open(f.abspath, "rb", buffering=0) as src, open(tmp, "wb") as out:
                set_nocache(out.fileno())
                while True:
                    obs.check_cancel()
                    b = src.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
                    m.update(b)
                    out.write(b)
                    done += len(b)
                    obs.progress(done, total_work, f.name)
                out.flush()
                if opts.fsync:
                    os.fsync(out.fileno())
            digest = Digests(h.hexdigest(), m.hexdigest())
            os.replace(tmp, dst)
            try:
                shutil.copystat(f.abspath, dst, follow_symlinks=False)
            except OSError as e:
                # exFAT / FAT cannot hold every attribute; timestamps still land
                res.warnings.append(f"could not copy all attributes of {f.rel}: {e}") if len(res.warnings) < 5 else None
            _clear_immutable(dst)
            if opts.verify_after_write:
                def _tick(n: int) -> None:
                    nonlocal done
                    done += n
                    obs.progress(done, total_work, "verifying " + f.name)
                back = sha256_file(dst, observer=obs, on_bytes=_tick, nocache=True)
                if back != digest.sha256:
                    try:
                        remove_any_spelling(dst)   # exFAT may only unlink the spelling the file was created with
                    except OSError:
                        pass
                    res.failures.append((f.rel, "read-back after writing did not match; the destination may be faulty"))
                    continue
            res.files_copied += 1
            res.bytes_copied += f.size
            res.hashes[f.rel] = digest
        except Cancelled:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            res.failures.append((f.rel, str(e)))
    if opts.fsync:
        try:
            os.sync()
        except (AttributeError, OSError):
            pass
        _full_fsync(target)
    res.ok = not res.failures
    res.duration_s = time.time() - t0
    return res
