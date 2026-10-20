# Third-party software in Cold Archive

Cold Archive's own code and documentation are under the Apache License 2.0
(`LICENSE`, `NOTICE`). Anyone may use, change, fork and redistribute them,
including in closed or commercial products, as long as the licence and
notices travel with them.

The Python package (`pip install` / `pipx install` from source) contains only
that code; it asks the installer for PySide6, which comes with its own
licence. The macOS app (`Cold_Archive.dmg`) bundles the parts below. Each
part's licence permits this distribution, and none of them places Cold
Archive's own code under the GPL or LGPL; the reasons follow the table.

| Component | Version | Licence | How it is included | Licence text in `licenses/` | Source code |
|---|---|---|---|---|---|
| par2cmdline (`bin/par2`, `bin/par2-fast`) | 1.4.0 | GPL-2.0-or-later | separate programs the app runs; never linked into it | `GPL-2.0-par2cmdline.txt` | included: `par2cmdline-1.4.0.tar.bz2` inside the app, and in the TOOLS folder of every archive |
| LLVM OpenMP runtime (`libomp.dylib`) | 23.1.3 | Apache-2.0 WITH LLVM-exception | a library loaded only by `bin/par2-fast` | `LLVM-OpenMP-libomp.txt` | https://github.com/llvm/llvm-project |
| Qt 6: Core, GUI, Widgets, D-Bus, SVG | 6.11.2 | LGPL-3.0-only | unmodified, dynamically linked frameworks | `LGPL-3.0.txt`, `GPL-3.0.txt`, `Qt-third-party-notices.md` | https://download.qt.io/official_releases/qt/6.11/6.11.2/ |
| PySide6 and shiboken6 | 6.11.2 | LGPL-3.0-only | unmodified, dynamically linked libraries and Python modules | `LGPL-3.0.txt`, `GPL-3.0.txt` | https://download.qt.io/official_releases/QtForPython/pyside6/ (PySide6-6.11.2-src) and https://code.qt.io/cgit/pyside/pyside-setup.git (tag v6.11.2) |
| CPython, interpreter and standard library | 3.12.13 (python-build-standalone 20260610) | PSF-2.0 | the Python runtime inside the app | `Python-PSF.txt`, `CPython-incorporated-software.rst` | https://www.python.org/downloads/source/ and https://github.com/astral-sh/python-build-standalone |
| OpenSSL | 3.5.7 | Apache-2.0 | compiled into the bundled Python | `Apache-2.0.txt`; also `CPython-incorporated-software.rst`, section OpenSSL | https://www.openssl.org/source/ |
| expat | 2.8.1 | MIT | compiled into the bundled Python | `CPython-incorporated-software.rst`, section expat | https://github.com/libexpat/libexpat |
| mpdecimal | 4.0.0 | BSD-2-Clause | compiled into the bundled Python | `CPython-incorporated-software.rst`, section libmpdec | https://www.bytereef.org/mpdecimal/ |
| libffi | as built by python-build-standalone 20260610 | MIT | compiled into the bundled Python | `libffi-MIT.txt` | https://github.com/libffi/libffi |
| xz (liblzma) | as built by python-build-standalone 20260610 | 0BSD | compiled into the bundled Python | `xz-liblzma-0BSD.txt` | https://github.com/tukaani-project/xz |
| bzip2 | as built by python-build-standalone 20260610 | bzip2-1.0.6 | compiled into the bundled Python | `bzip2-LICENSE.txt` | https://sourceware.org/bzip2/ |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later WITH Bootloader exception | the small program that starts the app | `PyInstaller-COPYING.txt` | https://github.com/pyinstaller/pyinstaller |

zlib comes from macOS itself and is not bundled. The exact versions in a
given build are recorded in `BUILD-VERSIONS.txt` inside the app.

## Why none of this changes Cold Archive's licence

- **par2cmdline (GPL).** Cold Archive runs par2 as a separate program and
  talks to it only through files, command-line arguments and its printed
  output. The GPL treats two programs shipped side by side this way as an
  aggregate: each keeps its own licence. What the GPL asks of the
  distributor is met in the package itself: the licence text and the exact
  source of the binaries travel with them. `bin/par2` is built unmodified
  from that source; `bin/par2-fast` is Homebrew's unmodified build of the
  same release.
- **Qt, PySide6 and shiboken6 (LGPL).** They are unmodified and dynamically
  linked, as separate files under `Cold Archive.app/Contents/Frameworks/`.
  You may replace any of them with your own build of the same version:
  put the new file in place and re-sign the app for your own use with
  `codesign --force --deep --sign - "Cold Archive.app"`. Only five Qt
  modules are shipped, all under the LGPL. Qt modules that are available
  only under the GPL, such as Qt Virtual Keyboard, are kept out of the app,
  and the build refuses to finish if one appears.
- **The PyInstaller bootloader (GPL with an exception).** Its licence
  explicitly allows bundling it with programs under any licence.
- **Everything else** is under permissive licences that ask only that their
  notices are kept, which `licenses/` does. Where a component offers a
  choice of licences, as FreeType and D-Bus do, the permissive choice is the
  one used.

## Where the source code is

The complete source of every GPL and LGPL component travels with the app
itself, so nothing has to be requested:

- **par2cmdline:** inside the app (`Contents/Resources/resources/`), and in
  the TOOLS folder of every archive the app makes.
- **Qt, PySide6 and shiboken6:** the exact source archives the bundled
  libraries come from are attached to every Cold Archive release, next to
  `Cold_Archive.dmg`, at https://github.com/wwward/cold-archive/releases,
  with `SOURCES.txt` describing them and giving their SHA-256. They are
  gathered and checked by `packaging/release_sources.py`.
- **The PyInstaller bootloader:** its licence exception allows it to be
  distributed inside the app without its source; it is at the address in
  the table.

If you received Cold Archive from somewhere else, the release page above has
the same files for every version.

## Acknowledgements

Portions of this software are copyright © The FreeType Project
(www.freetype.org). All rights reserved.

The full copyright lines and licence texts of the code compiled into the
bundled Qt modules are in `licenses/Qt-third-party-notices.md`, taken from
https://doc.qt.io/qt-6/licenses-used-in-qt.html (regenerate with
`packaging/qt_notices.py`).
