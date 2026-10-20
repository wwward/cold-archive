"""Every build must check and repair archives made by every earlier build.

The archives in tests/compat/archives/ were made by real past releases (see
tests/compat/README.md) and are kept byte-for-byte. A Shoebox drive made in
2026 has to be repairable by whatever build someone downloads in 2036; this
is where that promise is enforced, and packaging/compat_gate.py enforces it
again on the built app with its own bundled par2 engines.

git does not keep file dates, so each test first restores the dates the
archive recorded, as they would be on the drive itself (Repair uses dates to
tell an edit from damage).
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess

import pytest

from cold_archive.engine import par2 as p2
from cold_archive.engine.layout import CHECK_COMMAND, MANIFEST_FILE, SUMS_FILE
from cold_archive.engine.manifest import load_manifest
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.repair import RepairOptions, repair
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import needs_par2

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = sorted(glob.glob(os.path.join(HERE, "compat", "archives", "v*")))


def load_fixture(fixture: str, dest: str) -> str:
    shutil.copytree(fixture, dest)
    m = load_manifest(os.path.join(dest, MANIFEST_FILE))
    assert m is not None
    for table in (m.files, m.parity):
        for rel, e in table.items():
            p = os.path.join(dest, rel)
            if os.path.exists(p):
                os.utime(p, (e.mtime, e.mtime))
    return dest


def _rot(path: str, offset: int = 4000, n: int = 3000) -> None:
    st = os.stat(path)
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(b"\x5a" * n)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_there_are_fixtures_from_past_releases():
    names = [os.path.basename(f) for f in FIXTURES]
    assert "v0.2.1" in names and "v0.2.2" in names, names     # the Shoebox drives were made with 0.2.1


@needs_par2
@pytest.mark.parametrize("fixture", FIXTURES, ids=os.path.basename)
def test_old_archive_checks_clean(fixture, tmp_path):
    arc = load_fixture(fixture, str(tmp_path / "arc"))
    for mode in ("standard", "thorough"):
        v = verify(arc, VerifyOptions(mode=mode))
        assert v.verdict == "ok", (mode, v.summary())


@needs_par2
@pytest.mark.parametrize("fixture", FIXTURES, ids=os.path.basename)
def test_old_archive_repairs_rot_and_a_missing_file(fixture, tmp_path):
    arc = load_fixture(fixture, str(tmp_path / "arc"))
    rotten = os.path.join(arc, "2022", "2022-04-27", "DSC_1040.ARW")
    missing = os.path.join(arc, "2021", "2021-12-01", "IMG_0002.CR3")
    want = {p: open(os.path.join(fixture, os.path.relpath(p, arc)), "rb").read() for p in (rotten, missing)}
    _rot(rotten)
    os.remove(missing)
    assert verify(arc, VerifyOptions(mode="standard")).verdict == "problem"
    r = repair(arc, RepairOptions())
    assert r.verdict == "ok", r.summary()
    for p, data in want.items():
        assert open(p, "rb").read() == data, p
    assert verify(arc, VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
@pytest.mark.parametrize("fixture", FIXTURES, ids=os.path.basename)
def test_old_archive_rebuilds_damaged_records(fixture, tmp_path):
    arc = load_fixture(fixture, str(tmp_path / "arc"))
    sums = os.path.join(arc, SUMS_FILE)
    want = open(sums, "rb").read()
    _rot(sums, offset=100, n=200)
    r = repair(arc, RepairOptions())
    # Restoring the records from PARITY-INDEX is reported as needing attention
    # by design: anything changed after the last Protect would now show up.
    assert r.verdict in ("ok", "attention") and "restored from PARITY-INDEX" in r.summary(), r.summary()
    # Every file's fingerprint is restored exactly. The recovery kit
    # (READ-ME-FIRST, the check script) may be rewritten in the current
    # version's words while the records are being rewritten, so only its
    # own lines may differ.
    kit = {"READ-ME-FIRST.txt", "READ-ME-FIRST.html", CHECK_COMMAND}

    def lines(b: bytes) -> dict:
        out = {}
        for ln in b.decode("utf-8").splitlines():
            h, _, rel = ln.partition(" *")
            if rel not in kit:
                out[rel] = h
        return out
    assert lines(open(sums, "rb").read()) == lines(want)
    assert verify(arc, VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
@pytest.mark.parametrize("fixture", FIXTURES, ids=os.path.basename)
def test_old_archive_own_check_script_still_works(fixture, tmp_path):
    """The script written into the archive when it was made, run with today's
    par2: it is what someone without the app double-clicks."""
    arc = load_fixture(fixture, str(tmp_path / "arc"))
    exe = p2.find_par2()
    env = dict(os.environ, PATH=os.path.dirname(exe) + os.pathsep + "/usr/bin:/bin")
    out = subprocess.run(["bash", os.path.join(arc, CHECK_COMMAND)], cwd=arc, env=env,
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0 and "RESULT: this archive is healthy." in out.stdout, out.stdout[-2000:]


@needs_par2
@pytest.mark.parametrize("fixture", FIXTURES, ids=os.path.basename)
def test_protecting_an_old_archive_again_asks_nothing_and_keeps_its_parity(fixture, tmp_path):
    arc = load_fixture(fixture, str(tmp_path / "arc"))
    before = {rel: e.sha256 for rel, e in load_manifest(os.path.join(arc, MANIFEST_FILE)).files.items()}
    res = protect(arc, ProtectOptions(include_tools=False))
    assert res.ok and not res.needs_consent, res.summary()
    # Every folder's repair data is kept as it was; only the index over the
    # records is remade, because the records now name the newer version.
    rebuilt = sorted(s.set_name for s in res.sets if s.action == "created")
    assert rebuilt in ([], ["PARITY-INDEX"]), res.summary()
    after = {rel: e.sha256 for rel, e in load_manifest(os.path.join(arc, MANIFEST_FILE)).files.items()}
    assert after == before
    assert verify(arc, VerifyOptions(mode="thorough")).verdict == "ok"
