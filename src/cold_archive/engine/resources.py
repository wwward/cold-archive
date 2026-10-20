"""Files the app carries for the archive's TOOLS folder, and where they live.

Every archive gets a ``TOOLS`` folder holding the par2cmdline source release
the app's own par2 was built from (GPL; a trustee in 2036 can build it on
whatever they have) and, on request, a zip of this app. The folder is ordinary
payload: it is hashed into SHA256SUMS and gets its own PARITY set like any
other folder, so the tools are protected by the same mechanism they serve.

Locations differ between the frozen .app (PyInstaller puts ``datas`` under
``Contents/Resources``) and a source checkout (``build/par2-bundle`` after
``packaging/build_app.sh``, or ``build/par2-src``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from typing import List, Optional, Tuple

from .. import __version__
from .layout import TOOLS_DIR, TOOLS_TMP_SUFFIX
from .observer import NullObserver, Observer

PAR2_SOURCE_GLOB = "par2cmdline-"       # par2cmdline-1.3.0.tar.bz2
PAR2_LICENCE_NAME = "LICENSE-par2cmdline.txt"
BUILD_NOTES_NAME = "HOW-TO-BUILD-par2.txt"

BUILD_NOTES = """How to build the par2 program from the source in this folder
=========================================================

The file par2cmdline-<version>.tar.bz2 is the unmodified source code of
par2cmdline, the free program (GNU GPL v2 or later, see LICENSE-par2cmdline.txt)
that made and can repair the PARITY files in this archive. Prefer a ready-made
package if one exists (Mac: brew install par2; Debian/Ubuntu: apt install par2;
Fedora: dnf install par2cmdline; Windows: MultiPar or a par2cmdline release).
If none does, on any Unix-like system with a C++ compiler:

    tar xjf par2cmdline-*.tar.bz2
    cd par2cmdline-*/
    ./configure
    make
    ./par2 -V

Then, from inside a folder of this archive that has damaged files:

    /path/to/par2 repair PARITY.par2

The format is public (Parchive 2.0, https://parchive.github.io) and other
programs read it too; this source is here so that at least one always exists.
"""


def _resource_dirs() -> List[str]:
    dirs: List[str] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(os.path.join(base, "resources"))
        dirs.append(os.path.join(os.path.dirname(base), "Resources", "resources"))
        dirs.append(os.path.join(os.path.dirname(base), "Resources"))
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.abspath(os.path.join(here, "..", "..", ".."))
    dirs.append(os.path.join(repo, "build", "par2-bundle"))
    dirs.append(os.path.join(repo, "build", "par2-src"))
    return dirs


def par2_source_tarball() -> Optional[str]:
    """Path of the par2cmdline source tarball shipped with this build, or None."""
    for d in _resource_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.startswith(PAR2_SOURCE_GLOB) and n.endswith((".tar.bz2", ".tar.gz", ".tar.xz")):
                return os.path.join(d, n)
    return None


def par2_source_licence() -> Optional[str]:
    for d in _resource_dirs():
        p = os.path.join(d, PAR2_LICENCE_NAME)
        if os.path.isfile(p):
            return p
    return None


def app_bundle() -> Optional[str]:
    """The .app this process runs from (frozen only), or None from source."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    # Contents/Frameworks (= _MEIPASS) -> Contents -> Cold Archive.app
    app = os.path.dirname(os.path.dirname(os.path.abspath(base)))
    if app.endswith(".app") and os.path.isdir(os.path.join(app, "Contents", "MacOS")):
        return app
    return None


def app_zip_name(bundle: str) -> str:
    stem = os.path.basename(bundle)[:-4] if bundle.endswith(".app") else os.path.basename(bundle)
    return f"{stem.replace(' ', '-')}-{__version__}-macOS.zip"


def _write_if_absent(dst: str, produce) -> str:
    """Create *dst* via ``produce(tmp_path)`` unless something already exists
    there. Returns 'written', 'exists' or 'differs'. An existing file is NEVER
    replaced: it is either ours from an earlier run (then the records vouch
    for it and any rot must be caught, not papered over) or the owner's own
    file under that name (not ours to destroy). The temporary is always
    removed on failure."""
    if os.path.lexists(dst):
        return "exists"
    tmp = dst + TOOLS_TMP_SUFFIX
    try:
        produce(tmp)
        os.replace(tmp, dst)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return "written"


def _copy_file(src: str):
    return lambda tmp: shutil.copyfile(src, tmp)


def _write_bytes(data: bytes):
    def produce(tmp: str) -> None:
        with open(tmp, "wb") as f:
            f.write(data)
    return produce


def _zip_bundle(bundle: str):
    """Zip the .app keeping its parent folder name. ``ditto`` preserves the
    code signature and resource forks; the zipfile fallback keeps plain files."""
    def produce(tmp: str) -> None:
        ditto = shutil.which("ditto")
        if ditto:
            subprocess.run([ditto, "-c", "-k", "--sequesterRsrc", "--keepParent", bundle, tmp], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        else:
            parent = os.path.dirname(bundle)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, _dirs, files in os.walk(bundle):
                    for f in files:
                        p = os.path.join(dirpath, f)
                        z.write(p, os.path.relpath(p, parent))
    return produce


def materialize_tools(root: str, include_source: bool = True, include_app: bool = False,
                      observer: Optional[Observer] = None) -> Tuple[List[str], List[str]]:
    """Put the tools into ``root/TOOLS``. Returns (files written this run, warnings).

    Runs before the scan so the files are payload in the same Protect run.
    Never removes anything and never overwrites an existing file: a file that
    is already there is left exactly as it is, whether it is ours from an
    earlier run (the records catch rot) or the owner's own. A TOOLS that is
    not a real folder (a file, a symbolic link) is left alone with a warning,
    because writing through a link would put the files outside the archive."""
    obs = observer or NullObserver()
    written: List[str] = []
    warnings: List[str] = []
    tools = os.path.join(root, TOOLS_DIR)
    tarball = par2_source_tarball() if include_source else None
    bundle = app_bundle() if include_app else None
    if include_source and tarball is None:
        warnings.append(f"the par2cmdline source could not be found next to this program, so {TOOLS_DIR}/ has no copy of it")
    if include_app and bundle is None:
        warnings.append("a copy of the app was requested, but this program is not running from an installed Cold Archive.app, "
                        f"so none was added to {TOOLS_DIR}/")
    if tarball is None and bundle is None:
        return written, warnings
    if os.path.lexists(tools) and (os.path.islink(tools) or not os.path.isdir(tools)):
        warnings.append(f"{TOOLS_DIR} exists but is not a real folder (a file or a link), so no tool files were added; "
                        "rename it if you want the par2 source in the archive")
        return written, warnings
    try:
        os.makedirs(tools, exist_ok=True)
    except OSError as e:
        warnings.append(f"the {TOOLS_DIR} folder could not be created ({e}); no tool files were added")
        return written, warnings

    def put(name: str, produce) -> None:
        dst = os.path.join(tools, name)
        try:
            outcome = _write_if_absent(dst, produce)
        except (OSError, subprocess.CalledProcessError) as e:
            warnings.append(f"{TOOLS_DIR}/{name} could not be written ({e})")
            return
        if outcome == "written":
            written.append(f"{TOOLS_DIR}/{name}")

    if tarball is not None:
        put(os.path.basename(tarball), _copy_file(tarball))
        lic = par2_source_licence()
        if lic:
            put(PAR2_LICENCE_NAME, _copy_file(lic))
        put(BUILD_NOTES_NAME, _write_bytes(BUILD_NOTES.encode("utf-8")))
    if bundle is not None:
        name = app_zip_name(bundle)
        if not os.path.lexists(os.path.join(tools, name)):
            obs.phase("Packing a copy of the app")
        put(name, _zip_bundle(bundle))
    return written, warnings
