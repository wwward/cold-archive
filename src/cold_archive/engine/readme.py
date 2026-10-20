"""The recovery kit written into every archive: READ-ME-FIRST.txt, an HTML
twin, and a double-clickable check script.

This is the part of the tool that has to work with no tool: it is written for
a person who finds the drive years from now, knows nothing about PAR2, and
may not have a Mac. It names standard commands, where to get the software,
what repair can and cannot do, and how to keep the archive alive.
"""
from __future__ import annotations

import html
import os
from typing import List

from .. import __version__
from .layout import CHECK_COMMAND, MANIFEST_FILE, README_HTML, README_TXT, SUMS_FILE, TOOLS_DIR, human_bytes
from .manifest import Manifest, write_if_changed

DOWNLOAD_PAGE = "https://github.com/wwward/cold-archive"

MEDIA_ADVICE = {
    "hdd": "Hard disk: power it up and run the check at least once a year; copy the archive to a new "
           "drive after about five years, or sooner if the check ever reports damage. Store it in a padded, "
           "anti-static bag, away from magnets, heat and damp.",
    "ssd": "Solid-state drive / flash: unpowered flash slowly loses its charge; power it up and run the "
           "check every year, and do not rely on it as the only copy for more than a few years.",
    "bluray": "Blu-ray / M-DISC: keep the discs upright in their cases, cool, dark and dry; never write on the "
              "data side; run the check every two to three years and copy to new media at the first sign of "
              "trouble. Quality M-DISC media is rated for decades; ordinary dye-based discs are not.",
    "dvd": "DVD: keep the discs upright in their cases, cool, dark and dry; run the check every two years; "
           "dye-based DVDs can fade within ten years, so copy to fresh media in good time.",
    "tape": "LTO tape: store at stable temperature and humidity; read it fully every two to three years; "
            "tape needs a compatible drive, which becomes hard to find after two generations, so migrate early.",
    "cloud": "Cloud or network storage: the provider keeps the bits, but you keep the guarantee: download and "
             "run the check every year, and keep a copy under your own control.",
}


def _zip_version(name: str) -> tuple:
    """(0, 2, 10) from 'Cold-Archive-0.2.10-macOS.zip'; unparseable names sort first."""
    import re
    m = re.search(r"-(\d+(?:\.\d+)*)-macOS\.zip$", name)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _media_paragraph(media_note: str) -> str:
    key = (media_note or "").strip().lower()
    for k, v in MEDIA_ADVICE.items():
        if k in key:
            return v
    if key:
        return f"Medium noted by the owner: {media_note.strip()}. Keep it cool, dry and dark, run the check every year or two, and copy to fresh media in good time."
    return "Whatever this archive is stored on: keep it cool, dry and dark, run the check every year or two, and copy to fresh media in good time."


def readme_text(manifest: Manifest, media_note: str = "") -> str:
    created = manifest.created or "unknown"
    updated = manifest.updated or created
    r = manifest.redundancy_percent
    lines: List[str] = []
    a = lines.append
    a("READ ME FIRST: how to check and repair this archive")
    a("=" * 52)
    a("")
    a(f"Archive:        {manifest.label}")
    a(f"Created:        {created}")
    a(f"Last updated:   {updated}")
    a(f"Contents:       {len(manifest.files):,} files, {human_bytes(manifest.total_bytes)}")
    a(f"Repair data:    {r}% of every folder (more for the index files)")
    a(f"Made with:      Cold Archive {manifest.tool_version or __version__}")
    a("")
    a("WHAT THIS IS")
    a("-" * 12)
    a("This drive or disc holds ordinary files in ordinary folders. Nothing is")
    a("compressed, encrypted or packed. You can open, copy and use every file")
    a("with no special software at all.")
    a("")
    a("Alongside the files are a few extra files whose only job is to detect")
    a("and undo damage. Please keep them together with the files when you copy")
    a("the archive anywhere:")
    a("")
    a(f"  {SUMS_FILE:<28} a list of every file with its SHA-256 fingerprint")
    a(f"  {MANIFEST_FILE:<28} the same list with sizes and dates (JSON)")
    a("  PARITY.par2, PARITY.vol*.par2  repair data, in every folder, for that folder")
    a("  PARITY-2.par2, PARITY-3.par2   more repair data when a folder has thousands of files")
    a("  PARITY-INDEX*.par2             repair data, top folder only, for the files above")
    a(f"  {CHECK_COMMAND:<28} a script that runs the whole check (Mac / Linux)")
    a(f"  {README_TXT} / .html      this document")
    a("")
    a("The PARITY files of every folder are themselves listed in SHA256SUMS, so")
    a("the check also notices when repair data has been lost or damaged.")
    a("")
    a("IF YOU WERE GIVEN THIS ARCHIVE: use Check (and Repair if needed). Do not")
    a("run Protect on it unless you have changed files on purpose; Protect")
    a("re-records the files as they are now, and will ask before recording any")
    a("file whose contents differ from the records.")
    a("")
    a("HOW TO CHECK THE ARCHIVE")
    a("-" * 24)
    a("Do this first, before anything else, and again every year or two.")
    a("")
    a("On a Mac with the Cold Archive app: open the app, choose \"Check an archive\"")
    a("and pick this folder. That is all. The app is free at")
    a(f"  {DOWNLOAD_PAGE}")
    a("and a copy may also be in this archive's TOOLS folder.")
    a("")
    a(f"On a Mac or Linux without the app: double-click {CHECK_COMMAND}.")
    a("If it will not open (the drive may not carry permission bits): open the")
    a("Terminal app, type   bash   followed by a space, drag the file onto the")
    a("Terminal window, and press Return. It prints OK or FAILED for every file,")
    a("then checks the repair data.")
    a("")
    a("By hand, with standard tools, from inside the top folder of the archive:")
    a("  Mac:      shasum -a 256 -c SHA256SUMS")
    a("  Linux:    sha256sum -c SHA256SUMS")
    a("  Windows:  open PowerShell in this folder and paste this one line:")
    a("            Get-Content SHA256SUMS | ForEach-Object { $h,$f = $_ -split ' \\*',2; if ((Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash -ne $h.ToUpper()) { \"FAILED $f\" } else { \"OK $f\" } }")
    a("Every line should say OK. Any FAILED or missing file is damage; go to the")
    a("repair section below. A FAILED line for a PARITY file means the repair")
    a("data itself is damaged: your files are fine, but less damage can be")
    a("repaired from now on, so make a fresh copy from another copy if you have")
    a("one (or run Protect in the Cold Archive app, which rebuilds it).")
    a("Lines in SHA256SUMS that start with a backslash name files containing")
    a("unusual characters; the Mac and Linux tools handle them, PowerShell may not.")
    a("")
    a("HOW TO REPAIR DAMAGED OR MISSING FILES")
    a("-" * 38)
    a("1. If the drive or disc itself is failing, first copy the whole archive,")
    a("   including all PARITY files, to a healthy disk and work on the copy.")
    a("")
    a("2. Get a PAR2 program. PAR2 is an open, published file format (Parchive")
    a("   2.0, from 2003) with several independent implementations; any of them")
    a("   can read these files:")
    a("     Mac:      the Cold Archive app (Repair button), or install Homebrew")
    a("               from https://brew.sh and run:  brew install par2")
    a("     Linux:    sudo apt install par2      (Debian, Ubuntu)")
    a("               sudo dnf install par2cmdline (Fedora)")
    a("     Windows:  MultiPar  https://github.com/Yutaka-Sawada/MultiPar")
    a("               or par2cmdline releases (see below)")
    a("     Source:   https://github.com/Parchive/par2cmdline")
    a("               https://parchive.github.io  (the format specification)")
    tools_files = [k[len(TOOLS_DIR) + 1:] for k in manifest.files if k.startswith(TOOLS_DIR + "/")]
    source_tar = next((n for n in tools_files if n.startswith("par2cmdline-") and ".tar" in n), "")
    # The newest copy of the app, by version: a refresh adds a new zip beside an
    # older one (TOOLS files are never removed), and name order would pick
    # the older (found 2026-10-19 while planning a drive's refresh).
    app_zips = [n for n in tools_files if n.endswith("-macOS.zip")]
    app_zip = max(app_zips, key=_zip_version, default="")
    if source_tar:
        a(f"   If none of those exist any more: the {TOOLS_DIR} folder of this archive holds the")
        a(f"   par2cmdline source code ({source_tar}) with build")
        a("   instructions (HOW-TO-BUILD-par2.txt). It compiles on any Unix-like system.")
    if app_zip:
        a(f"   The {TOOLS_DIR} folder also holds a copy of the Cold Archive app for the Mac")
        a(f"   ({app_zip}). Unzip it and drag the app to Applications. If macOS")
        a("   refuses to open it: open System Settings > Privacy & Security, scroll")
        a("   down to the message about the app and click \"Open Anyway\" (on macOS 14")
        a("   and older, right-click the app and choose Open instead). The app is only")
        a("   a convenience: everything it does can be done with the tools above.")
    a("")
    a("3. In each folder that has damaged or missing files, run:")
    a("     par2 repair PARITY.par2")
    a("   If the folder also has PARITY-2.par2, PARITY-3.par2 ..., run the same")
    a("   command for each of them. For the top folder's own records, run:")
    a("     par2 repair PARITY-INDEX.par2")
    a("   If the small PARITY.par2 file itself is missing, name any of the")
    a("   PARITY.vol...par2 files instead; each of them carries what par2 needs.")
    a("   With MultiPar, double-click the PARITY.par2 file and press Repair.")
    a("")
    a("4. The program rebuilds the damaged or missing files in place. It keeps")
    a("   each damaged original next to the repaired file with \".1\" added to the")
    a("   name; delete those once the check passes. (The Cold Archive app's Repair")
    a("   does all of this, restores file dates, and keeps a file's current")
    a("   version if it looks like it was changed on purpose.)")
    a("")
    a("5. Run the check again. It should now report every file OK.")
    a("")
    a("WHAT REPAIR CAN AND CANNOT DO")
    a("-" * 29)
    a(f"Each folder's PARITY files can rebuild up to about {r}% of that folder's")
    a("data, in any pattern: scattered bad sectors, a few whole files, or a file")
    a("that is missing altogether. They cannot rebuild more than that, and they")
    a("cannot bring back a folder that is entirely gone. That is why more than")
    a("one copy of this archive should exist.")
    a("")
    a("The PARITY files protect each other: if some of them are damaged, repair")
    a("still works with the rest. If every PARITY file of a folder is lost, the")
    a("files in it can still be checked with SHA256SUMS, but not repaired.")
    a("Files of zero bytes have no repair data (there is nothing to rebuild);")
    a("they are still listed in SHA256SUMS.")
    a("")
    a("KEEPING THIS ARCHIVE ALIVE")
    a("-" * 26)
    a(_media_paragraph(media_note))
    a("")
    a("Keep at least two copies in different places. A copy is only known to")
    a("be good while it passes the check. When you copy the archive to new media,")
    a("copy everything (including the PARITY and SHA256SUMS files) and run the")
    a("check on the copy before trusting it. Repair anything that fails, then")
    a("move on to fresh media well before the old one reaches the end of its life.")
    a("")
    a("FOR THE TECHNICALLY MINDED")
    a("-" * 26)
    a("SHA256SUMS: one line per file: 64 hexadecimal characters, a space, an")
    a("asterisk, then the path relative to the top folder using forward slashes,")
    a("spelled exactly as the file is named on the disk (no Unicode normalisation;")
    a("names with a newline or backslash use the GNU coreutils escape form). Any")
    a("sha256sum-compatible tool reads it. It lists the files, the records, and")
    a("every folder's PARITY files; it cannot list PARITY-INDEX, which is built")
    a("from it. The manifest is JSON: \"files\" maps each path to its size in bytes,")
    a("mtime in seconds since 1970 (UTC), sha256 and md5 (PAR2 identifies files by")
    a("MD5); \"parity_files\" does the same for the PARITY files. PAR2 sets were")
    a("created with par2cmdline, one set per folder (split when a folder has more")
    a("than a few thousand files), with a block size chosen per folder and uniform")
    a("recovery volumes. Names containing a backslash or a control character")
    a("cannot be stored by par2cmdline; such files are listed in SHA256SUMS only.")
    if tools_files:
        a(f"The {TOOLS_DIR} folder is ordinary payload: its files are listed in SHA256SUMS and")
        a("covered by their own PARITY set, so the tools are protected by the same")
        a("mechanism they serve. Deleting it loses nothing else.")
    a("")
    return "\n".join(lines)


def readme_html(text: str) -> str:
    """Render the plain text as simple, dependency-free HTML."""
    out = ["<!DOCTYPE html>", "<html lang=\"en\"><head><meta charset=\"utf-8\">",
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
           "<title>Read me first: how to check and repair this archive</title>",
           "<style>body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:46em;margin:2em auto;padding:0 1em;line-height:1.5;color:#222;background:#fff}"
           "h1{font-size:1.6em}h2{font-size:1.2em;margin-top:1.6em;border-bottom:1px solid #ccc}"
           "pre{background:#f4f4f4;padding:.6em .8em;overflow-x:auto;font-size:.95em}code{background:#f4f4f4;padding:0 .2em}"
           "p{margin:.6em 0}</style></head><body>"]
    lines = text.split("\n")
    i = 0
    para: List[str] = []
    pre: List[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + html.escape(" ".join(para)) + "</p>")
            para.clear()

    def flush_pre() -> None:
        if pre:
            out.append("<pre>" + html.escape("\n".join(pre)) + "</pre>")
            pre.clear()

    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt and set(nxt.strip()) == {"="}:
            flush_para(); flush_pre()
            out.append("<h1>" + html.escape(line) + "</h1>")
            i += 2
            continue
        if nxt and set(nxt.strip()) == {"-"}:
            flush_para(); flush_pre()
            out.append("<h2>" + html.escape(line) + "</h2>")
            i += 2
            continue
        if line.startswith("  ") or line.startswith("\t"):
            flush_para()
            pre.append(line)
        elif not line.strip():
            flush_para(); flush_pre()
        else:
            flush_pre()
            para.append(line.strip())
        i += 1
    flush_para(); flush_pre()
    out.append("</body></html>\n")
    return "\n".join(out)


CHECK_SCRIPT = r'''#!/bin/bash
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
'''


def write_recovery_kit(root: str, manifest: Manifest, media_note: str = "") -> bool:
    """Write the three kit files; return True if any of them changed."""
    text = readme_text(manifest, media_note)
    changed = False
    changed |= write_if_changed(os.path.join(root, README_TXT), text.encode("utf-8"))
    changed |= write_if_changed(os.path.join(root, README_HTML), readme_html(text).encode("utf-8"))
    changed |= write_if_changed(os.path.join(root, CHECK_COMMAND), CHECK_SCRIPT.encode("utf-8"), mode=0o755)
    return changed
