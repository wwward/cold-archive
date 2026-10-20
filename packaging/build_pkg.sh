#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

APP="$ROOT/dist/Cold Archive.app"
if [ ! -d "$APP" ]; then
    echo "App not found at $APP. Please run packaging/build_app.sh first."
    exit 1
fi

PKG_DIR="$ROOT/build/pkg"
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR/scripts"

# For managed installs. The public download is the notarized DMG
# (packaging/build_app.sh): it needs no administrator password and leaves the
# app owned by the user. This package is signed but not notarized.
# Create postinstall script to link the CLI tool. /usr/local/bin does not
# exist on a fresh Apple Silicon Mac, and a failing postinstall fails the install.
cat > "$PKG_DIR/scripts/postinstall" << 'EOF'
#!/bin/bash
mkdir -p /usr/local/bin
ln -sf "/Applications/Cold Archive.app/Contents/MacOS/Cold Archive" "/usr/local/bin/cold-archive-cli"
exit 0
EOF
chmod +x "$PKG_DIR/scripts/postinstall"

# Build the PKG
mkdir -p "$ROOT/release"
PKG_OUT="$ROOT/release/Cold_Archive.pkg"

echo "Building Installer Package..."
if [ -n "${SIGN_IDENTITY:-}" ]; then
  pkgbuild --component "$APP" \
           --install-location "/Applications" \
           --scripts "$PKG_DIR/scripts" \
           --sign "$SIGN_IDENTITY" \
           "$PKG_OUT"
else
  pkgbuild --component "$APP" \
           --install-location "/Applications" \
           --scripts "$PKG_DIR/scripts" \
           "$PKG_OUT"
  echo "(Not signed: set SIGN_IDENTITY to sign the package for distribution)"
fi

echo "Done: $PKG_OUT"
