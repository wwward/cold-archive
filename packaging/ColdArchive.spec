# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cold Archive.app. Run via packaging/build_app.sh, which
prepares build/par2-bundle (universal par2 built from source; optional par2-fast + libomp) first."""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

sys.path.insert(0, os.path.abspath("src"))
import cold_archive  # noqa: E402

HERE = os.path.abspath("packaging")
PAR2_BUNDLE = os.path.abspath(os.path.join("build", "par2-bundle"))

binaries = []
for name in ("par2", "par2-fast", "libomp.dylib"):
    p = os.path.join(PAR2_BUNDLE, name)
    if os.path.exists(p):
        binaries.append((p, "bin"))

datas = [(os.path.join(HERE, "ColdArchive.png"), "resources")]
if os.path.exists(os.path.join(PAR2_BUNDLE, "par2-fast.minos")):
    datas.append((os.path.join(PAR2_BUNDLE, "par2-fast.minos"), "bin"))
for name in os.listdir(PAR2_BUNDLE) if os.path.isdir(PAR2_BUNDLE) else []:
    if name in ("BUILD-VERSIONS.txt", "LICENSE-par2cmdline.txt") or name.endswith((".tar.bz2", ".tar.gz")):
        datas.append((os.path.join(PAR2_BUNDLE, name), "resources"))
# Licences: Cold Archive's own, and every bundled component's (THIRD-PARTY-NOTICES.md).
ROOT = os.path.abspath(".")
for name in ("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"):
    datas.append((os.path.join(ROOT, name), "licenses"))
for name in sorted(os.listdir(os.path.join(ROOT, "licenses"))):
    datas.append((os.path.join(ROOT, "licenses", name), "licenses"))

# Qt: ship only what the app uses, and nothing that is GPL-only. PyInstaller's
# PySide6 hooks collect every plugin of the Qt plugin types QtGui touches,
# and with them their dependencies. MEASURED 2026-10-19 (0.2.2 bundle): the
# platforminputcontexts plugin pulled in Qt Virtual Keyboard, which Qt
# licenses only under the GPL v3 (or commercially), together with Quick, Qml,
# Network, OpenGL and Pdf, none of which the app uses. QT_ALLOWED must match
# packaging/build_app.sh, which refuses any other Qt framework in the bundle,
# and packaging/qt_notices.py, which writes the notices for exactly these.
QT_ALLOWED = {"QtCore", "QtGui", "QtWidgets", "QtDBus", "QtSvg"}   # QtGui links QtDBus on macOS
QT_PLUGINS = {"platforms/libqcocoa.dylib", "platforms/libqoffscreen.dylib", "platforms/libqminimal.dylib",
              "styles/libqmacstyle.dylib", "iconengines/libqsvgicon.dylib",
              "imageformats/libqsvg.dylib", "imageformats/libqicns.dylib"}


def _qt_keep(dest: str) -> bool:
    d = dest.replace("\\", "/")
    if "/Qt/lib/" in d:
        fw = d.split("/Qt/lib/", 1)[1].split("/", 1)[0]
        return fw.split(".")[0] in QT_ALLOWED
    if "/Qt/plugins/" in d:
        return d.split("/Qt/plugins/", 1)[1] in QT_PLUGINS
    if "/Qt/qml/" in d:
        return False
    base = d.rsplit("/", 1)[-1]
    if d.startswith("PySide6/") and base.startswith("Qt") and base.endswith(".abi3.so"):
        return base.split(".")[0] in QT_ALLOWED
    return True

a = Analysis(
    [os.path.join(HERE, "launcher.py")],
    pathex=[os.path.abspath("src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=collect_submodules("cold_archive"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtNetwork", "PySide6.QtDBus", "PySide6.QtOpenGL", "PySide6.QtPdf", "PySide6.QtVirtualKeyboard"],
    noarchive=False,
)
def _keep_entry(entry) -> bool:
    # PyInstaller also lists top-level links such as Frameworks/QtQuick ->
    # PySide6/Qt/lib/QtQuick.framework/...; a link whose target is filtered out
    # must go too, or signing fails on the dangling link (MEASURED 2026-10-19).
    dest, src, typecode = entry[0], entry[1], entry[2]
    return _qt_keep(dest) and not (typecode == "SYMLINK" and not _qt_keep(str(src)))


a.binaries = [t for t in a.binaries if _keep_entry(t)]
a.datas = [t for t in a.datas if _keep_entry(t)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Cold Archive",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Cold Archive")
app = BUNDLE(
    coll,
    name="Cold Archive.app",
    icon=os.path.join(HERE, "ColdArchive.icns"),
    bundle_identifier=cold_archive.BUNDLE_ID,
    info_plist={
        "CFBundleName": "Cold Archive",
        "CFBundleDisplayName": "Cold Archive",
        "CFBundleShortVersionString": cold_archive.__version__,
        "CFBundleVersion": cold_archive.__version__,
        "LSMinimumSystemVersion": "13.0",   # Qt 6.9 and the bundled par2 are built for 13+
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": "Copyright 2026 Billie Grace Ward and the Cold Archive contributors. Apache License 2.0; bundled software under its own licences, see Resources/licenses.",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSHasLocalizedDisplayName": False,
    },
)
