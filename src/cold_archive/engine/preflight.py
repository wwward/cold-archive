"""Preflight: everything that should be known before a long job starts.

Reports facts and lists errors/warnings; it never raises for a warning. The
caller (CLI or GUI) decides whether to proceed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import par2 as p2
from .layout import MANIFEST_FILE, TOOLS_DIR, name_problem, human_bytes
from .observer import NullObserver, Observer
from .scan import scan_archive

FAT32_MAX_FILE = (4 << 30) - 1
_WINDOWS_ILLEGAL = set('<>:"|?*') | {chr(c) for c in range(32)}
# Names Windows refuses in any folder, with or without an extension.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
# MEASURED (2026-09-04, par2cmdline 1.3.0): the records of an archive come to
# about 470 bytes per file (manifest + SHA256SUMS lines) plus the kit.
RECORD_BYTES_PER_FILE = 470
RECORD_BYTES_PER_DIR = 1080


@dataclass
class PreflightReport:
    source: str
    target: str
    in_place: bool
    file_count: int = 0
    dir_count: int = 0
    payload_bytes: int = 0
    largest_file: Tuple[str, int] = ("", 0)
    symlinks: int = 0
    junk: int = 0
    scan_errors: int = 0
    parity_estimate_bytes: int = 0
    records_estimate_bytes: int = 0     # manifest, SHA256SUMS, kit, and PARITY-INDEX over them
    tools_estimate_bytes: int = 0       # TOOLS/ (par2 source) and its parity
    already_on_destination_bytes: int = 0   # counted off: identical files from an interrupted copy, parity already built
    total_required_bytes: int = 0
    available_bytes: Optional[int] = None
    # Create protects the source where it is before copying, so its parity and
    # records land on the SOURCE volume first; these are that step's numbers
    # (zero and None when protecting in place, where the two volumes are one).
    source_required_bytes: int = 0
    source_available_bytes: Optional[int] = None
    same_volume: bool = False               # source and destination share one pool of free space
    target_fs: Optional[str] = None
    par2_path: Optional[str] = None
    par2_version: str = ""
    redundancy_percent: int = 10
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def robustness_text(self) -> str:
        r = self.redundancy_percent
        return (f"With {r}% repair data, each folder can lose up to about {r}% of its contents "
                f"(roughly {human_bytes(self.payload_bytes * r / 100)} across the whole archive) to bad sectors, "
                "damaged or missing files, and still be rebuilt completely. Damage beyond that in any one folder "
                "cannot be repaired from this copy alone; keep a second copy.")

    def summary(self) -> str:
        lines = [("READY: " if self.ok else "NOT READY: ") + (f"{self.source} -> {self.target}" if not self.in_place else f"protect {self.source} in place"),
                 f"  {self.file_count:,} files in {self.dir_count:,} folders, {human_bytes(self.payload_bytes)}; "
                 f"largest file {human_bytes(self.largest_file[1])}",
                 f"  repair data about {human_bytes(self.parity_estimate_bytes)}, records about {human_bytes(self.records_estimate_bytes)}"
                 + (f", tools about {human_bytes(self.tools_estimate_bytes)}" if self.tools_estimate_bytes else "")
                 + (f"; {human_bytes(self.already_on_destination_bytes)} already there" if self.already_on_destination_bytes else ""),
                 f"  needs about {human_bytes(self.total_required_bytes)} on the destination; "
                 + (f"{human_bytes(self.available_bytes)} free" if self.available_bytes is not None else "free space unknown")
                 + (f" ({self.target_fs})" if self.target_fs else "")]
        if not self.in_place:
            lines.append(f"  needs about {human_bytes(self.source_required_bytes)} on the source volume first "
                         "(its repair data and records are made there, then copied); "
                         + ("same volume as the destination" if self.same_volume
                            else f"{human_bytes(self.source_available_bytes)} free" if self.source_available_bytes is not None
                            else "free space unknown"))
        lines.append(f"  par2: {self.par2_version} at {self.par2_path}" if self.par2_path else "  par2: NOT FOUND")
        for e in self.errors:
            lines.append("  ERROR: " + e)
        for w in self.warnings:
            lines.append("  warning: " + w)
        for n in self.notes:
            lines.append("  note: " + n)
        lines.append("  " + self.robustness_text())
        return "\n".join(lines)


def filesystem_type(path: str) -> Optional[str]:
    """Best-effort filesystem type of the volume holding *path* ('apfs', 'exfat', 'msdos', 'ntfs', ...)."""
    try:
        real = os.path.realpath(path)
        if sys.platform == "darwin":
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
            best: Tuple[int, Optional[str]] = (-1, None)
            for line in out.splitlines():
                # "/dev/disk3s1 on /Volumes/Archive (exfat, local, nodev, nosuid, noowners)"
                if " on " not in line or " (" not in line:
                    continue
                _, rest = line.split(" on ", 1)
                mp, opts = rest.rsplit(" (", 1)
                fstype = opts.split(",", 1)[0].strip(")")
                if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
                    if len(mp) > best[0]:
                        best = (len(mp), fstype.lower())
            return best[1]
        if os.path.exists("/proc/mounts"):
            best = (-1, None)
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mp, fstype = parts[1].replace("\\040", " "), parts[2]
                    if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
                        if len(mp) > best[0]:
                            best = (len(mp), fstype.lower())
            return best[1]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def estimate_parity_bytes(scan, redundancy_percent: int) -> int:
    total = 0
    for sd in scan.dirs:
        sizes = sorted(f.size for f in sd.files if f.size > 0 and not name_problem(f.name))
        for i in range(0, len(sizes), p2.MAX_FILES_PER_SET):
            chunk = sizes[i:i + p2.MAX_FILES_PER_SET]
            try:
                plan = p2.plan_blocks(chunk, redundancy_percent)
            except ValueError:
                continue
            total += p2.estimate_set_bytes(chunk, plan)
    return total


def destination_folds_names(directory: str) -> bool:
    """True if *directory* lives on a filesystem that treats names differing
    only in case, or only in Unicode spelling (NFC/NFD), as the same file:
    default APFS, HFS+, exFAT, FAT, NTFS, and case-sensitive APFS for the
    spelling half. Probed, because there is no portable way to ask."""
    import unicodedata
    made: List[str] = []
    try:
        fd, upper = tempfile.mkstemp(prefix=".ColdArchiveCASE-", dir=directory)
        os.close(fd)
        made.append(upper)
        lower = os.path.join(os.path.dirname(upper), os.path.basename(upper).lower())
        if lower != upper and os.path.exists(lower):
            return True
        nfc_name = os.path.join(directory, ".ColdArchiveNORM-" + os.path.basename(upper) + "-\u00e9")
        with open(nfc_name, "wb"):
            pass
        made.append(nfc_name)
        nfd_name = unicodedata.normalize("NFD", nfc_name)
        return nfd_name != nfc_name and os.path.exists(nfd_name)
    except OSError:
        return True    # assume the cautious answer
    finally:
        for p in made:
            try:
                os.remove(p)
            except OSError:
                pass


def _fold(rel: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", rel).casefold()


def name_collisions(scan) -> List[Tuple[str, str]]:
    """Pairs of relative paths (files or folders) that fold to the same name,
    so that on a case- or spelling-insensitive destination the second would
    land on the first: two files, two folders (their contents would merge),
    or a file and a folder."""
    seen: Dict[str, str] = {}
    out: List[Tuple[str, str]] = []
    reported: set = set()
    dirs = getattr(scan, "dirs", [])
    entries = [sd.rel for sd in dirs if sd.rel] + [f.rel for f in scan.files]
    for rel in entries:      # scan order: the first spelling seen is the one that stays
        key = _fold(rel)
        first = seen.get(key)
        if first is not None and first != rel:
            out.append((first, rel))
            reported.add(rel)
        else:
            seen.setdefault(key, rel)
    # a file under a folder that itself collides lands in the wrong place too
    dir_clash = {rel for _, rel in out if any(sd.rel == rel for sd in dirs)}
    for f in scan.files:
        if f.rel not in reported and any(f.rel.startswith(d + "/") for d in dir_clash):
            out.append((next(a for a, b in out if b in dir_clash and f.rel.startswith(b + "/")), f.rel))
    return out


def estimate_records_bytes(file_count: int, dir_count: int, redundancy_percent: int) -> int:
    """Manifest + SHA256SUMS + kit, and PARITY-INDEX over them (50 %, at least
    20 recovery blocks, critical packets repeated per volume)."""
    base = file_count * RECORD_BYTES_PER_FILE + dir_count * RECORD_BYTES_PER_DIR + 64 * 1024
    try:
        plan = p2.plan_blocks([base], max(50, redundancy_percent), target_blocks=200, min_block_size=16 * 1024,
                              min_recovery_blocks=20, overhead_tolerance=1.0)
        return base + p2.estimate_set_bytes([base], plan)
    except ValueError:
        return base


def estimate_tools_bytes(redundancy_percent: int) -> int:
    """The TOOLS folder (par2 source tarball, licence, notes) and its parity."""
    from .resources import par2_source_tarball
    tarball = par2_source_tarball()
    if not tarball:
        return 0
    try:
        sizes = [os.path.getsize(tarball), 18_000, 1_500]
    except OSError:
        return 0
    try:
        plan = p2.plan_blocks(sizes, redundancy_percent)
        return sum(sizes) + p2.estimate_set_bytes(sizes, plan)
    except ValueError:
        return sum(sizes)


def largest_volume_estimate(scan, redundancy_percent: int) -> Tuple[str, int]:
    """(folder rel, bytes) of the biggest single recovery volume file any
    folder's parity would produce (par2 is asked for up to 8 uniform files)."""
    best = ("", 0)
    for sd in scan.dirs:
        sizes = [f.size for f in sd.files if f.size > 0 and not name_problem(f.name)]
        for i in range(0, len(sizes), p2.MAX_FILES_PER_SET):
            try:
                plan = p2.plan_blocks(sizes[i:i + p2.MAX_FILES_PER_SET], redundancy_percent)
            except ValueError:
                continue
            n_vol = max(1, min(8, plan.recovery_blocks))
            per = -(-plan.recovery_blocks // n_vol) * plan.block_size
            if per > best[1]:
                best = (sd.rel, per)
    return best


def windows_unopenable(scan) -> List[str]:
    """Relative paths (files and folders) that Windows cannot open: illegal
    characters, a trailing dot or space, or a reserved device name in any
    path component. macOS stores all of these happily on exFAT and FAT; it is
    Windows that refuses them."""
    def bad(component: str) -> bool:
        stem = component.split(".", 1)[0].upper()
        return (any(c in _WINDOWS_ILLEGAL for c in component) or component.rstrip(" .") != component
                or stem in _WINDOWS_RESERVED)
    out = []
    for sd in scan.dirs:
        if sd.rel and bad(sd.rel.rsplit("/", 1)[-1]):
            out.append(sd.rel)
    for f in scan.files:
        if bad(f.name):
            out.append(f.rel)
    return sorted(out)


def _writable(directory: str) -> Optional[str]:
    try:
        fd, tmp = tempfile.mkstemp(prefix=".coldarchive-writetest-", dir=directory)
        os.close(fd)
        os.remove(tmp)
        return None
    except OSError as e:
        return str(e)


def _write_permitted(directory: str) -> Optional[str]:
    """Writability asked without writing, for the in-place case, where the
    folder is the source itself. A test file, even one removed at once,
    changes the folder's date: found on 2026-10-19 after an
    in-place preflight touched a live Lightroom tree whose owner had been
    told preflight only reads. access(2) answers for permissions; statvfs
    for a read-only volume. Protect fails loudly anyway if this is wrong."""
    try:
        if os.statvfs(directory).f_flag & getattr(os, "ST_RDONLY", 1):
            return "the volume is read-only"
    except OSError:
        pass
    if not os.access(directory, os.W_OK | os.X_OK):
        return "permission denied"
    return None


def _parity_bytes_present(scan) -> int:
    """Bytes of parity already on disk in the scanned tree (an earlier Protect
    of the same folder), which a new run keeps rather than makes again."""
    total = 0
    for sd in scan.dirs:
        for names in sd.parity_sets.values():
            for n in names:
                try:
                    total += os.path.getsize(os.path.join(sd.abspath, n))
                except OSError:
                    pass
    return total


def _same_volume(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _space_margin(need: int) -> int:
    return max(16 << 20, min(64 << 20, need // 100))


def _nearest_existing(path: str) -> str:
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def run_preflight(source: str, target: Optional[str] = None, redundancy_percent: int = 10,
                  par2_path: Optional[str] = None, observer: Optional[Observer] = None) -> PreflightReport:
    obs = observer or NullObserver()
    source = os.path.abspath(source)
    in_place = target is None or os.path.abspath(target) == source
    target = source if in_place else os.path.abspath(target)
    rep = PreflightReport(source=source, target=target, in_place=in_place, redundancy_percent=redundancy_percent)

    if not os.path.isdir(source):
        rep.errors.append(f"the source folder does not exist or is not a folder: {source}")
        return rep
    if not os.access(source, os.R_OK | os.X_OK):
        rep.errors.append(f"the source folder cannot be read: {source}")
    if redundancy_percent < 1:
        rep.errors.append("repair data must be at least 1 percent")
    elif redundancy_percent < 5:
        rep.warnings.append(f"{redundancy_percent}% repair data is thin; 10% is the usual choice for archives")
    elif redundancy_percent > 50:
        rep.warnings.append(f"{redundancy_percent}% repair data will take a long time to create and use a lot of space")

    try:
        rep.par2_path = p2.find_par2(par2_path)
        rep.par2_version = p2.par2_version(rep.par2_path)
    except p2.Par2NotFound as e:
        rep.errors.append(str(e))

    if not in_place:
        s, t = os.path.realpath(source), os.path.realpath(target)
        if t.startswith(s + os.sep):
            rep.errors.append("the destination is inside the source folder")
        elif s.startswith(t + os.sep):
            rep.errors.append("the source is inside the destination folder")
        if os.path.exists(target) and not os.path.isdir(target):
            rep.errors.append(f"the destination exists but is not a folder: {target}")

    scan = scan_archive(source, obs)
    rep.file_count = len(scan.files)
    rep.dir_count = len(scan.dirs)
    rep.payload_bytes = scan.total_bytes
    rep.symlinks = len(scan.symlinks)
    rep.junk = len(scan.junk_skipped)
    rep.scan_errors = len(scan.errors)
    if scan.files:
        big = max(scan.files, key=lambda f: f.size)
        rep.largest_file = (big.rel, big.size)
    if scan.errors:
        rep.errors.append(f"{len(scan.errors)} entries in the source cannot be read (for example {scan.errors[0][0] or '(top folder)'}: {scan.errors[0][1]})")
    if scan.symlinks:
        rep.warnings.append(f"{len(scan.symlinks)} symbolic links will be skipped (an archive only holds real files)")
    if rep.file_count == 0:
        rep.errors.append("the source folder contains no files to archive")
    source_is_archive = os.path.isfile(os.path.join(source, MANIFEST_FILE))
    if scan.reserved and not in_place:
        if source_is_archive:
            rep.notes.append(f"the source is itself a Cold Archive: its {len(scan.reserved)} record and parity files are not copied; fresh ones are made")
        else:
            rep.errors.append(f"{len(scan.reserved)} files in the source carry names reserved for the archive's own files "
                              f"(for example {scan.reserved[0]!r}); rename them, or they would be left out of the copy")
    unreadable = [f.rel for f in scan.files if not os.access(f.abspath, os.R_OK)]
    if unreadable:
        rep.errors.append(f"{len(unreadable)} files cannot be read (for example {unreadable[0]!r}); fix their permissions first")

    # Estimates from the real block policy, folder by folder: par2 repeats its
    # per-file packets in every recovery volume, so folders of many small
    # files cost far more than "10 % of the data" (see par2.estimate_set_bytes).
    rep.parity_estimate_bytes = estimate_parity_bytes(scan, redundancy_percent)
    rep.records_estimate_bytes = estimate_records_bytes(rep.file_count, rep.dir_count, redundancy_percent)
    rep.tools_estimate_bytes = estimate_tools_bytes(redundancy_percent)

    bad_names = [(f.rel, name_problem(f.name)) for f in scan.files if name_problem(f.name)]
    if bad_names:
        rep.errors.append(f"{len(bad_names)} file names cannot be protected by PAR2 (for example {bad_names[0][0]!r}: {bad_names[0][1]}); "
                          "rename them first")

    probe = _nearest_existing(target)
    dest_files: Dict[str, int] = {}
    dest_has_archive = False
    if not in_place and os.path.isdir(target):
        try:
            dest_scan = scan_archive(target, obs)
            dest_files = {f.rel: f.size for f in dest_scan.files}
            dest_has_archive = os.path.isfile(os.path.join(target, MANIFEST_FILE))
            if dest_has_archive:
                rep.warnings.append("the destination already holds a Cold Archive: files with the same names are replaced by the "
                                    "source's versions and the records are updated to match (fine when resuming or refreshing "
                                    "this same archive; choose an empty folder for a new one)")
            elif dest_files or dest_scan.dirs[1:] if dest_scan.dirs else False:
                rep.warnings.append(f"the destination is not empty ({len(dest_files)} files already there); they become part of the archive "
                                    "and same-named files are replaced by the source's versions")
        except OSError:
            pass
    # What is already on the destination is not needed again: identical files
    # from an interrupted copy, and (in place) the parity and records already built.
    if in_place:
        rep.already_on_destination_bytes = _parity_bytes_present(scan)
    else:
        rep.already_on_destination_bytes = sum(f.size for f in scan.files if dest_files.get(f.rel) == f.size)
    need = (0 if in_place else rep.payload_bytes) + rep.parity_estimate_bytes + rep.records_estimate_bytes + rep.tools_estimate_bytes
    need = max(0, need - rep.already_on_destination_bytes)
    rep.total_required_bytes = need + _space_margin(need)
    try:
        rep.available_bytes = shutil.disk_usage(probe).free
    except OSError as e:
        rep.warnings.append(f"free space on the destination could not be determined: {e}")
    rep.target_fs = filesystem_type(probe)

    if not in_place:
        # Create protects the source where it is and only then copies
        # (create.py), so the parity, records and tools are written to the
        # SOURCE volume first. Without this check that step fails partway,
        # hours in, with nothing copied: a 12.76 TB masters tree on a volume
        # with 1.50 TB free (measured 2026-10-19) needs more than
        # that for its parity alone. Parity already there from an earlier
        # Protect is kept, so a second Create of a protected source needs
        # almost nothing.
        src_need = rep.parity_estimate_bytes + rep.records_estimate_bytes + rep.tools_estimate_bytes
        src_need = max(0, src_need - _parity_bytes_present(scan))
        rep.source_required_bytes = src_need + _space_margin(src_need)
        rep.same_volume = _same_volume(source, probe)
        if rep.same_volume:
            rep.source_available_bytes = rep.available_bytes
        else:
            try:
                rep.source_available_bytes = shutil.disk_usage(source).free
            except OSError as e:
                rep.warnings.append(f"free space on the source volume could not be determined: {e}")

    if rep.same_volume and rep.available_bytes is not None:
        both = rep.total_required_bytes + rep.source_required_bytes
        if both > rep.available_bytes:
            rep.errors.append(f"not enough space: the source and the destination are on one volume, which needs about "
                              f"{human_bytes(both)} for the source's repair data plus the copy, and has "
                              f"{human_bytes(rep.available_bytes)} free")
    else:
        if rep.available_bytes is not None and rep.total_required_bytes > rep.available_bytes:
            rep.errors.append(f"not enough space on the destination: about {human_bytes(rep.total_required_bytes)} needed, "
                              f"{human_bytes(rep.available_bytes)} free")
        if rep.source_available_bytes is not None and rep.source_required_bytes > rep.source_available_bytes:
            rep.errors.append(f"not enough space on the source volume: Create makes the source's repair data and records "
                              f"there before copying, about {human_bytes(rep.source_required_bytes)}, and it has "
                              f"{human_bytes(rep.source_available_bytes)} free; free space on that volume first")
    if os.path.isdir(probe):
        # In place the probe IS the source: ask, never write (see _write_permitted).
        # With a destination, a real test file (made and removed there) is the
        # only reliable answer for SMB, ACLs and odd mounts; Create writes there anyway.
        err = _write_permitted(probe) if in_place else _writable(probe)
        if err:
            where = "the folder" if in_place else "the destination"
            rep.errors.append(f"{where} cannot be written to ({err})" + (" ; macOS cannot write NTFS volumes without extra software" if rep.target_fs == "ntfs" else ""))

    fs = rep.target_fs or ""
    if fs in ("msdos", "fat", "fat32", "vfat"):
        if rep.largest_file[1] > FAT32_MAX_FILE:
            rep.errors.append(f"the destination is FAT32, which cannot hold files over 4 GB; {rep.largest_file[0]} is {human_bytes(rep.largest_file[1])}")
        big_vol = largest_volume_estimate(scan, redundancy_percent)
        if big_vol[1] > FAT32_MAX_FILE:
            rep.errors.append(f"the destination is FAT32, which cannot hold files over 4 GB; the repair data of the folder "
                              f"{big_vol[0] or '(top folder)'!r} would come in files of about {human_bytes(big_vol[1])}")
        rep.warnings.append("the destination is FAT32: timestamps are rounded to 2 seconds and file names are restricted; exFAT or APFS is a better choice")
    elif fs == "exfat":
        rep.notes.append("the destination is exFAT: fine for an archive and readable on Mac, Windows and Linux; "
                         "macOS will add invisible ._ files that the archive ignores; always eject before unplugging")
    elif fs == "ntfs":
        rep.warnings.append("the destination is NTFS: macOS can read it but normally cannot write it")
    if fs in ("msdos", "fat", "fat32", "vfat", "exfat", "ntfs"):
        bad = windows_unopenable(scan)
        if bad:
            rep.errors.append(f"{len(bad)} names could not be opened on Windows (for example {bad[0]!r}: characters like : ? * \" < > |, "
                              "a trailing dot or space, or a reserved device name); rename them, or choose an APFS destination "
                              "if no Windows user will ever need the archive")
    if not in_place and os.path.isdir(probe) and destination_folds_names(probe):
        clashes = name_collisions(scan)
        if clashes:
            a, b = clashes[0]
            rep.errors.append(f"{len(clashes)} pairs of names would land on the same name on this destination, which does not "
                              f"distinguish upper/lower case or accent spellings (for example {a!r} and {b!r}); rename them or choose a case-sensitive destination")

    if rep.payload_bytes > 200 << 30:
        rep.notes.append("large archive: expect roughly 10 to 20 seconds per gigabyte to build repair data on a fast Mac, "
                         "more on slow external drives; the job can run overnight and can be resumed")
    return rep
