"""cold-archive-cli: create, protect, check, repair, plan.

Exit codes: 0 all good; 1 intact but needs attention (degraded parity, files
not yet in the records); 2 problems (damaged or missing files, or the job did
not fully complete); 3 could not run (bad arguments, par2 missing, folder not
found); 130 cancelled.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import Optional

from .. import __version__
from . import par2 as p2
from .create import create_archive
from .layout import human_bytes
from .lock import ArchiveBusy
from .logs import log_file, setup_logging, write_report
from .observer import Cancelled, LoggingObserver
from .preflight import run_preflight
from .protect import ProtectOptions, protect
from .repair import RepairOptions, repair
from .rescue import rescue_copy
from .spanning import MEDIA_LIMITS, Spanner
from .verify import MODES, VerifyOptions, verify

EXIT_OK, EXIT_ATTENTION, EXIT_PROBLEMS, EXIT_USAGE, EXIT_NEEDS_CONSENT, EXIT_CANCELLED = 0, 1, 2, 3, 4, 130


def _verdict_exit(verdict: str) -> int:
    return {"ok": EXIT_OK, "attention": EXIT_ATTENTION}.get(verdict, EXIT_PROBLEMS)


class ConsoleObserver(LoggingObserver):
    """Logging observer plus a single-line progress display on a TTY."""

    def __init__(self) -> None:
        super().__init__(logging.getLogger("cold_archive"))
        self.tty = sys.stderr.isatty()
        self._phase_name = ""
        self._unit = ""
        self._last_len = 0

    def phase(self, name: str, total: Optional[int] = None, unit: str = "") -> None:
        self._end_line()
        self._phase_name, self._unit = name, unit
        super().phase(name, total, unit)

    def progress(self, done: int, total: Optional[int] = None, detail: str = "") -> None:
        super().progress(done, total, detail)
        if not self.tty or not total:
            return
        pct = min(100, int(done * 100 / total))
        what = f"{human_bytes(done)} / {human_bytes(total)}" if self._unit == "bytes" else f"{done} / {total}"
        line = f"\r  {self._phase_name}: {pct:3d}%  {what}  {detail[:40]}"
        sys.stderr.write(line + " " * max(0, self._last_len - len(line)))
        sys.stderr.flush()
        self._last_len = len(line)

    def _end_line(self) -> None:
        if self.tty and self._last_len:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._last_len = 0

    def log(self, level: str, message: str) -> None:
        self._end_line()
        super().log(level, message)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--par2", help="path to the par2 program (default: bundled, then PATH, then /opt/homebrew/bin)")
    p.add_argument("--hash-workers", type=int, default=4)
    p.add_argument("--par2-workers", type=int, default=2, help="par2 processes at once (each uses many threads)")


def _protect_options(args: argparse.Namespace) -> ProtectOptions:
    return ProtectOptions(
        redundancy_percent=args.redundancy, hash_workers=args.hash_workers, par2_workers=args.par2_workers,
        full_rehash=getattr(args, "full_rehash", False), rebuild_parity=getattr(args, "rebuild_parity", False),
        accept_removals=getattr(args, "accept_removals", False), accept_changes=getattr(args, "accept_changes", False),
        verify_after_create=not getattr(args, "no_verify_parity", False),
        label=getattr(args, "label", "") or "", media_note=getattr(args, "media", "") or "", par2_path=args.par2,
        target_blocks=getattr(args, "target_blocks", 2000), overhead_tolerance=getattr(args, "parity_tolerance", 0.25),
        include_tools=not getattr(args, "no_tools", False), include_app=getattr(args, "include_app", False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cold-archive-cli",
                                     description="Cold Archive: long-term integrity protection for archived files (SHA-256 + PAR2).")
    parser.add_argument("--version", action="version", version=f"cold-archive {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the final report")
    sub = parser.add_subparsers(dest="command", required=True)

    pf = sub.add_parser("preflight", help="check space, filesystem and par2 before a job; reads the source only, and makes "
                                          "and removes small test files in the destination when one is given")
    pf.add_argument("source")
    pf.add_argument("target", nargs="?", help="destination folder (omit to preflight protecting in place)")
    pf.add_argument("--redundancy", type=int, default=10)
    pf.add_argument("--par2")

    def protect_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--redundancy", type=int, default=10, help="repair data as a percentage of each folder (default 10)")
        p.add_argument("--label", help="a name for the archive, written into the records and READ-ME-FIRST")
        p.add_argument("--media", help="what the archive is stored on (hdd, ssd, bluray, dvd, tape, cloud), for the care advice")
        p.add_argument("--full-rehash", action="store_true", help="re-read every file even if size and date are unchanged")
        p.add_argument("--rebuild-parity", action="store_true", help="rebuild every parity set (only after a thorough check)")
        p.add_argument("--accept-removals", action="store_true", help="allow files that vanished since the last run to be dropped from the records")
        p.add_argument("--accept-changes", action="store_true", help="allow files whose contents changed since the last run to be re-recorded (you edited them on purpose)")
        p.add_argument("--no-verify-parity", action="store_true", help="skip the par2 verify of each freshly built set (faster, less safe)")
        p.add_argument("--no-kit", action="store_true", help="do not write READ-ME-FIRST and the check script")
        p.add_argument("--no-tools", action="store_true", help="do not put the par2cmdline source release in TOOLS/")
        p.add_argument("--include-app", action="store_true", help="also put a zip of the Cold Archive app in TOOLS/ (needs the installed .app)")
        p.add_argument("--target-blocks", type=int, default=2000, help=argparse.SUPPRESS)
        p.add_argument("--parity-tolerance", type=float, default=0.25, help=argparse.SUPPRESS)
        _add_common(p)

    cr = sub.add_parser("create", help="protect a folder where it is, then copy it with its records to a destination, verified")
    cr.add_argument("source")
    cr.add_argument("target")
    cr.add_argument("--force", action="store_true", help="proceed despite preflight warnings (errors still stop)")
    protect_args(cr)

    pr = sub.add_parser("protect", help="hash and build parity for a folder in place (or bring an archive up to date)")
    pr.add_argument("archive")
    protect_args(pr)

    vf = sub.add_parser("verify", aliases=["check"], help="check every file and every parity set; report what can be repaired")
    vf.add_argument("archive")
    vf.add_argument("--mode", choices=MODES, default="standard", help="standard (default), thorough, hashes, parity")
    _add_common(vf)

    rp = sub.add_parser("repair", help="rebuild damaged or missing files from parity, then re-check")
    rp.add_argument("archive")
    rp.add_argument("--keep-backups", action="store_true", help="keep par2's .1 copies of damaged originals")
    _add_common(rp)

    rs = sub.add_parser("rescue", help="copy everything readable off a failing drive (never writes to it); then repair the copy")
    rs.add_argument("source", help="the archive on the failing drive")
    rs.add_argument("target", help="an empty folder on a different, healthy drive")

    sp = sub.add_parser("plan", aliases=["span"], help="plan how to split an archive across discs")
    sp.add_argument("source")
    sp.add_argument("--media", choices=list(MEDIA_LIMITS), default="BD_25")
    sp.add_argument("--capacity", type=int, help="custom capacity in bytes")
    sp.add_argument("--redundancy", type=int, default=10)

    sub.add_parser("doctor", help="show where par2 and the logs are")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = setup_logging(console=not args.quiet)
    obs = ConsoleObserver()

    def on_sigint(signum, frame):  # noqa: ANN001
        obs.cancel_event.set()
    try:
        signal.signal(signal.SIGINT, on_sigint)
    except ValueError:
        pass  # not main thread

    try:
        return _dispatch(args, obs, log_path)
    except Cancelled:
        print("\nCancelled. The archive is in a consistent but incomplete state; run the same command again to resume.", file=sys.stderr)
        return EXIT_CANCELLED
    except p2.Par2NotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (NotADirectoryError, FileNotFoundError, ValueError, ArchiveBusy) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as e:
        # a folder that cannot be created, a read-only or full medium: one
        # line, not a traceback, and an exit code the documentation lists
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE


def _dispatch(args: argparse.Namespace, obs: ConsoleObserver, log_path: str) -> int:
    cmd = args.command
    if cmd == "doctor":
        try:
            exe = p2.find_par2()
            print(f"par2: {exe} ({p2.par2_version(exe)})")
        except p2.Par2NotFound as e:
            print(f"par2: NOT FOUND. {e}")
        print(f"log:  {log_file()}")
        print(f"cold-archive {__version__}, python {sys.version.split()[0]}")
        return EXIT_OK

    if cmd == "preflight":
        rep = run_preflight(args.source, args.target, args.redundancy, par2_path=args.par2, observer=obs)
        print(rep.summary())
        return EXIT_PROBLEMS if not rep.ok else (EXIT_ATTENTION if rep.warnings else EXIT_OK)

    if cmd in ("plan", "span"):
        plan = Spanner(args.source, args.media, args.capacity, args.redundancy).plan(obs)
        print(plan.render())
        return EXIT_OK if not plan.oversized_files else EXIT_PROBLEMS

    if cmd == "create":
        rep = run_preflight(args.source, args.target, args.redundancy, par2_path=args.par2, observer=obs)
        print(rep.summary())
        if not rep.ok:
            return EXIT_PROBLEMS
        if rep.warnings and not args.force:
            print("Preflight raised warnings; re-run with --force to proceed anyway.", file=sys.stderr)
            return EXIT_ATTENTION

        opts = _protect_options(args)
        opts.write_kit = not args.no_kit
        opts.label = opts.label or os.path.basename(os.path.abspath(args.source))
        # The source is protected where it is, then copied (see create.py). It
        # may already carry records, so --accept-changes / --accept-removals
        # mean exactly what they mean for protect: nothing is accepted by default.
        res = create_archive(args.source, args.target, opts, obs)
        print(res.summary())
        path = write_report("create", args.target if res.copy is not None else args.source, res.summary())
        print(f"Report saved to {path}")
        if res.needs_consent:
            print("The source folder has changed since it was last recorded; nothing was written or copied. "
                  "If you made those changes on purpose, run again with --accept-changes and/or --accept-removals; "
                  "if not, run check, then repair, on the source folder.")
            return EXIT_NEEDS_CONSENT
        if res.copy is None:
            print("Protect did not complete on the source. Fix the failures and run again.")
            return EXIT_PROBLEMS
        if not res.copy.ok:
            print("Copy did not complete. Fix the failures and run again (already-copied files are skipped).")
            return EXIT_PROBLEMS
        if res.source_mismatches:
            print("The source folder read differently than its records during the copy; see SOURCE PROBLEMS above.")
        return EXIT_OK if res.ok else EXIT_PROBLEMS

    if cmd == "protect":
        opts = _protect_options(args)
        opts.write_kit = not args.no_kit
        pres = protect(args.archive, opts, obs)
        path = write_report("protect", args.archive, pres.summary())
        print(pres.summary())
        print(f"Report saved to {path}")
        if pres.needs_consent:
            return EXIT_NEEDS_CONSENT
        return EXIT_OK if pres.ok else EXIT_PROBLEMS

    if cmd in ("verify", "check"):
        vres = verify(args.archive, VerifyOptions(mode=args.mode, hash_workers=args.hash_workers,
                                                  par2_workers=args.par2_workers, par2_path=args.par2), obs)
        path = write_report("check", args.archive, vres.summary())
        print(vres.summary())
        print(f"Report saved to {path}")
        return _verdict_exit(vres.verdict)

    if cmd == "repair":
        rres = repair(args.archive, RepairOptions(hash_workers=args.hash_workers, par2_path=args.par2,
                                                  remove_backups=not args.keep_backups), obs)
        text = rres.summary()
        if rres.after is not None:
            text += "\n\nState after repair:\n" + rres.after.summary()
        path = write_report("repair", args.archive, text)
        print(rres.summary())
        print(f"Report saved to {path}")
        return _verdict_exit(rres.verdict)

    if cmd == "rescue":
        sres = rescue_copy(args.source, args.target, obs)
        print(sres.summary())
        path = write_report("rescue", args.target, sres.summary())
        print(f"Report saved to {path}")
        if sres.refused:
            return EXIT_PROBLEMS
        return EXIT_ATTENTION if sres.lost_anything or not sres.is_archive else EXIT_OK

    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
