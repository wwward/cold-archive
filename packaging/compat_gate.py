"""Build gate: the built app must check and repair archives made by every
earlier release, with its own command line and each of its bundled par2
engines. Run by packaging/build_app.sh before anything is signed for
release; a failure stops the build.

    python packaging/compat_gate.py "dist/Cold Archive.app"

For each fixture in tests/compat/archives/ (archives made by real past
releases), and for each bundled engine (bin/par2, bin/par2-fast):
copy it, restore the recorded file dates (git does not keep them), then
  check            must pass,
  rot a file, delete another, check   must report damage (exit 2),
  repair           must succeed,
  thorough check   must pass, and the two files must be byte-identical
                   to the originals,
and the archive's own Check-This-Archive.command, run with only the app's
bundled par2 on PATH, must report the archive healthy.

It also requires bin/par2 at Contents/Frameworks/bin/par2: the check script
written into every archive since 0.2.1 looks for the installed app's par2
there, so moving it would break every drive already handed out.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURES = sorted(glob.glob(os.path.join(ROOT, "tests", "compat", "archives", "v*")))
ROTTEN = os.path.join("2022", "2022-04-27", "DSC_1040.ARW")
MISSING = os.path.join("2021", "2021-12-01", "IMG_0002.CR3")


def fail(msg: str) -> None:
    print("REFUSED (compatibility): " + msg)
    sys.exit(1)


def restore_dates(arc: str) -> None:
    with open(os.path.join(arc, "cold-archive-manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    for table in ("files", "parity_files"):
        for rel, e in (m.get(table) or {}).items():
            p = os.path.join(arc, rel)
            if os.path.exists(p):
                os.utime(p, (e["mtime"], e["mtime"]))


def run(cli: str, *args: str) -> int:
    return subprocess.run([cli, "-q", *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=600).returncode


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: compat_gate.py <path to Cold Archive.app>")
    app = os.path.abspath(sys.argv[1])
    bindir = os.path.join(app, "Contents", "Frameworks", "bin")
    if not os.access(os.path.join(bindir, "par2"), os.X_OK):
        fail(f"bin/par2 is not at {bindir}; every archive's check script looks for it there")
    engines = [e for e in ("par2", "par2-fast") if os.access(os.path.join(bindir, e), os.X_OK)]
    if not FIXTURES:
        fail("no fixtures in tests/compat/archives")
    work = tempfile.mkdtemp(prefix="compat-gate-")
    try:
        cli = os.path.join(work, "cold-archive-cli")          # the app runs its CLI under this name
        os.symlink(os.path.join(app, "Contents", "MacOS", "Cold Archive"), cli)
        for fixture in FIXTURES:
            name = os.path.basename(fixture)
            for engine in engines:
                par2 = os.path.join(bindir, engine)
                arc = os.path.join(work, f"{name}-{engine}")
                shutil.copytree(fixture, arc)
                restore_dates(arc)
                if (rc := run(cli, "check", arc, "--par2", par2)) != 0:
                    fail(f"{name} with {engine}: check of the intact archive exited {rc}")
                st = os.stat(os.path.join(arc, ROTTEN))
                with open(os.path.join(arc, ROTTEN), "r+b") as f:
                    f.seek(4000)
                    f.write(b"\x5a" * 3000)
                os.utime(os.path.join(arc, ROTTEN), ns=(st.st_atime_ns, st.st_mtime_ns))
                os.remove(os.path.join(arc, MISSING))
                if (rc := run(cli, "check", arc, "--par2", par2)) != 2:
                    fail(f"{name} with {engine}: check did not report the damage (exit {rc}, want 2)")
                if (rc := run(cli, "repair", arc, "--par2", par2)) != 0:
                    fail(f"{name} with {engine}: repair exited {rc}")
                if (rc := run(cli, "check", arc, "--mode", "thorough", "--par2", par2)) != 0:
                    fail(f"{name} with {engine}: thorough check after repair exited {rc}")
                for rel in (ROTTEN, MISSING):
                    with open(os.path.join(arc, rel), "rb") as a, open(os.path.join(fixture, rel), "rb") as b:
                        if a.read() != b.read():
                            fail(f"{name} with {engine}: {rel} is not byte-identical after repair")
                env = {"PATH": bindir + os.pathsep + "/usr/bin:/bin", "HOME": work}
                out = subprocess.run(["bash", os.path.join(arc, "Check-This-Archive.command")], cwd=arc, env=env,
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
                if out.returncode != 0 or "RESULT: this archive is healthy." not in out.stdout:
                    fail(f"{name} with {engine}: the archive's own check script failed:\n{out.stdout[-1500:]}")
                print(f"   {name}: checked, damage found, repaired byte-identical, check script healthy ({engine})")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
