#!/bin/bash
# Release check on a clean Mac: a fresh macOS VM that has never seen Cold
# Archive, Homebrew or Python downloads the app (or takes a build you pass),
# installs it the way a person would, and runs guest_test.sh: Gatekeeper on
# the quarantined DMG and app, Create, Check, Repair, Rescue's refusal, the
# archive's own check script, and the window app on screen.
#
#   packaging/clean_mac/run.sh                          # the published release
#   packaging/clean_mac/run.sh release/Cold_Archive.dmg # a build before publishing
#
# Needs tart (https://tart.run; Apple's own virtualization). The default image,
# ghcr.io/cirruslabs/macos-sequoia-vanilla, is macOS 15, the oldest macOS the
# app supports: about 24 GB to download once, 30 GB on disk; set TART_HOME to
# keep it on a roomy drive. It logs in automatically as admin/admin (Cirrus
# Labs' documented default); a throwaway SSH key is used for the session, and
# the VM is deleted afterwards.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TART="${TART:-tart}"
IMAGE="${IMAGE:-ghcr.io/cirruslabs/macos-sequoia-vanilla:latest}"
DMG="${1:-}"
VM="cold-archive-cleanmac-$$"
[ -z "$DMG" ] || [ -f "$DMG" ] || { echo "no such file: $DMG"; exit 1; }
# The first run downloads the image; refuse rather than fill a nearly full disk.
if ! "$TART" list --source oci --quiet 2>/dev/null | grep -qxF "$IMAGE"; then
  TH="${TART_HOME:-$HOME/.tart}"; mkdir -p "$TH"
  FREE_GB=$(( $(df -k "$TH" | awk 'NR==2{print $4}') / 1048576 ))
  if [ "$FREE_GB" -lt 80 ]; then
    echo "$IMAGE is not downloaded yet, and $TH has only $FREE_GB GB free."
    echo "It needs about 24 GB to download and 30 GB unpacked. Point TART_HOME at a roomier drive."
    exit 1
  fi
fi
W=$(mktemp -d)
cleanup() { "$TART" stop "$VM" >/dev/null 2>&1; "$TART" delete "$VM" >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
"$TART" clone "$IMAGE" "$VM" || { echo "could not create the VM from $IMAGE"; exit 1; }
"$TART" run "$VM" --no-graphics >"$W/run.log" 2>&1 &
IP=$("$TART" ip "$VM" --wait 300) || { echo "the VM did not come up"; exit 1; }
SSHO=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
for _ in $(seq 1 60); do nc -z "$IP" 22 2>/dev/null && break; sleep 5; done
ssh-keygen -q -t ed25519 -N "" -f "$W/key"
/usr/bin/expect <<EXP >/dev/null
set timeout 90
spawn sh -c "ssh ${SSHO[*]} admin@$IP 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys' < $W/key.pub"
expect {
  "assword:" { send "admin\r"; exp_continue }
  eof
}
EXP
ssh -i "$W/key" "${SSHO[@]}" -o BatchMode=yes admin@"$IP" true || { echo "could not log in to the VM"; exit 1; }
scp -i "$W/key" "${SSHO[@]}" -q "$HERE/guest_test.sh" admin@"$IP":/tmp/guest_test.sh
[ -n "$DMG" ] && scp -i "$W/key" "${SSHO[@]}" -q "$DMG" admin@"$IP":/tmp/Cold_Archive.dmg
ssh -i "$W/key" "${SSHO[@]}" admin@"$IP" 'bash /tmp/guest_test.sh'
