"""par2cmdline wrapper: locating the binary, planning block sizes, running
create / verify / repair with streamed progress, and reading PAR2 files
directly (packet-level validation and the file table of an index).

Every fact about par2cmdline encoded here was measured against version 1.3.0
on 2026-09-03; see the comments marked MEASURED. Re-checked against 1.4.0 on
2026-10-19: the full suite passes on it unchanged, and sets made by either
version repair byte-identically with the other. 1.4.0 no longer hashes whole
files during verify unless asked (--full-hash), which this tool never relied
on; its own SHA-256 records do that job.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .layout import BUILD_DIR_PREFIX, PAR2_SUFFIX, nfc

# Temporary names the finished parity files get inside the target folder
# while an old set is being replaced (see create). Covered by layout.is_junk.
_SWAP_PREFIX = ".cold-archive-new-"
_BUILD_DIR_RE = re.compile(r"^" + re.escape(BUILD_DIR_PREFIX) + r"\d+$")
LIST_FILE_NAME = "names.lst"   # the @list file par2 reads the source names from, inside the build folder
from .observer import Cancelled, NullObserver, Observer

# MEASURED: par2cmdline refuses a set with more than 32768 source blocks
# ("Too many source blocks (40000 > 32768)", exit 3). Keep headroom.
MAX_SOURCE_BLOCKS = 32768
SAFE_MAX_BLOCKS = 32000
# Cap on files per set so that block size can stay near the file size; see
# plan_blocks. 4000 files leaves room for ~8 blocks per file under the cap.
MAX_FILES_PER_SET = 4000
# MEASURED: block size must be a multiple of 4 ("Block size must be a multiple
# of 4.", exit 3). We use 4 KiB multiples so blocks align with sectors.
BLOCK_ALIGN = 4096

# MEASURED exit codes of par2cmdline 1.3.0 (src/libpar2internal.h Result enum):
EXIT_OK = 0                    # success / all files correct / repair complete
EXIT_REPAIR_POSSIBLE = 1       # verify: damage found, enough recovery blocks
EXIT_REPAIR_NOT_POSSIBLE = 2   # verify/repair: not enough recovery blocks
EXIT_INVALID_ARGS = 3          # bad options, missing par2 file, empty file list
EXIT_INSUFFICIENT_CRITICAL = 4 # the par2 files themselves are too damaged to use
EXIT_REPAIR_FAILED = 5
EXIT_FILE_IO = 6
EXIT_LOGIC = 7
EXIT_MEMORY = 8

_KNOWN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")


class Par2Error(Exception):
    pass


class Par2NotFound(Par2Error):
    pass


def _fast_binary_usable(bin_dir: str) -> bool:
    """``par2-fast`` is a threaded build for one architecture and a minimum
    macOS, recorded next to it as ``par2-fast.minos`` ("26.0 arm64"). Only use
    it when this machine satisfies both; the universal ``par2`` is the fallback."""
    try:
        with open(os.path.join(bin_dir, "par2-fast.minos"), "r", encoding="ascii") as f:
            minos, arch = f.read().split()[:2]
        import platform
        if platform.system() != "Darwin" or platform.machine() != arch:
            return False
        have = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2] if x.isdigit())
        need = tuple(int(x) for x in minos.split(".")[:2] if x.isdigit())
        return bool(have) and have >= need
    except (OSError, ValueError):
        return False


def bundled_par2_candidates() -> List[str]:
    """Locations a bundled par2 may live in when running frozen (PyInstaller),
    the threaded build first where it can run."""
    out = []
    dirs = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(os.path.join(base, "bin"))
        # .app layout: Contents/Frameworks (= _MEIPASS) and Contents/Resources
        dirs.append(os.path.join(os.path.dirname(base), "Resources", "bin"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    dirs.append(os.path.join(exe_dir, "bin"))
    for d in dirs:
        if _fast_binary_usable(d):
            out.append(os.path.join(d, "par2-fast"))
        out.append(os.path.join(d, "par2"))
    if base:
        out.append(os.path.join(base, "par2"))
    out.append(os.path.join(exe_dir, "par2"))
    return out


def find_par2(explicit: Optional[str] = None) -> str:
    """Return the path of a usable par2 binary or raise Par2NotFound.

    Order: explicit argument, $COLD_ARCHIVE_PAR2, a binary bundled with the
    app, PATH, then the usual package-manager locations (a Finder-launched app
    has PATH=/usr/bin:/bin:/usr/sbin:/sbin and would otherwise never see
    Homebrew's /opt/homebrew/bin).
    """
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("COLD_ARCHIVE_PAR2")
    if env:
        candidates.append(env)
    candidates.extend(bundled_par2_candidates())
    w = shutil.which("par2")
    if w:
        candidates.append(w)
    for d in _KNOWN_DIRS:
        candidates.append(os.path.join(d, "par2"))
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise Par2NotFound(
        "The par2 program (par2cmdline) was not found. Install it with 'brew install par2' "
        "on macOS, 'apt install par2' on Debian/Ubuntu, or download it from "
        "https://github.com/Parchive/par2cmdline/releases, or set COLD_ARCHIVE_PAR2 to its path."
    )


def par2_version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "-V"], capture_output=True, text=True, timeout=20)
        line = (r.stdout or r.stderr).strip().splitlines()
        return line[0] if line else "unknown"
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown ({e})"


_supports_threads_cache: Dict[str, bool] = {}

def supports_threads(exe: str) -> bool:
    if exe in _supports_threads_cache:
        return _supports_threads_cache[exe]
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=10)
        out = (r.stdout or r.stderr)
        _supports_threads_cache[exe] = "-t<" in out or " -t" in out
        return _supports_threads_cache[exe]
    except (OSError, subprocess.SubprocessError):
        return False

# --- block planning ---------------------------------------------------------

@dataclass(frozen=True)
class BlockPlan:
    block_size: int
    source_blocks: int
    recovery_blocks: int
    parity_bytes: int  # recovery_blocks * block_size, an upper bound on parity size


def _round_up(n: int, align: int) -> int:
    return ((n + align - 1) // align) * align


def plan_blocks(sizes: Iterable[int], redundancy_percent: int, *, target_blocks: int = 2000,
                min_block_size: int = 64 * 1024, max_block_size: int = 1 << 30,
                min_recovery_blocks: int = 10, overhead_tolerance: float = 0.25) -> BlockPlan:
    """Choose a PAR2 block size and recovery-block count for one set.

    PAR2 pads every file to whole blocks, and each recovery block is a full
    block, so when blocks are larger than the files "10 % of blocks" costs far
    more than 10 % of the data (MEASURED reasoning: 20,000 files of 5 MB with
    par2's default 2000-block target gives 50 MB blocks, 20,000 source blocks
    and 2,000 recovery blocks = 100 GB of parity for 100 GB of data). So we
    evaluate a ladder of block sizes and keep those whose parity stays within
    ``overhead_tolerance`` of the nominal redundancy; among them we pick the
    block count closest to ``target_blocks`` (create time grows linearly with
    recovery blocks: MEASURED 11 s / 19 s / 26 s per GB at 200 / 400 / 800).
    Tiny sets get at least ``min_recovery_blocks`` (capped at the source block
    count) so a small folder can lose whole small files and still be rebuilt.
    """
    sizes = [int(s) for s in sizes if s > 0]
    n = len(sizes)
    if n == 0:
        raise ValueError("no non-empty files to protect")
    if n > SAFE_MAX_BLOCKS:
        raise ValueError(f"{n} files exceed the per-set limit; split the set first")
    r = max(1, int(redundancy_percent))
    total = sum(sizes)
    nominal = total * r / 100.0

    candidates: List[Tuple[int, int, int, int]] = []
    bs = _round_up(max(BLOCK_ALIGN, min_block_size), BLOCK_ALIGN)
    while True:
        blocks = sum((s + bs - 1) // bs for s in sizes)
        if blocks <= SAFE_MAX_BLOCKS:
            rec = max(-(-blocks * r // 100), min(blocks, min_recovery_blocks))
            rec = min(rec, blocks, 65535)
            candidates.append((bs, blocks, rec, rec * bs))
        # Once every file fits in one block, larger blocks only inflate parity.
        if blocks == n or bs >= max_block_size:
            break
        bs *= 2
    if not candidates:
        raise ValueError("cannot fit files into a PAR2 set")

    import math
    within = [c for c in candidates if c[3] <= nominal * (1 + overhead_tolerance) + BLOCK_ALIGN]
    if within:
        best = min(within, key=lambda c: (abs(math.log(c[1] / target_blocks)), c[0]))
    else:
        best = min(candidates, key=lambda c: (c[3], c[0]))
    return BlockPlan(block_size=best[0], source_blocks=best[1], recovery_blocks=best[2], parity_bytes=best[3])


def estimate_set_bytes(sizes: Sequence[int], plan: BlockPlan, name_bytes: int = 24, recovery_files: int = 8) -> int:
    """Bytes a set will occupy on disk: recovery blocks plus the critical
    packets (file descriptions and per-block checksums) that par2cmdline
    repeats in every recovery file, several times over.

    MEASURED (par2cmdline 1.3.0, 2026-09-04): 2000 files of 5 KB at 10 %
    came out at 2.55x the recovery bytes, 3000 files of 1 KB at 2.86x, 200
    files of 100 KB at 1.5x, 20 files of 5 MB at 1.15x. The fit below is
    within about 35 % of those and errs high.
    """
    n_vol = max(1, min(recovery_files, plan.recovery_blocks, 31))
    per_file = max(1, plan.recovery_blocks / n_vol)
    import math
    copies = 1 + n_vol * (1.5 + math.log2(max(1.0, per_file)))
    critical = 0
    for s in sizes:
        blocks = -(-s // plan.block_size) if s > 0 else 0
        critical += 64 + 56 + name_bytes + 64 + 16 + 20 * blocks   # FileDesc + IFSC packets with headers
    critical += 64 + 12 + 16 * len(sizes) + 64 + 32                # main + creator packets
    return int(plan.parity_bytes + copies * critical * 1.3)


def chunk_files(names_and_sizes: Sequence[Tuple[str, int]], max_files: Optional[int] = None) -> List[List[Tuple[str, int]]]:
    """Split a directory's non-empty files (sorted by name) into sets of at most
    *max_files* (default MAX_FILES_PER_SET, read at call time), so block size
    can stay near file size and the 32768-block ceiling is never hit."""
    max_files = max_files or MAX_FILES_PER_SET
    items = sorted(((n, s) for n, s in names_and_sizes if s > 0), key=lambda t: t[0])
    return [items[i:i + max_files] for i in range(0, len(items), max_files)] if items else []


def default_resources(par2_workers: int) -> Tuple[int, int]:
    """(memory_mb, threads) for one par2 process given how many run at once.
    par2cmdline 1.3.0 defaults to half of physical RAM and all cores PER
    PROCESS, so two concurrent processes would claim all RAM and 2x cores."""
    cpus = os.cpu_count() or 4
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        ram = 8 << 30
    workers = max(1, par2_workers)
    mem_mb = int(ram / (1 << 20) * 0.25 / workers)
    mem_mb = max(256, min(mem_mb, 8192))
    threads = max(1, cpus // workers)
    return mem_mb, threads


# --- running par2 -----------------------------------------------------------

_PROGRESS_RE = re.compile(r"(Loading|Scanning|Processing|Constructing|Solving|Repairing|Verifying):\s*(\d+(?:\.\d+)?)%")
# MEASURED (par2cmdline 1.3.0): per-file verdict lines come in several shapes.
#   Target: "name" - found.
#   Target: "name" - damaged. Found 3 of 4 data blocks.
#   Target: "name" - missing.
#   File: "name" - no data found.            <- the file exists but NOT ONE block matches
#   File: "other" - is a match for "name".   <- a renamed copy of a missing target
# The "no data found" form replaces the Target line entirely, so a parser that
# only knows "Target:" reports a fully corrupted file as absent from the set.
_TARGET_RE = re.compile(r'^Target: "(.*)" - (found|damaged|missing)\.')
_NODATA_RE = re.compile(r'^File: "(.*)" - no data found\.')
_NEED_RE = re.compile(r"You need (\d+) more recovery blocks")
_MATCH_RE = re.compile(r'^File: "(.*)" - is a match for "(.*)"\.')


@dataclass
class Par2Result:
    action: str
    returncode: int
    status: str            # ok | repairable | unrepairable | repaired | unusable | error | cancelled
    output: str
    files: Dict[str, str] = field(default_factory=dict)   # name -> found|damaged|missing
    needs_blocks: Optional[int] = None
    message: str = ""

    @property
    def damaged(self) -> List[str]:
        return sorted(n for n, s in self.files.items() if s == "damaged")

    @property
    def missing(self) -> List[str]:
        return sorted(n for n, s in self.files.items() if s == "missing")

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "repaired")


def _run(exe: str, args: List[str], cwd: str, observer: Observer, phase_label: str) -> Tuple[int, str]:
    """Run par2 streaming its output; report percentages; kill on cancel."""
    proc = subprocess.Popen([exe] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    chunks: List[bytes] = []
    buf = b""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    try:
        while True:
            try:
                observer.check_cancel()
            except Cancelled:
                proc.kill()
                proc.wait()
                raise
            # os.read returns whatever is available; BufferedReader.read(4096)
            # would block until 4096 bytes arrived, so progress (and the cancel
            # check) only happened when par2 exited.
            data = os.read(fd, 1 << 16)
            if not data:
                break
            chunks.append(data)
            buf += data
            # par2 separates progress updates with \r and lines with \n
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                m = _PROGRESS_RE.search(p.decode("utf-8", "replace"))
                if m:
                    observer.progress(int(float(m.group(2)) * 10), 1000, f"{phase_label}: {m.group(1).lower()}")
        proc.wait()
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
    text = b"".join(chunks).decode("utf-8", "replace")
    return proc.returncode, text


def _parse_files(text: str) -> Tuple[Dict[str, str], Optional[int]]:
    files: Dict[str, str] = {}
    needs = None
    for line in re.split(r"[\r\n]", text):
        m = _TARGET_RE.match(line)
        if m:
            files[unicodedata.normalize("NFC", m.group(1))] = m.group(2)
            continue
        m = _NODATA_RE.match(line)
        if m:
            files[unicodedata.normalize("NFC", m.group(1))] = "damaged"
            continue
        m = _MATCH_RE.match(line)
        if m:
            # the target is recoverable from a renamed copy; keep "missing" so
            # callers still know the file under its recorded name is absent
            continue
        m = _NEED_RE.search(line)
        if m:
            needs = int(m.group(1))
    return files, needs


def _tail(text: str, n: int = 6) -> str:
    lines = [l for l in re.split(r"[\r\n]", text) if l.strip() and not _PROGRESS_RE.search(l) and not l.startswith("Loading ")]
    return "\n".join(lines[-n:])


def create(exe: str, directory: str, set_name: str, files: Sequence[str], plan: BlockPlan, *,
           memory_mb: int, threads: int, recovery_files: int = 8,
           observer: Optional[Observer] = None, verify_after: bool = True,
           expect: Optional[Sequence[Tuple[str, int, str]]] = None) -> Par2Result:
    """Create ``<set_name>.par2`` + uniform volumes in *directory* covering *files*
    (names relative to *directory*), replacing any existing set of that name
    only after the new one is complete, covers exactly *files*, and (when
    *verify_after*) passed par2's own verify. Returns status ok|error.

    ``expect`` is the ``(name, size, md5)`` rows the records hold for those
    files. par2 reads every byte and stores each file's real MD5 in the index,
    so comparing the two is a free corruption check: a set built around bytes
    that differ from the records is refused, and the old set stays."""
    obs = observer or NullObserver()
    directory = os.path.abspath(directory)
    n_files = max(1, min(recovery_files, plan.recovery_blocks, 31))
    for n in files:
        if os.path.isabs(n):
            raise ValueError(f"file names must be relative to the set's folder: {n!r}")
    remove_stale_build_dirs(directory)
    # A payload file whose name is ours in another letter case ("parity.par2")
    # would be overwritten by the swap on a case-folding filesystem: refuse.
    clash = _case_clashes(directory, set_name)
    if clash:
        return Par2Result(action="create", returncode=EXIT_INVALID_ARGS, status="error", output="",
                          message=f"a file named {clash[0]!r} differs only in letter case from this folder's parity files; rename it")
    build = os.path.join(directory, BUILD_DIR_PREFIX + str(os.getpid()))
    try:
        os.makedirs(build, exist_ok=True)
    except OSError as e:
        return Par2Result(action="create", returncode=EXIT_FILE_IO, status="error", output="",
                          message=f"could not create the parity build folder: {e}")
    swapping = False
    try:
        # MEASURED: with -B<absolute base> and absolute paths in a list file,
        # par2cmdline stores the source names relative to the base exactly as given.
        # This handles '@'-prefixed names safely and avoids ARG_MAX limits.
        list_file_path = os.path.join(build, LIST_FILE_NAME)
        with open(list_file_path, "w", encoding="utf-8") as lf:
            for n in files:
                lf.write(os.path.join(directory, n) + "\n")

        args = ["c", f"-s{plan.block_size}", f"-c{plan.recovery_blocks}", "-u", f"-n{n_files}",
                f"-m{memory_mb}"]
        if supports_threads(exe):
            args.append(f"-t{threads}")
        args += ["-B" + directory, "--", os.path.join(build, set_name + PAR2_SUFFIX), "@" + list_file_path]
        rc, out = _run(exe, args, directory, obs, f"parity {set_name}")
        if rc != EXIT_OK:
            return Par2Result(action="create", returncode=rc, status="error", output=out, message=_tail(out))
        produced = set_files_on_disk(build, set_name)
        if not produced or (set_name + PAR2_SUFFIX) not in produced:
            return Par2Result(action="create", returncode=rc, status="error", output=out,
                              message="par2 reported success but wrote no index file")
        # The set must cover exactly the files we asked for: par2 drops some
        # names silently (see layout.name_problem), and a set that is missing
        # a file is worse than no set because it looks complete.
        try:
            table = {nfc(f.name) for f in read_index(os.path.join(build, set_name + PAR2_SUFFIX)).file_table}
        except Par2Error as e:
            return Par2Result(action="create", returncode=rc, status="error", output=out, message=f"new index unreadable: {e}")
        wanted = {nfc(n) for n in files}
        if table != wanted:
            missing = sorted(wanted - table)[:3]
            extra = sorted(table - wanted)[:3]
            return Par2Result(action="create", returncode=rc, status="error", output=out,
                              message=f"the new parity does not cover the requested files (missing {missing}, unexpected {extra})")
        if expect is not None:
            got = {nfc(f.name): (f.size, f.md5.hex()) for f in read_index(os.path.join(build, set_name + PAR2_SUFFIX)).file_table}
            differ = sorted(n for n, s, m in expect if got.get(nfc(n)) != (s, m))
            if differ:
                return Par2Result(action="create", returncode=rc, status="error", output=out,
                                  message=(f"{len(differ)} files differ from their records (for example {differ[0]!r}): par2 read different "
                                           "contents than the records describe. This looks like corruption; run Check and Repair"))
        if verify_after:
            v = verify(exe, build, set_name, memory_mb=memory_mb, threads=threads, observer=obs,
                       par2_file=os.path.join(build, set_name + PAR2_SUFFIX), basepath=directory)
            if v.status != "ok":
                return Par2Result(action="create", returncode=v.returncode, status="error", output=out + v.output,
                                  message=f"freshly created parity did not verify ({v.status}): {v.message}")
        # Swap, in two phases so that no failure leaves the folder without a
        # usable set. Phase 1 moves every new file into the folder under a
        # temporary name (same filesystem: a rename, no copy); anything that
        # fails here (disk full, I/O error) leaves the old set untouched.
        # Phase 2 renames each temporary over its final name, volumes first
        # and the index last, so an interruption leaves the old index with
        # whatever old volumes were not yet replaced, still a set par2 can
        # use; then the old files the new set did not replace are removed.
        old = set_files_on_disk(directory, set_name)
        staged: List[Tuple[str, str]] = []
        for n in produced:
            tmp = os.path.join(directory, _SWAP_PREFIX + n)
            os.replace(os.path.join(build, n), tmp)
            staged.append((tmp, n))
        swapping = True
        index_name = set_name + PAR2_SUFFIX
        for tmp, n in sorted(staged, key=lambda t: (t[1] == index_name, t[1])):
            os.replace(tmp, os.path.join(directory, n))
        for n in old:
            if n not in produced:
                os.remove(os.path.join(directory, n))
        return Par2Result(action="create", returncode=rc, status="ok", output=out, message=_tail(out))
    except OSError as e:
        return Par2Result(action="create", returncode=EXIT_FILE_IO, status="error", output="",
                          message=(f"the finished parity could not be moved into place ({e})"
                                   + ("; the folder may hold a mix of old and new parity files, run Protect again" if swapping else "")))
    finally:
        # The build folder is ours for this run (pid-named) and holds the list
        # file plus, on failure, the unfinished set: remove it whether or not
        # the swap happened. MEASURED 2026-09-10: when only the failure branch
        # cleaned up, every SUCCESSFUL create left ".cold-archive-build-<pid>/
        # names.lst" behind in every protected folder, and the stale sweep
        # below skipped it because the list file is not a .par2 file.
        try:
            for n in os.listdir(build):
                os.remove(os.path.join(build, n))
            os.rmdir(build)
        except OSError:
            pass
        if not swapping:
            # temporaries of a phase-1 failure
            _remove_swap_leftovers(directory)


def _case_clashes(directory: str, set_name: str) -> List[str]:
    """Payload names in *directory* that equal one of this set's file names
    when case-folded but are not spelled that way ("parity.par2")."""
    pat = re.compile(r"^" + re.escape(set_name) + r"(?:\.vol\d+\+\d+)?\.par2$", re.IGNORECASE)
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for n in names:
        if pat.match(n) and parity_set_of_name(n) is None:
            out.append(n)
    return sorted(out)


def parity_set_of_name(name: str) -> Optional[str]:
    from .layout import parity_set_of
    return parity_set_of(name)


def _remove_swap_leftovers(directory: str) -> List[str]:
    removed = []
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for n in names:
        if n.startswith(_SWAP_PREFIX):
            try:
                os.remove(os.path.join(directory, n))
                removed.append(n)
            except OSError:
                pass
    return removed


def remove_stale_build_dirs(directory: str) -> List[str]:
    """Delete leftover hidden build folders and swap temporaries (an earlier
    run was interrupted). Only folders named exactly ``.cold-archive-build-<pid>``
    that hold nothing but ``.par2`` files are touched: a user's own folder that
    merely starts with our prefix is not ours to delete."""
    removed = []
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for n in names:
        if _BUILD_DIR_RE.match(n):
            p = os.path.join(directory, n)
            if os.path.isdir(p) and not os.path.islink(p):
                try:
                    inside = os.listdir(p)
                except OSError:
                    continue
                if all(x.endswith(PAR2_SUFFIX) or x == LIST_FILE_NAME for x in inside):
                    shutil.rmtree(p, ignore_errors=True)
                    removed.append(n)
    removed.extend(_remove_swap_leftovers(directory))
    return removed


def entry_file(directory: str, set_name: str) -> Optional[str]:
    """The file to hand par2 for this set: the index if present, else the
    first volume. MEASURED: par2cmdline verifies and repairs from a volume
    file alone (every volume carries copies of the critical packets), so a
    lost index file must not make a folder unrepairable."""
    files = set_files_on_disk(directory, set_name)
    if not files:
        return None
    index = set_name + PAR2_SUFFIX
    return index if index in files else files[0]


def _no_files(action: str, set_name: str) -> Par2Result:
    return Par2Result(action=action, returncode=EXIT_INVALID_ARGS, status="error", output="",
                      message=f"no parity files found for {set_name}")


def verify(exe: str, directory: str, set_name: str, *, memory_mb: int = 1024, threads: int = 4,
           observer: Optional[Observer] = None, par2_file: Optional[str] = None,
           basepath: Optional[str] = None) -> Par2Result:
    obs = observer or NullObserver()
    target = par2_file or entry_file(directory, set_name)
    if target is None:
        return _no_files("verify", set_name)
    args = ["v", f"-m{memory_mb}"]
    if supports_threads(exe):
        args.append(f"-t{threads}")
    if basepath:
        args.append("-B" + basepath)
    args += ["--", target]
    rc, out = _run(exe, args, directory, obs, f"check {set_name}")
    files, needs = _parse_files(out)
    status = {EXIT_OK: "ok", EXIT_REPAIR_POSSIBLE: "repairable", EXIT_REPAIR_NOT_POSSIBLE: "unrepairable",
              EXIT_INSUFFICIENT_CRITICAL: "unusable"}.get(rc, "error")
    return Par2Result(action="verify", returncode=rc, status=status, output=out, files=files,
                      needs_blocks=needs, message=_tail(out))


def repair(exe: str, directory: str, set_name: str, *, memory_mb: int = 1024, threads: int = 4,
           observer: Optional[Observer] = None) -> Par2Result:
    obs = observer or NullObserver()
    target = entry_file(directory, set_name)
    if target is None:
        return _no_files("repair", set_name)
    args = ["r", f"-m{memory_mb}"]
    if supports_threads(exe):
        args.append(f"-t{threads}")
    args += ["--", target]
    rc, out = _run(exe, args, directory, obs, f"repair {set_name}")
    files, needs = _parse_files(out)
    if rc == EXIT_OK:
        status = "ok" if "repair is not required" in out else "repaired"
    else:
        status = {EXIT_REPAIR_NOT_POSSIBLE: "unrepairable", EXIT_INSUFFICIENT_CRITICAL: "unusable",
                  EXIT_REPAIR_FAILED: "error"}.get(rc, "error")
    return Par2Result(action="repair", returncode=rc, status=status, output=out, files=files,
                      needs_blocks=needs, message=_tail(out))


# --- reading PAR2 files directly --------------------------------------------

MAGIC = b"PAR2\x00PKT"
TYPE_MAIN = b"PAR 2.0\x00Main\x00\x00\x00\x00"
TYPE_FILEDESC = b"PAR 2.0\x00FileDesc"
TYPE_IFSC = b"PAR 2.0\x00IFSC\x00\x00\x00\x00"
TYPE_RECOVERY = b"PAR 2.0\x00RecvSlic"
TYPE_CREATOR = b"PAR 2.0\x00Creator\x00"


@dataclass
class Par2IndexFile:
    name: str
    size: int
    md5: bytes


@dataclass
class Par2Scan:
    """Result of walking every packet of one .par2 file."""
    path: str
    packets_ok: int = 0
    packets_bad: int = 0
    recovery_blocks: int = 0            # intact recovery blocks in this file
    block_size: Optional[int] = None
    set_id: Optional[bytes] = None
    files: Dict[bytes, Par2IndexFile] = field(default_factory=dict)  # by file id
    recoverable_ids: List[bytes] = field(default_factory=list)
    creator: str = ""
    truncated: bool = False

    @property
    def has_main(self) -> bool:
        return self.block_size is not None

    @property
    def file_table(self) -> List[Par2IndexFile]:
        """Recoverable files in main-packet order (only those whose FileDesc was intact)."""
        return [self.files[i] for i in self.recoverable_ids if i in self.files]


class Par2IndexError(Par2Error):
    pass


def _decode_name(raw: bytes) -> str:
    raw = raw.rstrip(b"\x00")
    try:
        return unicodedata.normalize("NFC", raw.decode("utf-8"))
    except UnicodeDecodeError:
        return raw.decode("utf-8", "surrogateescape")


def scan_par2_file(path: str, *, want_recovery_count: bool = True) -> Par2Scan:
    """Walk the packets of a PAR2 file, validating each packet's MD5.

    Damaged packets are counted, not fatal: par2cmdline itself skips them and
    uses the duplicates that the volume files carry. Resynchronises on the
    packet magic after a bad region.
    """
    scan = Par2Scan(path=path)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        while pos + 64 <= size:
            f.seek(pos)
            head = f.read(64)
            if len(head) < 64:
                scan.truncated = True
                break
            if head[:8] != MAGIC:
                # resync: search forward for the next magic
                nxt = _find_magic(f, pos + 1, size)
                scan.packets_bad += 1
                if nxt is None:
                    scan.truncated = True
                    break
                pos = nxt
                continue
            (length,) = struct.unpack("<Q", head[8:16])
            if length < 64 or length % 4 or pos + length > size:
                scan.packets_bad += 1
                nxt = _find_magic(f, pos + 8, size)
                if nxt is None:
                    scan.truncated = True
                    break
                pos = nxt
                continue
            md5_expected = head[16:32]
            set_id = head[32:48]
            ptype = head[48:64]
            body_len = length - 64
            h = hashlib.md5()
            h.update(head[32:64])
            body = b""
            if ptype in (TYPE_MAIN, TYPE_FILEDESC, TYPE_CREATOR) or (ptype == TYPE_RECOVERY and not want_recovery_count):
                body = f.read(body_len)
                h.update(body)
            else:
                remaining = body_len
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
                if remaining:
                    scan.truncated = True
                    scan.packets_bad += 1
                    break
            if h.digest() != md5_expected:
                scan.packets_bad += 1
                pos += length
                continue
            scan.packets_ok += 1
            if scan.set_id is None:
                scan.set_id = set_id
            if ptype == TYPE_MAIN and len(body) >= 12:
                (bs, nrec) = struct.unpack("<QI", body[:12])
                ids = [body[12 + 16 * i: 28 + 16 * i] for i in range((len(body) - 12) // 16)]
                scan.block_size = int(bs)
                scan.recoverable_ids = ids[:nrec]
            elif ptype == TYPE_FILEDESC and len(body) >= 56:
                fid = body[:16]
                md5_full = body[16:32]
                (flen,) = struct.unpack("<Q", body[48:56])
                scan.files[fid] = Par2IndexFile(name=_decode_name(body[56:]), size=int(flen), md5=md5_full)
            elif ptype == TYPE_RECOVERY:
                scan.recovery_blocks += 1
            elif ptype == TYPE_CREATOR:
                scan.creator = _decode_name(body)
            pos += length
        if pos != size:
            # bytes after the last whole packet: a truncated packet or garbage
            scan.truncated = True
    return scan


def _find_magic(f, start: int, size: int) -> Optional[int]:
    window = 1 << 16
    pos = start
    carry = b""
    while pos < size:
        f.seek(pos)
        data = carry + f.read(window)
        idx = data.find(MAGIC)
        if idx >= 0:
            return pos - len(carry) + idx
        carry = data[-(len(MAGIC) - 1):]
        pos += window
    return None


def read_index(path: str) -> Par2Scan:
    """Read an index (.par2) file and require an intact main packet."""
    scan = scan_par2_file(path, want_recovery_count=False)
    if not scan.has_main:
        raise Par2IndexError(f"{os.path.basename(path)} has no intact main packet")
    return scan


def set_files_on_disk(directory: str, set_name: str) -> List[str]:
    """All files of a set present in *directory*: the index and its volumes,
    matched strictly (``SET.par2`` and ``SET.volNNN+NN.par2``) so a payload file
    that merely starts with the set name is never mistaken for parity."""
    pat = re.compile(r"^" + re.escape(set_name) + r"(?:\.vol\d+\+\d+)?\.par2$")
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for n in names:
        if pat.match(n):
            out.append(n)
    return sorted(out)


_VOL_RE = re.compile(r"\.vol(\d+)\+(\d+)\.par2$")


def recovery_blocks_in_name(name: str) -> Optional[int]:
    """Recovery blocks a volume file claims to hold, from its name (``+NN``), or None for the index."""
    m = _VOL_RE.search(name)
    return int(m.group(2)) if m else None


def recovery_blocks_by_name(directory: str, set_name: str) -> int:
    """Cheap count of recovery blocks a set *should* contain, from volume file
    names (``PARITY.vol000+016.par2`` holds 16). Does not read the files."""
    total = 0
    for n in set_files_on_disk(directory, set_name):
        m = _VOL_RE.search(n)
        if m:
            total += int(m.group(2))
    return total
