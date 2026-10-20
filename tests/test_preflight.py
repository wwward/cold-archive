"""Preflight and spanning: the numbers must be real numbers."""
from __future__ import annotations

import os
import subprocess

import pytest

from cold_archive.engine import par2 as p2
from cold_archive.engine import preflight as pf
from cold_archive.engine.spanning import MEDIA_LIMITS, Spanner

from conftest import make_tree, needs_par2


def test_media_capacities_are_physical():
    assert MEDIA_LIMITS["DVD"] == 4_700_372_992
    assert MEDIA_LIMITS["DVD_DL"] == 8_543_666_176
    assert MEDIA_LIMITS["BD_25"] == 25_025_314_816
    assert MEDIA_LIMITS["CD"] == 737_280_000
    for k, v in MEDIA_LIMITS.items():
        assert v % 2048 == 0, k


def test_spanning_keeps_folders_together(tmp_path):
    for d in ("A", "B", "C"):
        os.makedirs(tmp_path / d)
        for i in range(3):
            (tmp_path / d / f"{i}.bin").write_bytes(b"\0" * 300_000)
    # a 900 KB folder gets the 10-recovery-block floor (640 KB of parity) plus
    # par2's packet overhead, so it needs ~1.6 MB; two do not fit in 2.7 MB usable
    plan = Spanner(str(tmp_path), custom_capacity=3_000_000, redundancy_percent=10).plan()
    assert len(plan.volumes) == 3 and not plan.split_dirs, plan.render()
    # the records, kit and PARITY-INDEX are a unit of their own on one volume
    records_units = [u for v in plan.volumes for u in v.units if u.rel.startswith("(records")]
    assert len(records_units) == 1 and records_units[0].bytes > 0, plan.render()
    for v in plan.volumes:
        assert len({u.rel for u in v.units if not u.rel.startswith("(records")}) == 1
    big = tmp_path / "Huge"
    big.mkdir()
    for i in range(5):
        (big / f"{i}.bin").write_bytes(b"\0" * 500_000)
    plan = Spanner(str(tmp_path), custom_capacity=1_400_000, redundancy_percent=10).plan()
    assert "Huge" in plan.split_dirs and not plan.oversized_files
    assert all(v.used <= v.capacity for v in plan.volumes)
    with pytest.raises(ValueError):
        Spanner(str(tmp_path), media_type="FLOPPY")


def test_filesystem_type_parses_mount_output(monkeypatch):
    sample = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
              "/dev/disk5s1 on /Volumes/Archive (exfat, local, nodev, nosuid, noowners)\n"
              "/dev/disk6s2 on /Volumes/Archive Two (msdos, local, nodev, nosuid, noowners)\n")

    class R:
        stdout = sample
    monkeypatch.setattr(pf.sys, "platform", "darwin")
    monkeypatch.setattr(pf.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(pf.os.path, "realpath", lambda p: p)
    assert pf.filesystem_type("/Volumes/Archive/sub/dir") == "exfat"
    assert pf.filesystem_type("/Volumes/Archive Two/x") == "msdos"
    assert pf.filesystem_type("/Users/me") == "apfs"


@needs_par2
def test_preflight_facts_and_guards(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    files = make_tree(str(src))
    rep = pf.run_preflight(str(src), str(tmp_path / "dst"), 10)
    assert rep.ok, rep.summary()
    assert rep.file_count == len(files) and rep.payload_bytes == sum(len(b) for b in files.values())
    assert rep.symlinks == 1 and rep.par2_path
    assert rep.total_required_bytes > rep.payload_bytes * 1.1
    # nesting
    assert not pf.run_preflight(str(src), str(src / "inner"), 10).ok
    assert not pf.run_preflight(str(tmp_path), str(src), 10).ok
    # not enough space
    class DU:
        free = 10
        total = 10
        used = 0
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: DU())
    rep = pf.run_preflight(str(src), str(tmp_path / "dst"), 10)
    assert any("not enough space" in e for e in rep.errors)


@needs_par2
def test_preflight_checks_the_source_volume_because_create_protects_it_first(tmp_path, monkeypatch):
    """Create writes the source's parity and records on the SOURCE volume
    before copying (create.py). Measured 2026-10-19: a 12.76 TB tree
    on a volume with 1.50 TB free, which 0.2.1 would have started and failed
    hours in. Preflight must refuse that up front, and must count the two
    volumes as one pool when they are the same."""
    src = tmp_path / "src"
    src.mkdir()
    make_tree(str(src))
    dst = tmp_path / "dst"

    class DU:
        def __init__(self, free):
            self.free, self.total, self.used = free, free, 0

    # Different volumes: the destination is roomy, the source volume is not.
    monkeypatch.setattr(pf, "_same_volume", lambda a, b: False)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: DU(10 if os.path.abspath(p) == str(src) else 10 ** 12))
    rep = pf.run_preflight(str(src), str(dst), 10)
    assert not rep.ok
    assert rep.source_required_bytes > 0 and rep.source_available_bytes == 10 and not rep.same_volume
    assert [e for e in rep.errors if "source volume" in e] and not [e for e in rep.errors if "destination" in e]
    assert "on the source volume first" in rep.summary()
    # Parity already built on the source (an earlier Protect) is not needed again.
    assert rep.source_required_bytes < rep.parity_estimate_bytes + rep.records_estimate_bytes + rep.tools_estimate_bytes + (64 << 20) + 1
    # Enough room on both: no error, both figures reported.
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: DU(10 ** 12))
    rep = pf.run_preflight(str(src), str(dst), 10)
    assert rep.ok, rep.summary()
    assert rep.source_required_bytes > 0 and rep.source_available_bytes == 10 ** 12

    # One volume for both: the two needs are added before the one free figure is judged.
    monkeypatch.setattr(pf, "_same_volume", lambda a, b: True)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: DU(rep.total_required_bytes + rep.source_required_bytes - 1))
    rep2 = pf.run_preflight(str(src), str(dst), 10)
    assert rep2.same_volume and not rep2.ok
    assert [e for e in rep2.errors if "on one volume" in e] and len(rep2.errors) == 1
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: DU(rep.total_required_bytes + rep.source_required_bytes))
    assert pf.run_preflight(str(src), str(dst), 10).ok

    # In place there is no second volume and no second line.
    rep3 = pf.run_preflight(str(src), None, 10)
    assert rep3.ok and rep3.source_required_bytes == 0 and rep3.source_available_bytes is None
    assert "on the source volume first" not in rep3.summary()


@needs_par2
def test_preflight_in_place_leaves_the_source_exactly_as_it_was(tmp_path):
    """Found 2026-10-19: in place, preflight proved the source
    writable by creating and removing a test file in it, which changed the
    folder's date on a live Lightroom tree whose owner had been told the dry
    run touches nothing. In place the source must only be read: same entries,
    same folder date. A folder that cannot be written is still reported."""
    src = tmp_path / "src"
    src.mkdir()
    make_tree(str(src))
    before = (os.stat(src).st_mtime_ns, sorted(os.listdir(src)))
    rep = pf.run_preflight(str(src), None, 10)
    assert rep.ok, rep.summary()
    assert (os.stat(src).st_mtime_ns, sorted(os.listdir(src))) == before

    os.chmod(src, 0o555)
    try:
        rep = pf.run_preflight(str(src), None, 10)
        assert any("cannot be written" in e for e in rep.errors), rep.summary()
        assert (os.stat(src).st_mtime_ns, sorted(os.listdir(src))) == before
    finally:
        os.chmod(src, 0o755)


def test_preflight_without_par2_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(pf.p2, "find_par2", lambda explicit=None: (_ for _ in ()).throw(p2.Par2NotFound("nope")))
    rep = pf.run_preflight(str(tmp_path), None, 10)
    assert not rep.ok and any("nope" in e for e in rep.errors)


def test_preflight_fat32_limits(tmp_path, monkeypatch):
    (tmp_path / "big.bin").write_bytes(b"x")
    monkeypatch.setattr(pf, "filesystem_type", lambda p: "msdos")
    monkeypatch.setattr(pf.os.path, "getsize", lambda p: 5 << 30)
    rep = pf.run_preflight(str(tmp_path), str(tmp_path / "out"), 10)
    # scan uses lstat for sizes, so patch the largest_file through the report instead
    rep.largest_file = ("big.bin", 5 << 30)
    rep.target_fs = "msdos"
    assert any("FAT32" in w for w in rep.warnings)
