"""Create protects the SOURCE folder first (since 0.2.1) and then copies, so
the source's own records are the ones a second Create meets. The same consent
gates as a standalone Protect must apply there: a file that changed since the
records (contents and date differ, what an edit looks like) is re-recorded
only with an explicit yes, and silent corruption (bytes differ, size and date
intact) is never re-recorded at all, with or without that yes.

Control, 2026-10-19: against 06f291f (accept_removals = accept_changes = True
for the source) the first test fails at its first assertion: EXIT_OK instead
of EXIT_NEEDS_CONSENT, and the edited file's new hash is in SHA256SUMS."""
from __future__ import annotations

import os

from cold_archive.engine import cli
from cold_archive.engine.create import create_archive
from cold_archive.engine.layout import SUMS_FILE
from cold_archive.engine.manifest import read_sums
from cold_archive.engine.protect import ProtectOptions
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import make_tree, needs_par2, rot


def _edit(path) -> None:
    """Rewrite a file the way an application would: new bytes, new date."""
    st = os.stat(path)
    path.write_bytes(b"Dear trustee, this letter was rewritten.\n" * 3000)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 90 * 10 ** 9))


@needs_par2
def test_create_asks_before_re_recording_an_edited_source(tmp_path):
    src, dst, dst2 = tmp_path / "src", tmp_path / "dst", tmp_path / "dst2"
    src.mkdir()
    make_tree(str(src))
    assert cli.main(["-q", "create", str(src), str(dst), "--force", "--label", "T"]) == cli.EXIT_OK
    sums_before = (src / SUMS_FILE).read_bytes()
    old_hash = read_sums(str(src / SUMS_FILE))["Docs/letter.txt"]

    _edit(src / "Docs" / "letter.txt")
    assert cli.main(["-q", "create", str(src), str(dst2), "--force", "--label", "T"]) == cli.EXIT_NEEDS_CONSENT
    assert (src / SUMS_FILE).read_bytes() == sums_before     # nothing re-recorded
    assert not (dst2 / SUMS_FILE).exists()                    # and nothing copied

    # the explicit yes records the edit, and the copy carries the new records
    assert cli.main(["-q", "create", str(src), str(dst2), "--force", "--label", "T", "--accept-changes"]) == cli.EXIT_OK
    new_hash = read_sums(str(src / SUMS_FILE))["Docs/letter.txt"]
    assert new_hash != old_hash
    assert read_sums(str(dst2 / SUMS_FILE))["Docs/letter.txt"] == new_hash
    assert verify(str(dst2), VerifyOptions(mode="standard")).verdict == "ok"


@needs_par2
def test_create_never_launders_rot_in_the_source(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    files = make_tree(str(src))
    assert cli.main(["-q", "create", str(src), str(dst), "--force", "--label", "T"]) == cli.EXIT_OK
    good = read_sums(str(src / SUMS_FILE))["root file.bin"]

    # Protect is incremental and the rotten file keeps its size and date, so
    # the Protect step cannot see this; the copy reads every byte and must.
    rot(str(src / "root file.bin"))
    for extra in ([], ["--accept-changes", "--accept-removals"]):
        assert cli.main(["-q", "create", str(src), str(dst), "--force", "--label", "T"] + extra) == cli.EXIT_PROBLEMS
        assert read_sums(str(src / SUMS_FILE))["root file.bin"] == good      # the original truth is kept on both sides
        assert read_sums(str(dst / SUMS_FILE))["root file.bin"] == good
    r = create_archive(str(src), str(dst), ProtectOptions(label="T"))
    assert r.source_mismatches == ["root file.bin"] and not r.ok and "read differently" in r.summary()

    # the records still say what the bytes should be, so both sides repair
    assert cli.main(["-q", "check", str(dst)]) == cli.EXIT_PROBLEMS
    assert cli.main(["-q", "repair", str(src)]) == cli.EXIT_OK
    assert cli.main(["-q", "repair", str(dst)]) == cli.EXIT_OK
    assert (src / "root file.bin").read_bytes() == files["root file.bin"]
    assert (dst / "root file.bin").read_bytes() == files["root file.bin"]
    assert cli.main(["-q", "create", str(src), str(dst), "--force", "--label", "T"]) == cli.EXIT_OK
    assert cli.main(["-q", "check", str(dst), "--mode", "thorough"]) == cli.EXIT_OK


@needs_par2
def test_create_result_reads_the_first_run_twice_and_agrees(tmp_path):
    """A first Create hashes the source once in Protect and once in the copy;
    the two readings agree on a healthy source, so no mismatch is reported."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    make_tree(str(src))
    r = create_archive(str(src), str(dst), ProtectOptions(label="T"))
    assert r.ok and r.copy is not None and not r.source_mismatches and not r.needs_consent, r.summary()
    assert r.copy.files_copied + r.copy.files_skipped_identical == len(r.copy.hashes)
