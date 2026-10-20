#!/bin/bash
# Cold Archive: check this archive. Works on macOS and Linux.
# Double-click it, or run:  bash Check-This-Archive.command
cd "$(dirname "$0")" || exit 1
echo "Checking the archive in: $PWD"
echo
fail=0
# Choose a checksum tool. macOS ships a BSD sha256sum in /sbin that silently
# SKIPS lines it cannot parse (names with special characters) and still exits
# 0, so prefer shasum there. Whatever tool runs, every line of SHA256SUMS must
# come back OK: a line that was skipped counts as a failure.
SUMCMD=""
if [ "$(uname 2>/dev/null)" = "Darwin" ] && command -v shasum >/dev/null 2>&1; then SUMCMD="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1 && sha256sum --version 2>/dev/null | grep -qi 'coreutils\|busybox'; then SUMCMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then SUMCMD="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1; then SUMCMD="sha256sum"
fi
if [ -z "$SUMCMD" ]; then
  echo "No sha256sum or shasum program found; cannot check fingerprints."; fail=1
elif [ ! -f SHA256SUMS ]; then
  echo "SHA256SUMS is missing. If PARITY-INDEX.par2 exists, 'par2 repair PARITY-INDEX.par2' can rebuild it."; fail=1
else
  echo "== 1. Checking every file against SHA256SUMS (with $SUMCMD) =="
  out=$($SUMCMD -c SHA256SUMS 2>&1); rc=$?
  listed=$(grep -c '[^[:space:]]' SHA256SUMS)
  okcount=$(printf '%s\n' "$out" | grep -c ': OK$')
  # lines that report trouble (a name containing a newline is printed over two lines; only the verdict matters)
  trouble=$(printf '%s\n' "$out" | grep -E 'FAILED|No such file|WARNING|improperly|cannot|not found|Permission denied|did not match|not a regular')
  bad_data=$(printf '%s\n' "$trouble" | grep -v '^$' | grep -v 'PARITY[^/]*\.par2: ')
  bad_par=$(printf '%s\n' "$trouble" | grep 'PARITY[^/]*\.par2: ')
  if [ -n "$bad_data" ]; then
    printf '%s\n' "$bad_data"
    echo; echo "SOME FILES FAILED OR ARE MISSING (listed above). See READ-ME-FIRST.txt, section HOW TO REPAIR."; fail=1
  fi
  if [ -n "$bad_par" ]; then
    printf '%s\n' "$bad_par"
    echo; echo "The lines above are PARITY (repair data) files that are damaged or missing. Your files are not affected,"
    echo "but less damage can be repaired from now on. Make a fresh copy from another copy of this archive if you have one,"
    echo "or open it in the Cold Archive app and run Protect, which rebuilds the repair data."; fail=1
  fi
  if [ "$okcount" -lt "$listed" ]; then
    echo; echo "Only $okcount of the $listed lines in SHA256SUMS were checked and found OK; the rest failed, are missing, or could not be read."; fail=1
  elif [ "$rc" -ne 0 ] && [ -z "$bad_data$bad_par" ]; then
    echo; echo "The checksum tool reported a problem (exit code $rc):"; printf '%s\n' "$out" | grep -v ': OK$'; fail=1
  fi
  if [ "$fail" -eq 0 ]; then echo "All $listed listed files are intact."; fi
fi
# PARITY-INDEX is not listed in SHA256SUMS (it is the repair data FOR that list),
# so its loss has to be looked for by name.
if [ -f SHA256SUMS ] && ! ls PARITY-INDEX*.par2 >/dev/null 2>&1; then
  echo; echo "The repair data for the records themselves (PARITY-INDEX*.par2) is missing: if SHA256SUMS were ever damaged it could not be rebuilt. Copy those files back from another copy of this archive, or run Protect in the Cold Archive app."; fail=1
fi
echo
PAR2=""
unchecked=0
for c in par2 /opt/homebrew/bin/par2 /usr/local/bin/par2 "/Applications/Cold Archive.app/Contents/Frameworks/bin/par2" "/Applications/Cold Archive.app/Contents/Resources/bin/par2"; do
  if [ -x "$c" ]; then PAR2="$c"; break; fi
  if command -v "$c" >/dev/null 2>&1; then PAR2="$(command -v "$c")"; break; fi
done
if [ -z "$PAR2" ]; then
  echo "== 2. Repair data (PAR2) NOT checked: no par2 program installed. READ-ME-FIRST.txt says where to get one. =="; unchecked=1
else
  echo "== 2. Checking repair data with $PAR2 =="
  bad=0; n=0; seen=""
  # one par2 verify per set; use the index file, or any volume when the index is lost
  while IFS= read -r -d '' f; do
    d=$(dirname "$f"); b=$(basename "$f")
    set=${b%%.vol*}; set=${set%.par2}
    key="$d/$set"
    if printf '%s\n' "$seen" | grep -Fxq -- "$key"; then continue; fi
    seen="$seen
$key"
    n=$((n+1))
    entry="$set.par2"; [ -f "$d/$entry" ] || entry="$b"
    if ( cd "$d" && "$PAR2" verify -q -q -- "$entry" >/dev/null 2>&1 ); then :; else
      echo "NEEDS REPAIR: $d/$set   (run:  cd \"$d\" && par2 repair \"$entry\")"; bad=$((bad+1)); fi
  done < <(find . -name 'PARITY*.par2' -print0 | sort -z)
  if [ "$n" -eq 0 ]; then echo "No parity sets found."; fail=1
  elif [ "$bad" -eq 0 ]; then echo "All $n parity sets are intact."; else echo "$bad of $n parity sets report damage."; fail=1; fi
fi
echo
if [ "$fail" -ne 0 ]; then echo "RESULT: this archive needs attention (see above)."
elif [ "$unchecked" -ne 0 ]; then echo "RESULT: every file matches its fingerprint, but the repair data was NOT checked (no par2 program). Install par2 and run this again."; fail=2
else echo "RESULT: this archive is healthy."; fi
if [ -t 0 ]; then echo; read -r -p "Press Return to close this window. "; fi
exit $fail
