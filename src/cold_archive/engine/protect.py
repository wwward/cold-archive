"""Protect a directory tree in place: hash everything, (re)build PAR2 parity
per directory, then write the records and the recovery kit.

Safety rules that this module enforces, because parity is only useful if it
is never replaced at the wrong moment and records are only useful if they are
never quietly rewritten:

* Files that are missing compared with the records abort the run unless the
  caller explicitly accepts the removals.
* Files whose contents changed since the last run abort the run unless the
  caller explicitly accepts the changes. A person knows whether they edited a
  file; the tool cannot tell an edit from corruption that also touched the
  modification time (a copy through exFAT, rsync, a cloud sync).
* A file whose bytes changed while its size and modification time did not is
  treated as suspected silent corruption: the records keep the original hash,
  the directory's parity is left untouched, and the run reports it.
* When the manifest is unreadable, the run does NOT start over from whatever
  is on disk: it falls back to SHA256SUMS (treating every difference as
  suspected corruption) or refuses and points at Repair.
* A file the manifest has forgotten but the folder's existing parity still
  knows is judged against that parity (name, size, MD5), so a renamed folder
  or a lost record cannot launder corruption. The parity is read from any of
  its files, so a lost index does not erase the witness.
* A file the records do not know whose size and date equal those of a file
  that vanished is a renamed or moved file: if its contents equal that record
  it is simply recorded, and if they differ it is suspected corruption (the
  old record is kept, the folder is blocked) rather than a new file.
* "Rebuild parity" re-reads every file and still verifies the old set before
  replacing it; it is never a way around the checks above.
* When par2 builds a set it stores every file's real MD5; a set whose table
  differs from the records is refused (the old set stays), because that means
  par2 read different bytes than the records describe.
* Before an existing parity set is replaced, it is verified with par2. Every
  file par2 reports as damaged is hashed and compared with the records: one
  that still matches was merely edited since the parity was built (fine); one
  that does not is suspected corruption and blocks the replacement. Damage
  par2 cannot attribute to a file blocks too.
* New parity is built in a hidden folder and swapped in only after par2 has
  verified it and its file table equals the files we asked for.
* Names PAR2 cannot represent (backslash, control characters) are recorded in
  SHA256SUMS but cannot get parity; the run says so and is not "ok".
* Parity files are listed in SHA256SUMS, so a lost or corrupt recovery volume
  shows up in any checksum tool, not only in this program.

Whether a set is still current is judged on content: the file table inside
the PAR2 index (name, size, MD5) is compared with the records.

Order of writes: parity per folder, then the recovery kit, then the manifest,
then SHA256SUMS, then PARITY-INDEX. Repair knows this order: records that are
self-consistent are authoritative, and an index set that merely predates
them is rebuilt, never used to roll them back.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union

from . import par2 as p2
from .hashing import Digests, digest_file, hash_many, sha256_file
import dataclasses

from .layout import (INDEX_PARITY_BASENAME, same_date, MANIFEST_FILE, ROOT_TOOL_FILES, SUMS_FILE, human_bytes,
                     is_our_parity_path, name_problem, nfc, parity_set_of, payload_set_name)
from .manifest import (FileEntry, Manifest, ManifestError, ManifestTooNew, load_manifest, now_iso,
                       read_sums, save_manifest, sums_content, write_if_changed)
from .lock import ArchiveLock
from .observer import NullObserver, Observer
from .readme import write_recovery_kit
from .resources import materialize_tools
from .scan import ScannedDir, ScannedFile, ScanResult, scan_archive

# (name, size, md5) rows describing the files a set must cover
Row = Tuple[str, int, str]


@dataclass
class ProtectOptions:
    redundancy_percent: int = 10
    index_redundancy_percent: int = 50
    hash_workers: int = 4
    par2_workers: int = 2
    target_blocks: int = 2000
    min_block_size: int = 64 * 1024
    min_recovery_blocks: int = 10
    overhead_tolerance: float = 0.25
    full_rehash: bool = False
    rebuild_parity: bool = False
    accept_removals: Union[bool, Set[str]] = False
    accept_changes: Union[bool, Set[str]] = False
    verify_after_create: bool = True
    check_parity_packets: bool = True   # validate every parity packet when deciding whether a set is current
    write_kit: bool = True
    include_tools: bool = True    # TOOLS/: the par2cmdline source release the bundled par2 was built from
    include_app: bool = False     # TOOLS/: a zip of this app (only when running from an installed .app)
    label: str = ""
    media_note: str = ""
    par2_path: Optional[str] = None


@dataclass
class SetOutcome:
    directory: str
    set_name: str
    action: str  # created | kept | blocked | failed | removed
    files: int = 0
    bytes: int = 0
    block_size: int = 0
    recovery_blocks: int = 0
    message: str = ""


@dataclass
class ProtectResult:
    root: str
    ok: bool = False
    files_total: int = 0
    bytes_total: int = 0
    files_hashed: int = 0
    bytes_hashed: int = 0
    files_added: List[str] = field(default_factory=list)
    files_updated: List[str] = field(default_factory=list)      # content changed since the records (consent needed)
    files_removed: List[str] = field(default_factory=list)
    hash_failures: List[Tuple[str, str]] = field(default_factory=list)
    rot_suspected: List[str] = field(default_factory=list)
    unprotectable: List[Tuple[str, str]] = field(default_factory=list)  # (rel, why) names PAR2 cannot cover
    files_moved: List[Tuple[str, str]] = field(default_factory=list)     # (old rel, new rel): same contents under a new name
    suspected_renames: List[Tuple[str, str]] = field(default_factory=list)  # (old rel, new rel): same size and date, different contents
    tools_written: List[str] = field(default_factory=list)              # TOOLS/ files put in place this run
    sets: List[SetOutcome] = field(default_factory=list)
    symlinks_skipped: List[str] = field(default_factory=list)
    junk_skipped: int = 0
    scan_errors: List[Tuple[str, str]] = field(default_factory=list)
    needs_consent: str = ""      # "" | removals | changes | removals+changes: the run stopped before changing anything
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    def count(self, action: str) -> int:
        return sum(1 for s in self.sets if s.action == action)

    def summary(self) -> str:
        lines = []
        if self.needs_consent:
            lines.append("NOTHING CHANGED, YOUR DECISION IS NEEDED: " + self.root)
        else:
            lines.append(("PROTECTED: " if self.ok else "NOT FULLY PROTECTED: ") + self.root)
        lines.append(f"  {self.files_total} files, {human_bytes(self.bytes_total)}; hashed {self.files_hashed} files ({human_bytes(self.bytes_hashed)})")
        if self.tools_written:
            lines.append(f"  {len(self.tools_written)} tool files put in the TOOLS folder: " + ", ".join(os.path.basename(t) for t in self.tools_written))
        if self.files_added:
            lines.append(f"  {len(self.files_added)} files added to the records")
        if self.files_updated:
            lines.append(f"  {len(self.files_updated)} files have different contents than the records" + (":" if self.needs_consent else " (accepted, records updated):"))
            for r in self.files_updated[:10]:
                lines.append(f"    {r}")
            if len(self.files_updated) > 10:
                lines.append(f"    ... and {len(self.files_updated) - 10} more")
        if self.files_removed:
            lines.append(f"  {len(self.files_removed)} files recorded but no longer present" + (":" if self.needs_consent else " (accepted, dropped from the records):"))
            for r in self.files_removed[:10]:
                lines.append(f"    {r}")
            if len(self.files_removed) > 10:
                lines.append(f"    ... and {len(self.files_removed) - 10} more")
        if self.needs_consent:
            lines.append("  If you made these changes on purpose, run again accepting them. If you did not, run Check and Repair first.")
        else:
            lines.append(f"  parity sets: {self.count('created')} created, {self.count('kept')} already current, "
                         f"{self.count('blocked')} blocked, {self.count('failed')} failed, {self.count('removed')} removed")
        if self.files_moved:
            lines.append(f"  {len(self.files_moved)} files were renamed or moved since the last run (same contents, recorded under the new name)")
        if self.suspected_renames:
            lines.append(f"  {len(self.suspected_renames)} files look like renamed copies of recorded files but their contents differ; "
                         "the old records were kept and the folders left alone:")
            for old, new in self.suspected_renames[:10]:
                lines.append(f"    {new}  (same size and date as {old})")
            lines.append("    Run Repair: it rebuilds the recorded file. If you did change the file on purpose, delete the rebuilt one afterwards "
                         "and run Protect accepting removals and changes.")
        if self.unprotectable:
            lines.append(f"  {len(self.unprotectable)} files are in the records but CANNOT get parity because of their names:")
            for rel, why in self.unprotectable[:10]:
                lines.append(f"    {rel}  ({why})")
            lines.append("    Rename them and run Protect again.")
        for e in self.errors:
            lines.append("  ERROR: " + e)
        for s in self.sets:
            if s.action in ("blocked", "failed"):
                lines.append(f"    {s.action.upper()} {s.directory or '(top folder)'} / {s.set_name}: {s.message}")
        for w in self.warnings:
            lines.append("  warning: " + w)
        lines.append(f"  took {self.duration_s:.0f} s")
        return "\n".join(lines)


def _dir_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def protect(root: str, options: Optional[ProtectOptions] = None, observer: Optional[Observer] = None,
            known_hashes: Optional[Dict[str, Digests]] = None) -> ProtectResult:
    """Protect *root* in place. ``known_hashes`` (rel -> Digests) lets a caller
    that just copied and verified the files skip re-hashing them."""
    opts = options or ProtectOptions()
    root = os.path.abspath(root)
    if opts.redundancy_percent < 1:
        raise ValueError("redundancy must be at least 1 percent")
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    exe = p2.find_par2(opts.par2_path)  # fail before touching anything
    with ArchiveLock(root, exclusive=True) as lock:
        res = _protect_locked(root, opts, observer or NullObserver(), known_hashes, exe)
        if lock.warning:
            res.warnings.append(lock.warning)
        return res


# --- previous truth ----------------------------------------------------------

@dataclass
class OldRecords:
    """What we knew before this run, and how much to trust it."""
    manifest: Optional[Manifest]
    files: Dict[str, FileEntry]
    source: str            # manifest | sums | none
    parity_files: Dict[str, FileEntry]   # rel -> entry, from the manifest's parity section
    error: str = ""        # non-empty: refuse to run
    reread_all: bool = False   # the records cannot be cross-checked: read every file instead of trusting size and date
    warning: str = ""


def _load_old_records(root: str) -> OldRecords:
    manifest_path = os.path.join(root, MANIFEST_FILE)
    sums_path = os.path.join(root, SUMS_FILE)
    manifest_err = ""
    old: Optional[Manifest] = None
    try:
        old = load_manifest(manifest_path)
    except ManifestTooNew as e:
        return OldRecords(None, {}, "none", {}, error=str(e))
    except ManifestError as e:
        manifest_err = str(e)
    sums: Optional[Dict[str, str]] = None
    sums_err = ""
    if os.path.exists(sums_path):
        try:
            sums = read_sums(sums_path)
        except (ManifestError, OSError, UnicodeError) as e:
            sums_err = str(e)

    has_index = bool(p2.set_files_on_disk(root, INDEX_PARITY_BASENAME))

    def sums_entries(src: Dict[str, str]) -> Dict[str, FileEntry]:
        # Records taken from SHA256SUMS carry no size, date or MD5: any
        # difference in contents is treated as suspected corruption.
        return {rel: FileEntry(size=-1, mtime=-1.0, sha256=sha, md5="") for rel, sha in src.items()
                if rel not in ROOT_TOOL_FILES and not is_our_parity_path(rel)}

    if old is not None:
        # Cross-check: the two records must agree on every payload file. A
        # flipped digit in either one, or an interrupted run, shows up here
        # instead of being copied forward.
        if sums is not None:
            disagree = [r for r, e in old.files.items() if r in sums and sums[r] != e.sha256]
            only_manifest = [r for r in old.files if r not in sums]
            if disagree or only_manifest:
                return OldRecords(old, {}, "none", {}, error=(
                    f"the archive records disagree with each other ({len(disagree)} files have different fingerprints "
                    f"in {MANIFEST_FILE} and {SUMS_FILE}, {len(only_manifest)} are missing from {SUMS_FILE}). "
                    "Run Repair, which restores the records from PARITY-INDEX, then Protect again."))
            files = dict(old.files)
            # A payload file that SHA256SUMS lists and the manifest forgot is
            # still previous truth, not a new file.
            only_sums = {r: s for r, s in sums.items() if r not in files}
            files.update(sums_entries(only_sums))
            # A parity file's hash the two records disagree on is re-hashed
            # from disk rather than copied forward from either.
            parity = {r: e for r, e in old.parity.items() if sums.get(r) == e.sha256}
            return OldRecords(old, files, "manifest", parity)
        if sums_err:
            return OldRecords(old, {}, "none", {}, error=(
                f"{SUMS_FILE} is damaged ({sums_err}). Run Repair, which restores it from PARITY-INDEX, then Protect again."))
        # SHA256SUMS is missing. PARITY-INDEX can bring it back exactly;
        # without one, the manifest is the only record left and cannot be
        # cross-checked, so nothing in it is trusted without re-reading the file.
        if has_index:
            return OldRecords(old, {}, "none", {}, error=(
                f"{SUMS_FILE} is missing. Run Repair, which restores it from PARITY-INDEX, then Protect again."))
        return OldRecords(old, dict(old.files), "manifest", {}, reread_all=True, warning=(
            f"{SUMS_FILE} is missing and there is no PARITY-INDEX to restore it from; every file was re-read and "
            f"compared with {MANIFEST_FILE}, which cannot be cross-checked with anything"))

    if sums is not None:
        # The manifest is gone or unreadable but the authoritative list is fine.
        if has_index:
            return OldRecords(None, {}, "none", {}, error=(
                f"{MANIFEST_FILE} is {'damaged (' + manifest_err + ')' if manifest_err else 'missing'}. "
                "Run Repair, which restores it from PARITY-INDEX, then Protect again."))
        return OldRecords(None, sums_entries(sums), "sums", {}, warning=(
            f"{MANIFEST_FILE} was {'unreadable' if manifest_err else 'missing'} and there is no PARITY-INDEX to restore it from; "
            f"the records were taken from {SUMS_FILE} and every file was re-read. Run Repair afterwards if anything is reported."))
    if manifest_err:
        return OldRecords(None, {}, "none", {}, error=(
            f"{MANIFEST_FILE} is damaged ({manifest_err}) and {SUMS_FILE} is {'damaged' if sums_err else 'missing'}. "
            "Run Repair, which restores the records from PARITY-INDEX, then Protect again."))
    if sums_err:
        return OldRecords(None, {}, "none", {}, error=(
            f"{SUMS_FILE} is damaged ({sums_err}) and there is no manifest. Run Repair first."))
    return OldRecords(None, {}, "none", {})


def _parity_truth(scan: ScanResult) -> Dict[str, Tuple[int, str]]:
    """rel -> (size, md5) for every payload file an existing PAR2 index in its
    folder knows about. The old parity is a witness that outlives a lost or
    rewritten record."""
    truth: Dict[str, Tuple[int, str]] = {}
    for sd in scan.dirs:
        by_nfc = {nfc(f.name): f.rel for f in sd.files}
        for set_name in sd.parity_sets:
            if set_name == INDEX_PARITY_BASENAME:
                continue
            # Every volume carries the file table, so a lost or damaged
            # index does not erase the witness.
            idx = None
            for n in p2.set_files_on_disk(sd.abspath, set_name):
                try:
                    idx = p2.read_index(os.path.join(sd.abspath, n))
                    break
                except (p2.Par2Error, OSError):
                    continue
            if idx is None:
                continue
            for f in idx.file_table:
                rel = by_nfc.get(nfc(f.name))
                if rel is not None:
                    truth[rel] = (f.size, f.md5.hex())
    return truth


# --- the run -----------------------------------------------------------------

def _protect_locked(root: str, opts: ProtectOptions, obs: Observer, known_hashes: Optional[Dict[str, Digests]], exe: str) -> ProtectResult:
    t0 = time.time()
    res = ProtectResult(root=root)
    if opts.rebuild_parity and not opts.full_rehash:
        # Rebuilding destroys every old set; a file that rotted with its size
        # and date intact must be found before that, so every byte is read.
        opts = dataclasses.replace(opts, full_rehash=True)

    # ---- 0. the tools folder, before the scan so its files are payload of
    # this very run (hashed, listed, given parity like any other folder)
    if opts.include_tools or opts.include_app:
        written, warns = materialize_tools(root, include_source=opts.include_tools, include_app=opts.include_app, observer=obs)
        res.tools_written = written
        res.warnings.extend(warns)

    # ---- 1. scan
    scan = scan_archive(root, obs)
    res.files_total = len(scan.files)
    res.bytes_total = scan.total_bytes
    res.symlinks_skipped = list(scan.symlinks)
    res.junk_skipped = len(scan.junk_skipped)
    res.scan_errors = list(scan.errors)
    if scan.symlinks:
        res.warnings.append(f"{len(scan.symlinks)} symbolic links were skipped; an archive only holds real files")
    if scan.errors:
        res.errors.append(f"{len(scan.errors)} entries could not be read: " + "; ".join(f"{p or '(top folder)'} ({e})" for p, e in scan.errors[:5]))

    # ---- 2. previous records
    old_rec = _load_old_records(root)
    if old_rec.error:
        res.errors.append(old_rec.error)
        res.duration_s = time.time() - t0
        return res
    old = old_rec.manifest
    old_files = old_rec.files
    if old_rec.warning:
        res.warnings.append(old_rec.warning)
    if old_rec.reread_all and not opts.full_rehash:
        opts = dataclasses.replace(opts, full_rehash=True)
    truth = _parity_truth(scan)
    present_nfc = {}
    collision = False
    for r, f in scan.by_rel.items():
        nr = nfc(r)
        if nr in present_nfc:
            res.errors.append(f"Name collision: {r!r} and {present_nfc[nr].rel!r} differ only in accent encoding. This is not supported.")
            collision = True
        present_nfc[nr] = f
    if collision:
        res.duration_s = time.time() - t0
        return res

    old_files_nfc = {}
    for r, e in old_files.items():
        nr = nfc(r)
        if nr in old_files_nfc:
            res.errors.append(f"Recorded name collision: {r!r} and {old_files_nfc[nr]!r} differ only in accent encoding. This archive needs Repair.")
            collision = True
        old_files_nfc[nr] = r
    if collision:
        res.duration_s = time.time() - t0
        return res
    # Files recorded under a folder the scan could not enter are not gone,
    # they are unreadable: their records are kept and their folders blocked.
    unreadable_prefixes = tuple((d + "/") for d in scan.unreadable_dirs if d)
    root_unreadable = "" in scan.unreadable_dirs
    unreadable_recorded = sorted(r for r in old_files if nfc(r) not in present_nfc and (root_unreadable or r.startswith(unreadable_prefixes)))
    removed = sorted(r for r in old_files if nfc(r) not in present_nfc and r not in unreadable_recorded)
    res.files_removed = removed

    # ---- 3. hashing
    to_hash = []
    reuse: Dict[str, Digests] = {}
    for f in scan.files:
        effective_rel = old_files_nfc.get(nfc(f.rel), f.rel)
        if known_hashes and effective_rel in known_hashes and known_hashes[effective_rel].md5:
            reuse[effective_rel] = known_hashes[effective_rel]
            res.files_hashed += 1      # hashed by the caller (verified copy)
            res.bytes_hashed += f.size
            continue
        e = old_files.get(effective_rel)
        if (e is not None and not opts.full_rehash and e.size == f.size and same_date(e.mtime, f.mtime)
                and e.sha256 and e.md5):
            reuse[effective_rel] = Digests(e.sha256, e.md5)
        else:
            to_hash.append((f, effective_rel))
    obs.phase("Hashing files", total=sum(f.size for f, _ in to_hash), unit="bytes")
    outcomes = hash_many(((eff, f.abspath, f.size) for f, eff in to_hash), workers=opts.hash_workers, observer=obs)

    new_files: Dict[str, FileEntry] = {}
    added: Set[str] = set()
    updated: Set[str] = set()
    omitted: Set[str] = set()    # suspected corruption with no trustworthy record to keep
    fresh: Set[str] = set()      # digests computed this run (or by a verified copy), not copied from the records
    for r in unreadable_recorded:
        new_files[r] = old_files[r]
        res.hash_failures.append((r, "its folder could not be read"))
    # A vanished record whose size and date match a file the records do not
    # know is that file, renamed or moved. Its contents decide whether the
    # rename is fine or hides corruption.
    removed_by_sig: Dict[Tuple[int, float], str] = {}
    for r in removed:
        e = old_files[r]
        if e.size >= 0 and e.sha256:
            removed_by_sig.setdefault((e.size, e.mtime), r)
    kept_removed: Set[str] = set()
    # on-disk spelling -> recorded spelling, for every file the two differ on
    # (an NFC-recorded archive copied to exFAT is spelled NFD on disk). The
    # records, the consent sets and the parity rows all use the recorded
    # spelling; _protect_dir must look files up through this map.
    rel_map: Dict[str, str] = {}
    for f in scan.files:
        effective_rel = old_files_nfc.get(nfc(f.rel), f.rel)
        if effective_rel != f.rel:
            rel_map[f.rel] = effective_rel
            res.warnings.append(f"{effective_rel} is spelled differently on disk ({f.rel}); keeping recorded name")
        why = name_problem(effective_rel.rsplit("/", 1)[-1])
        if why:
            res.unprotectable.append((effective_rel, why))
        if effective_rel in reuse:
            dg = reuse[effective_rel]
            if known_hashes and effective_rel in known_hashes:
                fresh.add(effective_rel)
        else:
            o = outcomes.get(effective_rel)
            if not isinstance(o, Digests):
                res.hash_failures.append((effective_rel, str(o) if o is not None else "not hashed"))
                e = old_files.get(effective_rel)
                if e is not None:
                    new_files[effective_rel] = e     # keep the previous record: forgetting it would launder the next run
                continue
            dg = o
            fresh.add(effective_rel)
            res.files_hashed += 1
            res.bytes_hashed += f.size
        e = old_files.get(effective_rel)
        if e is None:
            known = truth.get(effective_rel)
            if known is not None and known[1] != dg.md5:
                # The records forgot this file but the folder's parity remembers
                # it with different contents.
                if known[0] == f.size:
                    res.rot_suspected.append(effective_rel)
                    omitted.add(effective_rel)
                    continue
                updated.add(effective_rel)
            else:
                origin = removed_by_sig.get((f.size, f.mtime))
                if origin is not None and origin not in kept_removed:
                    if old_files[origin].sha256 == dg.sha256:
                        res.files_moved.append((origin, effective_rel))
                    else:
                        # same size, same date, different bytes, new name: a
                        # renamed file that rotted. Keep the old record (Repair
                        # rebuilds it under its old name), do not record this.
                        res.rot_suspected.append(effective_rel)
                        res.suspected_renames.append((origin, effective_rel))
                        kept_removed.add(origin)
                        new_files[origin] = old_files[origin]
                        omitted.add(effective_rel)
                        continue
                added.add(effective_rel)
        elif e.sha256 != dg.sha256:
            if (e.size == f.size and same_date(e.mtime, f.mtime)) or e.size < 0:
                # Same size, same modification time, different bytes: this is
                # exactly what bit-rot looks like. (Records taken from
                # SHA256SUMS carry no size or date: any difference is treated
                # the same way, because nothing supports "edit".) Keep the
                # original truth.
                res.rot_suspected.append(effective_rel)
                dg = Digests(e.sha256, e.md5)
            else:
                updated.add(effective_rel)
        elif not e.md5 or e.size < 0:
            pass   # records upgraded (md5 learned, size/date learned)
        new_files[effective_rel] = FileEntry(size=f.size, mtime=f.mtime, sha256=dg.sha256, md5=dg.md5)
    if kept_removed:
        removed = [r for r in removed if r not in kept_removed]
        res.files_removed = removed
    res.files_added = sorted(added)
    res.files_updated = sorted(updated)

    # ---- 3b. consent gates: stop before touching anything
    need = []
    if removed:
        if opts.accept_removals is True:
            pass
        elif isinstance(opts.accept_removals, set) and set(removed).issubset(opts.accept_removals):
            pass
        else:
            need.append("removals")
    if updated:
        if opts.accept_changes is True:
            pass
        elif isinstance(opts.accept_changes, set) and set(updated).issubset(opts.accept_changes):
            pass
        else:
            need.append("changes")
    if need:
        res.needs_consent = "+".join(need)
        if "removals" in need:
            res.errors.append(f"{len(removed)} files recorded in the records are missing from the archive (for example {removed[0]!r}). "
                              "If you removed them on purpose, run again accepting removals; if not, run Repair first.")
        if "changes" in need:
            res.errors.append(f"{len(updated)} files have different contents than the records (for example {res.files_updated[0]!r}). "
                              "If you changed them on purpose, run again accepting changes; if not, run Check, then Repair.")
        res.errors.append("Nothing was changed.")
        res.duration_s = time.time() - t0
        return res
    changed: Set[str] = added | updated

    if res.hash_failures:
        res.errors.append(f"{len(res.hash_failures)} files could not be read (their previous records were kept where they existed): "
                          + "; ".join(f"{p} ({e})" for p, e in res.hash_failures[:5]))
    if res.rot_suspected:
        res.errors.append(f"{len(res.rot_suspected)} files have different contents but unchanged size and date, "
                          "which is what silent corruption looks like (for example "
                          f"{res.rot_suspected[0]!r}). Their original fingerprints were kept and their folders' parity was left "
                          "untouched. Run Repair.")
    if res.suspected_renames:
        res.errors.append(f"{len(res.suspected_renames)} files look like renamed copies of recorded files but their contents differ "
                          f"(for example {res.suspected_renames[0][1]!r}, same size and date as {res.suspected_renames[0][0]!r}). "
                          "The old records were kept. Run Repair.")
    if res.unprotectable:
        res.errors.append(f"{len(res.unprotectable)} files have names that PAR2 cannot protect (for example {res.unprotectable[0][0]!r}); "
                          "they are in the records but have no parity. Rename them and run Protect again.")

    # ---- 4. parity per directory (before the records, so an interrupted run
    # leaves the old records describing the old state, never new records over
    # unverified parity)
    p2_names_bad = {rel for rel, _ in res.unprotectable}
    blocked_dirs: Set[str] = {_dir_of(r) for r in res.rot_suspected} | {_dir_of(r) for r, _ in res.hash_failures} \
        | {_dir_of(r) for r, _ in scan.errors} | {_dir_of(r) for r in kept_removed}
    mem, thr = p2.default_resources(opts.par2_workers)
    obs.phase("Building parity", total=len(scan.dirs) + 1, unit="folders")
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, opts.par2_workers), thread_name_prefix="par2") as pool:
        futs = {pool.submit(_protect_dir, exe, sd, new_files, added, updated, p2_names_bad, sd.rel in blocked_dirs, opts, mem, thr, obs, fresh, rel_map): sd
                for sd in scan.dirs}
        try:
            for fut in as_completed(futs):
                sd = futs[fut]
                res.sets.extend(fut.result())
                done += 1
                obs.progress(done, len(scan.dirs) + 1, sd.rel or "(top folder)")
                obs.check_cancel()
        except BaseException:
            for f in futs:
                f.cancel()
            raise

    # ---- 5. records + kit + index
    obs.phase("Writing records")
    label = opts.label or (old.label if old and old.label else os.path.basename(root) or root)
    media = opts.media_note or (old.media if old else "")
    manifest = Manifest(created=(old.created if old and old.created else now_iso()), label=label, media=media,
                        redundancy_percent=opts.redundancy_percent, files=new_files,
                        notes=list(old.notes) if old else [])
    index_changed = write_records(root, manifest, scan, old, old_rec.parity_files, opts, obs, res)
    res.sets.extend(_protect_index(exe, root, index_changed, opts, mem, thr, obs))
    obs.progress(len(scan.dirs) + 1, len(scan.dirs) + 1, "index")

    res.ok = not res.errors and not any(s.action in ("blocked", "failed") for s in res.sets)
    res.duration_s = time.time() - t0
    return res


def write_records(root: str, manifest: Manifest, scan: ScanResult, old: Optional[Manifest],
                  old_parity: Dict[str, FileEntry], opts: ProtectOptions, obs: Observer,
                  res: Optional[ProtectResult] = None) -> bool:
    """Write kit, manifest and SHA256SUMS (in that order; SHA256SUMS lists the
    other two) and return True if PARITY-INDEX must be rebuilt."""
    # hashes of every payload parity file, reused from the previous manifest
    # when size and date are unchanged (par2 never rewrites a volume in place)
    parity_entries: Dict[str, FileEntry] = {}
    to_hash: List[Tuple[str, str, int]] = []
    stat_of: Dict[str, Tuple[int, float]] = {}
    for sd in scan.dirs:
        for set_name in p2_sets_present(sd):
            if set_name == INDEX_PARITY_BASENAME:
                continue
            for n in p2.set_files_on_disk(sd.abspath, set_name):
                rel = (sd.rel + "/" + n) if sd.rel else n
                try:
                    st = os.stat(os.path.join(sd.abspath, n))
                except OSError:
                    continue
                stat_of[rel] = (int(st.st_size), float(st.st_mtime))
                e = old_parity.get(rel)
                if e is not None and e.size == st.st_size and same_date(e.mtime, st.st_mtime) and e.sha256:
                    parity_entries[rel] = e
                else:
                    to_hash.append((rel, os.path.join(sd.abspath, n), int(st.st_size)))
    if to_hash:
        obs.phase("Recording parity files", total=sum(s for _, _, s in to_hash), unit="bytes")
        outcomes = hash_many(to_hash, workers=opts.hash_workers, observer=obs, with_md5=False)
        for rel, _, size in to_hash:
            o = outcomes.get(rel)
            if isinstance(o, Digests):
                parity_entries[rel] = FileEntry(size=size, mtime=stat_of[rel][1], sha256=o.sha256)
            elif res is not None:
                res.warnings.append(f"parity file {rel} could not be read for the records: {o}")
    manifest.parity = parity_entries

    content_changed = (old is None or old.files != manifest.files or old.parity != manifest.parity
                       or old.redundancy_percent != manifest.redundancy_percent
                       or old.label != manifest.label or old.media != manifest.media)
    manifest.updated = now_iso() if content_changed or not (old and old.updated) else old.updated
    kit_written = False
    if opts.write_kit:
        kit_written = write_recovery_kit(root, manifest, media_note=manifest.media)
    manifest_written = save_manifest(os.path.join(root, MANIFEST_FILE), manifest)
    pairs: List[Tuple[str, str]] = [(rel, e.sha256) for rel, e in manifest.files.items()]
    pairs += [(rel, e.sha256) for rel, e in parity_entries.items()]
    for name in ROOT_TOOL_FILES:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            pairs.append((name, sha256_file(p)))
    sums_written = write_if_changed(os.path.join(root, SUMS_FILE), sums_content(pairs))
    return manifest_written or kit_written or sums_written


def p2_sets_present(sd: ScannedDir) -> List[str]:
    """Set names with any file on disk in *sd* (re-listed, because parity may
    have been built after the scan)."""
    names = set(sd.parity_sets)
    try:
        for n in os.listdir(sd.abspath):
            s = p2_set_of(n)
            if s:
                names.add(s)
    except OSError:
        pass
    return sorted(names)


def p2_set_of(name: str) -> Optional[str]:
    return parity_set_of(name)


def _plan_for(rows: List[Row], redundancy: int, opts: ProtectOptions) -> p2.BlockPlan:
    return p2.plan_blocks([s for _, s, _ in rows], redundancy, target_blocks=opts.target_blocks,
                          min_block_size=opts.min_block_size, min_recovery_blocks=opts.min_recovery_blocks,
                          overhead_tolerance=opts.overhead_tolerance)


def _set_state(dirpath: str, set_name: str, rows: List[Row], plan: p2.BlockPlan, check_packets: bool = True) -> str:
    """'current', 'absent', or 'stale: <reason>' for an existing set, judged on
    the content the set actually covers (name, size, MD5 from its index)."""
    index_path = os.path.join(dirpath, set_name + p2.PAR2_SUFFIX)
    if not os.path.isfile(index_path):
        return "absent" if not p2.set_files_on_disk(dirpath, set_name) else "stale: index file missing"
    try:
        idx = p2.read_index(index_path)
    except (p2.Par2Error, OSError) as e:
        return f"stale: index unreadable ({e})"
    table = {(nfc(f.name), f.size, f.md5.hex()) for f in idx.file_table}
    want = {(nfc(n), s, m) for n, s, m in rows}
    if table != want:
        return "stale: files differ from the ones the parity covers"
    if idx.block_size != plan.block_size:
        return "stale: block size policy changed"
    if check_packets:
        # Read every parity file and validate each packet's MD5, so that a set
        # whose volumes have rotted is rebuilt rather than reported "current".
        # Only blocks of THIS set count: a same-named volume from another set
        # (a mixed restore from backups) is parity par2 cannot use here.
        bad = 0
        have = 0
        for n in p2.set_files_on_disk(dirpath, set_name):
            try:
                s = p2.scan_par2_file(os.path.join(dirpath, n))
            except OSError as e:
                return f"stale: {n} unreadable ({e})"
            if s.set_id is not None and idx.set_id is not None and s.set_id != idx.set_id:
                return f"stale: {n} belongs to a different parity set"
            bad += s.packets_bad + (1 if s.truncated else 0)
            have += s.recovery_blocks
        if bad:
            return f"stale: {bad} damaged packets in the parity files"
    else:
        have = p2.recovery_blocks_by_name(dirpath, set_name)
    if have < plan.recovery_blocks:
        return f"stale: {have} of {plan.recovery_blocks} recovery blocks present"
    return "current"


def _blocked(sd: ScannedDir, names: Set[str], message: str) -> List[SetOutcome]:
    return [SetOutcome(sd.rel, s, "blocked", message=message) for s in (sorted(names) or ["PARITY"])]


def _protect_dir(exe: str, sd: ScannedDir, records: Dict[str, FileEntry], added: Set[str], updated: Set[str],
                 names_bad: Set[str], blocked: bool, opts: ProtectOptions, mem: int, thr: int, obs: Observer,
                 fresh: Optional[Set[str]] = None, rel_map: Optional[Dict[str, str]] = None) -> List[SetOutcome]:
    """Bring one folder's parity up to date. ``fresh`` names the files whose
    digests were computed this run; the others were copied from the records
    because size and date were unchanged. ``rel_map`` gives the recorded
    spelling of files the disk spells differently (NFD on exFAT): records,
    ``added``, ``updated`` and ``fresh`` are keyed by the recorded spelling."""
    out: List[SetOutcome] = []
    label = sd.rel or "(top folder)"
    rel_map = rel_map or {}
    key = {f.rel: rel_map.get(f.rel, f.rel) for f in sd.files}   # on-disk rel -> recorded rel
    by_nfc = {nfc(f.name): f for f in sd.files}   # par2 prints NFC; open by the on-disk name
    eligible: List[Row] = []
    for f in sd.files:
        e = records.get(key[f.rel])
        if f.size > 0 and e is not None and key[f.rel] not in names_bad:
            eligible.append((f.name, f.size, e.md5))
    chunks = _chunk_rows(eligible)
    wanted = {payload_set_name(i): c for i, c in enumerate(chunks)}
    existing = {s for s in sd.parity_sets if s != INDEX_PARITY_BASENAME}
    if blocked:
        return _blocked(sd, existing | set(wanted), "folder has unreadable or suspect files; parity left as it was")
    if p2.remove_stale_build_dirs(sd.abspath):
        obs.info(f"{label}: removed a leftover parity build folder from an interrupted run")

    plans: Dict[str, p2.BlockPlan] = {}
    states: Dict[str, str] = {}
    for set_name, rows in wanted.items():
        plans[set_name] = _plan_for(rows, opts.redundancy_percent, opts)
        states[set_name] = _set_state(sd.abspath, set_name, rows, plans[set_name], opts.check_parity_packets)
    leftovers = sorted(existing - set(wanted))
    needs_change = opts.rebuild_parity or bool(leftovers) or any(s != "current" for s in states.values())
    if not needs_change:
        for set_name, rows in wanted.items():
            pl = plans[set_name]
            out.append(SetOutcome(sd.rel, set_name, "kept", len(rows), sum(s for _, s, _ in rows), pl.block_size, pl.recovery_blocks, "current"))
        return out

    # A set whose table disagrees with the records on a file whose digest was
    # merely copied forward (size and date unchanged) is decided by reading
    # the file: if its SHA-256 still matches the record, the record's MD5 was
    # wrong (a flipped digit in the manifest) and is corrected; if it does
    # not, the file rotted and the folder is blocked.
    disputed = _disputed_rows(sd, wanted, states, records, fresh or set(), rel_map)
    if disputed:
        for f, e in disputed:
            try:
                now = digest_file(f.abspath, observer=obs)
            except OSError as ex:
                return _blocked(sd, existing | set(wanted), f"{f.name!r} could not be read ({ex})")
            if now.sha256 != e.sha256:
                return _blocked(sd, existing | set(wanted), (
                    f"{f.name!r} differs from both the existing parity and the records; this looks like corruption. "
                    "Run Repair before re-protecting."))
            if now.md5 != e.md5:
                records[f.rel] = FileEntry(size=e.size, mtime=e.mtime, sha256=e.sha256, md5=now.md5)
                obs.info(f"{label}: corrected the recorded MD5 of {f.name!r} (its SHA-256 was right)")
        # the rows and states carry the corrected MD5s
        eligible = [(f.name, f.size, records[f.rel].md5) for f in sd.files
                    if f.size > 0 and records.get(key[f.rel]) is not None and key[f.rel] not in names_bad]
        chunks = _chunk_rows(eligible)
        wanted = {payload_set_name(i): c for i, c in enumerate(chunks)}
        for set_name, rows in wanted.items():
            plans[set_name] = _plan_for(rows, opts.redundancy_percent, opts)
            states[set_name] = _set_state(sd.abspath, set_name, rows, plans[set_name], opts.check_parity_packets)
        leftovers = sorted(existing - set(wanted))

    # Safety: before replacing or removing any existing set, make sure it is
    # not the only intact copy of data that has silently rotted. This runs
    # under 'rebuild parity' too: that option is not a way around it.
    for set_name in sorted(existing):
        if states.get(set_name) == "current":
            continue
        v = p2.verify(exe, sd.abspath, set_name, memory_mb=mem, threads=thr, observer=obs)
        if v.status in ("unusable", "error"):
            return _blocked(sd, existing | set(wanted), (
                f"existing {set_name} could not be checked ({v.status}: {v.message.splitlines()[-1] if v.message else 'no output'}); "
                "run a thorough Check; if it finds nothing wrong, run Protect again with 'rebuild parity' (it re-reads every file)"))
        flagged = {n: st for n, st in v.files.items() if st != "found"}
        if v.status != "ok" and not flagged:
            # par2's exit code says there is damage but no line named a
            # file: an output shape we do not understand. Never proceed.
            return _blocked(sd, existing | set(wanted), (
                f"existing {set_name} reports damage ({v.status}) but no file could be attributed; "
                "run a thorough Check and report this"))
        suspicious = []
        for n, st in flagged.items():
            f = by_nfc.get(nfc(n))
            if f is None:
                continue           # a file par2 knows that is no longer payload (removed, accepted)
            if key[f.rel] in updated:
                continue           # changed, and the caller accepted the change
            if key[f.rel] in added or st == "missing":
                # The records did not know this file but the old parity did,
                # and disagrees with its contents. Consent given for OTHER
                # files does not cover it.
                suspicious.append(n)
                continue
            # par2 says damaged and our records did not change: is the file
            # still what the records say it is (an earlier edit whose parity
            # rebuild never happened), or has it rotted?
            e = records.get(key[f.rel])
            try:
                now = digest_file(f.abspath, observer=obs, with_md5=False).sha256
            except OSError:
                suspicious.append(n)
                continue
            if e is None or now != e.sha256:
                suspicious.append(n)
        if suspicious:
            return _blocked(sd, existing | set(wanted), (
                f"{len(suspicious)} files in this folder differ from both the existing parity and the records "
                f"(for example {suspicious[0]!r}); this looks like corruption. Run Repair before re-protecting."))

    for set_name, rows in wanted.items():
        pl = plans[set_name]
        if states[set_name] == "current" and not opts.rebuild_parity:
            out.append(SetOutcome(sd.rel, set_name, "kept", len(rows), sum(s for _, s, _ in rows), pl.block_size, pl.recovery_blocks, "current"))
            continue
        obs.info(f"{label}: building {set_name} for {len(rows)} files ({states[set_name]})")
        names = [n for n, _, _ in rows]
        r = p2.create(exe, sd.abspath, set_name, names, pl, memory_mb=mem, threads=thr, observer=obs,
                      verify_after=opts.verify_after_create, expect=rows)
        if r.status != "ok":
            out.append(SetOutcome(sd.rel, set_name, "failed", len(rows), message=f"par2 create failed (exit {r.returncode}): {r.message}"))
            continue
        out.append(SetOutcome(sd.rel, set_name, "created", len(rows), sum(s for _, s, _ in rows), pl.block_size, pl.recovery_blocks,
                              f"{pl.source_blocks} blocks of {human_bytes(pl.block_size)}, {pl.recovery_blocks} recovery blocks"))
    if all(o.action in ("created", "kept") for o in out):
        for set_name in leftovers:
            try:
                for n in p2.set_files_on_disk(sd.abspath, set_name):
                    os.remove(os.path.join(sd.abspath, n))
                out.append(SetOutcome(sd.rel, set_name, "removed", message="no longer needed"))
            except OSError as e:
                out.append(SetOutcome(sd.rel, set_name, "failed", message=f"could not remove obsolete parity: {e}"))
    return out


def _disputed_rows(sd: ScannedDir, wanted: Dict[str, List[Row]], states: Dict[str, str], records: Dict[str, FileEntry],
                   fresh: Set[str], rel_map: Optional[Dict[str, str]] = None) -> List[Tuple[ScannedFile, FileEntry]]:
    """Files whose (size, MD5) in the records differ from what an existing
    set's table says, among those whose digest was NOT computed this run."""
    out = []
    for set_name, rows in wanted.items():
        if not str(states.get(set_name, "")).startswith("stale: files differ"):
            continue
        idx = None
        for n in p2.set_files_on_disk(sd.abspath, set_name):
            try:
                idx = p2.read_index(os.path.join(sd.abspath, n))
                break
            except (p2.Par2Error, OSError):
                continue
        if idx is None:
            continue
        table = {nfc(f.name): (f.size, f.md5.hex()) for f in idx.file_table}
        for f in sd.files:
            rel = (rel_map or {}).get(f.rel, f.rel)
            e = records.get(rel)
            if e is None or rel in fresh or not e.md5:
                continue
            t = table.get(nfc(f.name))
            if t is not None and t != (e.size, e.md5):
                out.append((f, e))
    return out


def chunk_rows(rows: List[Row]) -> List[List[Row]]:
    """How a folder's eligible files are split into sets: sorted by name,
    MAX_FILES_PER_SET per set (PARITY, PARITY-2, ...). Check reproduces this
    to judge whether each set of a split folder is still current."""
    items = sorted(rows, key=lambda t: t[0])
    n = p2.MAX_FILES_PER_SET
    return [items[i:i + n] for i in range(0, len(items), n)] if items else []


_chunk_rows = chunk_rows


def index_rows(root: str) -> List[Row]:
    rows: List[Row] = []
    for name in (SUMS_FILE,) + ROOT_TOOL_FILES:
        p = os.path.join(root, name)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            rows.append((name, os.path.getsize(p), digest_file(p).md5))
    return rows


def index_plan(rows: List[Row], opts: ProtectOptions) -> p2.BlockPlan:
    return p2.plan_blocks([s for _, s, _ in rows], max(opts.index_redundancy_percent, opts.redundancy_percent),
                          target_blocks=200, min_block_size=16 * 1024, min_recovery_blocks=20, overhead_tolerance=1.0)


def _protect_index(exe: str, root: str, dirty: bool, opts: ProtectOptions, mem: int, thr: int, obs: Observer) -> List[SetOutcome]:
    rows = index_rows(root)
    if not rows:
        return []
    set_name = INDEX_PARITY_BASENAME
    plan = index_plan(rows, opts)
    state = "stale: records changed" if dirty else _set_state(root, set_name, rows, plan, opts.check_parity_packets)
    if state == "current" and not opts.rebuild_parity:
        return [SetOutcome("", set_name, "kept", len(rows), sum(s for _, s, _ in rows), plan.block_size, plan.recovery_blocks, "current")]
    obs.info(f"(top folder): building {set_name} ({state})")
    r = p2.create(exe, root, set_name, [n for n, _, _ in rows], plan, memory_mb=mem, threads=thr, observer=obs,
                  verify_after=opts.verify_after_create, expect=rows)
    if r.status != "ok":
        return [SetOutcome("", set_name, "failed", len(rows), message=f"par2 create failed (exit {r.returncode}): {r.message}")]
    return [SetOutcome("", set_name, "created", len(rows), sum(s for _, s, _ in rows), plan.block_size, plan.recovery_blocks,
                       f"{plan.source_blocks} blocks, {plan.recovery_blocks} recovery blocks")]
