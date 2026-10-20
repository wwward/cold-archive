"""Behaviours pinned after the second adversarial review (2026-09-04/05): the
residual defects the fix-reproduction agents found behind the first round of
fixes, and the new findings of the protect and copy lenses. Every test reads
the disk back; none trusts a summary string alone."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from cold_archive.engine import par2 as p2
from cold_archive.engine.copy import PARTIAL_PREFIX, copy_tree
from cold_archive.engine.layout import INDEX_PARITY_BASENAME, MANIFEST_FILE, SUMS_FILE, TOOLS_DIR, is_junk, name_problem
from cold_archive.engine.manifest import load_manifest, read_sums, save_manifest
from cold_archive.engine.observer import Cancelled, Observer
from cold_archive.engine.preflight import name_collisions, run_preflight, windows_unopenable
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.repair import repair
from cold_archive.engine.resources import BUILD_NOTES_NAME
from cold_archive.engine.scan import scan_archive
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import make_tree, needs_par2, rot


def _sha(data: bytes) -> str:
    return p2.hashlib.sha256(data).hexdigest()


def _rename_keeping_date(src: str, dst: str) -> None:
    st = os.stat(src)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def _set_files(d) -> dict:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.name.startswith("PARITY") and p.name.endswith(".par2")}


# --- renamed and moved files ----------------------------------------------------

@needs_par2
def test_renamed_file_with_rot_is_not_laundered(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    old_parity = _set_files(tmp_path / "Docs")
    _rename_keeping_date(str(tmp_path / "Docs" / "letter.txt"), str(tmp_path / "Docs" / "letter-renamed.txt"))
    rot(str(tmp_path / "Docs" / "letter-renamed.txt"))
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert not r.ok and r.rot_suspected == ["Docs/letter-renamed.txt"], r.summary()
    assert r.suspected_renames == [("Docs/letter.txt", "Docs/letter-renamed.txt")]
    sums = read_sums(str(tmp_path / SUMS_FILE))
    assert sums["Docs/letter.txt"] == _sha(files["Docs/letter.txt"])      # the old record was kept
    assert "Docs/letter-renamed.txt" not in sums                           # the rotted bytes were not recorded
    assert _set_files(tmp_path / "Docs") == old_parity                     # the parity that can rebuild it is intact
    rr = repair(str(tmp_path))
    assert (tmp_path / "Docs" / "letter.txt").read_bytes() == files["Docs/letter.txt"], rr.summary()


@needs_par2
def test_moved_file_with_rot_is_not_laundered(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    old_parity = _set_files(tmp_path / "Photos" / "2019")
    _rename_keeping_date(str(tmp_path / "Photos" / "2019" / "IMG_001.jpg"), str(tmp_path / "Docs" / "IMG_001.jpg"))
    rot(str(tmp_path / "Docs" / "IMG_001.jpg"))
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert not r.ok and r.rot_suspected == ["Docs/IMG_001.jpg"], r.summary()
    sums = read_sums(str(tmp_path / SUMS_FILE))
    assert sums["Photos/2019/IMG_001.jpg"] == _sha(files["Photos/2019/IMG_001.jpg"]) and "Docs/IMG_001.jpg" not in sums
    assert _set_files(tmp_path / "Photos" / "2019") == old_parity
    rr = repair(str(tmp_path))
    assert (tmp_path / "Photos" / "2019" / "IMG_001.jpg").read_bytes() == files["Photos/2019/IMG_001.jpg"], rr.summary()


@needs_par2
def test_moved_file_with_intact_contents_is_simply_recorded(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    _rename_keeping_date(str(tmp_path / "Photos" / "2019" / "IMG_001.jpg"), str(tmp_path / "Docs" / "IMG_001.jpg"))
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert r.ok and r.files_moved == [("Photos/2019/IMG_001.jpg", "Docs/IMG_001.jpg")], r.summary()
    sums = read_sums(str(tmp_path / SUMS_FILE))
    assert sums["Docs/IMG_001.jpg"] == _sha(files["Photos/2019/IMG_001.jpg"]) and "Photos/2019/IMG_001.jpg" not in sums
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


# --- rebuild parity is not a way around the checks -------------------------------

@needs_par2
def test_rebuild_parity_never_destroys_the_only_good_parity(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    old_parity = _set_files(tmp_path / "Docs")
    rot(str(tmp_path / "Docs" / "letter.txt"))
    r = protect(str(tmp_path), ProtectOptions(rebuild_parity=True))
    assert not r.ok and r.rot_suspected == ["Docs/letter.txt"], r.summary()
    assert _set_files(tmp_path / "Docs") == old_parity
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] == _sha(files["Docs/letter.txt"])
    rr = repair(str(tmp_path))
    assert (tmp_path / "Docs" / "letter.txt").read_bytes() == files["Docs/letter.txt"], rr.summary()
    # the CLI flag goes through the same path
    from cold_archive.engine.cli import main
    rot(str(tmp_path / "Docs" / "letter.txt"))
    assert main(["-q", "protect", "--rebuild-parity", str(tmp_path)]) == 2
    assert (tmp_path / "Docs" / "letter.txt").read_bytes() != files["Docs/letter.txt"]
    assert "Docs/letter.txt" in repair(str(tmp_path)).repaired


# --- create() checks contents, not just names -------------------------------------

@needs_par2
def test_create_refuses_a_set_that_disagrees_with_the_records(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    old_parity = _set_files(d)
    exe = p2.find_par2()
    rows = [("letter.txt", len(files["Docs/letter.txt"]), "0" * 32), ("-leading-dash.txt", len(files["Docs/-leading-dash.txt"]), "0" * 32)]
    plan = p2.plan_blocks([s for _, s, _ in rows], 10)
    r = p2.create(exe, str(d), "PARITY", [n for n, _, _ in rows], plan, memory_mb=256, threads=2, expect=rows)
    assert r.status == "error" and "differ from their records" in r.message, r.message
    assert _set_files(d) == old_parity
    assert not [p for p in d.iterdir() if p.name.startswith(".cold-archive")]


@needs_par2
def test_flipped_md5_in_the_manifest_is_corrected_not_looped(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    m = load_manifest(str(tmp_path / MANIFEST_FILE))
    e = m.files["Docs/letter.txt"]
    good_md5 = e.md5
    e.md5 = ("f" if e.md5[0] != "f" else "0") + e.md5[1:]
    save_manifest(str(tmp_path / MANIFEST_FILE), m)
    r = protect(str(tmp_path))
    assert r.ok, r.summary()
    assert load_manifest(str(tmp_path / MANIFEST_FILE)).files["Docs/letter.txt"].md5 == good_md5
    r2 = protect(str(tmp_path))
    assert r2.ok and r2.count("created") == 0, r2.summary()
    assert verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_flipped_md5_over_a_rotted_file_is_caught(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    m = load_manifest(str(tmp_path / MANIFEST_FILE))
    e = m.files["Docs/letter.txt"]
    e.md5 = ("f" if e.md5[0] != "f" else "0") + e.md5[1:]
    save_manifest(str(tmp_path / MANIFEST_FILE), m)
    old_parity = _set_files(tmp_path / "Docs")
    rot(str(tmp_path / "Docs" / "letter.txt"))
    r = protect(str(tmp_path))
    assert not r.ok and any(s.action == "blocked" and s.directory == "Docs" for s in r.sets), r.summary()
    assert _set_files(tmp_path / "Docs") == old_parity
    assert (tmp_path / "Docs" / "letter.txt").read_bytes() != files["Docs/letter.txt"]
    assert "Docs/letter.txt" in repair(str(tmp_path)).repaired


# --- a lost index does not erase the witness --------------------------------------

@needs_par2
def test_lost_index_still_witnesses_a_renamed_folder(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / "Photos" / "2019" / "PARITY.par2")
    volumes = _set_files(tmp_path / "Photos" / "2019")
    os.rename(tmp_path / "Photos" / "2019", tmp_path / "Photos" / "2020")
    rot(str(tmp_path / "Photos" / "2020" / "IMG_001.jpg"))
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True, accept_changes=True))
    assert not r.ok and r.rot_suspected == ["Photos/2020/IMG_001.jpg"], r.summary()
    assert _set_files(tmp_path / "Photos" / "2020") == volumes
    assert "Photos/2020/IMG_001.jpg" not in read_sums(str(tmp_path / SUMS_FILE))
    # a second plain run does not launder it either
    r = protect(str(tmp_path))
    assert not r.ok and r.rot_suspected == ["Photos/2020/IMG_001.jpg"], r.summary()
    assert _set_files(tmp_path / "Photos" / "2020") == volumes
    rr = repair(str(tmp_path))
    assert (tmp_path / "Photos" / "2020" / "IMG_001.jpg").read_bytes() == files["Photos/2019/IMG_001.jpg"], rr.summary()


# --- a file only the parity knows can be repaired --------------------------------

@needs_par2
def test_a_file_only_the_parity_knows_is_repaired_and_the_folder_recovers(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.rename(tmp_path / "Photos" / "2019", tmp_path / "Photos" / "2020")
    rot(str(tmp_path / "Photos" / "2020" / "IMG_001.jpg"))
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert not r.ok and r.rot_suspected == ["Photos/2020/IMG_001.jpg"], r.summary()
    v = verify(str(tmp_path))
    assert v.verdict != "ok" and any(s.directory == "Photos/2020" and s.status == "repairable" for s in v.sets), v.summary()
    rr = repair(str(tmp_path))
    assert (tmp_path / "Photos" / "2020" / "IMG_001.jpg").read_bytes() == files["Photos/2019/IMG_001.jpg"], rr.summary()
    assert "Photos/2020/IMG_001.jpg" in rr.changed_kept and (tmp_path / "Photos" / "2020" / "IMG_001.jpg.1").exists()
    os.remove(tmp_path / "Photos" / "2020" / "IMG_001.jpg.1")
    r = protect(str(tmp_path))
    assert r.ok, r.summary()
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


# --- records: the SHA256SUMS fallback keeps unreadable files' records -------------

@needs_par2
def test_sums_fallback_keeps_an_unreadable_files_record(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / MANIFEST_FILE)
    for p in tmp_path.glob("PARITY-INDEX*.par2"):
        p.unlink()
    target = tmp_path / "Docs" / "letter.txt"
    os.chmod(target, 0)
    try:
        r = protect(str(tmp_path))
        assert not r.ok and any(rel == "Docs/letter.txt" for rel, _ in r.hash_failures), r.summary()
        assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] == _sha(files["Docs/letter.txt"])
        assert "Docs/letter.txt" in load_manifest(str(tmp_path / MANIFEST_FILE)).files
    finally:
        os.chmod(target, 0o644)
    assert protect(str(tmp_path)).ok


@needs_par2
def test_missing_sums_with_index_points_at_repair_and_without_index_rereads(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / SUMS_FILE)
    r = protect(str(tmp_path))
    assert not r.ok and r.count("created") == 0 and "Repair" in " ".join(r.errors), r.summary()
    rr = repair(str(tmp_path))
    assert SUMS_FILE in rr.records_restored and verify(str(tmp_path)).verdict == "ok", rr.summary()
    # no index: the manifest alone, every file re-read; a flipped digit means
    # the file reads as damaged rather than being sealed into fresh records
    os.remove(tmp_path / SUMS_FILE)
    for p in tmp_path.glob("PARITY-INDEX*.par2"):
        p.unlink()
    m = load_manifest(str(tmp_path / MANIFEST_FILE))
    e = m.files["Docs/letter.txt"]
    e.sha256 = ("f" if e.sha256[0] != "f" else "0") + e.sha256[1:]
    save_manifest(str(tmp_path / MANIFEST_FILE), m)
    r = protect(str(tmp_path))
    assert not r.ok and r.rot_suspected == ["Docs/letter.txt"] and r.files_hashed == 7, r.summary()


@needs_par2
def test_parity_file_hash_the_records_disagree_on_is_rehashed(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    m = load_manifest(str(tmp_path / MANIFEST_FILE))
    rel = "Docs/PARITY.par2"
    true_sha = read_sums(str(tmp_path / SUMS_FILE))[rel]
    m.parity[rel].sha256 = ("f" if true_sha[0] != "f" else "0") + true_sha[1:]
    save_manifest(str(tmp_path / MANIFEST_FILE), m)
    assert protect(str(tmp_path)).ok
    assert read_sums(str(tmp_path / SUMS_FILE))[rel] == true_sha
    assert load_manifest(str(tmp_path / MANIFEST_FILE)).parity[rel].sha256 == true_sha


@needs_par2
def test_file_only_sha256sums_knows_is_previous_truth(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    m = load_manifest(str(tmp_path / MANIFEST_FILE))
    del m.files["Docs/letter.txt"]
    save_manifest(str(tmp_path / MANIFEST_FILE), m)
    for p in (tmp_path / "Docs").glob("PARITY*.par2"):
        p.unlink()
    for p in tmp_path.glob("PARITY-INDEX*.par2"):
        p.unlink()
    rot(str(tmp_path / "Docs" / "letter.txt"))
    r = protect(str(tmp_path))
    assert not r.ok and r.rot_suspected == ["Docs/letter.txt"], r.summary()
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] == _sha(files["Docs/letter.txt"])


# --- Check sees stale split sets and index parity loss ---------------------------

@needs_par2
def test_split_folder_stale_set_is_seen_in_standard_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(p2, "MAX_FILES_PER_SET", 1)
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    assert (d / "PARITY-2.par2").exists()
    old = {p.name: p.read_bytes() for p in d.glob("PARITY-2*.par2")}
    (d / "letter.txt").write_bytes(files["Docs/letter.txt"] + b"edited\n")
    assert protect(str(tmp_path), ProtectOptions(accept_changes=True)).ok
    for n, data in old.items():
        (d / n).write_bytes(data)
    v = verify(str(tmp_path))
    assert v.verdict == "attention", v.summary()
    assert any(s.directory == "Docs" and s.set_name == "PARITY-2" and s.status == "stale" for s in v.sets), v.summary()


@needs_par2
def test_index_parity_loss_is_seen_by_check(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    vols = sorted(p for p in tmp_path.glob("PARITY-INDEX.vol*.par2"))
    assert len(vols) >= 3, [v.name for v in vols]
    for p in vols[: len(vols) // 2]:
        p.unlink()
    v = verify(str(tmp_path))
    assert v.verdict == "attention", v.summary()
    st = next(s for s in v.sets if s.set_name == INDEX_PARITY_BASENAME)
    assert st.status == "degraded" and st.recovery_blocks < st.recovery_expected, v.summary()
    r = protect(str(tmp_path))
    assert r.ok and any(s.set_name == INDEX_PARITY_BASENAME and s.action == "created" for s in r.sets), r.summary()


@needs_par2
def test_block_size_policy_change_rebuilds_every_set(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    r = protect(str(tmp_path), ProtectOptions(min_block_size=256 * 1024))
    assert r.ok and r.count("created") >= 3 and r.count("kept") == 0, r.summary()
    assert p2.read_index(str(tmp_path / "Docs" / "PARITY.par2")).block_size == 262144


@needs_par2
def test_a_volume_from_another_set_is_not_counted(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    docs = sorted((tmp_path / "Docs").glob("PARITY.vol*.par2"))
    photos = sorted((tmp_path / "Photos" / "2019").glob("PARITY.vol*.par2"))
    docs[0].write_bytes(photos[0].read_bytes())
    v = verify(str(tmp_path))
    assert v.verdict == "attention", v.summary()
    st = next(s for s in v.sets if s.directory == "Docs")
    assert st.status == "degraded" and "different parity set" in st.message, v.summary()
    r = protect(str(tmp_path))
    assert r.ok and any(s.directory == "Docs" and s.action == "created" for s in r.sets), r.summary()
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


# --- Repair keeps the only copy when it cannot judge ------------------------------

@needs_par2
def test_repair_without_manifest_keeps_the_only_copy_of_an_edit(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / MANIFEST_FILE)
    for p in tmp_path.glob("PARITY-INDEX*.par2"):
        p.unlink()
    p = tmp_path / "Docs" / "letter.txt"
    time.sleep(0.01)
    p.write_bytes(files["Docs/letter.txt"][:-40] + b"my edit" + b"X" * 33)
    my_edit = p.read_bytes()
    rr = repair(str(tmp_path))
    assert p.read_bytes() == files["Docs/letter.txt"], rr.summary()
    assert (tmp_path / "Docs" / "letter.txt.1").read_bytes() == my_edit
    assert "Docs/letter.txt" in rr.changed_kept and ".1" in rr.summary()


# --- names that collide with ours ------------------------------------------------

@needs_par2
def test_case_variant_of_a_parity_name_is_refused_and_survives(tmp_path):
    make_tree(str(tmp_path))
    user = tmp_path / "Docs" / "parity.par2"
    user.write_bytes(b"not ours " * 100)
    r = protect(str(tmp_path))
    assert not r.ok and any(rel == "Docs/parity.par2" for rel, _ in r.unprotectable), r.summary()
    assert user.read_bytes() == b"not ours " * 100
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/parity.par2"] == _sha(b"not ours " * 100)
    assert name_problem("Parity.vol00+2.par2") and name_problem("PARITY-INDEX.PAR2") and not name_problem("PARITY.par2.bak")


@needs_par2
def test_index_named_file_in_a_subfolder_is_payload(tmp_path):
    make_tree(str(tmp_path))
    user = tmp_path / "Docs" / "PARITY-INDEX.par2"
    user.write_bytes(b"someone else's index " * 50)
    r = protect(str(tmp_path))
    assert r.ok, r.summary()
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/PARITY-INDEX.par2"] == _sha(user.read_bytes())
    v = verify(str(tmp_path), VerifyOptions(mode="thorough"))
    assert v.verdict == "ok" and not any(s.directory == "Docs" and s.set_name == INDEX_PARITY_BASENAME for s in v.sets), v.summary()


# --- the swap ---------------------------------------------------------------------

@needs_par2
def test_swap_failure_never_leaves_a_folder_without_parity(tmp_path, monkeypatch):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    old = _set_files(d)
    (d / "letter.txt").write_bytes(files["Docs/letter.txt"] + b"more\n")
    real_replace = os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        if os.path.dirname(os.path.abspath(dst)) == str(d) and os.path.basename(dst).startswith(".cold-archive-new-"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(5, "Input/output error")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_replace)
    r = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    monkeypatch.setattr(os, "replace", real_replace)
    assert not r.ok and any(s.directory == "Docs" and s.action == "failed" for s in r.sets), r.summary()
    assert _set_files(d) == old                                    # the old set is untouched
    assert not [p for p in d.iterdir() if p.name.startswith(".cold-archive")]
    r = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    assert r.ok and verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok", r.summary()


def test_stale_build_dir_cleanup_is_strict(tmp_path):
    ours = tmp_path / ".cold-archive-build-12345"
    ours.mkdir()
    (ours / "PARITY.par2").write_bytes(b"x")
    theirs = tmp_path / ".cold-archive-build-notes"
    theirs.mkdir()
    (theirs / "keep.txt").write_bytes(b"mine")
    mixed = tmp_path / ".cold-archive-build-777"
    mixed.mkdir()
    (mixed / "photo.jpg").write_bytes(b"mine too")
    removed = p2.remove_stale_build_dirs(str(tmp_path))
    assert removed == [".cold-archive-build-12345"]
    assert (theirs / "keep.txt").exists() and (mixed / "photo.jpg").exists()


# --- TOOLS/ never overwrites anything ---------------------------------------------

@needs_par2
def test_tools_never_overwrites_the_owners_files_or_writes_through_a_link(tmp_path, monkeypatch):
    from cold_archive.engine import resources
    tarball = tmp_path / "par2cmdline-1.3.0.tar.bz2"
    tarball.write_bytes(b"T" * 300_000)
    monkeypatch.setattr(resources, "par2_source_tarball", lambda: str(tarball))
    arc = tmp_path / "arc"
    make_tree(str(arc))
    (arc / TOOLS_DIR).mkdir()
    (arc / TOOLS_DIR / BUILD_NOTES_NAME).write_text("my own notes\n")
    (arc / TOOLS_DIR / "par2cmdline-1.3.0.tar.bz2").write_bytes(b"different size")
    r = protect(str(arc))
    assert r.ok, r.summary()
    assert (arc / TOOLS_DIR / BUILD_NOTES_NAME).read_text() == "my own notes\n"
    assert (arc / TOOLS_DIR / "par2cmdline-1.3.0.tar.bz2").read_bytes() == b"different size"
    assert not [p for p in (arc / TOOLS_DIR).iterdir() if p.name.endswith(".cold-archive-tmp")]
    # a TOOLS symlink: nothing is written through it
    arc2 = tmp_path / "arc2"
    make_tree(str(arc2))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), str(arc2 / TOOLS_DIR))
    r = protect(str(arc2))
    assert r.ok and any("not a real folder" in w for w in r.warnings), r.summary()
    assert not list(elsewhere.iterdir())
    # a file named TOOLS is not a crash either
    arc3 = tmp_path / "arc3"
    make_tree(str(arc3))
    (arc3 / TOOLS_DIR).write_text("a note called TOOLS")
    r = protect(str(arc3))
    assert r.ok and (arc3 / TOOLS_DIR).read_text() == "a note called TOOLS", r.summary()


def test_tools_temporaries_are_junk():
    assert is_junk("par2cmdline-1.3.0.tar.bz2.cold-archive-tmp") and is_junk(".cold-archive-new-PARITY.par2")
    assert is_junk(PARTIAL_PREFIX + "0123456789abcdef") and not is_junk("report.tmp")


# --- unreadable folders and files -------------------------------------------------

@needs_par2
def test_unreadable_folder_keeps_its_records(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    d = tmp_path / "Docs"
    os.chmod(d, 0)
    try:
        r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
        assert not r.ok and r.files_removed == [], r.summary()
        assert any(rel.startswith("Docs/") for rel, _ in r.hash_failures)
        sums = read_sums(str(tmp_path / SUMS_FILE))
        assert sums["Docs/letter.txt"] == _sha(files["Docs/letter.txt"]) and "Docs/-leading-dash.txt" in sums
        assert "Docs/letter.txt" in load_manifest(str(tmp_path / MANIFEST_FILE)).files
        v = verify(str(tmp_path))
        assert v.verdict == "problem" and any(p.kind == "unreadable" and p.rel == "Docs" for p in v.problems), v.summary()
    finally:
        os.chmod(d, 0o755)
    assert protect(str(tmp_path)).ok and verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_check_reports_an_unreadable_file(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    p = tmp_path / "Docs" / "letter.txt"
    os.chmod(p, 0)
    try:
        v = verify(str(tmp_path))
        assert v.verdict == "problem" and any(x.kind == "unreadable" and x.rel == "Docs/letter.txt" for x in v.problems), v.summary()
        assert "UNREADABLE" in v.summary()
    finally:
        os.chmod(p, 0o644)


def test_copy_reports_an_unreadable_source_file(tmp_path):
    src = tmp_path / "src"
    make_tree(str(src))
    p = src / "Docs" / "letter.txt"
    os.chmod(p, 0)
    try:
        r = copy_tree(str(src), str(tmp_path / "dst"))
        assert not r.ok and any(rel == "Docs/letter.txt" for rel, _ in r.failures), r.summary()
        assert not (tmp_path / "dst" / "Docs" / "letter.txt").exists()
        assert not [x for x in (tmp_path / "dst" / "Docs").iterdir() if x.name.startswith(PARTIAL_PREFIX)]
    finally:
        os.chmod(p, 0o644)
    rep = run_preflight(str(src), str(tmp_path / "dst2"))
    assert rep.ok, rep.summary()


# --- copy: reserved names, locks, temporaries, folders ---------------------------

def test_copy_refuses_reserved_names_unless_the_source_is_an_archive(tmp_path):
    src = tmp_path / "src"
    make_tree(str(src))
    (src / SUMS_FILE).write_text("upstream checksums\n")
    (src / "Docs" / "PARITY.par2").write_bytes(b"someone's par2 file")
    r = copy_tree(str(src), str(tmp_path / "dst"))
    assert not r.ok and {rel for rel, _ in r.failures} == {SUMS_FILE, "Docs/PARITY.par2"}, r.summary()
    rep = run_preflight(str(src), str(tmp_path / "dst2"))
    assert not rep.ok and any("reserved" in e for e in rep.errors), rep.summary()
    # an archive copied to a fresh medium is the expected case: a note, not a failure
    (src / MANIFEST_FILE).write_text('{"format": "cold-archive-manifest", "format_version": 1, "files": {}}')
    r = copy_tree(str(src), str(tmp_path / "dst3"))
    assert r.ok and any("itself a Cold Archive" in w for w in r.warnings), r.summary()
    assert not (tmp_path / "dst3" / SUMS_FILE).exists()


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="no chflags")
def test_copy_resume_unlocks_an_identical_locked_file(tmp_path):
    src = tmp_path / "src"
    files = make_tree(str(src))
    dst = tmp_path / "dst"
    assert copy_tree(str(src), str(dst)).ok
    locked = dst / "Docs" / "letter.txt"
    os.chflags(str(locked), stat.UF_IMMUTABLE)
    changed = dst / "Photos" / "2019" / "IMG_001.jpg"
    os.chflags(str(changed), stat.UF_IMMUTABLE)
    (src / "Photos" / "2019" / "IMG_001.jpg").write_bytes(files["Photos/2019/IMG_001.jpg"] + b"new")
    r = copy_tree(str(src), str(dst))
    assert r.ok, r.summary()
    assert not (os.stat(locked).st_flags & stat.UF_IMMUTABLE)
    assert changed.read_bytes() == files["Photos/2019/IMG_001.jpg"] + b"new" and not (os.stat(changed).st_flags & stat.UF_IMMUTABLE)


def test_copy_temporaries_are_short_and_leftovers_are_removed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    long_name = "x" * 250 + ".txt"
    (src / long_name).write_bytes(b"long name")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / (PARTIAL_PREFIX + "deadbeefdeadbeef")).write_bytes(b"half a file")
    (dst / "old.coldarchive-partial").write_bytes(b"older temp")
    r = copy_tree(str(src), str(dst))
    assert r.ok and r.partials_removed == 2, r.summary()
    assert (dst / long_name).read_bytes() == b"long name"
    assert sorted(p.name for p in dst.iterdir()) == [long_name]


def test_copy_reports_a_folder_it_cannot_create(tmp_path):
    src = tmp_path / "src"
    make_tree(str(src))
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "Docs").write_text("a file where a folder must go")
    r = copy_tree(str(src), str(dst))
    assert not r.ok, r.summary()
    assert any(rel == "Docs" for rel, _ in r.failures) and any(rel == "Docs/letter.txt" for rel, _ in r.failures)
    assert (dst / "Photos" / "2019" / "IMG_001.jpg").exists()


def test_preflight_warns_about_a_non_empty_destination(tmp_path):
    src = tmp_path / "src"
    make_tree(str(src))
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "other.txt").write_text("already here")
    rep = run_preflight(str(src), str(dst))
    assert rep.ok and any("not empty" in w for w in rep.warnings), rep.summary()
    (dst / MANIFEST_FILE).write_text('{"format": "cold-archive-manifest", "format_version": 1, "files": {}}')
    rep = run_preflight(str(src), str(dst))
    assert any("already holds a Cold Archive" in w for w in rep.warnings), rep.summary()


def test_preflight_counts_off_what_is_already_on_the_destination(tmp_path):
    src = tmp_path / "src"
    make_tree(str(src))
    dst = tmp_path / "dst"
    before = run_preflight(str(src), str(dst))
    assert copy_tree(str(src), str(dst)).ok
    after = run_preflight(str(src), str(dst))
    assert after.already_on_destination_bytes == before.payload_bytes
    assert after.total_required_bytes < before.total_required_bytes


def test_windows_names_are_checked_in_every_path_component(tmp_path):
    src = tmp_path / "src"
    (src / "Trip:Rome").mkdir(parents=True)
    (src / "Trip:Rome" / "a.jpg").write_bytes(b"a")
    (src / "ok").mkdir()
    (src / "ok" / "CON.txt").write_bytes(b"c")
    (src / "ok" / "trailing.").write_bytes(b"d")
    (src / "ok" / "fine.txt").write_bytes(b"e")
    bad = windows_unopenable(scan_archive(str(src)))
    assert bad == ["Trip:Rome", "ok/CON.txt", "ok/trailing."], bad


def test_name_collisions_include_folders():
    # a Linux or NAS source can hold both spellings; the local disk cannot,
    # so the scan is built by hand
    class D:
        def __init__(self, rel):
            self.rel = rel

    class F:
        def __init__(self, rel):
            self.rel = rel

    class S:
        dirs = [D(""), D("Backup"), D("backup"), D("Notes")]
        files = [F("Backup/x.txt"), F("backup/y.txt"), F("notes")]
    pairs = name_collisions(S())
    assert ("Backup", "backup") in pairs and ("Notes", "notes") in pairs, pairs
    assert any(b == "backup/y.txt" for _, b in pairs), pairs


# --- par2 progress and cancel do not wait for par2 to exit -----------------------

def test_progress_and_cancel_do_not_wait_for_par2_exit(tmp_path):
    fake = tmp_path / "par2"
    fake.write_text("#!/bin/sh\nprintf 'Constructing: 10.0%%\\r'\nsleep 3\nprintf 'Constructing: 100.0%%\\n'\n")
    fake.chmod(0o755)

    class Obs(Observer):
        def __init__(self):
            super().__init__()
            self.first = None
            self.stop = False
            self.t0 = time.monotonic()

        def progress(self, done, total=None, detail=""):
            if self.first is None:
                self.first = time.monotonic() - self.t0
                self.stop = True

        def check_cancel(self):
            if self.stop:
                raise Cancelled()

    obs = Obs()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        p2._run(str(fake), ["c"], str(tmp_path), obs, "test")
    assert obs.first is not None and obs.first < 1.0
    assert time.monotonic() - t0 < 2.0


@needs_par2
def test_parity_mode_reports_a_missing_file_by_name(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / "Docs" / "letter.txt")
    os.remove(tmp_path / SUMS_FILE)
    os.remove(tmp_path / MANIFEST_FILE)
    v = verify(str(tmp_path), VerifyOptions(mode="parity"))
    assert v.verdict == "problem", v.summary()
    assert any(p.kind == "missing" and p.rel == "Docs/letter.txt" for p in v.problems), v.summary()


@needs_par2
def test_added_file_par2_calls_damaged_is_blocked_even_if_the_witness_fails(tmp_path, monkeypatch):
    """Defence in depth: the parity witness normally catches a renamed folder's
    rotted file before the safety check; if it ever cannot (every FileDesc
    packet damaged, say), par2's own verdict on a file the records did not
    know must still block, whatever consent was given for other files."""
    from cold_archive.engine import protect as protect_mod
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    monkeypatch.setattr(protect_mod, "_parity_truth", lambda scan: {})
    os.rename(tmp_path / "Photos" / "2019", tmp_path / "Photos" / "2020")
    old_parity = _set_files(tmp_path / "Photos" / "2020")
    damaged = tmp_path / "Photos" / "2020" / "IMG_001.jpg"
    rot(str(damaged))
    os.utime(damaged, (time.time(), time.time()))   # no size-and-date match with the vanished record either
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True, accept_changes=True))
    assert not r.ok and any(s.directory == "Photos/2020" and s.action == "blocked" for s in r.sets), r.summary()
    assert _set_files(tmp_path / "Photos" / "2020") == old_parity


@needs_par2
def test_index_parity_is_judged_by_its_own_policy(tmp_path):
    """PARITY-INDEX is planned at 50 % with at least 20 recovery blocks; a
    Check that expected the payload policy (10 %, at least 10) would call
    the loss of almost half the records' parity ALL GOOD."""
    d = tmp_path / "many"
    d.mkdir()
    for i in range(1500):
        (d / f"f{i:04d}.txt").write_bytes(b"%04d\n" % i * 20)
    r = protect(str(tmp_path))
    assert r.ok, r.summary()
    st = next(s for s in r.sets if s.set_name == INDEX_PARITY_BASENAME)
    assert st.recovery_blocks >= 20, st
    vols = sorted(p for p in tmp_path.glob("PARITY-INDEX.vol*.par2"))
    assert len(vols) >= 4, [v.name for v in vols]
    # remove volumes until fewer than planned but more than the payload policy would demand
    removed = 0
    for p in vols:
        if st.recovery_blocks - removed - (p2.recovery_blocks_in_name(p.name) or 0) < 11:
            break
        removed += p2.recovery_blocks_in_name(p.name) or 0
        p.unlink()
    assert 0 < st.recovery_blocks - removed < 20
    v = verify(str(tmp_path))
    ix = next(s for s in v.sets if s.set_name == INDEX_PARITY_BASENAME)
    assert v.verdict == "attention" and ix.status == "degraded", v.summary()
