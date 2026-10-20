"""Rescue copy: everything readable off a failing drive, the source untouched,
and Repair on the copy finishing the job. Read errors are simulated at the
two places a failing drive produces them: opening a file, and reading a
stretch of it."""
from __future__ import annotations

import errno
import hashlib
import os
import random

import pytest

from cold_archive.engine import rescue as rs
from cold_archive.engine import volumes
from cold_archive.engine.layout import MANIFEST_FILE
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.repair import RepairOptions, repair
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import needs_par2


def _archive(root) -> dict:
    rnd = random.Random(20261019)
    files = {}
    for folder in ("photos", "letters"):
        os.makedirs(os.path.join(root, folder))
        for i in range(6):
            data = rnd.randbytes(200_000 + i * 1_111)
            rel = f"{folder}/f{i}.bin"
            with open(os.path.join(root, rel), "wb") as f:
                f.write(data)
            files[rel] = data
    res = protect(str(root), ProtectOptions(redundancy_percent=10))
    assert res.ok, res.summary()
    return files


def _snapshot(root) -> dict:
    out = {}
    for dp, dns, fns in os.walk(root):
        out[os.path.relpath(dp, root) + "/"] = os.stat(dp).st_mtime_ns
        for n in fns:
            p = os.path.join(dp, n)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
    return out


@needs_par2
def test_rescue_keeps_what_reads_and_repair_finishes_the_job(tmp_path, monkeypatch):
    src, dst = tmp_path / "failing", tmp_path / "healthy"
    files = _archive(src)
    before = _snapshot(src)

    bad_read = str(src / "photos" / "f2.bin")
    bad_open = str(src / "photos" / "f5.bin")
    fd_path = {}
    real_open, real_pread = rs._open_source, rs._pread

    def fake_open(path, flags):
        if path == bad_open:
            raise OSError(errno.EIO, "Input/output error")
        fd = real_open(path, flags)
        fd_path[fd] = path
        return fd

    def fake_pread(fd, n, off):
        if fd_path.get(fd) == bad_read and off < 131072 and off + n > 65536:
            raise OSError(errno.EIO, "Input/output error")
        return real_pread(fd, n, off)

    monkeypatch.setattr(rs, "_open_source", fake_open)
    monkeypatch.setattr(rs, "_pread", fake_pread)
    res = rs.rescue_copy(str(src), str(dst), allow_same_drive=True)

    assert not res.refused and res.is_archive
    assert res.partial == {"photos/f2.bin": 65536}, res.summary()
    assert [r for r, _ in res.unreadable] == ["photos/f5.bin"], res.summary()
    got = (dst / "photos" / "f2.bin").read_bytes()
    assert got[65536:131072] == b"\0" * 65536
    assert got[:65536] == files["photos/f2.bin"][:65536] and got[131072:] == files["photos/f2.bin"][131072:]
    assert not (dst / "photos" / "f5.bin").exists()
    assert (dst / MANIFEST_FILE).is_file() and (dst / "SHA256SUMS").is_file()
    assert _snapshot(src) == before, "the rescue must not change anything on the failing drive"

    monkeypatch.setattr(rs, "_open_source", real_open)
    monkeypatch.setattr(rs, "_pread", real_pread)
    rep = repair(str(dst), RepairOptions())
    assert rep.verdict == "ok", rep.summary()
    for rel, data in files.items():
        assert (dst / rel).read_bytes() == data, rel
    assert verify(str(dst), VerifyOptions(mode="standard")).verdict == "ok"


@needs_par2
def test_rescue_refuses_the_same_drive_and_a_drive_too_small(tmp_path, monkeypatch):
    src = tmp_path / "failing"
    _archive(src)
    res = rs.rescue_copy(str(src), str(tmp_path / "copy"))
    assert res.refused and "different" in res.refused and res.files_copied == 0

    class DU:
        free, total, used = 10, 10, 0
    monkeypatch.setattr(rs.shutil, "disk_usage", lambda p: DU())
    res = rs.rescue_copy(str(src), str(tmp_path / "copy2"), allow_same_drive=True)
    assert res.refused and "larger drive" in res.refused


@needs_par2
def test_rescue_started_again_does_not_read_the_failing_drive_twice(tmp_path):
    src, dst = tmp_path / "failing", tmp_path / "healthy"
    _archive(src)
    first = rs.rescue_copy(str(src), str(dst), allow_same_drive=True)
    assert first.ok and first.files_copied == first.files_total
    again = rs.rescue_copy(str(src), str(dst), allow_same_drive=True)
    assert again.ok and again.files_copied == 0 and again.files_skipped == first.files_total


def test_rescue_of_a_folder_without_records_says_it_cannot_be_repaired(tmp_path):
    src = tmp_path / "plain"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    res = rs.rescue_copy(str(src), str(tmp_path / "copy"), allow_same_drive=True)
    assert res.ok and not res.is_archive and "cannot be rebuilt" in res.summary()


def test_cli_rescue_exit_codes(tmp_path):
    from cold_archive.engine import cli
    src = tmp_path / "plain"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    assert cli.main(["-q", "rescue", str(src), str(tmp_path / "copy")]) == 2      # same drive: refused


def test_find_archives_on_connected_drives(tmp_path):
    vols = tmp_path / "Volumes"
    for p in ("Shoebox", "Backup/Family Archive", "Empty", ".hidden", "com.apple.TimeMachine.localsnapshots"):
        (vols / p).mkdir(parents=True)
    for p in ("Shoebox", "Backup/Family Archive", ".hidden", "com.apple.TimeMachine.localsnapshots"):
        (vols / p / MANIFEST_FILE).write_text("{}")
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / MANIFEST_FILE).write_text("{}")
    os.symlink(boot, vols / "Macintosh HD")       # the startup disk appears as a link
    found = volumes.find_archives(str(vols))
    assert found == [str(vols / "Backup" / "Family Archive"), str(vols / "Shoebox")]
    assert volumes.describe(found[0], str(vols)) == "Backup / Family Archive"
    assert volumes.find_archives(str(tmp_path / "nowhere")) == []


@needs_par2
def test_check_writes_nothing_into_the_archive(tmp_path):
    """Found 2026-10-19 on a real archive: Check took its shared lock by
    creating .cold-archive.lock in the archive's top folder and left it there,
    changing the folder's date. A check must change nothing; it now locks
    only a lock file a writer has already made."""
    arc = tmp_path / "arc"
    _archive(arc)
    root_before = (sorted(os.listdir(arc)), os.stat(arc).st_mtime_ns)
    for mode in ("standard", "parity"):
        assert verify(str(arc), VerifyOptions(mode=mode)).verdict == "ok"
    assert (sorted(os.listdir(arc)), os.stat(arc).st_mtime_ns) == root_before


def test_a_check_still_waits_out_a_writer(tmp_path):
    """While Protect or Repair holds the lock, a check must not start."""
    from cold_archive.engine.lock import ArchiveBusy, ArchiveLock
    with ArchiveLock(str(tmp_path), exclusive=True):
        with pytest.raises(ArchiveBusy):
            with ArchiveLock(str(tmp_path), exclusive=False):
                pass
    assert not os.path.exists(tmp_path / ".cold-archive.lock")      # the writer removes its file
