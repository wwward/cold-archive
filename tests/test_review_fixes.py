"""Behaviours pinned after the adversarial review of 2026-09-04. Each test is a
scenario a finder agent actually reproduced against the previous code; each
one reads back the disk, never a summary string alone."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import unicodedata

import pytest

from cold_archive.engine import par2 as p2
from cold_archive.engine.copy import copy_tree
from cold_archive.engine.layout import CHECK_COMMAND, INDEX_PARITY_BASENAME, MANIFEST_FILE, SUMS_FILE, name_problem
from cold_archive.engine.manifest import load_manifest, parse_sums, read_sums, sums_content
from cold_archive.engine.observer import Cancelled, Observer
from cold_archive.engine.preflight import name_collisions, run_preflight
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.repair import repair
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import make_tree, needs_par2, rot


def _vols(d):
    return sorted(p for p in d.iterdir() if ".vol" in p.name and p.name.endswith(".par2"))


# --- Protect: consent and the records ----------------------------------------

@needs_par2
def test_changed_files_need_consent_and_nothing_is_touched(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    snap = {n: (tmp_path / n).read_bytes() for n in (SUMS_FILE, MANIFEST_FILE)}
    idx = (tmp_path / "Docs" / "PARITY.par2").read_bytes()
    # a copy through exFAT or rsync changes dates; corruption on the way changes bytes
    p = tmp_path / "Docs" / "letter.txt"
    p.write_bytes(files["Docs/letter.txt"][:-40] + b"X" * 40)
    r = protect(str(tmp_path))
    assert r.needs_consent == "changes" and r.files_updated == ["Docs/letter.txt"] and not r.ok
    assert {n: (tmp_path / n).read_bytes() for n in snap} == snap             # records untouched
    assert (tmp_path / "Docs" / "PARITY.par2").read_bytes() == idx            # parity untouched
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] != p2.hashlib.sha256(p.read_bytes()).hexdigest()
    # the honest path: Check says damaged, Repair restores, records stay true
    assert verify(str(tmp_path)).verdict == "problem"
    rr = repair(str(tmp_path))
    assert p.read_bytes() == files["Docs/letter.txt"] and "Docs/letter.txt" in rr.repaired
    # the other path: the person says it was them
    p.write_bytes(files["Docs/letter.txt"][:-40] + b"Y" * 40)
    r = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    assert r.ok and r.files_updated == ["Docs/letter.txt"], r.summary()
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_removed_and_changed_together_report_both(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / "Photos" / "2019" / "IMG_002.jpg")
    (tmp_path / "Docs" / "letter.txt").write_bytes(b"new letter\n" * 100)
    r = protect(str(tmp_path))
    assert r.needs_consent == "removals+changes"
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert r.needs_consent == "changes"
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True, accept_changes=True))
    assert r.ok and verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_nfd_named_file_rot_is_caught_by_the_safety_check(tmp_path):
    files = make_tree(str(tmp_path))
    d = tmp_path / "Photos" / "2019"
    nfc_name = "héllo wörld ✓.jpg"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    os.rename(d / nfc_name, d / nfd_name)
    assert protect(str(tmp_path)).ok
    listed = [n for n in read_sums(str(tmp_path / SUMS_FILE)) if n.startswith("Photos/2019/h")]
    assert listed and os.path.exists(tmp_path / listed[0])        # recorded as spelled on disk
    rot(str(d / nfd_name), offset=100, length=200)
    with open(d / "IMG_002.jpg", "ab") as f:
        f.write(b"trailer")                                        # a real edit next to it
    r = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    sets = [s for s in r.sets if s.directory == "Photos/2019"]
    assert sets and all(s.action == "blocked" for s in sets), r.summary()
    rr = repair(str(tmp_path))
    assert (d / nfd_name).read_bytes() == files["Photos/2019/" + nfc_name], rr.summary()
    assert (d / "IMG_002.jpg").read_bytes().endswith(b"trailer")   # the edit survived
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_unreadable_manifest_never_restarts_from_disk(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    sums_before = (tmp_path / SUMS_FILE).read_bytes()
    mpath = tmp_path / MANIFEST_FILE
    data = bytearray(mpath.read_bytes())
    data[5] ^= 0xFF
    mpath.write_bytes(bytes(data))
    rot(str(tmp_path / "Docs" / "letter.txt"))
    os.remove(tmp_path / "Photos" / "2019" / "IMG_002.jpg")
    r = protect(str(tmp_path))
    assert not r.ok and r.count("created") == 0 and "Repair" in " ".join(r.errors), r.summary()
    assert (tmp_path / SUMS_FILE).read_bytes() == sums_before
    rr = repair(str(tmp_path))
    assert MANIFEST_FILE in rr.records_restored
    assert (tmp_path / "Docs" / "letter.txt").read_bytes() == files["Docs/letter.txt"]
    assert (tmp_path / "Photos" / "2019" / "IMG_002.jpg").read_bytes() == files["Photos/2019/IMG_002.jpg"]
    assert verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_missing_manifest_falls_back_to_sums_and_blocks_on_difference(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / MANIFEST_FILE)
    rot(str(tmp_path / "Docs" / "letter.txt"))
    # PARITY-INDEX can bring the manifest back exactly: Protect refuses and
    # points at Repair rather than rebuilding records from a lesser source
    r = protect(str(tmp_path))
    assert not r.ok and r.count("created") == 0 and "Repair" in " ".join(r.errors), r.summary()
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] == p2.hashlib.sha256(files["Docs/letter.txt"]).hexdigest()
    rr = repair(str(tmp_path))
    assert MANIFEST_FILE in rr.records_restored and "Docs/letter.txt" in rr.repaired, rr.summary()
    assert verify(str(tmp_path)).verdict == "ok" and protect(str(tmp_path)).ok
    # without PARITY-INDEX the fallback to SHA256SUMS is the only option:
    # every file is re-read and a difference is suspected corruption
    os.remove(tmp_path / MANIFEST_FILE)
    for p in tmp_path.glob("PARITY-INDEX*.par2"):
        p.unlink()
    rot(str(tmp_path / "Docs" / "letter.txt"))
    r = protect(str(tmp_path))
    assert not r.ok and r.rot_suspected == ["Docs/letter.txt"], r.summary()
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] == p2.hashlib.sha256(files["Docs/letter.txt"]).hexdigest()
    rr = repair(str(tmp_path))
    assert "Docs/letter.txt" in rr.repaired and verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_unreadable_file_keeps_its_record(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    p = tmp_path / "Docs" / "letter.txt"
    os.chmod(p, 0)
    try:
        r = protect(str(tmp_path), ProtectOptions(full_rehash=True))
        assert not r.ok and r.hash_failures and r.hash_failures[0][0] == "Docs/letter.txt"
        assert "Docs/letter.txt" in read_sums(str(tmp_path / SUMS_FILE))
        assert load_manifest(str(tmp_path / MANIFEST_FILE)).files["Docs/letter.txt"].sha256 == p2.hashlib.sha256(files["Docs/letter.txt"]).hexdigest()
    finally:
        os.chmod(p, 0o644)
    rot(str(p))
    r = protect(str(tmp_path), ProtectOptions(full_rehash=True))
    assert not r.ok and r.rot_suspected == ["Docs/letter.txt"], r.summary()   # not "added", not laundered
    assert "Docs/letter.txt" in repair(str(tmp_path)).repaired


@needs_par2
def test_records_that_disagree_are_restored_not_trusted(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    sums = (tmp_path / SUMS_FILE).read_text()
    line = next(l for l in sums.splitlines() if l.endswith("Docs/letter.txt"))
    flipped = ("0" if line[0] != "0" else "1") + line[1:]
    (tmp_path / SUMS_FILE).write_text(sums.replace(line, flipped))
    v = verify(str(tmp_path))
    assert v.verdict != "ok" and any("disagree" in e for e in v.errors), v.summary()
    r = protect(str(tmp_path))
    assert not r.ok and r.count("created") == 0 and "disagree" in r.errors[0]
    rr = repair(str(tmp_path))
    assert SUMS_FILE in rr.records_restored and verify(str(tmp_path)).verdict == "ok", rr.summary()


@needs_par2
def test_truncated_sums_is_not_a_narrower_check(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    lines = (tmp_path / SUMS_FILE).read_text().splitlines(keepends=True)
    (tmp_path / SUMS_FILE).write_text("".join(lines[: len(lines) // 2]))
    rot(str(tmp_path / "root file.bin"))
    v = verify(str(tmp_path))
    assert v.verdict == "problem", v.summary()
    assert any(p.rel == "root file.bin" and p.kind == "mismatch" for p in v.problems) or any("fewer" in e for e in v.errors)


# --- Verify: parity that is missing, damaged or out of date ----------------------

@needs_par2
def test_missing_volumes_are_never_all_good(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Photos" / "2019"
    for vol in _vols(d):
        os.remove(vol)
    for mode in ("standard", "thorough", "parity", "hashes"):
        v = verify(str(tmp_path), VerifyOptions(mode=mode))
        assert v.verdict != "ok", (mode, v.summary())
    v = verify(str(tmp_path))
    assert any(p.kind == "missing" and "PARITY.vol" in p.rel for p in v.parity_problems)
    st = next(s for s in v.sets if s.directory == "Photos/2019")
    assert st.status == "degraded" and st.recovery_blocks == 0
    # Repair rebuilds it because the files themselves are intact
    rr = repair(str(tmp_path))
    assert "Photos/2019/PARITY" in rr.parity_rebuilt and rr.verdict == "ok", rr.summary()
    assert _vols(d) and verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_corrupt_or_truncated_volumes_are_seen_in_every_mode(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Photos" / "2019"
    for vol in _vols(d):
        with open(vol, "r+b") as f:
            f.seek(200)
            f.write(b"\0" * 64)
    for mode in ("standard", "thorough", "parity"):
        v = verify(str(tmp_path), VerifyOptions(mode=mode))
        assert v.verdict != "ok", (mode, v.summary())
        assert next(s for s in v.sets if s.directory == "Photos/2019").status == "degraded"
    # truncation to a point inside the last packet
    assert repair(str(tmp_path)).verdict == "ok"
    vol = _vols(d)[-1]
    with open(vol, "r+b") as f:
        f.truncate(vol.stat().st_size - 100)
    v = verify(str(tmp_path))
    assert next(s for s in v.sets if s.directory == "Photos/2019").status == "degraded", v.summary()
    assert v.verdict != "ok"


@needs_par2
def test_lost_index_file_is_not_the_end(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    os.remove(d / "PARITY.par2")
    rot(str(d / "letter.txt"))
    v = verify(str(tmp_path))
    assert v.verdict == "problem"
    st = next(s for s in v.sets if s.directory == "Docs")
    assert st.status == "repairable", v.summary()
    rr = repair(str(tmp_path))
    assert (d / "letter.txt").read_bytes() == files["Docs/letter.txt"], rr.summary()
    assert (d / "PARITY.par2").exists() and "Docs/PARITY" in rr.parity_rebuilt
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_parity_mode_calls_damage_a_problem(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    rot(str(tmp_path / "Docs" / "letter.txt"))
    os.remove(tmp_path / SUMS_FILE)
    os.remove(tmp_path / MANIFEST_FILE)
    v = verify(str(tmp_path), VerifyOptions(mode="parity"))
    assert v.verdict == "problem" and any(p.rel == "Docs/letter.txt" for p in v.damaged_or_missing), v.summary()


@needs_par2
def test_stale_parity_is_reported_and_interrupted_protect_is_not_rolled_back(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    (tmp_path / "Docs" / "new.txt").write_bytes(b"added later\n" * 200)

    class CancelAtIndex(Observer):
        """Let everything run until the records are written, then cancel
        while PARITY-INDEX is being rebuilt: the shape of a pulled plug."""
        armed = False

        def phase(self, name, total=None, unit=""):
            if name == "Writing records":
                self.armed = True

        def info(self, message):
            if "PARITY-INDEX" in message:
                self.fire = True

        def check_cancel(self):
            if getattr(self, "fire", False):
                raise Cancelled()

    with pytest.raises(Cancelled):
        protect(str(tmp_path), observer=CancelAtIndex())
    assert "Docs/new.txt" in read_sums(str(tmp_path / SUMS_FILE))     # records are new
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".cold-archive-build")]
    v = verify(str(tmp_path))
    assert v.verdict == "attention", v.summary()
    idx = next(s for s in v.sets if s.set_name == INDEX_PARITY_BASENAME)
    assert idx.status in ("stale", "repairable"), v.summary()
    rr = repair(str(tmp_path))
    assert not rr.records_restored, rr.summary()                        # never rolled back
    assert "Docs/new.txt" in read_sums(str(tmp_path / SUMS_FILE))
    assert f"(top folder)/{INDEX_PARITY_BASENAME}" in rr.parity_rebuilt
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


# --- Names -----------------------------------------------------------------------

@needs_par2
def test_backslash_and_control_names_are_refused_not_dropped(tmp_path):
    make_tree(str(tmp_path))
    (tmp_path / "Docs" / "back\\slash.txt").write_bytes(b"b" * 5000)
    (tmp_path / "Docs" / "tab\tname.txt").write_bytes(b"t" * 5000)
    r = protect(str(tmp_path))
    assert not r.ok and {rel for rel, _ in r.unprotectable} == {"Docs/back\\slash.txt", "Docs/tab\tname.txt"}, r.summary()
    sums = read_sums(str(tmp_path / SUMS_FILE))
    assert "Docs/back\\slash.txt" in sums and "Docs/tab\tname.txt" in sums     # recorded, checkable
    table = {f.name for f in p2.read_index(str(tmp_path / "Docs" / "PARITY.par2")).file_table}
    assert "letter.txt" in table and not any("\\" in n or "\t" in n for n in table)
    v = verify(str(tmp_path))
    assert set(v.unprotected) == {"Docs/back\\slash.txt", "Docs/tab\tname.txt"} and v.verdict == "attention"
    assert name_problem("ok name.txt") is None and name_problem("a\x7fb") is not None


@needs_par2
def test_payload_named_like_parity_is_left_alone(tmp_path):
    make_tree(str(tmp_path))
    odd = tmp_path / "Docs" / "PARITY.volume-notes.par2"
    odd.write_bytes(b"not ours\n" * 100)
    assert protect(str(tmp_path)).ok
    assert odd.exists() and "Docs/PARITY.volume-notes.par2" in read_sums(str(tmp_path / SUMS_FILE))
    assert protect(str(tmp_path)).ok and odd.exists()
    assert verify(str(tmp_path)).verdict == "ok"


def test_parse_sums_survives_exotic_line_separators_in_names():
    names = {"a\x0cb.txt": "0" * 64, "c\x0bd.txt": "1" * 64, "e f.txt": "2" * 64, "g\x85h.txt": "3" * 64}
    text = sums_content(names.items()).decode("utf-8")
    assert parse_sums(text) == names


def test_name_collisions_are_found():
    class F:
        def __init__(self, rel):
            self.rel = rel

    class S:
        files = [F("a.txt"), F("A.txt"), F("café.txt"), F("café.txt"), F("other.txt")]
    pairs = name_collisions(S())
    assert ("a.txt", "A.txt") in pairs and ("café.txt", "café.txt") in pairs and len(pairs) == 2


# --- Repair keeps what it cannot judge ----------------------------------------------

@needs_par2
def test_repair_keeps_a_change_made_after_protect(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    (d / "letter.txt").write_bytes(b"my new letter\n" * 500)          # changed after Protect, never re-protected
    rot(str(d / "-leading-dash.txt"), offset=10, length=30)           # and rot next to it
    rr = repair(str(tmp_path))
    assert (d / "-leading-dash.txt").read_bytes() == files["Docs/-leading-dash.txt"]
    assert (d / "letter.txt").read_bytes() == files["Docs/letter.txt"]           # recorded version restored
    assert (d / "letter.txt.1").read_bytes() == b"my new letter\n" * 500         # the change is NOT lost
    assert rr.changed_kept == ["Docs/letter.txt"] and rr.verdict == "attention", rr.summary()
    assert not (d / "-leading-dash.txt.1").exists()                              # rot backup cleaned
    assert "letter.txt.1" in rr.summary()


@needs_par2
def test_repair_never_deletes_a_users_own_dot_one_file(tmp_path):
    files = make_tree(str(tmp_path))
    mine = tmp_path / "Docs" / "letter.txt.1"
    mine.write_bytes(b"my rotated log\n" * 10)
    assert protect(str(tmp_path)).ok
    rot(str(tmp_path / "Docs" / "letter.txt"))
    rr = repair(str(tmp_path))
    assert "Docs/letter.txt" in rr.repaired and (tmp_path / "Docs" / "letter.txt").read_bytes() == files["Docs/letter.txt"]
    assert mine.read_bytes() == b"my rotated log\n" * 10
    assert not (tmp_path / "Docs" / "letter.txt.2").exists()          # par2's own backup (next free number) was removed
    assert repair(str(tmp_path)).verdict == "ok" and mine.exists()    # a healthy run removes nothing


# --- The trustee's check script -------------------------------------------------------

@needs_par2
@pytest.mark.skipif(not shutil.which("shasum"), reason="shasum missing")
def test_check_script_handles_escaped_names_and_reports_damage(tmp_path):
    files = make_tree(str(tmp_path))
    (tmp_path / "Docs" / "new\nline.txt").write_bytes(b"n" * 3000)
    assert not protect(str(tmp_path)).ok                              # a control character: no parity, still listed
    env = dict(os.environ, PATH="/sbin:/usr/sbin:" + os.environ.get("PATH", ""))   # a Mac's BSD sha256sum first
    out = subprocess.run(["bash", CHECK_COMMAND], cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
    assert out.returncode == 0 and "healthy" in out.stdout, out.stdout + out.stderr
    rot(str(tmp_path / "Docs" / "new\nline.txt"), offset=5, length=5)
    out = subprocess.run(["bash", CHECK_COMMAND], cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
    assert out.returncode != 0 and "healthy" not in out.stdout, out.stdout + out.stderr
    assert "FAILED" in out.stdout or "not" in out.stdout


@needs_par2
@pytest.mark.skipif(not shutil.which("shasum"), reason="shasum missing")
def test_check_script_counts_lines_a_bad_tool_skipped(tmp_path):
    make_tree(str(tmp_path))
    (tmp_path / "Docs" / "back\\slash.txt").write_bytes(b"b" * 3000)
    assert not protect(str(tmp_path)).ok
    shim = tmp_path / "_bin"
    shim.mkdir()
    (shim / "uname").write_text("#!/bin/sh\necho Linux\n")
    # a sha256sum that, like the BSD one, skips escaped lines and still exits 0
    (shim / "sha256sum").write_text(
        "#!/bin/bash\n"
        "if [ \"$1\" = --version ]; then echo 'sha256sum (GNU coreutils) 9.0'; exit 0; fi\n"
        "t=$(mktemp); grep -v '^\\\\' SHA256SUMS > \"$t\"; shasum -a 256 -c \"$t\"; rm -f \"$t\"\n"
        "echo 'sha256sum: WARNING: 1 lines are improperly formatted' >&2; exit 0\n")
    for f in shim.iterdir():
        f.chmod(0o755)
    env = dict(os.environ, PATH=str(shim) + ":" + os.environ.get("PATH", ""))
    out = subprocess.run(["bash", CHECK_COMMAND], cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
    assert out.returncode != 0 and "healthy" not in out.stdout and "were checked" in out.stdout, out.stdout + out.stderr


@needs_par2
@pytest.mark.skipif(not shutil.which("shasum"), reason="shasum missing")
def test_check_script_explains_lost_parity(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    for vol in _vols(tmp_path / "Docs"):
        os.remove(vol)
    out = subprocess.run(["bash", CHECK_COMMAND], cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode != 0 and "repair data" in out.stdout and "not affected" in out.stdout, out.stdout


# --- Copy and preflight --------------------------------------------------------------

@needs_par2
def test_preflight_estimate_is_not_below_what_par2_writes(tmp_path):
    src = tmp_path / "src"
    (src / "many").mkdir(parents=True)
    for i in range(300):
        (src / "many" / f"f{i:03d}.bin").write_bytes(os.urandom(2000 + i * 7))
    rep = run_preflight(str(src), str(tmp_path / "dst"))
    est = rep.parity_estimate_bytes
    c = copy_tree(str(src), str(tmp_path / "dst"))
    assert protect(str(tmp_path / "dst"), known_hashes=c.hashes).ok
    actual = sum(p.stat().st_size for p in (tmp_path / "dst" / "many").iterdir() if p.name.startswith("PARITY"))
    assert est >= actual, (est, actual)
    assert est <= actual * 3, (est, actual)


def test_copy_clears_the_finder_lock_flag(tmp_path):
    if not hasattr(os, "chflags"):
        pytest.skip("no chflags here")
    src = tmp_path / "src"
    src.mkdir()
    (src / "locked.bin").write_bytes(b"L" * 4000)
    os.chflags(src / "locked.bin", stat.UF_IMMUTABLE)
    try:
        c = copy_tree(str(src), str(tmp_path / "dst"))
        assert c.ok
        assert not (os.lstat(tmp_path / "dst" / "locked.bin").st_flags & stat.UF_IMMUTABLE)
        with open(tmp_path / "dst" / "locked.bin", "ab") as f:      # par2 must be able to rewrite it later
            f.write(b"!")
    finally:
        os.chflags(src / "locked.bin", 0)


def test_preflight_refuses_names_par2_cannot_store(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.txt").write_bytes(b"x" * 100)
    (src / "bad\\name.txt").write_bytes(b"x" * 100)
    rep = run_preflight(str(src), str(tmp_path / "dst"))
    assert any("cannot be protected" in e for e in rep.errors), rep.summary()


# --- par2 wrapper -------------------------------------------------------------------

def test_set_files_on_disk_is_strict(tmp_path):
    for n in ("PARITY.par2", "PARITY.vol000+10.par2", "PARITY.volume.par2", "PARITY-2.par2", "PARITY.par2.bak", "xPARITY.par2"):
        (tmp_path / n).write_bytes(b"")
    assert p2.set_files_on_disk(str(tmp_path), "PARITY") == ["PARITY.par2", "PARITY.vol000+10.par2"]
    assert p2.set_files_on_disk(str(tmp_path), "PARITY-2") == ["PARITY-2.par2"]
    assert p2.recovery_blocks_in_name("PARITY.vol000+10.par2") == 10 and p2.recovery_blocks_in_name("PARITY.par2") is None


@needs_par2
def test_scan_flags_trailing_partial_packet(tmp_path):
    (tmp_path / "a.bin").write_bytes(os.urandom(50000))
    plan = p2.plan_blocks([50000], 10)
    exe = p2.find_par2()
    assert p2.create(exe, str(tmp_path), "PARITY", ["a.bin"], plan, memory_mb=256, threads=2).status == "ok"
    vol = _vols(tmp_path)[0]
    with open(vol, "ab") as f:
        f.write(b"PAR2\0PKT" + b"\0" * 20)            # a packet header cut short
    s = p2.scan_par2_file(str(vol))
    assert s.truncated


@needs_par2
def test_create_never_leaves_a_folder_without_parity(tmp_path, monkeypatch):
    (tmp_path / "a.bin").write_bytes(os.urandom(50000))
    plan = p2.plan_blocks([50000], 10)
    exe = p2.find_par2()
    assert p2.create(exe, str(tmp_path), "PARITY", ["a.bin"], plan, memory_mb=256, threads=2).status == "ok"
    before = {n: (tmp_path / n).read_bytes() for n in p2.set_files_on_disk(str(tmp_path), "PARITY")}
    # a create that fails (bad block size) must not have touched the old set
    bad = p2.BlockPlan(block_size=4098, source_blocks=1, recovery_blocks=1, parity_bytes=4098)
    r = p2.create(exe, str(tmp_path), "PARITY", ["a.bin"], bad, memory_mb=256, threads=2)
    assert r.status == "error"
    assert {n: (tmp_path / n).read_bytes() for n in p2.set_files_on_disk(str(tmp_path), "PARITY")} == before
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".cold-archive-build")]


def test_bundled_fast_par2_only_where_it_runs(tmp_path, monkeypatch):
    import platform
    b = tmp_path / "bin"
    b.mkdir()
    (b / "par2").write_bytes(b"")
    (b / "par2-fast").write_bytes(b"")
    monkeypatch.setattr(p2.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("15.4", ("", "", ""), ""))
    (b / "par2-fast.minos").write_text("26.0 arm64\n")
    c = p2.bundled_par2_candidates()
    assert str(b / "par2") in c and str(b / "par2-fast") not in c          # too old a macOS
    (b / "par2-fast.minos").write_text("13.0 arm64\n")
    c = p2.bundled_par2_candidates()
    assert c.index(str(b / "par2-fast")) < c.index(str(b / "par2"))       # eligible: preferred
    (b / "par2-fast.minos").write_text("13.0 x86_64\n")
    assert str(b / "par2-fast") not in p2.bundled_par2_candidates()        # wrong architecture


def test_copy_resume_replaces_a_same_size_impostor(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(b"A" * 20000)
    dst = tmp_path / "dst"
    assert copy_tree(str(src), str(dst)).ok
    (dst / "a.bin").write_bytes(b"B" * 20000)          # same size, different bytes: a corrupted earlier copy
    c = copy_tree(str(src), str(dst))
    assert c.ok and c.files_copied == 1 and c.files_skipped_identical == 0
    assert (dst / "a.bin").read_bytes() == b"A" * 20000
    assert c.hashes["a.bin"].sha256 == p2.hashlib.sha256(b"A" * 20000).hexdigest()

@needs_par2
def test_clean_backups_survives_unreadable_sums(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    rot(str(d / "letter.txt"))
    # Corrupt SUMS_FILE so read_sums raises UnicodeError
    (tmp_path / SUMS_FILE).write_bytes(b"\xff\xff\xffnot-utf8")
    for p in tmp_path.glob("PARITY-INDEX*.par2"):
        p.unlink()
    
    rr = repair(str(tmp_path))
    # It should not crash, and the backup should be kept
    assert (d / "letter.txt.1").exists()
    assert "Docs/letter.txt.1" in rr.backups_kept

@needs_par2
def test_verify_hashes_mode_includes_parity_files(tmp_path, monkeypatch):
    from cold_archive.engine import verify as ver_module
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok

    called_with = []
    orig_hash_many = ver_module.hash_many
    def mock_hash_many(to_check, **kwargs):
        called_with.extend(to_check)
        return orig_hash_many(to_check, **kwargs)
        
    monkeypatch.setattr(ver_module, "hash_many", mock_hash_many)
    
    verify(str(tmp_path), VerifyOptions(mode="standard"))
    standard_files = [rel for rel, _, _ in called_with]
    assert not any("PARITY.par2" in rel for rel in standard_files)
    
    called_with.clear()
    verify(str(tmp_path), VerifyOptions(mode="hashes"))
    hashes_files = [rel for rel, _, _ in called_with]
    assert any("PARITY.par2" in rel for rel in hashes_files)
    assert any("PARITY.vol" in rel for rel in hashes_files)
