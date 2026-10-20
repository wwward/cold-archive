"""The CLI's exit codes are the machine-readable verdict; they must be right."""
from __future__ import annotations

import os

from cold_archive.engine import cli
from cold_archive.engine.layout import SUMS_FILE

from conftest import make_tree, needs_par2, rot


@needs_par2
def test_cli_lifecycle_exit_codes(tmp_path, capsys):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    make_tree(str(src))
    assert cli.main(["-q", "preflight", str(src), str(dst)]) == cli.EXIT_ATTENTION   # symlink warning
    assert cli.main(["-q", "create", str(src), str(dst), "--force", "--label", "T"]) == cli.EXIT_OK
    assert cli.main(["-q", "verify", str(dst)]) == cli.EXIT_OK
    assert cli.main(["-q", "protect", str(dst)]) == cli.EXIT_OK
    assert not os.path.exists(dst / "cold-archive.log")
    rot(str(dst / "root file.bin"))
    assert cli.main(["-q", "check", str(dst)]) == cli.EXIT_PROBLEMS
    assert cli.main(["-q", "protect", str(dst), "--full-rehash"]) == cli.EXIT_PROBLEMS
    assert cli.main(["-q", "repair", str(dst)]) == cli.EXIT_OK
    assert cli.main(["-q", "verify", str(dst), "--mode", "thorough"]) == cli.EXIT_OK
    (dst / "Docs" / "extra.txt").write_text("late addition")
    assert cli.main(["-q", "verify", str(dst)]) == cli.EXIT_ATTENTION
    assert cli.main(["-q", "protect", str(dst)]) == cli.EXIT_OK
    assert cli.main(["-q", "plan", str(dst), "--media", "BD_25"]) == cli.EXIT_OK
    assert cli.main(["-q", "doctor"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ALL GOOD" in out and "Report saved to" in out


def test_cli_usage_errors(tmp_path):
    assert cli.main(["-q", "verify", str(tmp_path / "nope")]) == cli.EXIT_USAGE


def test_cli_without_par2(tmp_path, monkeypatch):
    from cold_archive.engine import par2 as p2
    monkeypatch.setattr(p2, "find_par2", lambda explicit=None: (_ for _ in ()).throw(p2.Par2NotFound("no par2")))
    (tmp_path / "a.txt").write_text("x")
    assert cli.main(["-q", "protect", str(tmp_path)]) == cli.EXIT_USAGE
    assert not os.path.exists(tmp_path / SUMS_FILE)          # nothing written before the check
