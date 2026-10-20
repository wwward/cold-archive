"""Walk an archive directory and classify what is there.

Payload = every regular file that is not OS litter, not one of our parity
files, and (at the root) not one of our own records. Symlinks are never
followed and never archived: an archive must be self-contained, and a link's
target is not on the medium. They are reported so the owner can decide.
"""
from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .layout import INDEX_PARITY_BASENAME, is_junk, is_our_parity_file, is_root_tool_file, norm_rel, parity_set_of
from .observer import NullObserver, Observer


@dataclass
class ScannedFile:
    rel: str        # relative path with '/', spelled exactly as on disk (see layout.norm_rel)
    abspath: str
    name: str       # basename exactly as the filesystem returned it
    size: int
    mtime: float


@dataclass
class ScannedDir:
    rel: str        # '' for the root
    abspath: str
    files: List[ScannedFile] = field(default_factory=list)      # payload files directly inside
    parity_sets: Dict[str, List[str]] = field(default_factory=dict)  # our set name -> file names present


@dataclass
class ScanResult:
    root: str
    dirs: List[ScannedDir] = field(default_factory=list)
    files: List[ScannedFile] = field(default_factory=list)
    symlinks: List[str] = field(default_factory=list)
    junk_skipped: List[str] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)      # (rel, why): files and folders that could not be read
    unreadable_dirs: List[str] = field(default_factory=list)         # rel of folders os.walk could not enter ('' = the root)
    reserved: List[str] = field(default_factory=list)                # rel of files skipped because they carry one of OUR names

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def by_rel(self) -> Dict[str, ScannedFile]:
        return {f.rel: f for f in self.files}


def scan_archive(root: str, observer: Observer | None = None, include_reserved: bool = False) -> ScanResult:
    obs = observer or NullObserver()
    root = os.path.abspath(root)
    result = ScanResult(root=root)
    obs.phase("Scanning files")
    count = 0

    def on_error(e: OSError) -> None:
        # os.walk could not list a folder: record it by RELATIVE path (an
        # absolute path can never match a recorded file's folder) and remember
        # it as unreadable so the files recorded under it are not taken for
        # removed.
        p = getattr(e, "filename", None) or root
        rel = "" if os.path.abspath(p) == root else norm_rel(os.path.relpath(p, root))
        result.errors.append((rel, str(e)))
        result.unreadable_dirs.append(rel)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
        obs.check_cancel()
        # Deterministic order, skip symlinked and junk directories
        keep = []
        for d in sorted(dirnames):
            p = os.path.join(dirpath, d)
            if os.path.islink(p):
                result.symlinks.append(norm_rel(os.path.relpath(p, root)))
            elif is_junk(d):
                result.junk_skipped.append(norm_rel(os.path.relpath(p, root)))
            else:
                keep.append(d)
        dirnames[:] = keep
        rel_dir = "" if dirpath == root else norm_rel(os.path.relpath(dirpath, root))
        sd = ScannedDir(rel=rel_dir, abspath=dirpath)
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            rel = norm_rel(os.path.relpath(p, root))
            try:
                st = os.lstat(p)
            except OSError as e:
                result.errors.append((rel, str(e)))
                continue
            if _stat.S_ISLNK(st.st_mode):
                result.symlinks.append(rel)
                continue
            if not _stat.S_ISREG(st.st_mode):
                result.errors.append((rel, "not a regular file"))
                continue
            if is_junk(name):
                result.junk_skipped.append(rel)
                continue
            if not include_reserved:
                if is_our_parity_file(name) and (rel_dir == "" or parity_set_of(name) != INDEX_PARITY_BASENAME):
                    sd.parity_sets.setdefault(parity_set_of(name) or "", []).append(name)
                    result.reserved.append(rel)
                    continue
                if rel_dir == "" and is_root_tool_file(name):
                    result.reserved.append(rel)
                    continue
            f = ScannedFile(rel=rel, abspath=p, name=name, size=int(st.st_size), mtime=float(st.st_mtime))
            sd.files.append(f)
            result.files.append(f)
            count += 1
            if count % 2000 == 0:
                obs.progress(count, None, rel)
        result.dirs.append(sd)
    obs.progress(count, count)
    return result
