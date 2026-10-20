"""Check an archive: every listed file against SHA256SUMS, every parity set
for intactness and currency, and report exactly what is wrong and whether it
can be fixed.

Modes:
  standard  hash every listed file; validate every PAR2 packet of every
            parity file; run a full par2 verify only in folders that have a
            problem. Complete coverage of both data and parity at ~1.1x the
            archive's size read.
  thorough  as standard, plus par2 verify in every folder (a second,
            independent check of every file by par2's own MD5s). ~2.1x read.
  hashes    SHA256SUMS only, exactly what a trustee's ``sha256sum -c`` sees
            (parity files included). ~1.1x read.
  parity    par2 verify only (works even when SHA256SUMS is lost). ~1x read.

Nothing here says "all good" unless data AND parity were both read and found
whole and current.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import par2 as p2
from .hashing import Digests, hash_many
from .layout import (INDEX_PARITY_BASENAME, MANIFEST_FILE, ROOT_TOOL_FILES, SUMS_FILE, human_bytes,
                     is_our_parity_path, name_problem, nfc, parity_set_of, payload_set_name)
from .manifest import FileEntry, Manifest, ManifestError, load_manifest, read_sums
from .lock import ArchiveLock
from .observer import NullObserver, Observer
from .scan import ScanResult, scan_archive

MODES = ("standard", "thorough", "hashes", "parity")


@dataclass
class VerifyOptions:
    mode: str = "standard"
    hash_workers: int = 4
    par2_workers: int = 2
    par2_path: Optional[str] = None


@dataclass
class FileProblem:
    rel: str
    kind: str      # mismatch | missing | unreadable | extra
    detail: str = ""


@dataclass
class SetStatus:
    directory: str
    set_name: str
    status: str    # ok | repairable | unrepairable | unusable | degraded | stale | error | not-checked
    damaged: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    packets_bad: int = 0
    recovery_blocks: int = 0
    recovery_expected: int = 0
    message: str = ""


@dataclass
class VerifyResult:
    root: str
    mode: str
    verdict: str = "problem"      # ok | attention | problem
    sums_status: str = "ok"       # ok | from-manifest | missing | unreadable
    files_listed: int = 0          # data and record files listed in SHA256SUMS
    parity_listed: int = 0         # our parity files listed in SHA256SUMS
    files_checked: int = 0
    bytes_checked: int = 0
    problems: List[FileProblem] = field(default_factory=list)          # data and record files
    parity_problems: List[FileProblem] = field(default_factory=list)   # our parity files (mismatch | missing)
    sets: List[SetStatus] = field(default_factory=list)
    unprotected: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"

    def problems_of(self, kind: str) -> List[FileProblem]:
        return [p for p in self.problems if p.kind == kind]

    @property
    def damaged_or_missing(self) -> List[FileProblem]:
        return [p for p in self.problems if p.kind in ("mismatch", "missing", "unreadable")]

    @property
    def bad_sets(self) -> List[SetStatus]:
        return [s for s in self.sets if s.status not in ("ok", "not-checked")]

    def summary(self) -> str:
        head = {"ok": "ALL GOOD", "attention": "INTACT, BUT NEEDS ATTENTION", "problem": "PROBLEMS FOUND"}[self.verdict]
        lines = [f"{head}: {self.root}",
                 f"  checked {self.files_checked} of {self.files_listed} listed files ({human_bytes(self.bytes_checked)}), mode {self.mode}"
                 + (f"; {self.parity_listed} parity files listed" if self.parity_listed else "")]
        if self.sums_status != "ok":
            lines.append(f"  checksum list: {self.sums_status}")
        for kind, word in (("mismatch", "DAMAGED"), ("missing", "MISSING"), ("unreadable", "UNREADABLE")):
            ps = self.problems_of(kind)
            if ps:
                lines.append(f"  {len(ps)} {word} files:")
                for p in ps[:20]:
                    lines.append(f"    {p.rel}" + (f"  ({p.detail})" if p.detail else ""))
                if len(ps) > 20:
                    lines.append(f"    ... and {len(ps) - 20} more")
        extras = self.problems_of("extra")
        if extras:
            lines.append(f"  {len(extras)} files are not in the checksum list (added after the last Protect, or kept copies from a repair):")
            for p in extras[:10]:
                lines.append(f"    {p.rel}")
        if self.parity_problems:
            lines.append(f"  {len(self.parity_problems)} parity (repair data) files are damaged or missing; your files are not affected, "
                         "but less damage can be repaired until Protect rebuilds them:")
            for p in self.parity_problems[:10]:
                lines.append(f"    {p.rel} ({p.kind})")
        bad_sets = self.bad_sets
        not_checked = [s for s in self.sets if s.status == "not-checked"]
        checked = [s for s in self.sets if s.status == "ok"]
        if bad_sets:
            lines.append(f"  parity sets needing attention: {len(bad_sets)} of {len(self.sets)}")
            for s in bad_sets[:20]:
                lines.append(f"    {s.directory or '(top folder)'} / {s.set_name}: {s.status}" + (f" - {s.message}" if s.message else ""))
        elif checked:
            lines.append(f"  parity sets: {len(checked)} checked, all intact and current")
        elif not self.sets:
            lines.append("  parity sets: none found")
        if not_checked:
            why = "fingerprints-only mode" if self.mode == "hashes" else "no par2 program"
            lines.append(f"  parity sets: {len(not_checked)} NOT checked ({why}); whether they are intact is unknown")
        if self.unprotected:
            lines.append(f"  {len(self.unprotected)} non-empty files have no parity (never protected, or a name PAR2 cannot store):")
            for r in self.unprotected[:5]:
                lines.append(f"    {r}")
        for e in self.errors:
            lines.append("  ERROR: " + e)
        for w in self.warnings:
            lines.append("  warning: " + w)
        rep = [s for s in self.sets if s.status == "repairable"]
        unrep = [s for s in self.sets if s.status in ("unrepairable", "unusable")]
        if rep and not unrep:
            lines.append("  Repair can fix everything reported above.")
        elif rep:
            lines.append(f"  Repair can fix {len(rep)} folders; {len(unrep)} folders are beyond what parity can rebuild.")
        elif unrep:
            lines.append("  Parity cannot rebuild the damage reported above; restore these folders from another copy.")
        if any(s.status in ("degraded", "stale") for s in self.sets) and not rep and not unrep:
            lines.append("  Run Protect to rebuild the parity that is out of date or damaged.")
        lines.append(f"  took {self.duration_s:.0f} s")
        return "\n".join(lines)


def _dir_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def verify(root: str, options: Optional[VerifyOptions] = None, observer: Optional[Observer] = None) -> VerifyResult:
    opts = options or VerifyOptions()
    if opts.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    with ArchiveLock(root, exclusive=False) as lock:
        res = _verify_locked(root, opts, observer or NullObserver())
        if lock.warning:
            res.warnings.append(lock.warning)
        return res


def load_records(root: str, res: VerifyResult) -> Tuple[Dict[str, str], Optional[Manifest]]:
    """Expected hashes (from SHA256SUMS, cross-checked with the manifest) and
    the manifest itself when readable. Fills res.sums_status/errors/warnings."""
    expected: Dict[str, str] = {}
    sums_path = os.path.join(root, SUMS_FILE)
    if os.path.exists(sums_path):
        try:
            expected = read_sums(sums_path)
            res.sums_status = "ok"
        except (ManifestError, OSError, UnicodeError) as e:
            res.sums_status = "unreadable"
            res.errors.append(f"{SUMS_FILE} is damaged ({e}); Repair can rebuild it from PARITY-INDEX")
    else:
        res.sums_status = "missing"
        res.errors.append(f"{SUMS_FILE} is missing; Repair can rebuild it from PARITY-INDEX")
    m: Optional[Manifest] = None
    try:
        m = load_manifest(os.path.join(root, MANIFEST_FILE))
    except ManifestError as e:
        res.warnings.append(f"{MANIFEST_FILE} is unreadable ({e}); Repair can rebuild it from PARITY-INDEX")
    if res.sums_status != "ok":
        if m:
            expected = {rel: e.sha256 for rel, e in m.files.items()}
            expected.update({rel: e.sha256 for rel, e in m.parity.items()})
            res.sums_status = "from-manifest"
            res.warnings.append("file hashes were taken from the manifest because the checksum list is unusable")
    elif m:
        # The two records must agree; a truncated or bit-flipped SHA256SUMS
        # would otherwise silently narrow or corrupt the check.
        disagree = [rel for rel, e in m.files.items() if rel in expected and expected[rel] != e.sha256]
        only_manifest = [rel for rel in m.files if rel not in expected]
        if disagree:
            res.errors.append(f"{SUMS_FILE} and {MANIFEST_FILE} disagree on {len(disagree)} files (for example {disagree[0]!r}); "
                              "Repair restores the records from PARITY-INDEX")
        if only_manifest:
            res.errors.append(f"{SUMS_FILE} lists {len(only_manifest)} files fewer than {MANIFEST_FILE} (is it truncated?); "
                              "those files were checked against the manifest. Repair restores the records from PARITY-INDEX")
            for rel in only_manifest:
                expected[rel] = m.files[rel].sha256
    return expected, m


def _verify_locked(root: str, opts: VerifyOptions, obs: Observer) -> VerifyResult:
    t0 = time.time()
    res = VerifyResult(root=root, mode=opts.mode)
    exe: Optional[str] = None
    if opts.mode != "hashes":
        try:
            exe = p2.find_par2(opts.par2_path)
        except p2.Par2NotFound as e:
            if opts.mode == "parity":
                res.errors.append(str(e) + " Cannot check parity without par2.")
            else:
                res.warnings.append(str(e) + " Parity was not checked.")

    scan = scan_archive(root, obs)
    for rel, err in scan.errors:
        res.problems.append(FileProblem(rel, "unreadable", err))

    expected, manifest = load_records(root, res)
    expected_data = {r: h for r, h in expected.items() if not is_our_parity_path(r)}
    expected_parity = {r: h for r, h in expected.items() if is_our_parity_path(r)}
    res.files_listed = len(expected_data)
    res.parity_listed = len(expected_parity)

    # ---- on-disk view: payload files, the tool files at the root, our parity files
    on_disk: Dict[str, Tuple[str, int]] = {f.rel: (f.abspath, f.size) for f in scan.files}
    for name in ROOT_TOOL_FILES:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            on_disk[name] = (p, os.path.getsize(p))
    parity_on_disk: Dict[str, Tuple[str, int]] = {}
    for sd in scan.dirs:
        for set_name, names in sd.parity_sets.items():
            if set_name == INDEX_PARITY_BASENAME:
                continue
            for n in names:
                rel = (sd.rel + "/" + n) if sd.rel else n
                p = os.path.join(sd.abspath, n)
                try:
                    parity_on_disk[rel] = (p, os.path.getsize(p))
                except OSError:
                    pass

    on_disk_nfc: Dict[str, str] = {}
    collision = False
    for rel in on_disk:
        nr = nfc(rel)
        if nr in on_disk_nfc:
            res.errors.append(f"Name collision: {rel!r} and {on_disk_nfc[nr]!r} differ only in accent encoding. This is not supported.")
            collision = True
        on_disk_nfc[nr] = rel
    
    parity_on_disk_nfc: Dict[str, str] = {}
    for rel in parity_on_disk:
        nr = nfc(rel)
        if nr in parity_on_disk_nfc:
            res.errors.append(f"Name collision: {rel!r} and {parity_on_disk_nfc[nr]!r} differ only in accent encoding. This is not supported.")
            collision = True
        parity_on_disk_nfc[nr] = rel
        
    if collision:
        res.verdict = "problem"
        return res

    listed = set(expected_data)
    listed_nfc = {nfc(rel) for rel in listed}
    missing = sorted([rel for rel in listed if nfc(rel) not in on_disk_nfc])
    # with no records at all, "not in the checksum list" means nothing
    extra = sorted([rel for rel in on_disk if nfc(rel) not in listed_nfc and rel not in ROOT_TOOL_FILES]) if expected_data else []
    for rel in missing:
        res.problems.append(FileProblem(rel, "missing"))
    for rel in extra:
        res.problems.append(FileProblem(rel, "extra"))
    for rel in sorted([r for r in expected_parity if nfc(r) not in parity_on_disk_nfc]):
        res.parity_problems.append(FileProblem(rel, "missing"))

    # ---- hashes
    if opts.mode in ("standard", "thorough", "hashes") and expected:
        to_check = []
        for rel in sorted(listed):
            if nfc(rel) in on_disk_nfc:
                d_rel = on_disk_nfc[nfc(rel)]
                to_check.append((rel, on_disk[d_rel][0], on_disk[d_rel][1]))
        if opts.mode == "hashes":
            # what a trustee's sha256sum -c would see: parity files included
            for rel in sorted(expected_parity):
                if nfc(rel) in parity_on_disk_nfc:
                    d_rel = parity_on_disk_nfc[nfc(rel)]
                    to_check.append((rel, parity_on_disk[d_rel][0], parity_on_disk[d_rel][1]))
        obs.phase("Checking file fingerprints", total=sum(s for _, _, s in to_check), unit="bytes")
        outcomes = hash_many(to_check, workers=opts.hash_workers, observer=obs, with_md5=False)
        for rel, _, size in to_check:
            o = outcomes.get(rel)
            is_parity = rel in expected_parity
            if isinstance(o, Digests):
                res.files_checked += 1
                res.bytes_checked += size
                if o.sha256 != expected[rel]:
                    (res.parity_problems if is_parity else res.problems).append(
                        FileProblem(rel, "mismatch", "contents differ from the recorded fingerprint"))
            else:
                (res.parity_problems if is_parity else res.problems).append(FileProblem(rel, "unreadable", str(o)))

    # ---- parity
    problem_dirs: Set[str] = {_dir_of(p.rel) for p in res.problems if p.kind in ("mismatch", "missing", "unreadable")}
    all_sets: Dict[Tuple[str, str], str] = {}   # (dir rel, set name) -> dir abspath
    for sd in scan.dirs:
        for set_name in sd.parity_sets:
            all_sets[(sd.rel, set_name)] = sd.abspath
    for rel in expected_parity:    # sets that vanished entirely still count
        d = _dir_of(rel)
        s = parity_set_of(os.path.basename(rel))
        if s and (d, s) not in all_sets:
            all_sets[(d, s)] = os.path.join(root, d.replace("/", os.sep)) if d else root
    if (expected or manifest is not None) and ("", INDEX_PARITY_BASENAME) not in all_sets:
        # PARITY-INDEX is not listed in SHA256SUMS (it is built from it), so
        # its loss can only be seen by expecting it whenever records exist
        all_sets[("", INDEX_PARITY_BASENAME)] = root
    covered: Set[str] = set()
    if opts.mode == "hashes" or exe is None:
        for (d, s) in sorted(all_sets):
            res.sets.append(SetStatus(d, s, "not-checked"))
    else:
        mem, thr = p2.default_resources(1)
        obs.phase("Checking parity", total=len(all_sets), unit="sets")
        by_dir: Dict[str, Dict[str, object]] = {sd.rel: {nfc(f.name): f for f in sd.files} for sd in scan.dirs}
        # unlisted files by folder: a set that knows one of them (a file the
        # records forgot) gets par2's own verdict even in standard mode
        extras_by_dir: Dict[str, Set[str]] = {}
        for rel in extra:
            extras_by_dir.setdefault(_dir_of(rel), set()).add(nfc(rel.rsplit("/", 1)[-1]))
        redundancy = manifest.redundancy_percent if manifest else 10
        for i, ((d, set_name), dabs) in enumerate(sorted(all_sets.items())):
            obs.check_cancel()
            deep = opts.mode in ("thorough", "parity") or d in problem_dirs
            if set_name == INDEX_PARITY_BASENAME:
                rows = _index_rows(root)
                policy = (max(50, redundancy), 20)   # protect.index_plan: 50 % and at least 20 blocks
            else:
                rows = _records_rows(manifest, d, set_name)
                policy = (redundancy, 10)
            st, table_names = _check_set(exe, d, dabs, set_name, deep, mem, thr, obs, rows, policy,
                                         extras_by_dir.get(d, set()))
            res.sets.append(st)
            files_here = by_dir.get(d, {})
            for n in table_names:
                f = files_here.get(nfc(n))
                if f is not None:
                    covered.add(f.rel)   # type: ignore[attr-defined]
            if deep and opts.mode == "parity":
                # par2 is the only judge in this mode: its verdicts are the problems
                known = {p.rel for p in res.problems if p.kind != "extra"}
                for n in st.damaged:
                    f = files_here.get(nfc(n))
                    rel = f.rel if f is not None else ((d + "/" + n) if d else n)   # type: ignore[attr-defined]
                    if rel not in known:
                        res.problems = [p for p in res.problems if not (p.kind == "extra" and p.rel == rel)]
                        res.problems.append(FileProblem(rel, "mismatch", "par2 reports damage"))
                        known.add(rel)
                for n in st.missing:
                    rel = (d + "/" + n) if d else n
                    if rel not in known and rel not in on_disk:
                        res.problems.append(FileProblem(rel, "missing", "par2 reports it missing"))
                        known.add(rel)
            obs.progress(i + 1, len(all_sets), (d or "(top folder)") + "/" + set_name)
        # listed payload files with no parity coverage (never protected, a name
        # PAR2 cannot store, or the index is unreadable). Unlisted extras are
        # already reported as such.
        for f in scan.files:
            if f.size > 0 and f.rel not in covered and (not expected_data or f.rel in expected_data):
                res.unprotected.append(f.rel)

    recompute_verdict(res)
    res.duration_s = time.time() - t0
    return res


def recompute_verdict(res: VerifyResult) -> str:
    """Derive the three-way verdict from the result's contents (also used after
    Repair removes the backups it created, so the report matches the disk)."""
    data_bad = any(p.kind in ("mismatch", "missing", "unreadable") for p in res.problems)
    fatal_sets = any(s.status in ("unrepairable", "unusable") for s in res.sets)
    attention_sets = any(s.status in ("repairable", "error", "degraded", "stale") for s in res.sets)
    extra = any(p.kind == "extra" for p in res.problems)
    # Sets nobody opened: in parity mode that is the whole check not done (a
    # problem); in standard/thorough it is a check that fell short (attention);
    # in hashes mode it is by design.
    not_checked = any(s.status == "not-checked" for s in res.sets)
    if (data_bad or (res.sums_status in ("missing", "unreadable") and res.mode != "parity") or fatal_sets
            or (not_checked and res.mode == "parity")):
        res.verdict = "problem"
    elif (attention_sets or extra or res.unprotected or res.parity_problems or res.sums_status == "from-manifest" or res.errors
            or (not_checked and res.mode != "hashes")):
        res.verdict = "attention"
    else:
        res.verdict = "ok"
    return res.verdict


def _index_rows(root: str) -> Optional[Set[Tuple[str, int, str]]]:
    """(NFC name, size, md5) PARITY-INDEX should cover right now: the records
    as they are on disk. A set that covers older records is stale."""
    from .hashing import digest_file
    rows: Set[Tuple[str, int, str]] = set()
    for name in (SUMS_FILE,) + ROOT_TOOL_FILES:
        p = os.path.join(root, name)
        try:
            size = os.path.getsize(p)
            if size > 0:
                rows.add((nfc(name), size, digest_file(p).md5))
        except OSError:
            continue
    return rows or None


def _records_rows(manifest: Optional[Manifest], d: str, set_name: str) -> Optional[Set[Tuple[str, int, str]]]:
    """(NFC name, size, md5) the records say the payload set *set_name* in
    folder *d* should cover, or None when the records cannot say (no manifest,
    no MD5s). A folder with more files than fit one set is split exactly the
    way Protect splits it (protect.chunk_rows), so every set of a split folder
    is judged; a set beyond the last chunk should not exist (empty rows)."""
    if manifest is None:
        return None
    from .protect import chunk_rows
    entries: List[Tuple[str, FileEntry]] = []
    for rel, e in manifest.files.items():
        if _dir_of(rel) == d and e.size > 0:
            entries.append((rel.rsplit("/", 1)[-1], e))
    if any(not e.md5 for _, e in entries):
        return None
    chunks = chunk_rows([(n, e.size, e.md5) for n, e in entries if not name_problem(n)])
    for i, chunk in enumerate(chunks):
        if payload_set_name(i) == set_name:
            return {(nfc(n), s, m) for n, s, m in chunk}
    return set()


def _check_set(exe: str, d: str, dabs: str, set_name: str, deep: bool, mem: int, thr: int, obs: Observer,
               rows: Optional[Set[Tuple[str, int, str]]], policy: Tuple[int, int],
               extras: Optional[Set[str]] = None) -> Tuple[SetStatus, List[str]]:
    """Cheap packet validation of every file of the set (always), a currency
    check against the records, and par2's own verify when *deep* (or when the
    set knows a file the records do not: *extras*, NFC names). *policy* is
    (redundancy percent, minimum recovery blocks) the set was planned with.
    Returns the status and the names in the set's file table (for coverage)."""
    redundancy, min_rec = policy
    files = p2.set_files_on_disk(dabs, set_name)
    if not files:
        return SetStatus(d, set_name, "degraded", message="all parity files are missing"), []
    bad = 0
    rec = 0
    short_files: List[str] = []
    foreign: List[str] = []
    scans: List[Tuple[str, p2.Par2Scan]] = []
    for n in files:
        try:
            s = p2.scan_par2_file(os.path.join(dabs, n))
        except OSError as e:
            return SetStatus(d, set_name, "error", message=f"{n}: {e}"), []
        scans.append((n, s))
    table_names: List[str] = []
    expected_rec = 0
    stale = ""
    index_path = os.path.join(dabs, set_name + p2.PAR2_SUFFIX)
    idx = None
    for n in sorted(files, key=lambda x: x != set_name + p2.PAR2_SUFFIX):
        try:
            idx = p2.read_index(os.path.join(dabs, n))
            break
        except (p2.Par2Error, OSError):
            continue
    set_id = idx.set_id if idx is not None else None
    for n, s in scans:
        if set_id is not None and s.set_id is not None and s.set_id != set_id:
            foreign.append(n)      # a same-named file from another set: par2 cannot use its blocks here
            continue
        bad += s.packets_bad + (1 if s.truncated else 0)
        rec += s.recovery_blocks
        named = p2.recovery_blocks_in_name(n)
        if named is not None and s.recovery_blocks < named:
            short_files.append(n)
    if idx is not None:
        table_names = [f.name for f in idx.file_table]
        if idx.block_size:
            blocks = sum(-(-f.size // idx.block_size) for f in idx.file_table)
            expected_rec = min(blocks, max(-(-blocks * max(1, redundancy) // 100), min(blocks, min_rec)))
        if rows is not None:
            table = {(nfc(f.name), f.size, f.md5.hex()) for f in idx.file_table}
            if table != rows:
                stale = "covers different files or contents than the records; run Protect to rebuild it"
        if extras and any(nfc(n) in extras for n in table_names):
            deep = True
    st = SetStatus(d, set_name, "ok", recovery_blocks=rec, recovery_expected=expected_rec, packets_bad=bad)
    notes: List[str] = []
    if not os.path.isfile(index_path):
        notes.append("index file missing (par2 can still use the volumes)")
    if foreign:
        notes.append(f"{len(foreign)} parity files belong to a different parity set ({foreign[0]})")
    if bad:
        notes.append(f"{bad} damaged packets in the parity files")
    if short_files:
        notes.append(f"{len(short_files)} volume files hold fewer recovery blocks than their names say")
    by_name = p2.recovery_blocks_by_name(dabs, set_name) - sum(p2.recovery_blocks_in_name(n) or 0 for n in foreign)
    if rec < by_name:
        notes.append(f"{rec} of {by_name} recovery blocks intact")
    elif expected_rec and rec < expected_rec:
        notes.append(f"{rec} of {expected_rec} recovery blocks present (volume files missing)")
    if deep:
        v = p2.verify(exe, dabs, set_name, memory_mb=mem, threads=thr, observer=obs)
        st.damaged = v.damaged
        st.missing = v.missing
        if v.status != "ok":
            st.status = v.status
            if v.status == "unrepairable":
                st.message = f"needs {v.needs_blocks} more recovery blocks than exist" if v.needs_blocks else "not enough recovery data"
            elif v.status == "unusable":
                st.message = "the parity files themselves are too damaged to use"
            elif v.status == "error":
                st.message = v.message.splitlines()[-1] if v.message else f"par2 exit {v.returncode}"
            if notes:
                st.message = (st.message + "; " if st.message else "") + "; ".join(notes)
            return st, table_names
    if notes:
        st.status = "degraded"
        st.message = "; ".join(notes) + "; run Protect to rebuild this folder's parity"
    elif stale:
        st.status = "stale"
        st.message = stale
    return st, table_names
