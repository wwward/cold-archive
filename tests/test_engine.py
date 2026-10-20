"""Engine tests on real files with the real par2 binary."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from cold_archive.engine import par2 as p2
from cold_archive.engine.copy import copy_tree
from cold_archive.engine.layout import (CHECK_COMMAND, INDEX_PARITY_BASENAME, MANIFEST_FILE, README_TXT, SUMS_FILE,
                                        is_our_parity_file, nfc, norm_rel, parity_set_of, payload_set_name)
from cold_archive.engine.manifest import Manifest, load_manifest, parse_sums, read_sums, sums_content
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.repair import RepairOptions, repair
from cold_archive.engine.scan import scan_archive
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import make_tree, needs_par2, rot


# ---------------------------------------------------------------- pure units

def test_layout_parity_regex():
    assert parity_set_of("PARITY.par2") == "PARITY"
    assert parity_set_of("PARITY.vol000+016.par2") == "PARITY"
    assert parity_set_of("PARITY-2.par2") == "PARITY-2"
    assert parity_set_of("PARITY-12.vol3+4.par2") == "PARITY-12"
    assert parity_set_of("PARITY-INDEX.par2") == "PARITY-INDEX"
    assert parity_set_of("PARITY-INDEX.vol0+1.par2") == "PARITY-INDEX"
    assert parity_set_of("photos.par2") is None          # a foreign par2 set is payload
    assert parity_set_of("PARITY-1.par2") is None        # never produced; not ours
    assert not is_our_parity_file("PARITY.par2.bak")
    assert payload_set_name(0) == "PARITY" and payload_set_name(1) == "PARITY-2"


def test_norm_rel_keeps_spelling_and_fixes_slashes():
    nfd = "e\u0301tude/a.txt"
    assert norm_rel(nfd) == nfd                      # recorded as spelled on disk: Linux/Windows match bytes
    assert nfc(nfd) == "\u00e9tude/a.txt"           # NFC only for comparing with par2's output
    assert norm_rel(os.path.join("a", "b")) == "a/b"


def test_sums_roundtrip_with_hostile_names():
    digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    names = ["plain.txt", "sp ace/é✓.bin", "back\\slash", "new\nline", "-dash"]
    text = sums_content([(n, digest) for n in names]).decode("utf-8")
    parsed = parse_sums(text)
    assert parsed == {n: digest for n in names}
    # tolerate the text-mode (two spaces) and CRLF variants other tools write
    assert parse_sums(f"{digest}  a/b.txt\r\n") == {"a/b.txt": digest}
    with pytest.raises(Exception):
        parse_sums("not a sums line")


def test_manifest_json_roundtrip(tmp_path):
    from cold_archive.engine.manifest import FileEntry, save_manifest
    m = Manifest(label="L", media="hdd", redundancy_percent=12, files={"a/b": FileEntry(1, 2.5, "ab" * 32)})
    p = tmp_path / MANIFEST_FILE
    assert save_manifest(str(p), m) is True
    assert save_manifest(str(p), m) is False           # unchanged content is not rewritten
    m2 = load_manifest(str(p))
    assert m2.files == m.files and m2.label == "L" and m2.media == "hdd" and m2.redundancy_percent == 12
    data = json.loads(p.read_text())
    assert data["format"] == "cold-archive-manifest" and data["file_count"] == 1


def test_plan_blocks_keeps_parity_near_nominal():
    # 4000 photos of 5 MB: par2's default 2000-block target would give 100 % parity
    plan = p2.plan_blocks([5_000_000] * 4000, 10)
    assert plan.parity_bytes <= 5_000_000 * 4000 * 0.10 * 1.25
    assert plan.source_blocks <= p2.SAFE_MAX_BLOCKS
    # 20 videos of 5 GB: big blocks, few recovery blocks, still ~10 %
    plan = p2.plan_blocks([5_000_000_000] * 20, 10)
    assert 0.09 <= plan.parity_bytes / 100_000_000_000 <= 0.125
    # tiny set: recovery floor gives whole-file protection
    plan = p2.plan_blocks([100, 2000], 10)
    assert plan.recovery_blocks == plan.source_blocks
    assert plan.block_size % 4 == 0
    with pytest.raises(ValueError):
        p2.plan_blocks([0, 0], 10)
    with pytest.raises(ValueError):
        p2.plan_blocks([1] * (p2.SAFE_MAX_BLOCKS + 1), 10)


def test_chunk_files_splits_and_drops_empty(monkeypatch):
    monkeypatch.setattr(p2, "MAX_FILES_PER_SET", 5)
    chunks = p2.chunk_files([(f"f{i:02d}", 10 if i % 3 else 0) for i in range(12)])
    assert [len(c) for c in chunks] == [5, 3]
    assert all(s > 0 for c in chunks for _, s in c)


def test_scan_excludes_junk_symlinks_and_tool_files(tmp_path):
    make_tree(str(tmp_path))
    (tmp_path / SUMS_FILE).write_text("x")
    (tmp_path / "Docs" / "PARITY.par2").write_bytes(b"x")
    (tmp_path / "Docs" / "photos.par2").write_bytes(b"foreign")
    s = scan_archive(str(tmp_path))
    rels = {f.rel for f in s.files}
    assert "Docs/photos.par2" in rels               # foreign par2 is payload
    assert SUMS_FILE not in rels and ".DS_Store" not in rels and "Photos/._IMG_001.jpg" not in rels
    assert "link.txt" in s.symlinks
    assert "Docs/PARITY.par2" not in rels
    docs = next(d for d in s.dirs if d.rel == "Docs")
    assert docs.parity_sets == {"PARITY": ["PARITY.par2"]}
    assert any(d.rel == "Empty folder" for d in s.dirs)


# ---------------------------------------------------------------- par2 layer

@needs_par2
def test_par2_index_parse_and_packet_validation(tmp_path):
    d = str(tmp_path)
    names = ["a.bin", "-dash.bin", "héllo ✓.bin"]
    sizes = [300_000, 50, 7_000]
    for n, s in zip(names, sizes):
        (tmp_path / n).write_bytes(os.urandom(s))
    exe = p2.find_par2()
    plan = p2.plan_blocks(sizes, 10)
    mem, thr = p2.default_resources(2)
    r = p2.create(exe, d, "PARITY", names, plan, memory_mb=mem, threads=thr)
    assert r.status == "ok", r.output
    idx = p2.read_index(os.path.join(d, "PARITY.par2"))
    assert idx.block_size == plan.block_size
    assert {(f.name, f.size) for f in idx.file_table} == set(zip(names, sizes))
    assert sum(p2.scan_par2_file(os.path.join(d, n)).recovery_blocks for n in p2.set_files_on_disk(d, "PARITY")) == plan.recovery_blocks
    assert p2.recovery_blocks_by_name(d, "PARITY") == plan.recovery_blocks
    vol = [n for n in p2.set_files_on_disk(d, "PARITY") if ".vol" in n][0]
    with open(os.path.join(d, vol), "r+b") as f:
        f.seek(300)
        f.write(b"\xff" * 40)
    s = p2.scan_par2_file(os.path.join(d, vol))
    assert s.packets_bad >= 1
    assert p2.verify(exe, d, "PARITY").status == "ok"   # par2 tolerates it via duplicates
    rot(os.path.join(d, "a.bin"))
    v = p2.verify(exe, d, "PARITY")
    assert v.status == "repairable" and v.files["a.bin"] == "damaged"
    assert p2.repair(exe, d, "PARITY").status == "repaired"
    assert p2.verify(exe, d, "PARITY").status == "ok"
    assert os.path.exists(os.path.join(d, "a.bin.1"))    # par2's backup of the damaged original


# ---------------------------------------------------------------- lifecycle

@needs_par2
def test_create_verify_repair_lifecycle(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    files = make_tree(str(src))
    c = copy_tree(str(src), str(dst))
    assert c.ok and c.files_copied == len(files) and not c.failures
    assert not (dst / ".DS_Store").exists() and not (dst / "Photos" / "._IMG_001.jpg").exists()
    assert not (dst / "link.txt").exists() and (dst / "Empty folder").is_dir()
    for rel, data in files.items():
        assert (dst / rel).read_bytes() == data

    r = protect(str(dst), ProtectOptions(label="Test", media_note="hdd"), known_hashes=c.hashes)
    assert r.ok, r.summary()
    assert r.count("created") == 4 and not r.errors             # Photos/2019, Docs, root, index
    assert (dst / SUMS_FILE).exists() and (dst / MANIFEST_FILE).exists() and (dst / README_TXT).exists()
    assert (dst / "Photos" / "2019" / "PARITY.par2").exists() and (dst / "Docs" / "PARITY.par2").exists()
    assert (dst / (INDEX_PARITY_BASENAME + ".par2")).exists()
    assert not (dst / "Empty folder" / "PARITY.par2").exists()
    sums = read_sums(str(dst / SUMS_FILE))
    assert set(files) <= set(sums) and "Docs/empty.txt" in sums
    assert MANIFEST_FILE in sums and README_TXT in sums
    assert "Photos/2019/PARITY.par2" in sums and (INDEX_PARITY_BASENAME + ".par2") not in sums   # parity is listed, its own parity cannot be

    # the standard tool agrees, and so does the double-click script
    if shutil.which("shasum"):
        out = subprocess.run(["shasum", "-a", "256", "-c", SUMS_FILE], cwd=dst, capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        out = subprocess.run(["bash", CHECK_COMMAND], cwd=dst, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert out.returncode == 0 and "healthy" in out.stdout, out.stdout + out.stderr

    v = verify(str(dst))
    assert v.verdict == "ok" and not v.problems and all(s.status == "ok" for s in v.sets), v.summary()
    assert v.files_checked == len(files) + 4

    # idempotent: a second protect touches nothing
    m1, s1 = (dst / MANIFEST_FILE).read_bytes(), (dst / SUMS_FILE).read_bytes()
    r2 = protect(str(dst))
    assert r2.ok and r2.count("created") == 0 and r2.count("kept") == 4, r2.summary()
    assert (dst / MANIFEST_FILE).read_bytes() == m1 and (dst / SUMS_FILE).read_bytes() == s1
    assert load_manifest(str(dst / MANIFEST_FILE)).media == "hdd"

    # damage: rot with date intact, a deleted file, a lost checksum list, a hurt parity volume
    rot(str(dst / "Photos" / "2019" / "IMG_001.jpg"))
    os.remove(dst / "Docs" / "letter.txt")
    os.remove(dst / SUMS_FILE)
    vol = sorted(p for p in (dst / "Photos" / "2019").iterdir() if ".vol" in p.name)[0]
    with open(vol, "r+b") as f:
        f.seek(500)
        f.write(b"\0" * 64)

    v = verify(str(dst))
    assert v.verdict == "problem"
    kinds = {p.rel: p.kind for p in v.problems}
    assert kinds["Photos/2019/IMG_001.jpg"] == "mismatch" and kinds["Docs/letter.txt"] == "missing"
    assert v.sums_status == "from-manifest"
    assert {s.status for s in v.sets if s.directory in ("Photos/2019", "Docs")} == {"repairable"}

    # protect must refuse to launder a missing file into "current" parity
    r3 = protect(str(dst))
    assert not r3.ok and r3.count("created") == 0 and "missing" in r3.errors[0]
    assert (dst / "Docs" / "PARITY.par2").exists()

    rr = repair(str(dst))
    assert set(rr.repaired) == {"Docs/letter.txt", "Photos/2019/IMG_001.jpg"}, rr.summary()
    assert not rr.unrepaired
    assert (dst / "Docs" / "letter.txt").read_bytes() == files["Docs/letter.txt"]
    assert (dst / "Photos" / "2019" / "IMG_001.jpg").read_bytes() == files["Photos/2019/IMG_001.jpg"]
    assert (dst / SUMS_FILE).exists() and rr.records_restored == [SUMS_FILE]
    assert not (dst / "Photos" / "2019" / "IMG_001.jpg.1").exists()   # backup cleaned once proven
    assert "Photos/2019/PARITY" in rr.parity_rebuilt                    # the hurt volume was rebuilt too
    assert rr.verdict == "attention" and rr.after.verdict == "ok", rr.summary()   # attention: records were restored

    # nothing left for protect to do, and the archive is clean
    r4 = protect(str(dst))
    assert r4.ok and r4.count("created") == 0, r4.summary()
    v = verify(str(dst), VerifyOptions(mode="thorough"))
    assert v.verdict == "ok", v.summary()


@needs_par2
def test_protect_blocks_on_silent_corruption(tmp_path):
    make_tree(str(tmp_path))
    r = protect(str(tmp_path))
    assert r.ok
    old_hash = load_manifest(str(tmp_path / MANIFEST_FILE)).files["Docs/letter.txt"].sha256
    old_index = (tmp_path / "Docs" / "PARITY.par2").read_bytes()
    rot(str(tmp_path / "Docs" / "letter.txt"), offset=100, length=50)
    # a legitimate edit next to it makes the set stale, which is the dangerous moment
    (tmp_path / "Docs" / "-leading-dash.txt").write_bytes(b"edited\n" * 500)
    gate = protect(str(tmp_path))                       # the edit needs consent; nothing may change
    assert gate.needs_consent == "changes" and gate.files_updated == ["Docs/-leading-dash.txt"], gate.summary()
    assert (tmp_path / "Docs" / "PARITY.par2").read_bytes() == old_index
    r2 = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    assert not r2.ok
    docs = [s for s in r2.sets if s.directory == "Docs"]
    assert docs and all(s.action == "blocked" for s in docs), r2.summary()
    assert (tmp_path / "Docs" / "PARITY.par2").read_bytes() == old_index
    assert load_manifest(str(tmp_path / MANIFEST_FILE)).files["Docs/letter.txt"].sha256 == old_hash
    rr = repair(str(tmp_path))
    assert "Docs/letter.txt" in rr.repaired, rr.summary()
    assert rr.edits_kept == ["Docs/-leading-dash.txt"]
    assert (tmp_path / "Docs" / "-leading-dash.txt").read_bytes() == b"edited\n" * 500   # the edit survived
    assert not (tmp_path / "Docs" / "-leading-dash.txt.1").exists()
    assert "Docs/PARITY" in rr.parity_rebuilt and rr.verdict == "ok", rr.summary()
    r3 = protect(str(tmp_path))
    assert r3.ok and r3.count("created") == 0, r3.summary()
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_same_size_edit_is_seen_by_content(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    p = tmp_path / "Docs" / "letter.txt"
    data = bytearray(p.read_bytes())
    data[0] ^= 0xFF
    p.write_bytes(bytes(data))                 # same size, new mtime: a normal edit
    assert protect(str(tmp_path)).needs_consent == "changes"
    r = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    assert r.ok and r.files_updated == ["Docs/letter.txt"], r.summary()
    assert [s.directory for s in r.sets if s.action == "created"] == ["", "Docs"] or \
           sorted(s.directory for s in r.sets if s.action == "created") == ["", "Docs"]
    # now make the parity disagree with the records without any mtime change:
    # replace the Docs index with an old one is hard, so instead rebuild parity
    # for Docs from a different content and restore the records' content
    old_bytes = p.read_bytes()
    p.write_bytes(bytes(data[::-1]))
    assert protect(str(tmp_path), ProtectOptions(accept_changes=True)).ok
    p.write_bytes(old_bytes)
    assert protect(str(tmp_path), ProtectOptions(accept_changes=True)).ok   # records now match old_bytes again
    st = p.stat()
    # the set was rebuilt for old_bytes; the state check must call it current
    r = protect(str(tmp_path))
    assert r.count("created") == 0 and r.ok, r.summary()
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


@needs_par2
def test_full_rehash_flags_rot_and_keeps_truth(tmp_path):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    rot(str(tmp_path / "root file.bin"))
    r = protect(str(tmp_path), ProtectOptions(full_rehash=True))
    assert not r.ok and r.rot_suspected == ["root file.bin"]
    assert [s.action for s in r.sets if s.directory == "" and s.set_name == "PARITY"] == ["blocked"]
    rr = repair(str(tmp_path))
    assert "root file.bin" in rr.repaired
    assert (tmp_path / "root file.bin").read_bytes() == files["root file.bin"]


@needs_par2
def test_removals_need_explicit_consent(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    os.remove(tmp_path / "Photos" / "2019" / "IMG_002.jpg")
    r = protect(str(tmp_path))
    assert not r.ok and r.files_removed == ["Photos/2019/IMG_002.jpg"] and r.count("created") == 0
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert r.ok and r.count("created") == 2          # Photos/2019 and the index
    assert "Photos/2019/IMG_002.jpg" not in read_sums(str(tmp_path / SUMS_FILE))
    assert verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_added_file_updates_only_its_folder(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    (tmp_path / "Docs" / "new.txt").write_bytes(b"new\n" * 100)
    v = verify(str(tmp_path))
    assert v.verdict == "attention" and [p.rel for p in v.problems_of("extra")] == ["Docs/new.txt"]
    r = protect(str(tmp_path))
    assert r.ok and sorted(s.directory for s in r.sets if s.action == "created") == ["", "Docs"]
    assert verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_folder_split_into_several_sets(tmp_path, monkeypatch):
    monkeypatch.setattr(p2, "MAX_FILES_PER_SET", 5)
    d = tmp_path / "many"
    d.mkdir()
    for i in range(12):
        (d / f"f{i:02d}.bin").write_bytes(os.urandom(3000))
    r = protect(str(tmp_path))
    assert r.ok
    names = {s.set_name for s in r.sets if s.directory == "many" and s.action == "created"}
    assert names == {"PARITY", "PARITY-2", "PARITY-3"}
    rot(str(d / "f07.bin"), offset=10, length=20)
    os.remove(d / "f11.bin")
    rr = repair(str(tmp_path))
    assert set(rr.repaired) == {"many/f07.bin", "many/f11.bin"}, rr.summary()
    assert verify(str(tmp_path)).verdict == "ok"
    # fewer files now -> obsolete third set is removed, others kept
    for i in range(6):
        os.remove(d / f"f{i:02d}.bin")
    r = protect(str(tmp_path), ProtectOptions(accept_removals=True))
    assert r.ok and "PARITY-3" not in {s.set_name for s in r.sets if s.action != "removed"}
    assert not (d / "PARITY-3.par2").exists() and verify(str(tmp_path)).verdict == "ok"


@needs_par2
def test_empty_files_and_folders(tmp_path):
    (tmp_path / "only-empty").mkdir()
    (tmp_path / "only-empty" / "a.txt").write_bytes(b"")
    (tmp_path / "data.bin").write_bytes(os.urandom(5000))
    r = protect(str(tmp_path))
    assert r.ok and not (tmp_path / "only-empty" / "PARITY.par2").exists()
    v = verify(str(tmp_path))
    assert v.verdict == "ok" and not v.unprotected and "only-empty/a.txt" in read_sums(str(tmp_path / SUMS_FILE))


@needs_par2
def test_verify_modes_and_records_loss(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    assert verify(str(tmp_path), VerifyOptions(mode="hashes")).verdict == "ok"
    assert verify(str(tmp_path), VerifyOptions(mode="parity")).verdict == "ok"
    os.remove(tmp_path / SUMS_FILE)
    os.remove(tmp_path / MANIFEST_FILE)
    v = verify(str(tmp_path), VerifyOptions(mode="parity"))
    assert all(s.status == "ok" for s in v.sets if s.set_name != INDEX_PARITY_BASENAME)
    assert [s.status for s in v.sets if s.set_name == INDEX_PARITY_BASENAME] == ["repairable"]
    rr = repair(str(tmp_path))
    assert (tmp_path / SUMS_FILE).exists() and (tmp_path / MANIFEST_FILE).exists()
    assert verify(str(tmp_path)).verdict == "ok", rr.summary()


@needs_par2
def test_unrepairable_damage_is_reported_not_hidden(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    # wreck every photo: far beyond what 10 % (or the small-set floor) can rebuild
    photos = sorted(p for p in (tmp_path / "Photos" / "2019").iterdir() if p.suffix == ".jpg")
    for p in photos:
        with open(p, "r+b") as f:
            f.write(b"\0" * (p.stat().st_size - 10))
    v = verify(str(tmp_path))
    assert v.verdict == "problem" and any(s.status == "unrepairable" for s in v.sets), v.summary()
    rr = repair(str(tmp_path))
    assert rr.verdict == "problem" and {x.rel for x in rr.unrepaired} == {"Photos/2019/" + p.name for p in photos}
    assert not rr.repaired
    assert "another copy" in rr.summary()


def test_copy_resumes_and_refuses_nesting(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_tree(str(src))
    dst = tmp_path / "dst"
    c1 = copy_tree(str(src), str(dst))
    c2 = copy_tree(str(src), str(dst))
    assert c1.ok and c2.ok and c2.files_copied == 0 and c2.files_skipped_identical == c1.files_copied
    assert c2.hashes == c1.hashes
    with pytest.raises(ValueError):
        copy_tree(str(src), str(src / "inner"))
    with pytest.raises(ValueError):
        copy_tree(str(src), str(tmp_path))


def test_copy_detects_bad_readback(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(b"x" * 10000)
    from cold_archive.engine import copy as copymod
    real = copymod.sha256_file

    def lying(path, **kw):
        if path.endswith("a.bin") and str(tmp_path / "dst") in path:
            return "0" * 64
        return real(path, **kw)
    monkeypatch.setattr(copymod, "sha256_file", lying)
    c = copy_tree(str(src), str(tmp_path / "dst"))
    assert not c.ok and c.failures and "read-back" in c.failures[0][1]
    assert not (tmp_path / "dst" / "a.bin").exists()


def test_readme_kit_is_useful(tmp_path):
    from cold_archive.engine.readme import readme_text, readme_html, write_recovery_kit
    m = Manifest(label="Family photos", media="bluray", redundancy_percent=10, created="2026-09-03T00:00:00Z")
    t = readme_text(m, "bluray")
    for needle in ("shasum -a 256 -c SHA256SUMS", "sha256sum -c SHA256SUMS", "par2 repair PARITY.par2",
                   "par2 repair PARITY-INDEX.par2", "github.com/Parchive/par2cmdline", "MultiPar", "Get-FileHash",
                   "M-DISC", "10%"):
        assert needle in t, needle
    h = readme_html(t)
    assert "<h1>" in h and "shasum" in h and "&lt;" not in t
    assert write_recovery_kit(str(tmp_path), m, "bluray") is True
    assert write_recovery_kit(str(tmp_path), m, "bluray") is False
    assert os.access(tmp_path / CHECK_COMMAND, os.X_OK)
    out = subprocess.run(["bash", "-n", str(tmp_path / CHECK_COMMAND)], capture_output=True)
    assert out.returncode == 0, out.stderr


def test_logs_never_inside_archive(tmp_path):
    from cold_archive.engine import logs
    p = logs.write_report("check", str(tmp_path / "archive"), "hello")
    assert not p.startswith(str(tmp_path / "archive"))
    assert open(p).read().strip() == "hello"


def test_par2_parse_handles_every_verdict_line_shape():
    from cold_archive.engine.par2 import _parse_files
    text = ('Target: "ok.bin" - found.\nScanning: 3%\rTarget: "part.bin" - damaged. Found 3 of 4 data blocks.\n'
            'Target: "gone.bin" - missing.\nScanning: 100.0%\rFile: "wrecked.bin" - no data found.\n'
            'File: "renamed.bin" - is a match for "gone.bin".\nYou need 7 more recovery blocks to be able to repair.\n')
    files, needs = _parse_files(text)
    assert files == {"ok.bin": "found", "part.bin": "damaged", "gone.bin": "missing", "wrecked.bin": "damaged"}
    assert needs == 7


@needs_par2
def test_second_writer_is_refused_while_a_job_runs(tmp_path):
    import threading
    from cold_archive.engine.lock import ArchiveBusy, ArchiveLock
    from cold_archive.engine.observer import Observer
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    assert not (tmp_path / ".cold-archive.lock").exists()          # released and removed
    assert ".cold-archive.lock" not in read_sums(str(tmp_path / SUMS_FILE))
    with ArchiveLock(str(tmp_path), exclusive=True):
        with pytest.raises(ArchiveBusy):
            protect(str(tmp_path))
        with pytest.raises(ArchiveBusy):
            repair(str(tmp_path))
        with pytest.raises(ArchiveBusy):
            verify(str(tmp_path))
    with ArchiveLock(str(tmp_path), exclusive=False):
        assert verify(str(tmp_path)).verdict == "ok"                 # readers share
        with pytest.raises(ArchiveBusy):
            protect(str(tmp_path))
