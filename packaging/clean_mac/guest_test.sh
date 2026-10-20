#!/bin/bash
# Runs INSIDE a fresh macOS VM (started by packaging/clean_mac/run.sh): a Mac
# that has never seen Cold Archive, Homebrew or Python. Uses only what macOS
# ships with, and the app itself. With /tmp/Cold_Archive.dmg present (a build
# not yet published) it tests that; otherwise it downloads the latest release.
set -u
FAIL=0
ok() { echo "  ok    $*"; }; bad() { echo "  FAIL  $*"; FAIL=1; }
W="$HOME/cleanmac"; rm -rf "$W"; mkdir -p "$W"; cd "$W" || exit 1
APP="/Applications/Cold Archive.app"

echo "== the Mac"
echo "  macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion)), $(uname -m)"
echo "  Homebrew: $(ls -d /opt/homebrew /usr/local/Homebrew 2>/dev/null | tr '\n' ' ' || true)$( [ -d /opt/homebrew ] || [ -d /usr/local/Homebrew ] || echo none)"
echo "  Cold Archive installed before this test: $( [ -e "$APP" ] && echo YES || echo no)"

echo "== 1. download the published release and mark it as a browser would"
if [ -f /tmp/Cold_Archive.dmg ]; then cp /tmp/Cold_Archive.dmg Cold_Archive.dmg; echo "  (testing the build copied in, not the published release)"
else curl -sSL -o Cold_Archive.dmg https://github.com/wwward/cold-archive/releases/latest/download/Cold_Archive.dmg || bad "download"; fi
echo "  SHA-256 $(shasum -a 256 Cold_Archive.dmg | cut -c1-64)"
xattr -w com.apple.quarantine "0083;$(printf %x "$(date +%s)");Safari;$(uuidgen)" Cold_Archive.dmg
spctl -a -t open --context context:primary-signature -vv Cold_Archive.dmg 2>&1 | grep -q "source=Notarized Developer ID" && ok "Gatekeeper accepts the downloaded DMG" || bad "Gatekeeper on the DMG"

echo "== 2. install it: open the DMG, copy the app to Applications"
MNT=$(hdiutil attach -nobrowse -readonly Cold_Archive.dmg 2>/dev/null | awk -F'\t' '/\/Volumes\//{print $NF}')
ls "$MNT/Field Guide" 2>/dev/null | grep -q "ten-years-in-a-closet.md" && ok "the DMG carries the Field Guide" || bad "no Field Guide"
ditto "$MNT/Cold Archive.app" "$APP" && ok "copied to Applications" || bad "copy to Applications"
hdiutil detach "$MNT" -quiet
xattr -p com.apple.quarantine "$APP" >/dev/null 2>&1 && ok "the installed app is marked as downloaded" || bad "quarantine did not carry over"
spctl -a -t exec -vv "$APP" 2>&1 | grep -q "source=Notarized Developer ID" && ok "Gatekeeper accepts the app: first launch asks only 'downloaded from the internet, open?'" || bad "Gatekeeper on the app"
echo "  the app asks for macOS $(defaults read "$APP/Contents/Info.plist" LSMinimumSystemVersion) or later"

echo "== 3. the engine, end to end, on this Mac"
ln -sf "$APP/Contents/MacOS/Cold Archive" ./cold-archive-cli
./cold-archive-cli --version | sed 's/^/  /'
./cold-archive-cli doctor 2>&1 | grep -q "Cold Archive.app/Contents/Frameworks/bin/par2" && ok "uses its own bundled par2" || bad "bundled par2 not used"
mkdir -p "photos/2022/2022-04-27" "photos/Letters"
for i in 1 2 3 4 5 6; do head -c $((200000 + i * 1311)) /dev/urandom > "photos/2022/2022-04-27/IMG_$i.CR3"; done
for i in $(seq 1 200); do echo "Check the drive once a year."; done > "photos/Letters/note to self.txt"
printf 'Caf\xc3\xa9 au lait\n' > "photos/Letters/Caf$(printf '\xc3\xa9') menu.txt"
cp -Rp photos originals
./cold-archive-cli -q create photos drive/Archive >/dev/null 2>&1 && ok "Create: protected and copied" || bad "Create"
F="drive/Archive/2022/2022-04-27/IMG_3.CR3"
touch -r "$F" ref; dd if=/dev/zero of="$F" bs=1 seek=5000 count=3000 conv=notrunc 2>/dev/null; touch -r ref "$F"
rm "drive/Archive/Letters/note to self.txt"
./cold-archive-cli -q check drive/Archive >/dev/null 2>&1; [ $? = 2 ] && ok "Check finds the rot and the missing file" || bad "Check missed the damage"
./cold-archive-cli -q repair drive/Archive >/dev/null 2>&1 && ok "Repair" || bad "Repair"
cmp -s "$F" "originals/2022/2022-04-27/IMG_3.CR3" && cmp -s "drive/Archive/Letters/note to self.txt" "originals/Letters/note to self.txt" && ok "both files byte-identical to the originals" || bad "repaired files differ"
./cold-archive-cli -q check drive/Archive --mode thorough >/dev/null 2>&1 && ok "thorough check: everything exactly as it was" || bad "thorough check"
before="$(ls -A drive/Archive)$(stat -f %m drive/Archive)"; ./cold-archive-cli -q check drive/Archive >/dev/null 2>&1
[ "$before" = "$(ls -A drive/Archive)$(stat -f %m drive/Archive)" ] && ok "Check changed nothing in the archive" || bad "Check wrote into the archive"
./cold-archive-cli -q rescue drive/Archive rescued >/dev/null 2>&1; [ $? = 2 ] && ok "Rescue refuses a copy onto the same drive" || bad "rescue refusal"
(cd drive/Archive && PATH="$APP/Contents/Frameworks/bin:/usr/bin:/bin" bash Check-This-Archive.command </dev/null 2>&1 | grep -q "RESULT: this archive is healthy.") && ok "the archive's own check script passes with only the app's par2" || bad "check script"

echo "== 4. the window app on this Mac"
xattr -dr com.apple.quarantine "$APP"     # what clicking Open in the first-launch question records
open "$APP"; sleep 10
if pgrep -f "Cold Archive.app/Contents/MacOS/Cold Archive" >/dev/null; then ok "the app opened and is running"; else bad "the app did not stay open"; fi
# The window server's list says whether the app's window is on screen; unlike a
# screenshot taken over SSH, it needs no screen-recording permission.
WIN=$(osascript -l JavaScript -e 'ObjC.import("CoreGraphics"); var a = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0))); a.filter(function(w){return w.kCGWindowOwnerName==="Cold Archive" && w.kCGWindowLayer===0;}).map(function(w){return w.kCGWindowBounds.Width+"x"+w.kCGWindowBounds.Height;}).join(" ")' 2>/dev/null)
[ -n "$WIN" ] && ok "its window is on screen ($WIN)" || bad "no window on screen"
ls ~/Library/Logs/DiagnosticReports 2>/dev/null | grep -i "cold" && bad "crash report found" || ok "no crash report"
osascript -e 'tell application "Cold Archive" to quit' >/dev/null 2>&1; sleep 2; pkill -f "Cold Archive.app/Contents/MacOS/Cold Archive" 2>/dev/null

echo; [ "$FAIL" = 0 ] && echo "CLEAN-MAC TEST PASSED" || echo "CLEAN-MAC TEST FAILED"
exit $FAIL
