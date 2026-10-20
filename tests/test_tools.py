"""The TOOLS folder: par2cmdline source by default, a zip of the app on request.
Decided 2026-09-04. Every test reads the disk back."""
from __future__ import annotations

import os
import zipfile

from cold_archive.engine import resources
from cold_archive.engine.layout import SUMS_FILE, TOOLS_DIR
from cold_archive.engine.manifest import read_sums
from cold_archive.engine.protect import ProtectOptions, protect
from cold_archive.engine.verify import VerifyOptions, verify

from conftest import make_tree, needs_par2


def _fake_tarball(tmp_path, name="par2cmdline-1.3.0.tar.bz2", size=300_000):
    d = tmp_path / "_resources"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(os.urandom(size))
    (d / resources.PAR2_LICENCE_NAME).write_text("GNU GPL v2\n")
    return p, d / resources.PAR2_LICENCE_NAME


def _fake_app(tmp_path):
    app = tmp_path / "_app" / "Cold Archive.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Cold Archive").write_bytes(os.urandom(50_000))
    (app / "Contents" / "Info.plist").write_text("<plist/>")
    return app


@needs_par2
def test_tools_folder_is_written_listed_and_protected(tmp_path, monkeypatch):
    arc = tmp_path / "arc"
    make_tree(str(arc))
    tarball, lic = _fake_tarball(tmp_path)
    monkeypatch.setattr(resources, "par2_source_tarball", lambda: str(tarball))
    monkeypatch.setattr(resources, "par2_source_licence", lambda: str(lic))
    r = protect(str(arc))
    assert r.ok, r.summary()
    assert sorted(r.tools_written) == sorted([f"{TOOLS_DIR}/{tarball.name}", f"{TOOLS_DIR}/{resources.PAR2_LICENCE_NAME}",
                                              f"{TOOLS_DIR}/{resources.BUILD_NOTES_NAME}"])
    tools = arc / TOOLS_DIR
    assert (tools / tarball.name).read_bytes() == tarball.read_bytes()
    assert "tar xjf" in (tools / resources.BUILD_NOTES_NAME).read_text()
    sums = read_sums(str(arc / SUMS_FILE))
    assert f"{TOOLS_DIR}/{tarball.name}" in sums and f"{TOOLS_DIR}/PARITY.par2" in sums
    assert (tools / "PARITY.par2").is_file()
    assert "TOOLS" in (arc / "READ-ME-FIRST.txt").read_text()
    assert verify(str(arc), VerifyOptions(mode="thorough")).verdict == "ok"
    # second run: nothing to redo, no consent question
    r2 = protect(str(arc))
    assert r2.ok and not r2.tools_written and not r2.needs_consent
    assert not [s for s in r2.sets if s.action == "created"], r2.summary()
    # the trustee deleted the tarball: it comes back, and the records never saw a removal
    (tools / tarball.name).unlink()
    r3 = protect(str(arc))
    assert r3.ok and not r3.needs_consent and r3.tools_written == [f"{TOOLS_DIR}/{tarball.name}"], r3.summary()
    assert (tools / tarball.name).read_bytes() == tarball.read_bytes()
    assert verify(str(arc)).verdict == "ok"


@needs_par2
def test_rotted_tarball_is_caught_not_overwritten(tmp_path, monkeypatch):
    arc = tmp_path / "arc"
    make_tree(str(arc))
    tarball, lic = _fake_tarball(tmp_path)
    monkeypatch.setattr(resources, "par2_source_tarball", lambda: str(tarball))
    assert protect(str(arc)).ok
    from conftest import rot
    copy = arc / TOOLS_DIR / tarball.name
    rot(str(copy))
    # size and date unchanged: a plain Protect neither rehashes nor overwrites it
    r = protect(str(arc))
    assert r.ok and not r.tools_written
    assert copy.read_bytes() != tarball.read_bytes(), "the rotted copy was silently replaced"
    assert verify(str(arc)).verdict == "problem"                     # Check sees it
    r = protect(str(arc), ProtectOptions(full_rehash=True))          # so does a full re-read
    assert f"{TOOLS_DIR}/{tarball.name}" in r.rot_suspected and not r.ok
    assert copy.read_bytes() != tarball.read_bytes()


@needs_par2
def test_app_copy_is_optional_and_zipped(tmp_path, monkeypatch):
    arc = tmp_path / "arc"
    make_tree(str(arc))
    tarball, lic = _fake_tarball(tmp_path)
    app = _fake_app(tmp_path)
    monkeypatch.setattr(resources, "par2_source_tarball", lambda: str(tarball))
    monkeypatch.setattr(resources, "app_bundle", lambda: str(app))
    r = protect(str(arc))
    assert r.ok and not any(n.endswith(".zip") for n in os.listdir(arc / TOOLS_DIR))     # off by default
    r = protect(str(arc), ProtectOptions(include_app=True))
    assert r.ok, r.summary()
    zips = [n for n in os.listdir(arc / TOOLS_DIR) if n.endswith(".zip")]
    assert zips == [resources.app_zip_name(str(app))] and zips[0].endswith("-macOS.zip")
    with zipfile.ZipFile(arc / TOOLS_DIR / zips[0]) as z:
        names = z.namelist()
    assert any(n.endswith("Contents/MacOS/Cold Archive") and n.startswith("Cold Archive.app/") for n in names), names
    assert f"{TOOLS_DIR}/{zips[0]}" in read_sums(str(arc / SUMS_FILE))
    assert verify(str(arc), VerifyOptions(mode="thorough")).verdict == "ok"
    r2 = protect(str(arc), ProtectOptions(include_app=True))     # idempotent
    assert r2.ok and not r2.tools_written


@needs_par2
def test_missing_tools_are_reported_not_fatal(tmp_path):
    arc = tmp_path / "arc"
    make_tree(str(arc))
    r = protect(str(arc), ProtectOptions(include_app=True))     # conftest: no tarball, no app
    assert r.ok, r.summary()
    assert not (arc / TOOLS_DIR).exists()
    assert any("par2cmdline source" in w for w in r.warnings) and any("copy of the app" in w for w in r.warnings)


@needs_par2
def test_tools_can_be_switched_off(tmp_path, monkeypatch):
    arc = tmp_path / "arc"
    make_tree(str(arc))
    tarball, lic = _fake_tarball(tmp_path)
    monkeypatch.setattr(resources, "par2_source_tarball", lambda: str(tarball))
    r = protect(str(arc), ProtectOptions(include_tools=False))
    assert r.ok and not (arc / TOOLS_DIR).exists()
    assert not any("TOOLS" in w or "par2cmdline" in w for w in r.warnings), r.warnings
