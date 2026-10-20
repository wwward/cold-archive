"""Repair an archive: make the files match the records again using PAR2, make
the parity whole again, and prove the result against SHA256SUMS.

Rules:

* The records come first. SHA256SUMS and the manifest are AUTHORITATIVE when
  they are self-consistent (they agree on every file and SHA256SUMS carries
  the manifest's own fingerprint). Then a PARITY-INDEX set that merely
  predates them (an interrupted Protect) is rebuilt, never used to roll the
  records back. Only inconsistent or unreadable records are restored from
  PARITY-INDEX, and that is reported.
* A file par2 calls damaged but which matches the records was edited after
  the parity was built: par2 would revert it, so its current version is put
  back afterwards and its folder's parity is rebuilt.
* A file that matches neither the parity nor the records, and whose size or
  date differ from the manifest, may be an edit made after the last Protect.
  Repair restores the recorded version but KEEPS the current one next to it
  (``name.1``) and says so. Nothing is ever the only copy that gets deleted.
  The same holds when there is no manifest to judge by, and for a file the
  records do not know at all but a folder's parity does (a record that was
  lost): the parity's version is restored, the current bytes are kept.
* A file that matches neither, with unchanged size and date, is corruption:
  it is rebuilt, its date is restored, and par2's backup of the damaged bytes
  is removed once the rebuilt file proves against SHA256SUMS.
* Only backups that this run created are ever removed; a user's own
  ``report.txt.1`` is nobody's business.
* Afterwards, folders whose parity is damaged, incomplete or out of date get
  fresh parity (through Protect's own guarded path), the records list the new
  parity files, and PARITY-INDEX is rebuilt.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import par2 as p2
from .hashing import sha256_file
from .layout import INDEX_PARITY_BASENAME, MANIFEST_FILE, ROOT_TOOL_FILES, SUMS_FILE, human_bytes, name_problem, nfc, remove_any_spelling, replace_any_spelling, same_date
from .manifest import Manifest, ManifestError, load_manifest, read_sums
from .lock import ArchiveLock
from .observer import NullObserver, Observer
from .protect import ProtectOptions, _protect_dir, _protect_index, _set_state, index_plan, index_rows, write_records
from .scan import scan_archive
from .verify import FileProblem, VerifyOptions, VerifyResult, _verify_locked, recompute_verdict

_BACKUP_RE = re.compile(r"^(.*)\.(\d+)$")


@dataclass
class RepairOptions:
    par2_workers: int = 1
    hash_workers: int = 4
    par2_path: Optional[str] = None
    remove_backups: bool = True
    rebuild_parity: bool = True     # rebuild damaged / out-of-date parity after the files are whole


@dataclass
class SetAction:
    directory: str
    set_name: str
    status: str   # repaired | ok | rebuilt | unrepairable | unusable | error | blocked | failed
    message: str = ""


@dataclass
class RepairResult:
    root: str
    verdict: str = "problem"   # ok | attention | problem
    before: Optional[VerifyResult] = None
    after: Optional[VerifyResult] = None
    actions: List[SetAction] = field(default_factory=list)
    repaired: List[str] = field(default_factory=list)
    unrepaired: List[FileProblem] = field(default_factory=list)
    records_restored: List[str] = field(default_factory=list)   # record files rolled back to the last completed Protect
    edits_kept: List[str] = field(default_factory=list)         # matched the records; parity was merely old
    changed_kept: List[str] = field(default_factory=list)       # matched nothing; recorded version restored, current kept as .1
    backups_removed: List[str] = field(default_factory=list)
    backups_kept: List[str] = field(default_factory=list)
    parity_rebuilt: List[str] = field(default_factory=list)     # "dir/SET"
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"

    def summary(self) -> str:
        head = {"ok": "REPAIRED, ALL GOOD", "attention": "REPAIRED, BUT NEEDS ATTENTION", "problem": "NOT FULLY REPAIRED"}[self.verdict]
        lines = [f"{head}: {self.root}"]
        nothing = (self.before is not None and not self.before.damaged_or_missing and self.before.verdict == "ok"
                   and not self.records_restored and not self.parity_rebuilt)
        if nothing:
            lines.append("  nothing needed repair")
        if self.records_restored:
            lines.append(f"  the archive records were restored from PARITY-INDEX ({', '.join(self.records_restored)}). "
                         "Files added or changed after the last completed Protect will show as such; run Protect to record them.")
        if self.repaired:
            lines.append(f"  {len(self.repaired)} files rebuilt and verified:")
            for r in self.repaired[:20]:
                lines.append(f"    {r}")
            if len(self.repaired) > 20:
                lines.append(f"    ... and {len(self.repaired) - 20} more")
        if self.edits_kept:
            lines.append(f"  {len(self.edits_kept)} files had been edited after their parity was built; the edits were kept and the parity rebuilt.")
        if self.changed_kept:
            lines.append(f"  {len(self.changed_kept)} files matched neither the records nor the parity and had a different date or size, "
                         "so they may have been changed on purpose after the last Protect. The recorded version was restored and the "
                         "changed version was KEPT next to it with .1 added to its name:")
            for r in self.changed_kept[:10]:
                lines.append(f"    {r}  (your version: {r}.1)")
            lines.append("    If the change was yours: delete the restored file, rename the .1 file back, and run Protect accepting changes.")
        if self.unrepaired:
            lines.append(f"  {len(self.unrepaired)} files could NOT be repaired (restore them from another copy):")
            for p in self.unrepaired[:20]:
                lines.append(f"    {p.rel} ({p.kind})")
        if self.parity_rebuilt:
            lines.append(f"  parity rebuilt in {len(self.parity_rebuilt)} places: " + ", ".join(self.parity_rebuilt[:8])
                         + (" ..." if len(self.parity_rebuilt) > 8 else ""))
        for a in self.actions:
            if a.status not in ("repaired", "ok", "rebuilt"):
                lines.append(f"  {a.directory or '(top folder)'} / {a.set_name}: {a.status} {a.message}")
        if self.backups_removed:
            lines.append(f"  {len(self.backups_removed)} copies of damaged originals were removed after the rebuilt files proved correct")
        if self.backups_kept:
            lines.append(f"  {len(self.backups_kept)} damaged originals kept next to their repaired files (name ends in .1)")
        if self.verdict == "attention" and self.after is not None:
            for s in self.after.bad_sets:
                lines.append(f"  parity needs attention: {s.directory or '(top folder)'} / {s.set_name}: {s.status}"
                             + (f" - {s.message}" if s.message else ""))
            extras = self.after.problems_of("extra")
            if extras:
                lines.append(f"  {len(extras)} files are not in the checksum list (run Protect to add them): " + ", ".join(p.rel for p in extras[:5]))
            if self.after.unprotected:
                lines.append(f"  {len(self.after.unprotected)} non-empty files have no parity (run Protect)")
        for e in self.errors:
            lines.append("  ERROR: " + e)
        for w in self.warnings:
            lines.append("  warning: " + w)
        lines.append(f"  took {self.duration_s:.0f} s")
        return "\n".join(lines)


def _dir_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def repair(root: str, options: Optional[RepairOptions] = None, observer: Optional[Observer] = None) -> RepairResult:
    opts = options or RepairOptions()
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    exe = p2.find_par2(opts.par2_path)
    with ArchiveLock(root, exclusive=True) as lock:
        res = _repair_locked(root, opts, observer or NullObserver(), exe)
        if lock.warning:
            res.warnings.append(lock.warning)
        return res


# --- records -----------------------------------------------------------------

def _records_consistent(root: str) -> Tuple[bool, str, Optional[Manifest], Dict[str, str]]:
    """(consistent, why-not, manifest, sums)."""
    sums: Dict[str, str] = {}
    try:
        sums = read_sums(os.path.join(root, SUMS_FILE))
    except (ManifestError, OSError, UnicodeError) as e:
        return False, f"{SUMS_FILE} unreadable ({e})", None, {}
    try:
        m = load_manifest(os.path.join(root, MANIFEST_FILE))
    except ManifestError as e:
        return False, f"{MANIFEST_FILE} unreadable ({e})", None, sums
    if m is None:
        return False, f"{MANIFEST_FILE} missing", None, sums
    if MANIFEST_FILE not in sums:
        return False, f"{SUMS_FILE} does not list {MANIFEST_FILE}", m, sums
    try:
        if sha256_file(os.path.join(root, MANIFEST_FILE)) != sums[MANIFEST_FILE]:
            return False, f"{MANIFEST_FILE} does not match its fingerprint in {SUMS_FILE}", m, sums
    except OSError as e:
        return False, str(e), m, sums
    for rel, e in m.files.items():
        if sums.get(rel) != e.sha256:
            return False, f"{SUMS_FILE} and {MANIFEST_FILE} disagree about {rel!r}", m, sums
    return True, "", m, sums


def _records_stage(root: str, exe: str, mem: int, thr: int, obs: Observer, res: RepairResult) -> Tuple[bool, bool]:
    """Returns (records_usable, index_needs_rebuild)."""
    if not p2.set_files_on_disk(root, INDEX_PARITY_BASENAME):
        ok, why, _, _ = _records_consistent(root)
        if not ok:
            res.warnings.append(f"the archive records are not usable ({why}) and there is no PARITY-INDEX to restore them from; "
                                "file checks fall back to whatever is readable")
        return ok, False
    obs.phase("Checking the archive records")
    consistent, why, _, sums = _records_consistent(root)
    v = p2.verify(exe, root, INDEX_PARITY_BASENAME, memory_mb=mem, threads=thr, observer=obs)
    if v.status == "ok":
        return consistent, not consistent
    if v.status not in ("repairable",):
        res.actions.append(SetAction("", INDEX_PARITY_BASENAME, v.status, v.message))
        if consistent:
            res.warnings.append("PARITY-INDEX (the records' own parity) is damaged beyond repair; the records themselves are intact "
                                "and PARITY-INDEX will be rebuilt")
            return True, True
        res.warnings.append(f"the archive records are damaged ({why}) and PARITY-INDEX could not repair them; "
                            "file checks fall back to whatever is readable")
        return False, False
    flagged = {n: st for n, st in v.files.items() if st != "found"}
    legit: List[str] = []
    damaged: List[str] = []
    for n, st in flagged.items():
        name = nfc(n)
        path = os.path.join(root, name)
        if st == "missing" or not os.path.isfile(path):
            damaged.append(name)
        elif consistent and name in (SUMS_FILE, MANIFEST_FILE):
            legit.append(name)           # newer than their parity, and they vouch for each other
        elif consistent and name in sums and sha256_file(path) == sums[name]:
            legit.append(name)           # a newer kit file that the records list correctly
        else:
            damaged.append(name)
    if consistent and not damaged:
        obs.info("the records are newer than PARITY-INDEX (an earlier Protect was interrupted); it will be rebuilt")
        return True, True
    obs.phase("Restoring the archive records")
    before_names = set(os.listdir(root))
    r = p2.repair(exe, root, INDEX_PARITY_BASENAME, memory_mb=mem, threads=thr, observer=obs)
    res.actions.append(SetAction("", INDEX_PARITY_BASENAME, r.status, r.message if r.status not in ("repaired", "ok") else ""))
    if r.status not in ("repaired", "ok"):
        res.warnings.append("the archive records could not be restored; file checks fall back to whatever is readable")
        return False, False
    new_names = set(os.listdir(root)) - before_names
    # put back the files that were newer than the parity, drop par2's copies of the rest
    for n in new_names:
        m = _BACKUP_RE.match(n)
        if not m:
            continue
        stem = m.group(1)
        if stem in legit:
            os.replace(os.path.join(root, n), os.path.join(root, stem))
        elif stem in (SUMS_FILE,) + ROOT_TOOL_FILES:
            try:
                os.remove(os.path.join(root, n))
            except OSError:
                pass
    res.records_restored = sorted(damaged)
    ok, why, _, _ = _records_consistent(root)
    if not ok:
        res.warnings.append(f"after restoring, the records are still not consistent ({why})")
    return ok, True


# --- the run -----------------------------------------------------------------

def _repair_locked(root: str, opts: RepairOptions, obs: Observer, exe: str) -> RepairResult:
    t0 = time.time()
    res = RepairResult(root=root)
    mem, thr = p2.default_resources(1)
    p2.remove_stale_build_dirs(root)

    # ---- 1. the records first
    records_ok, index_rebuild = _records_stage(root, exe, mem, thr, obs, res)

    # ---- 2. what is wrong
    before = _verify_locked(root, VerifyOptions(mode="standard", hash_workers=opts.hash_workers, par2_path=exe), obs)
    res.before = before
    problems = before.damaged_or_missing
    manifest: Optional[Manifest] = None
    try:
        manifest = load_manifest(os.path.join(root, MANIFEST_FILE))
    except ManifestError:
        manifest = None

    # ---- 3. repair every set in a folder that has a problem (and every set
    # that par2 already judged repairable/unrepairable)
    targets: Set[Tuple[str, str]] = set()
    problem_dirs: Set[str] = {_dir_of(p.rel) for p in problems}
    for s in before.sets:
        if s.set_name == INDEX_PARITY_BASENAME:
            continue
        if s.directory in problem_dirs or s.status in ("repairable", "unrepairable"):
            targets.add((s.directory, s.set_name))
    problem_rels: Set[str] = {p.rel for p in problems}
    extra_rels: Set[str] = {p.rel for p in before.problems if p.kind == "extra"}
    rot_backups: List[Tuple[str, str]] = []    # (dir abs, backup name) created for corrupted files
    if targets:
        obs.phase("Repairing", total=len(targets), unit="sets")
    for i, (d, set_name) in enumerate(sorted(targets)):
        obs.check_cancel()
        dabs = os.path.join(root, d.replace("/", os.sep)) if d else root
        obs.info(f"{d or '(top folder)'}: par2 repair {set_name}")
        try:
            by_nfc = {}
            collision = False
            for n in os.listdir(dabs):
                nr = nfc(n)
                if nr in by_nfc:
                    res.actions.append(SetAction(d, set_name, "error", f"Name collision: {n!r} and {by_nfc[nr]!r} differ only in accent encoding. This is not supported."))
                    collision = True
                    break
                by_nfc[nr] = n
            if collision:
                continue
        except OSError as e:
            res.actions.append(SetAction(d, set_name, "error", str(e)))
            continue
        pre = p2.verify(exe, dabs, set_name, memory_mb=mem, threads=thr, observer=obs)
        edited: List[str] = []       # match the records: keep their current bytes
        changed: List[str] = []      # match nothing, date/size differ: keep current bytes as .1
        rot: List[str] = []          # match nothing, date/size unchanged: corruption
        
        manifest_nfc_map = {nfc(r): r for r in manifest.files} if manifest else {}
        problem_rels_nfc = {nfc(r) for r in problem_rels}
        extra_rels_nfc = {nfc(r) for r in extra_rels}

        for n, st in pre.files.items():
            if st == "found":
                continue
            raw = by_nfc.get(nfc(n))
            if st == "missing" or raw is None:
                continue
            disk_rel = (d + "/" + raw) if d else raw
            effective_rel = manifest_nfc_map.get(nfc(disk_rel), disk_rel)
            
            if nfc(effective_rel) in extra_rels_nfc:
                changed.append(raw)  # the records do not know it; only the parity does: restore that, keep these bytes
                continue
            if nfc(effective_rel) not in problem_rels_nfc:
                edited.append(raw)
                continue
            e = manifest.files.get(effective_rel) if manifest else None
            try:
                stt = os.stat(os.path.join(dabs, raw))
            except OSError:
                rot.append(raw)
                continue
            if e is not None and e.size == stt.st_size and same_date(e.mtime, stt.st_mtime):
                rot.append(raw)
            else:
                # a different date or size, or no manifest to compare with:
                # this may be an edit, and its only copy is never deleted
                changed.append(raw)
        before_names = set(os.listdir(dabs))
        r = p2.repair(exe, dabs, set_name, memory_mb=mem, threads=thr, observer=obs)
        if r.status in ("repaired", "ok"):
            new_names = set(os.listdir(dabs)) - before_names
            for n in new_names:
                m = _BACKUP_RE.match(n)
                if not m:
                    continue
                stem = m.group(1)
                if stem in edited:
                    replace_any_spelling(os.path.join(dabs, n), os.path.join(dabs, stem))   # exFAT: see layout
                    res.edits_kept.append((d + "/" + stem) if d else stem)
                elif stem in changed:
                    res.changed_kept.append((d + "/" + stem) if d else stem)
                elif stem in rot:
                    rot_backups.append((dabs, n))
        msg = ""
        if r.status == "unrepairable":
            msg = f"needs {r.needs_blocks} more recovery blocks than exist" if r.needs_blocks else "not enough recovery data"
        elif r.status == "unusable":
            msg = "the parity files themselves are too damaged to use"
        elif r.status == "error":
            msg = r.message.splitlines()[-1] if r.message else f"par2 exit {r.returncode}"
        res.actions.append(SetAction(d, set_name, r.status, msg))
        obs.progress(i + 1, len(targets), set_name)

    # ---- 4. prove the files
    after = _verify_locked(root, VerifyOptions(mode="standard", hash_workers=opts.hash_workers, par2_path=exe), obs)
    still_bad = {p.rel for p in after.damaged_or_missing}
    res.repaired = sorted(p.rel for p in problems if p.rel not in still_bad)
    _restore_mtimes(root, res.repaired, res)
    _clean_backups(root, rot_backups, res, opts)
    if res.backups_removed:
        gone = set(res.backups_removed)
        after.problems = [p for p in after.problems if not (p.kind == "extra" and p.rel in gone)]
        after.unprotected = [r for r in after.unprotected if r not in gone]
        recompute_verdict(after)

    # ---- 5. make the parity whole again
    if opts.rebuild_parity and records_ok and manifest is not None and not after.damaged_or_missing:
        rebuilt = _rebuild_parity(root, exe, mem, thr, obs, res, after, manifest, index_rebuild, opts)
        if rebuilt:
            after = _verify_locked(root, VerifyOptions(mode="standard", hash_workers=opts.hash_workers, par2_path=exe), obs)
    elif index_rebuild and records_ok:
        res.warnings.append("PARITY-INDEX is out of date; run Protect to rebuild it")
    res.after = after
    res.unrepaired = [p for p in after.damaged_or_missing]
    if res.unrepaired or after.verdict == "problem":
        res.verdict = "problem"
    elif after.verdict == "attention" or res.changed_kept or res.records_restored:
        res.verdict = "attention"
    else:
        res.verdict = "ok"
    res.duration_s = time.time() - t0
    return res


def _rebuild_parity(root: str, exe: str, mem: int, thr: int, obs: Observer, res: RepairResult, after: VerifyResult,
                    manifest: Manifest, index_rebuild: bool, opts: RepairOptions) -> bool:
    """Rebuild payload sets that are damaged, incomplete or out of date, then
    the records (they list the parity files) and PARITY-INDEX. Uses Protect's
    own guarded path, so a set is only replaced when its folder's files match
    the records."""
    want_dirs: Set[str] = set()
    for s in after.sets:
        if s.set_name == INDEX_PARITY_BASENAME:
            if s.status not in ("ok", "not-checked"):
                index_rebuild = True
            continue
        if s.status not in ("ok", "not-checked"):
            want_dirs.add(s.directory)
    for p in after.parity_problems:
        want_dirs.add(_dir_of(p.rel))
    if not want_dirs and not index_rebuild and not after.unprotected:
        return False
    for rel in after.unprotected:
        want_dirs.add(_dir_of(rel))
    scan = scan_archive(root, obs)
    popts = ProtectOptions(redundancy_percent=manifest.redundancy_percent, par2_workers=1, hash_workers=opts.hash_workers)
    names_bad = {f.rel for f in scan.files if name_problem(f.name)}
    if want_dirs:
        obs.phase("Rebuilding parity", total=len(want_dirs), unit="folders")
    done = 0
    changed_any = False
    for sd in scan.dirs:
        if sd.rel not in want_dirs:
            continue
        outcomes = _protect_dir(exe, sd, manifest.files, set(), set(), names_bad, False, popts, mem, thr, obs)
        for o in outcomes:
            if o.action == "created":
                res.parity_rebuilt.append(f"{o.directory or '(top folder)'}/{o.set_name}")
                changed_any = True
            elif o.action in ("blocked", "failed"):
                res.actions.append(SetAction(o.directory, o.set_name, o.action, o.message))
        done += 1
        obs.progress(done, len(want_dirs), sd.rel or "(top folder)")
    obs.phase("Writing records")
    index_changed = write_records(root, manifest, scan, manifest, dict(manifest.parity), popts, obs)
    if index_changed or index_rebuild or changed_any:
        for o in _protect_index(exe, root, True, popts, mem, thr, obs):
            if o.action == "created":
                res.parity_rebuilt.append(f"(top folder)/{o.set_name}")
            elif o.action in ("blocked", "failed"):
                res.actions.append(SetAction("", o.set_name, o.action, o.message))
        return True
    return changed_any


def _clean_backups(root: str, rot_backups: List[Tuple[str, str]], res: RepairResult, opts: RepairOptions) -> None:
    """Remove par2's ``name.N`` copies of corrupted originals, and only those,
    once the rebuilt file proves against SHA256SUMS right now."""
    try:
        expected: Dict[str, str] = read_sums(os.path.join(root, SUMS_FILE))
    except (OSError, ManifestError, UnicodeError):
        for dabs, n in rot_backups:
            res.backups_kept.append(os.path.relpath(os.path.join(dabs, n), root).replace(os.sep, "/"))
        return
    expected_nfc = {nfc(r): h for r, h in expected.items()}   # the disk may spell a recorded name NFD (exFAT)
    for dabs, n in rot_backups:
        m = _BACKUP_RE.match(n)
        if not m:
            continue
        stem = m.group(1)
        rel_stem = nfc(os.path.relpath(os.path.join(dabs, stem), root).replace(os.sep, "/"))
        rel = os.path.relpath(os.path.join(dabs, n), root).replace(os.sep, "/")
        if not opts.remove_backups:
            res.backups_kept.append(rel)
            continue
        try:
            if rel_stem in expected_nfc and sha256_file(os.path.join(dabs, stem)) == expected_nfc[rel_stem]:
                remove_any_spelling(os.path.join(dabs, n))   # exFAT lists NFD but may only unlink the NFC spelling
                res.backups_removed.append(rel)
            else:
                res.backups_kept.append(rel)
        except OSError as e:
            res.backups_kept.append(rel)
            res.warnings.append(f"could not remove backup {rel}: {e}")


def _restore_mtimes(root: str, rels: List[str], res: RepairResult) -> None:
    """par2 writes a rebuilt file with a fresh modification time. Put back the
    time the manifest recorded, so the archive's metadata stays faithful and
    the next Protect sees an unchanged file."""
    if not rels:
        return
    try:
        m = load_manifest(os.path.join(root, MANIFEST_FILE))
    except ManifestError:
        return
    if m is None:
        return
    for rel in rels:
        e = m.files.get(rel)
        if e is None:
            continue
        p = os.path.join(root, rel.replace("/", os.sep))
        try:
            os.utime(p, (e.mtime, e.mtime))
        except OSError as ex:
            res.warnings.append(f"could not restore the date of {rel}: {ex}")
