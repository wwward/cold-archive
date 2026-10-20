"""Gather the source code that must be published next to every app release.

The macOS app bundles software under the GPL and LGPL: par2cmdline (GPL-2.0+),
and Qt, PySide6 and shiboken6 (LGPL-3.0). Those licences let a binary be
distributed only together with access to its complete source. Cold Archive
meets that by publishing the exact source archives in the same place as the
app, as files attached to the same release, so nobody has to promise to send
anything later (GPLv3 section 6(d), which the LGPL incorporates; GPLv2
section 3(a) for par2cmdline, whose source is also inside the app).

    .venv/bin/python packaging/release_sources.py

writes release/sources/: the Qt module sources for the Qt version the
bundled PySide6 was built on (qtbase holds Core, GUI, Widgets, D-Bus and the
platform and style plugins; qtsvg holds SVG and its plugins; qtimageformats
holds the icns plugin; together they cover packaging/ColdArchive.spec's
QT_ALLOWED and QT_PLUGINS), the PySide6/shiboken6 source, the par2cmdline
source the build used, and SOURCES.txt describing them. Every download is
checked against the SHA-256 that download.qt.io publishes for it. Attach
everything in release/sources/ to the release, next to Cold_Archive.dmg.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "release", "sources")
QT_MODULES = ["qtbase", "qtsvg", "qtimageformats"]   # must cover QT_ALLOWED + QT_PLUGINS in the spec
BASE = "https://download.qt.io/official_releases"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def published_sha256(url: str) -> str:
    with urllib.request.urlopen(url + ".mirrorlist", timeout=60) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r"SHA-256 Hash</a>:\s*(?:<[^>]+>\s*)*([0-9a-f]{64})", page) or re.search(r"\b([0-9a-f]{64})\b", page)
    if not m:
        sys.exit(f"no SHA-256 published for {url}")
    return m.group(1)


def fetch(url: str) -> str:
    dest = os.path.join(OUT, url.rsplit("/", 1)[1])
    want = published_sha256(url)
    if not (os.path.isfile(dest) and sha256(dest) == want):
        print(f"downloading {url}")
        tmp = dest + ".part"
        with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        got = sha256(tmp)
        if got != want:
            os.remove(tmp)
            sys.exit(f"checksum mismatch for {url}: got {got}, Qt publishes {want}")
        os.replace(tmp, dest)
    return dest


def main() -> None:
    import PySide6
    import PySide6.QtCore as QtCore
    pyside, qt = PySide6.__version__, QtCore.qVersion()
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for mod in QT_MODULES:
        url = f"{BASE}/qt/{'.'.join(qt.split('.')[:2])}/{qt}/submodules/{mod}-everywhere-src-{qt}.tar.xz"
        rows.append((fetch(url), f"Qt {qt} {mod}", url))
    url = f"{BASE}/QtForPython/pyside6/PySide6-{pyside}-src/pyside-setup-everywhere-src-{pyside}.tar.xz"
    rows.append((fetch(url), f"PySide6 and shiboken6 {pyside}", url))
    par2 = sorted(glob.glob(os.path.join(ROOT, "build", "par2-src", "par2cmdline-*.tar.*")))
    if not par2:
        sys.exit("no par2cmdline source in build/par2-src; run packaging/build_app.sh first")
    dest = os.path.join(OUT, os.path.basename(par2[-1]))
    shutil.copy2(par2[-1], dest)
    rows.append((dest, "par2cmdline (also inside the app, Contents/Resources/resources)",
                 "https://github.com/Parchive/par2cmdline/releases"))

    lines = ["Source code for the GPL and LGPL software bundled in Cold Archive.app", "",
             "These are the exact, unmodified upstream releases the app's copies come from. Qt and",
             "PySide6 were built by The Qt Company into the PySide6 packages on PyPI; par2cmdline",
             "was built from the archive below by packaging/build_app.sh. They are published next",
             "to the app in every release, so the source is always where the app is.", ""]
    for path, what, url in rows:
        lines += [os.path.basename(path), f"  {what}", f"  from {url}", f"  SHA-256 {sha256(path)}", ""]
    with open(os.path.join(OUT, "SOURCES.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    for p in sorted(os.listdir(OUT)):
        print(f"  {p}  {os.path.getsize(os.path.join(OUT, p)):,} bytes")


if __name__ == "__main__":
    main()
