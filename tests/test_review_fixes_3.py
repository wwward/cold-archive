"""Third batch of review fixes (2026-09-10): what the verification of commit
7fe2531 found still open or regressed. Each test reproduces the scenario of
the finding it closes (F02, F11, F12, F19) rather than the code path."""
from __future__ import annotations

import os
import subprocess
import unicodedata

import pytest

from cold_archive.engine import par2 as p2
from cold_archive.engine.layout import CHECK_COMMAND, INDEX_PARITY_BASENAME, SUMS_FILE
from cold_archive.engine.manifest import read_sums
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.repair import repair
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import make_tree, needs_par2, rot

NFC_NAME = "héllo wörld ✓.jpg"
NFD_NAME = unicodedata.normalize("NFD", NFC_NAME)


def _index_names(directory: str):
    return {unicodedata.normalize("NFC", f.name) for f in p2.read_index(os.path.join(directory, "PARITY.par2")).file_table}


# --- F12: NFC records, NFD spelling on disk (an APFS archive copied to exFAT) --------

@needs_par2
def test_nfc_records_with_nfd_spelling_on_disk_keep_their_parity(tmp_path):
    files = make_tree(str(tmp_path))
    d = tmp_path / "Photos" / "2019"
    assert protect(str(tmp_path)).ok
    assert NFC_NAME in {unicodedata.normalize("NFC", n) for n in _index_names(str(d))}
    os.rename(d / NFC_NAME, d / NFD_NAME)                 # what exFAT does to the copy
    assert any(n.endswith(NFC_NAME) for n in read_sums(str(tmp_path / SUMS_FILE)))   # the records still say NFC

    # a healthy copy is healthy: nothing missing, nothing extra, nothing stale
    v = verify(str(tmp_path), VerifyOptions(mode="thorough"))
    assert v.verdict == "ok" and not v.problems, v.summary()

    # Protect must not rebuild the folder's parity without the file (the
    # regression of 2026-09-07), and must not ask for consent either
    p = protect(str(tmp_path))
    assert p.ok and not p.needs_consent, p.summary()
    photo_sets = [s for s in p.sets if s.directory == "Photos/2019"]
    assert photo_sets and all(s.action == "kept" for s in photo_sets), p.summary()
    assert NFC_NAME in _index_names(str(d))
    v = verify(str(tmp_path), VerifyOptions(mode="thorough"))
    assert v.verdict == "ok", v.summary()

    # even when the folder has to be rebuilt (a new neighbour), the file stays in the set
    (d / "IMG_003.jpg").write_bytes(b"\x01" * 4000)
    p = protect(str(tmp_path))
    assert p.ok, p.summary()
    assert {NFC_NAME, "IMG_003.jpg"} <= _index_names(str(d))
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"

    # and rot of that file is found under the recorded name and repaired
    rot(str(d / NFD_NAME), offset=100, length=200)
    v = verify(str(tmp_path), VerifyOptions(mode="thorough"))
    assert [(x.kind, unicodedata.normalize("NFC", x.rel)) for x in v.problems] == [("mismatch", "Photos/2019/" + NFC_NAME)]
    rr = repair(str(tmp_path))
    assert rr.ok, rr.summary()
    assert (d / NFD_NAME).read_bytes() == files["Photos/2019/" + NFC_NAME]
    assert verify(str(tmp_path), VerifyOptions(mode="thorough")).verdict == "ok"


# --- F19: a par2 backup is kept when the rebuilt file does not prove -----------------

@needs_par2
def test_backup_is_kept_when_the_rebuilt_file_does_not_prove(tmp_path, monkeypatch):
    files = make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    letter = tmp_path / "Docs" / "letter.txt"
    edited = files["Docs/letter.txt"][:-100] + b"P.S. edited after the first Protect\n"
    letter.write_bytes(edited)

    # the edit is accepted into the records, but the Docs parity rebuild fails:
    # the records are now newer than the parity
    real_create = p2.create

    def failing_create(exe, directory, set_name, *a, **k):
        if os.path.basename(directory) == "Docs":
            return p2.Par2Result(action="create", returncode=1, status="error", output="", message="simulated failure")
        return real_create(exe, directory, set_name, *a, **k)
    monkeypatch.setattr(p2, "create", failing_create)
    p = protect(str(tmp_path), ProtectOptions(accept_changes=True))
    monkeypatch.setattr(p2, "create", real_create)
    assert not p.ok and any(s.directory == "Docs" and s.action == "failed" for s in p.sets), p.summary()
    assert read_sums(str(tmp_path / SUMS_FILE))["Docs/letter.txt"] == p2.hashlib.sha256(edited).hexdigest()

    # the edited file rots; par2 can only rebuild the pre-edit bytes, which do
    # not match the records, so the damaged edit is the only copy of the edit
    rot(str(letter), offset=10, length=20)
    damaged = letter.read_bytes()
    rr = repair(str(tmp_path))
    assert "Docs/letter.txt" in [x.rel for x in rr.unrepaired], rr.summary()
    assert rr.backups_kept == ["Docs/letter.txt.1"] and rr.backups_removed == [], rr.summary()
    assert (tmp_path / "Docs" / "letter.txt.1").read_bytes() == damaged
    assert letter.read_bytes() == files["Docs/letter.txt"]          # par2's rebuild: the pre-edit bytes


# --- F02: a Check that could not open the parity never calls it intact --------------

@needs_par2
def test_check_without_par2_is_never_all_good(tmp_path, monkeypatch):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok

    def nope(path=None):
        raise p2.Par2NotFound("The par2 program (par2cmdline) was not found.")
    monkeypatch.setattr(p2, "find_par2", nope)
    v = verify(str(tmp_path), VerifyOptions(mode="parity"))
    assert v.verdict == "problem", v.summary()                       # the whole check did not happen
    assert "NOT checked" in v.summary() and "all intact" not in v.summary()
    for mode in ("standard", "thorough"):
        v = verify(str(tmp_path), VerifyOptions(mode=mode))
        assert v.verdict == "attention", v.summary()                 # the files were checked, the parity was not
        assert "NOT checked (no par2 program)" in v.summary() and "all intact" not in v.summary()
    monkeypatch.undo()
    v = verify(str(tmp_path), VerifyOptions(mode="hashes"))          # by design: fingerprints only
    assert v.verdict == "ok" and "NOT checked (fingerprints-only mode)" in v.summary() and "all intact" not in v.summary()
    v = verify(str(tmp_path), VerifyOptions(mode="thorough"))
    assert v.verdict == "ok" and "all intact and current" in v.summary()


# --- F11: losing every PARITY-INDEX file is seen; a plain folder is not blamed -------

@needs_par2
def test_whole_index_loss_is_seen_and_a_plain_folder_is_not_blamed(tmp_path):
    arc = tmp_path / "arc"
    make_tree(str(arc))
    assert protect(str(arc)).ok
    for n in os.listdir(arc):
        if n.startswith(INDEX_PARITY_BASENAME):
            os.remove(arc / n)
    v = verify(str(arc), VerifyOptions(mode="standard"))
    assert v.verdict != "ok" and any(s.set_name == INDEX_PARITY_BASENAME and s.status == "degraded" for s in v.sets), v.summary()
    out = subprocess.run(["bash", CHECK_COMMAND], cwd=arc, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode != 0 and "healthy" not in out.stdout and "PARITY-INDEX" in out.stdout, out.stdout + out.stderr

    plain = tmp_path / "plain"
    make_tree(str(plain))
    v = verify(str(plain), VerifyOptions(mode="standard"))
    assert not any(s.set_name == INDEX_PARITY_BASENAME for s in v.sets), v.summary()


@needs_par2
def test_check_script_without_par2_does_not_say_healthy(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    shim = tmp_path / "_bin"
    shim.mkdir()
    for name in ("shasum", "sha256sum", "uname", "dirname", "find", "sort", "grep", "printf", "mktemp", "rm", "basename", "ls"):
        real = None
        for d in ("/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"):
            if os.path.exists(os.path.join(d, name)):
                real = os.path.join(d, name)
                break
        if real:
            os.symlink(real, shim / name)
    # a PATH with the tools the script needs and no par2 anywhere; the script's
    # own fallbacks (/opt/homebrew/bin/par2, /usr/local/bin/par2, the app) are
    # hidden by running it inside a chroot-less copy: point them nowhere via a
    # wrapper that rewrites those absolute paths
    script = (tmp_path / CHECK_COMMAND).read_text()
    for fixed in ("/opt/homebrew/bin/par2", "/usr/local/bin/par2",
                  "/Applications/Cold Archive.app/Contents/Frameworks/bin/par2",
                  "/Applications/Cold Archive.app/Contents/Resources/bin/par2"):
        script = script.replace(fixed, str(tmp_path / "nonexistent-par2"))
    (tmp_path / "check-nopar2.command").write_text(script)
    env = {"PATH": str(shim), "HOME": os.environ.get("HOME", "/"), "LANG": os.environ.get("LANG", "C.UTF-8")}
    out = subprocess.run(["/bin/bash", "check-nopar2.command"], cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
    assert out.returncode != 0 and "healthy" not in out.stdout and "NOT checked" in out.stdout, out.stdout + out.stderr
    assert "matches its fingerprint" in out.stdout                    # the files were still checked, and said so


# --- macOS exFAT: a listed (NFD) name may only be unlinkable by its NFC spelling ------

def test_remove_and_replace_try_every_spelling(tmp_path, monkeypatch):
    """Measured on a real exFAT image: listdir returns NFD, unlink/rename need the
    spelling the entry was created with. APFS hides this, so the fallback is
    exercised here with an os.remove/os.replace that accept only NFC."""
    from cold_archive.engine import layout
    nfc = "héllo.txt"
    nfd = unicodedata.normalize("NFD", nfc)
    (tmp_path / nfc).write_bytes(b"x")
    (tmp_path / "other.txt").write_bytes(b"y")
    real_remove, real_replace = os.remove, os.replace

    def picky_remove(p):
        if os.path.basename(p) != unicodedata.normalize("NFC", os.path.basename(p)):
            raise FileNotFoundError(2, "No such file or directory", p)
        real_remove(p)

    def picky_replace(a, b):
        if os.path.basename(a) != unicodedata.normalize("NFC", os.path.basename(a)):
            raise FileNotFoundError(2, "No such file or directory", a)
        real_replace(a, b)
    monkeypatch.setattr(layout.os, "remove", picky_remove)
    monkeypatch.setattr(layout.os, "replace", picky_replace)
    layout.replace_any_spelling(str(tmp_path / nfd), str(tmp_path / "moved.txt"))
    assert (tmp_path / "moved.txt").read_bytes() == b"x" and not (tmp_path / nfc).exists()
    (tmp_path / nfc).write_bytes(b"z")
    layout.remove_any_spelling(str(tmp_path / nfd))
    assert not (tmp_path / nfc).exists()
    with pytest.raises(FileNotFoundError):
        layout.remove_any_spelling(str(tmp_path / "never-there.txt"))


# --- a successful Protect leaves no build folder behind (measured 2026-09-10) -------

@needs_par2
def test_successful_protect_leaves_no_build_folder(tmp_path):
    make_tree(str(tmp_path))
    assert protect(str(tmp_path)).ok
    left = [os.path.join(dp, n) for dp, dn, _ in os.walk(tmp_path) for n in dn if n.startswith(".cold-archive-build-")]
    assert left == [], left
    # a folder left by an interrupted run holds the list file next to the
    # unfinished set; the sweep must recognise it as ours
    stale = tmp_path / "Docs" / ".cold-archive-build-4242"
    stale.mkdir()
    (stale / "names.lst").write_text("/x/y\n")
    (stale / "PARITY.vol0+1.par2").write_bytes(b"x")
    assert p2.remove_stale_build_dirs(str(tmp_path / "Docs")) == [".cold-archive-build-4242"]
    assert not stale.exists()
