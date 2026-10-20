"""Rescue copy: get everything readable off a struggling drive, then repair the copy.

Create refuses a source it cannot fully protect, which is right when making
an archive and wrong when saving one from a failing drive. A rescue copy:

- only reads the source; nothing on the struggling drive is written, renamed
  or deleted, so a data-recovery service can still work on it afterwards;
- copies every file, including the archive's own records and repair data
  (SHA256SUMS, the manifest, every PARITY file), because the copy is what
  gets repaired;
- keeps going past read errors. Where part of a file cannot be read, that
  part is retried in small pieces and, if it still fails, written as zeros,
  so the readable rest of the file is kept: PAR2 then has to rebuild only the
  damaged blocks rather than the whole file;
- reads every copied file back from the healthy drive and compares it with
  what was written, as Create does;
- can be run again after an interruption: files already on the destination
  with the same size and date are left alone, so a failing drive is not read
  twice.

The person then runs Repair on the copy, which rebuilds the damaged and
missing files from the repair data, within its limits (about a tenth of
each folder at the usual setting).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .copy import _clear_immutable, _full_fsync, _is_partial, check_nesting, partial_name
from .hashing import CHUNK, set_nocache, sha256_file
from .layout import MANIFEST_FILE, human_bytes
from .observer import Cancelled, NullObserver, Observer
from .preflight import destination_folds_names, name_collisions
from .scan import scan_archive

SALVAGE_BLOCK = 64 * 1024     # retry granularity inside a chunk that failed to read
RETRIES = 2                   # extra attempts per small piece before it is given up

# Indirections so tests can simulate an unreadable sector or file.
_pread = os.pread
_open_source = os.open


@dataclass
class RescueResult:
    source: str
    target: str
    refused: str = ""                    # set when the rescue did not start, with the reason in plain words
    is_archive: bool = False             # the source carries Cold Archive records, so Repair can work on the copy
    files_total: int = 0
    bytes_total: int = 0
    files_copied: int = 0                # copied in full
    files_skipped: int = 0               # already on the destination from an interrupted rescue
    partial: Dict[str, int] = field(default_factory=dict)        # rel -> bytes that could not be read (written as zeros)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)   # rel -> why it is not on the copy at all
    warnings: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def lost_anything(self) -> bool:
        return bool(self.partial or self.unreadable)

    @property
    def ok(self) -> bool:
        return not self.refused and not self.lost_anything

    def summary(self) -> str:
        if self.refused:
            return f"RESCUE NOT STARTED: {self.source} -> {self.target}\n  {self.refused}"
        lines = [("RESCUED: " if not self.lost_anything else "RESCUED WITH DAMAGE: ") + f"{self.source} -> {self.target}",
                 f"  {self.files_copied:,} files copied in full"
                 + (f", {self.files_skipped:,} already there from an earlier rescue" if self.files_skipped else "")
                 + f", {len(self.partial):,} with unreadable parts, {len(self.unreadable):,} could not be read at all"]
        if self.partial:
            lines.append(f"  Unreadable parts, written as zeros ({human_bytes(sum(self.partial.values()))} in all):")
            for rel, n in sorted(self.partial.items())[:20]:
                lines.append(f"    {rel}: {human_bytes(n)}")
        for rel, why in self.unreadable[:20]:
            lines.append(f"    NOT COPIED {rel}: {why}")
        for w in self.warnings:
            lines.append("  warning: " + w)
        if self.is_archive:
            lines.append("  Next: run Repair on the copy. It rebuilds damaged and missing files from the repair data.")
        else:
            lines.append("  This folder has no Cold Archive records, so damaged files cannot be rebuilt; the copy keeps all that could be read.")
        lines.append(f"  took {self.duration_s:.0f} s")
        return "\n".join(lines)


def _same_drive(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _read_exactly(fd: int, n: int, offset: int) -> bytes:
    """Read n bytes at offset (fewer only at end of file); raises OSError."""
    parts = []
    got = 0
    while got < n:
        b = _pread(fd, n - got, offset + got)
        if not b:
            break
        parts.append(b)
        got += len(b)
    return b"".join(parts)


def _salvage_chunk(fd: int, n: int, offset: int) -> Tuple[bytes, int]:
    """A chunk failed to read in one go: read it in small pieces, retrying
    each a few times, and fill the pieces that never read with zeros.
    Returns the bytes for the chunk and how many of them are zeros-for-unreadable."""
    out = bytearray()
    lost = 0
    pos = 0
    while pos < n:
        m = min(SALVAGE_BLOCK, n - pos)
        piece = None
        for _ in range(1 + RETRIES):
            try:
                piece = _read_exactly(fd, m, offset + pos)
                break
            except OSError:
                piece = None
        if piece is None:
            piece = b"\0" * m
            lost += m
        elif len(piece) < m:          # the file ended early: the rest cannot be read
            lost += m - len(piece)
            piece = piece + b"\0" * (m - len(piece))
        out += piece
        pos += m
    return bytes(out), lost


def rescue_copy(source: str, target: str, observer: Optional[Observer] = None,
                allow_same_drive: bool = False) -> RescueResult:
    obs = observer or NullObserver()
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    t0 = time.time()
    res = RescueResult(source=source, target=target)
    if not os.path.isdir(source):
        res.refused = "The folder to rescue cannot be found. Check that the drive is connected."
        return res
    try:
        os.makedirs(target, exist_ok=True)
        check_nesting(source, target)
    except (OSError, ValueError) as e:
        res.refused = f"The destination cannot be used: {e}."
        return res
    if not allow_same_drive and _same_drive(source, target):
        res.refused = ("The copy must go onto a different, healthy drive. Copying a struggling drive onto itself "
                       "saves nothing and makes it work harder.")
        return res
    res.is_archive = os.path.isfile(os.path.join(source, MANIFEST_FILE))

    scan = scan_archive(source, obs, include_reserved=True)
    res.files_total = len(scan.files)
    res.bytes_total = scan.total_bytes
    for rel, err in scan.errors:
        res.unreadable.append((rel or "(top folder)", f"the folder could not be listed: {err}"))
    if scan.symlinks:
        res.warnings.append(f"{len(scan.symlinks)} symbolic links were not copied")
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        free = None
    already = sum(f.size for f in scan.files
                  if _already_there(os.path.join(target, os.path.relpath(f.abspath, source)), f.size, f.mtime))
    if free is not None and res.bytes_total - already > free:
        res.refused = (f"The destination drive has {human_bytes(free)} free, and the rescue needs about "
                       f"{human_bytes(res.bytes_total - already)}. Choose a larger drive.")
        return res

    colliding: Dict[str, str] = {}
    if destination_folds_names(target):
        for a, b in name_collisions(scan):
            colliding[b] = a
    bad_dirs = set()
    for sd in scan.dirs:
        dpath = os.path.join(target, os.path.relpath(sd.abspath, source)) if sd.rel else target
        try:
            os.makedirs(dpath, exist_ok=True)
            for n in os.listdir(dpath):
                if _is_partial(n):
                    os.remove(os.path.join(dpath, n))
        except OSError as e:
            bad_dirs.add(sd.rel)
            res.unreadable.append((sd.rel or "(top folder)", f"the folder could not be created on the destination: {e}"))

    total_work = res.bytes_total * 2
    done = 0
    obs.phase("Copying everything that can be read", total=total_work, unit="bytes")
    for f in scan.files:
        obs.check_cancel()
        rel_os = os.path.relpath(f.abspath, source)
        dst = os.path.join(target, rel_os)
        if f.rel in colliding:
            res.unreadable.append((f.rel, f"its name differs from {colliding[f.rel]!r} only in capitals, which this drive cannot keep apart"))
            done += 2 * f.size
            continue
        if any(f.rel.startswith(b + "/") or b == "" for b in bad_dirs):
            res.unreadable.append((f.rel, "its folder could not be created on the destination"))
            done += 2 * f.size
            continue
        if _already_there(dst, f.size, f.mtime):
            res.files_skipped += 1
            done += 2 * f.size
            obs.progress(done, total_work, f.name)
            continue
        tmp = os.path.join(os.path.dirname(dst), partial_name(f.name))
        lost = 0
        try:
            fd = _open_source(f.abspath, os.O_RDONLY)
        except OSError as e:
            res.unreadable.append((f.rel, f"could not be opened: {e.strerror or e}"))
            done += 2 * f.size
            continue
        try:
            h = hashlib.sha256()
            with open(tmp, "wb") as out:
                set_nocache(out.fileno())
                off = 0
                while off < f.size:
                    obs.check_cancel()
                    n = min(CHUNK, f.size - off)
                    try:
                        b = _read_exactly(fd, n, off)
                        if len(b) < n:
                            raise OSError("short read")
                    except OSError:
                        b, chunk_lost = _salvage_chunk(fd, n, off)
                        lost += chunk_lost
                    h.update(b)
                    out.write(b)
                    off += n
                    done += n
                    obs.progress(done, total_work, f.name)
                out.flush()
                os.fsync(out.fileno())
            written = h.hexdigest()
            os.replace(tmp, dst)
            try:
                shutil.copystat(f.abspath, dst, follow_symlinks=False)
            except OSError:
                try:
                    os.utime(dst, (f.mtime, f.mtime))
                except OSError:
                    pass
            _clear_immutable(dst)

            def _tick(k: int) -> None:
                nonlocal done
                done += k
                obs.progress(done, total_work, "checking the copy of " + f.name)
            if sha256_file(dst, observer=obs, on_bytes=_tick, nocache=True) != written:
                res.unreadable.append((f.rel, "the copy did not read back correctly; the destination drive may be faulty"))
                try:
                    os.remove(dst)
                except OSError:
                    pass
                continue
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
            res.unreadable.append((f.rel, f"could not be written to the destination: {e}"))
            continue
        finally:
            os.close(fd)
        if lost:
            res.partial[f.rel] = lost
            obs.log("warning", f"{f.rel}: {human_bytes(lost)} could not be read and was written as zeros")
        else:
            res.files_copied += 1
    try:
        os.sync()
    except (AttributeError, OSError):
        pass
    _full_fsync(target)
    res.duration_s = time.time() - t0
    return res


def _already_there(dst: str, size: int, mtime: float) -> bool:
    """Left by an earlier, interrupted rescue: same size and same date (to the
    second; FAT and exFAT round dates). Cheap on purpose: re-reading a failing
    drive to compare contents is what a rescue must avoid."""
    try:
        st = os.stat(dst)
    except OSError:
        return False
    return st.st_size == size and abs(st.st_mtime - mtime) < 2.0
