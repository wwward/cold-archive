#!/bin/bash
# Build Cold Archive.app (and a DMG) with the par2 engine bundled inside.
#
#   packaging/build_app.sh                 # unsigned (ad-hoc) build for local testing
#   SIGN_IDENTITY="Developer ID Application: ..." packaging/build_app.sh
#   SIGN_IDENTITY=... NOTARY_PROFILE=<keychain profile> packaging/build_app.sh   # signed + notarized + stapled
#
# Requires: the project venv (.venv) with PySide6 and pyinstaller; Xcode command line
# tools (par2 is built from source); optionally Homebrew par2 for the threaded engine.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "no .venv; run: uv venv && uv pip install -e '.[dev]'"; exit 1; }
"$PY" -c "import PyInstaller, PySide6" 2>/dev/null || { echo "installing build deps into .venv"; uv pip install --python "$PY" -e ".[dev]"; }

# Two par2 engines travel in the app:
#   par2       built here from the par2cmdline source release, universal (arm64 + x86_64),
#              macOS 13+, no OpenMP: runs everywhere the app runs. Creates parity ~4x
#              slower than the threaded build (MEASURED 45 s/GB vs 11 s/GB); verify is equal.
#   par2-fast  Homebrew's threaded build (+ libomp), used only on a macOS at least as new
#              as the one it was built for (its minos is recorded next to it).
PAR2_RELEASE="1.4.0"
PAR2_TARBALL_URL="https://github.com/Parchive/par2cmdline/releases/download/v$PAR2_RELEASE/par2cmdline-$PAR2_RELEASE.tar.bz2"
# Checked 2026-10-19 against Homebrew's formula and the release's checksums.sha512.
PAR2_TARBALL_SHA256="269aff9d49c6a0c0d1c394300d35c36229764588f56faad5850eaf36c7a298dc"
PAR2_MIN_MACOS="${PAR2_MIN_MACOS:-13.0}"
FAST_SRC="${PAR2_SRC:-$(command -v par2 || echo /opt/homebrew/bin/par2)}"

echo "== 1. icon"
"$PY" packaging/make_icon.py

echo "== 2a. build the universal par2 from source (macOS $PAR2_MIN_MACOS+, no OpenMP)"
B="$ROOT/build/par2-bundle"
rm -rf "$B"; mkdir -p "$B" "$ROOT/build/par2-src"
TARBALL="$ROOT/build/par2-src/$(basename "$PAR2_TARBALL_URL")"
if [ ! -f "$TARBALL" ] || [ "$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)" != "$PAR2_TARBALL_SHA256" ]; then
  echo "downloading $PAR2_TARBALL_URL"
  curl -sSL -o "$TARBALL" "$PAR2_TARBALL_URL"
fi
[ "$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)" = "$PAR2_TARBALL_SHA256" ] || { echo "par2cmdline tarball checksum mismatch"; exit 1; }
SRCDIR="$ROOT/build/par2-src/par2cmdline-$PAR2_RELEASE"
rm -rf "$SRCDIR"; tar -xjf "$TARBALL" -C "$ROOT/build/par2-src"
( cd "$SRCDIR" \
  && ./configure --disable-openmp \
       CFLAGS="-O3 -arch arm64 -arch x86_64 -mmacosx-version-min=$PAR2_MIN_MACOS" \
       CXXFLAGS="-O3 -arch arm64 -arch x86_64 -mmacosx-version-min=$PAR2_MIN_MACOS" \
       LDFLAGS="-arch arm64 -arch x86_64 -mmacosx-version-min=$PAR2_MIN_MACOS" >configure.log 2>&1 \
  && make -j"$(sysctl -n hw.ncpu)" >make.log 2>&1 ) || { echo "par2 build failed; see $SRCDIR/configure.log and make.log"; exit 1; }
cp "$SRCDIR/par2" "$B/par2"; chmod 755 "$B/par2"
file "$B/par2" | grep -q "x86_64" && file "$B/par2" | grep -q "arm64" || { echo "par2 is not universal"; exit 1; }
otool -L "$B/par2" | grep -q libomp && { echo "universal par2 must not depend on libomp"; exit 1; }
arch -x86_64 "$B/par2" -V >/dev/null 2>&1 && echo "   x86_64 slice runs under Rosetta" || echo "   (x86_64 slice not testable here)"
codesign --force --sign - "$B/par2"
"$B/par2" -V || { echo "bundled par2 does not run"; exit 1; }
PAR2_VERSION="$("$B/par2" -V | head -1)"
cp "$SRCDIR/COPYING" "$B/LICENSE-par2cmdline.txt"
cp "$TARBALL" "$B/"     # GPL: the corresponding source travels with the binary

echo "== 2b. the threaded build for new macOS (optional)"
if [ -x "$FAST_SRC" ]; then
  cp "$FAST_SRC" "$B/par2-fast"; chmod 755 "$B/par2-fast"
  LIBOMP_SRC="$(otool -L "$B/par2-fast" | awk '/libomp/ {print $1}' | head -1)"
  if [ -n "$LIBOMP_SRC" ] && [ -f "$LIBOMP_SRC" ]; then
    cp "$LIBOMP_SRC" "$B/libomp.dylib"; chmod 644 "$B/libomp.dylib"
    install_name_tool -change "$LIBOMP_SRC" "@loader_path/libomp.dylib" "$B/par2-fast"
    install_name_tool -id "@loader_path/libomp.dylib" "$B/libomp.dylib"
    codesign --force --sign - "$B/libomp.dylib"
  fi
  codesign --force --sign - "$B/par2-fast"
  FAST_MINOS="$(otool -l "$B/par2-fast" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')"
  FAST_ARCH="$(uname -m)"
  printf '%s %s\n' "${FAST_MINOS:-99.0}" "$FAST_ARCH" > "$B/par2-fast.minos"
  "$B/par2-fast" -V >/dev/null || { echo "par2-fast does not run"; exit 1; }
  echo "   par2-fast: minos $FAST_MINOS, $FAST_ARCH"
else
  echo "   (no Homebrew par2; the universal build serves every Mac)"
fi
# The licences and the component table live in the repo (THIRD-PARTY-NOTICES.md,
# licenses/) and are copied in by the spec; this records what THIS build used,
# read from the tools themselves, and the gate after PyInstaller checks the
# table against it.
FAST_VERSION="$( [ -x "$B/par2-fast" ] && "$B/par2-fast" -V 2>/dev/null | head -1 || echo none)"
# the version par2-fast links (opt/libomp), not the first of possibly several kegs
LIBOMP_VERSION="$(basename "$(readlink "$(brew --prefix 2>/dev/null)/opt/libomp" 2>/dev/null)" 2>/dev/null || true)"
cat > "$B/BUILD-VERSIONS.txt" <<EOF
Cold Archive $("$PY" -c 'import sys; sys.path.insert(0, "src"); import cold_archive; print(cold_archive.__version__)'), built $(date -u +%Y-%m-%dT%H:%MZ) from commit $(git rev-parse --short HEAD)$(git diff --quiet HEAD -- || echo "+uncommitted")
bin/par2:       $PAR2_VERSION, built here from $(basename "$TARBALL") (universal, macOS $PAR2_MIN_MACOS+, no OpenMP)
bin/par2-fast:  $FAST_VERSION (Homebrew build, threaded), libomp ${LIBOMP_VERSION:-none}
Python:         $("$PY" -c 'import sys, ssl; print(sys.version.split()[0], "with", ssl.OPENSSL_VERSION)')
PySide6:        $("$PY" -c 'import PySide6, shiboken6; print(PySide6.__version__, "/ shiboken6", shiboken6.__version__)')
PyInstaller:    $("$PY" -c 'import PyInstaller; print(PyInstaller.__version__)')
Licences: see licenses/THIRD-PARTY-NOTICES.md.
EOF
cat "$B/BUILD-VERSIONS.txt"

echo "== 3. pyinstaller"
DIST="${DIST:-$ROOT/dist}"
rm -rf "build/Cold Archive"
if ! rm -rf "$DIST/Cold Archive" "$DIST/Cold Archive.app" 2>/dev/null; then
  # a previous build left root-owned files there (sudo); do not ask for a password, build elsewhere
  DIST="$ROOT/build/dist"
  echo "(dist/ holds files this user cannot remove; building into $DIST instead)"
  rm -rf "$DIST/Cold Archive" "$DIST/Cold Archive.app"
fi
mkdir -p "$DIST"
"$ROOT/.venv/bin/pyinstaller" --noconfirm --clean --distpath "$DIST" --workpath build packaging/ColdArchive.spec
APP="$DIST/Cold Archive.app"
[ -d "$APP" ] || { echo "no app produced"; exit 1; }

echo "== 4. put the universal par2 back (PyInstaller thins fat binaries to the host arch) and check"
INNER="$(find "$APP/Contents" -type f -name par2 | head -1)"
[ -n "$INNER" ] || { echo "par2 missing from bundle"; exit 1; }
cp "$B/par2" "$INNER"; chmod 755 "$INNER"; codesign --force --sign - "$INNER"
file "$INNER" | grep -q universal || { echo "bundled par2 is not universal"; exit 1; }
"$INNER" -V
FAST="$(find "$APP/Contents" -type f -name par2-fast | head -1)"
if [ -n "$FAST" ]; then
  echo "par2-fast links: $(otool -L "$FAST" | grep -i omp | awk '{print $1}')"
  "$FAST" -V >/dev/null || { echo "bundled par2-fast does not run"; exit 1; }
  [ -f "$(dirname "$FAST")/par2-fast.minos" ] || { echo "par2-fast.minos missing"; exit 1; }
fi
PLIST_MIN="$(/usr/libexec/PlistBuddy -c "Print :LSMinimumSystemVersion" "$APP/Contents/Info.plist")"
QT_MIN="$(otool -l "$(find "$APP/Contents/Frameworks" -name QtCore -type f | head -1)" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')"
# MEASURED (2026-09-04): the PySide6 6.11 wheels are tagged macosx_13_0 but
# their .so files carry LC_BUILD_VERSION minos 15.0; dyld refuses to load a
# binary built for a newer macOS than the running one, so the app would die
# at "import PySide6" on 13 and 14 while claiming to support them. The plist
# must state the highest minos of ANY Mach-O in the bundle (par2-fast and
# its libomp.dylib excepted: they are only used where they can run, see
# par2.py; the Homebrew libomp carries the host's minos, 26.0 here).
MAX_MINOS="$(find "$APP/Contents" -type f ! -name par2-fast ! -name libomp.dylib \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 \
  | xargs -0 -n1 sh -c 'otool -l "$0" 2>/dev/null | awk "/LC_BUILD_VERSION/{f=1} f&&/minos/{print \$2; exit}"' \
  | sort -t. -k1,1n -k2,2n | tail -1)"
echo "Info.plist LSMinimumSystemVersion=$PLIST_MIN, Qt minos=$QT_MIN, par2 minos=$PAR2_MIN_MACOS, highest minos in bundle=$MAX_MINOS"
if [ -n "$MAX_MINOS" ] && [ "$(printf '%s\n%s\n' "$PLIST_MIN" "$MAX_MINOS" | sort -t. -k1,1n -k2,2n | tail -1)" != "$PLIST_MIN" ]; then
  echo "   raising LSMinimumSystemVersion to $MAX_MINOS (a bundled binary needs it)"
  /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion $MAX_MINOS" "$APP/Contents/Info.plist"
fi
# Gate (MEASURED 2026-10-19): a dependency upgrade can raise the floor on its
# own. A rebuilt .venv resolved PySide6 6.12.0, whose binaries carry minos
# 26.0, and the line above silently promoted the app to macOS 26 only, while
# the build said "Done". The app is meant for the macOS chosen as
# the floor (15, from PySide6 6.11.2); anything above that is a release
# blocker, not a note. Set APP_MAX_FLOOR to accept a higher floor knowingly.
APP_MAX_FLOOR="${APP_MAX_FLOOR:-15.0}"
FLOOR="$(/usr/libexec/PlistBuddy -c "Print :LSMinimumSystemVersion" "$APP/Contents/Info.plist")"
if [ "$(printf '%s\n%s\n' "$APP_MAX_FLOOR" "$FLOOR" | sort -t. -k1,1n -k2,2n | tail -1)" != "$APP_MAX_FLOOR" ]; then
  echo "REFUSED: the bundle needs macOS $FLOOR but the app is meant to run on macOS $APP_MAX_FLOOR and later."
  echo "Binaries above $APP_MAX_FLOOR (par2-fast and libomp.dylib are exempt, they are gated at runtime):"
  find "$APP/Contents" -type f ! -name par2-fast ! -name libomp.dylib \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 \
    | xargs -0 -n1 sh -c 'v=$(otool -l "$0" 2>/dev/null | awk "/LC_BUILD_VERSION/{f=1} f&&/minos/{print \$2; exit}"); [ -n "$v" ] && [ "$(printf "%s\n%s\n" "'"$APP_MAX_FLOOR"'" "$v" | sort -t. -k1,1n -k2,2n | tail -1)" != "'"$APP_MAX_FLOOR"'" ] && echo "   $v $0"' | head -20
  echo "Pin the dependency (pyproject.toml pins PySide6 <6.12 for this reason) or set APP_MAX_FLOOR=$FLOOR to accept it."
  exit 1
fi

echo "== 4b. licence gates"
# (1) Only the Qt modules the notices cover. Several Qt modules are GPL-only
# (Qt Virtual Keyboard reached the 0.2.2 bundle through a plugin; MEASURED
# 2026-10-19). An allow-list, not a deny-list: a new module fails the build
# until someone has read its licence. Must match QT_ALLOWED in the spec.
QT_ALLOWED="QtCore QtGui QtWidgets QtDBus QtSvg"
QTLIB="$(find "$APP/Contents" -type d -path "*PySide6/Qt/lib" | head -1)"
for fw in $(ls "$QTLIB" | sed 's/\.framework$//'); do
  case " $QT_ALLOWED " in *" $fw "*) ;; *) echo "REFUSED: Qt module $fw is in the bundle but not in the licence allow-list ($QT_ALLOWED)"; exit 1;; esac
done
# (2) Nothing in the bundle may link a Qt module that is not there (a filtered
# dependency would only fail at run time, when a plugin loads).
MISSING="$(find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 \
  | xargs -0 -n1 sh -c 'otool -L "$0" 2>/dev/null | awk "NR>2{print \$1}" | grep -o -E "@rpath/Qt[A-Za-z]+" | sed "s|@rpath/||" | while read -r m; do [ -d "'"$QTLIB"'/$m.framework" ] || echo "$0 needs $m"; done' | sort -u)"
[ -z "$MISSING" ] || { echo "REFUSED: bundle files link Qt modules that are not bundled:"; echo "$MISSING"; exit 1; }
# (3) The licences ship, and the notices table names the versions this build used.
LIC="$APP/Contents/Resources/licenses"
for f in LICENSE NOTICE THIRD-PARTY-NOTICES.md Apache-2.0.txt GPL-2.0-par2cmdline.txt LGPL-3.0.txt GPL-3.0.txt \
         Qt-third-party-notices.md Python-PSF.txt CPython-incorporated-software.rst PyInstaller-COPYING.txt LLVM-OpenMP-libomp.txt; do
  [ -s "$LIC/$f" ] || { echo "REFUSED: licences/$f missing from the bundle"; exit 1; }
done
find "$APP/Contents" -name "$(basename "$TARBALL")" | grep -q . || { echo "REFUSED: the par2cmdline source is not in the bundle (GPL)"; exit 1; }
NOTICES="$ROOT/THIRD-PARTY-NOTICES.md"
for want in "| $PAR2_RELEASE |" "| $("$PY" -c 'import PySide6; print(PySide6.__version__)') |" \
            "| $("$PY" -c 'import sys; print(sys.version.split()[0])') " "| $("$PY" -c 'import ssl; print(ssl.OPENSSL_VERSION.split()[1])') |" \
            "| $("$PY" -c 'import PyInstaller; print(PyInstaller.__version__)') |"; do
  grep -qF -- "$want" "$NOTICES" || { echo "REFUSED: THIRD-PARTY-NOTICES.md does not name '$want' from this build; update the table"; exit 1; }
done
if [ -n "${LIBOMP_VERSION:-}" ] && [ -n "$FAST" ]; then
  grep -qF -- "| $LIBOMP_VERSION |" "$NOTICES" || { echo "REFUSED: THIRD-PARTY-NOTICES.md does not name libomp $LIBOMP_VERSION; update the table"; exit 1; }
fi
BROKEN="$(find "$APP" -type l ! -exec test -e {} \; -print)"
[ -z "$BROKEN" ] || { echo "REFUSED: links in the bundle point at files that are not there:"; echo "$BROKEN" | head -20; exit 1; }
echo "   Qt modules: $(ls "$QTLIB" | sed 's/\.framework$//' | tr '\n' ' ')"
echo "   licences: $(ls "$LIC" | wc -l | tr -d ' ') files; notices match this build"

echo "== 5. sign"
if [ -n "${SIGN_IDENTITY:-}" ]; then
  # Sign inside-out: every Mach-O first, then the bundle, all with the hardened runtime.
  find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 \
    | xargs -0 -n1 codesign --force --options runtime --timestamp --entitlements packaging/entitlements.plist --sign "$SIGN_IDENTITY" 2>/dev/null || true
  find "$APP/Contents/Frameworks" -type d -name "*.framework" -print0 \
    | xargs -0 -n1 codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" 2>/dev/null || true
  codesign --force --options runtime --timestamp --entitlements packaging/entitlements.plist --sign "$SIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
else
  echo "(no SIGN_IDENTITY: ad-hoc signature; fine for this Mac, Gatekeeper will block it elsewhere)"
  codesign --force --deep --sign - "$APP"
fi

echo "== 6. smoke-launch the built app for 6 seconds"
QT_QPA_PLATFORM=offscreen "$PY" - "$APP/Contents/MacOS/Cold Archive" <<'EOF'
import subprocess, sys, time
p = subprocess.Popen([sys.argv[1]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(6)
alive = p.poll() is None
if alive:
    p.terminate()
    try: p.wait(5)
    except subprocess.TimeoutExpired: p.kill()
out = p.stdout.read()
print("app stayed alive for 6 s:", alive)
if out.strip(): print(out[-2000:])
sys.exit(0 if alive else 1)
EOF

echo "== 6b. compatibility gate: archives made by every earlier release must check and repair"
# Decided 2026-10-19: the Shoebox drives must be repairable by any build we
# produce, now or later. tests/compat/archives holds archives made by real
# past releases; this runs the BUILT app's command line and each bundled
# par2 engine against them, and refuses the build on any failure.
"$PY" packaging/compat_gate.py "$APP"

make_dmg() {
  mkdir -p release
  rm -f "$DMG"
  STAGE="$(mktemp -d "$ROOT/build/dmg-stage.XXXXXX")"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  # The plain-language guides travel with every download, as Markdown.
  mkdir "$STAGE/Field Guide"
  cp "$ROOT/docs/guides/README.md" "$STAGE/Field Guide/README.md"
  cp "$ROOT/docs/guides/ten-years-in-a-closet.md" "$ROOT/docs/guides/recovering-a-damaged-archive.md" "$STAGE/Field Guide/"
  hdiutil create -volname "Cold Archive" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
  rm -rf "$STAGE"
}
DMG="release/Cold_Archive.dmg"

if [ -n "${SIGN_IDENTITY:-}" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
  # Order matters. MEASURED 2026-09-11: building the DMG first and stapling the
  # app afterwards left the copy INSIDE the DMG without a ticket ("does not
  # have a ticket stapled"), so an installed app could only pass Gatekeeper
  # online. So: notarize the app on its own, staple it, THEN build the DMG
  # from the stapled app, notarize and staple the DMG. Two submissions, each
  # made once with the stored profile (never retried in a loop).
  echo "== 7. notarize + staple the app"
  ZIP="$(mktemp -d "$ROOT/build/notarize.XXXXXX")/Cold_Archive.zip"
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
  rm -rf "$(dirname "$ZIP")"
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  echo "== 8. dmg from the stapled app, notarize + staple the dmg"
  make_dmg
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  spctl -a -vv -t exec "$APP"
  # the gate that would have caught the ordering bug: the app INSIDE the dmg
  MNT="$(hdiutil attach -nobrowse -readonly "$DMG" | awk -F'\t' '/\/Volumes\//{print $NF}')"
  xcrun stapler validate "$MNT/Cold Archive.app"
  hdiutil detach "$MNT" -quiet
else
  echo "== 7. dmg"
  make_dmg
  echo "(not notarized: set SIGN_IDENTITY and NOTARY_PROFILE to notarize; unnotarized apps are blocked by Gatekeeper on other Macs)"
fi
echo "Done: $APP and $DMG"
