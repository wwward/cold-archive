# Cold Archive

Keep a folder of photos, or any files, safe for ten years or more, on a drive
you can hand to someone who knows nothing about computers.

**[Download Cold Archive for Mac](https://github.com/wwward/cold-archive/releases/latest/download/Cold_Archive.dmg)**:
free and open source (Apache-2.0), for Macs with Apple silicon and macOS 15 or
later. It makes no network connections and collects nothing.

Open the downloaded file and drag **Cold Archive** onto **Applications**. The
first time you open it, click **Open** when your Mac asks about an app from the
internet.

## What it does

Everything is a click in the app:

- **Create a new archive.** Every file gets a fingerprint, every folder gets
  repair data, and the whole folder is copied to a drive and read back.
- **Check an archive.** Finds out whether every file is still exactly as it was.
- **Repair an archive.** Rebuilds damaged or missing files from the repair data.
- **Rescue a failing drive.** Copies everything readable from a struggling
  drive onto a healthy one without changing the old drive, then repairs the
  copy.
- **Protect a folder in place.** Adds fingerprints and repair data where the
  files already are.

Archives on connected drives appear as buttons, and the Mac stays awake while
a job runs.

An archive is still just files in folders. Next to them the tool writes:

| File | What it is |
|---|---|
| `SHA256SUMS` | every file with its fingerprint, in the standard `sha256sum -c` / `shasum -a 256 -c` format; the `PARITY` files are listed too, so lost repair data shows up in any checksum tool |
| `cold-archive-manifest.json` | the same list with sizes and dates, plus the settings used |
| `PARITY*.par2` (in every folder) | PAR2 repair data for that folder, created with par2cmdline |
| `PARITY-INDEX*.par2` (top folder) | repair data for the records above, at 50 % redundancy |
| `READ-ME-FIRST.txt` / `.html` | recovery instructions for whoever opens the archive later |
| `Check-This-Archive.command` | a double-clickable script that runs the whole check with standard tools |
| `TOOLS/` | the par2cmdline source the bundled par2 was built from (GPL), build notes, and a copy of the Cold Archive app; protected like any other folder |

Nothing is compressed, encrypted or packed, so the files stay usable with no
software at all, and repairable with any PAR2 program on any computer.

## For whoever keeps the drive

The Field Guide, in `docs/guides/` and in every download, is written for
people who are not technical and meant to be printed and kept with the drive:

- [Ten years in a closet](docs/guides/ten-years-in-a-closet.md): what a
  long-stored drive can do when you plug it in, and what to do about each.
- [Recovering a damaged archive](docs/guides/recovering-a-damaged-archive.md):
  check, rescue, repair and make a fresh copy, with clicks; plus a section for
  a technical helper working without the app.

## Every version repairs every older archive

Drives handed out today must be repairable by whatever version someone
downloads years from now. `tests/compat/archives/` holds archives made by
real past releases, and every build must check them, repair a damaged and a
missing file byte-identical, and pass their own check scripts, with its own
bundled par2 engines, before it is signed (`packaging/compat_gate.py`). See
`docs/DESIGN.md`, "Compatibility promise".

## Licence

Cold Archive is under the Apache License 2.0 (`LICENSE`, `NOTICE`): use it,
change it, fork it and redistribute it freely, keeping the licence and
notices with it. The app bundles par2cmdline (GPL-2.0-or-later, run as a
separate program, with its source included), Qt and PySide6 (LGPL-3.0,
dynamically linked) and Python (PSF); none of them changes the licence of
Cold Archive's own code. `THIRD-PARTY-NOTICES.md` lists every bundled part,
its licence and where its source is; the texts are in `licenses/`.

## Building the app

```
uv venv --python 3.12 && uv pip install -e '.[dev]'
brew install par2                      # the threaded engine bundled for new macOS
packaging/build_app.sh                 # unsigned build -> dist/Cold Archive.app, release/Cold_Archive.dmg
SIGN_IDENTITY="Developer ID Application: <name> (<team>)" NOTARY_PROFILE=<profile> packaging/build_app.sh
```

Use a plain CPython (python.org, or `uv python install`), not a Conda
interpreter: the app bundles the interpreter and its OpenSSL. The build
refuses to finish when the app would need a newer macOS than intended, when
a Qt module outside the LGPL allow-list appears, when a licence file is
missing, or when the built app cannot repair the archives in
`tests/compat/archives/`.

An app that is not signed *and notarized* is blocked by Gatekeeper on other
Macs. The last line above signs, notarizes and staples in one go: the app
first, then the DMG built from the stapled app (two submissions), so the app
inside the DMG carries its own ticket and opens offline. Make the notary
profile once with
`xcrun notarytool store-credentials <profile> --apple-id <Apple ID> --team-id <team>`
(it prompts for an app-specific password). If it stops validating, do not
retry in a loop: repeated failures can lock the Apple ID. A 403 saying an
agreement is missing or expired means the developer agreement needs
accepting at developer.apple.com, not a new password.

To use the command line from the installed app:

```
sudo mkdir -p /usr/local/bin
sudo ln -sf "/Applications/Cold Archive.app/Contents/MacOS/Cold Archive" /usr/local/bin/cold-archive-cli
```

or install it on its own with `pipx install .` from this repository.

## The command line

```
cold-archive-cli create  SOURCE TARGET [--redundancy 10] [--label NAME] [--media hdd|ssd|bluray|dvd|tape|cloud]
cold-archive-cli protect ARCHIVE       [--redundancy 10] [--accept-removals] [--accept-changes] [--full-rehash] [--rebuild-parity]
                                       [--no-tools] [--include-app]     (both also accepted by create)
cold-archive-cli verify  ARCHIVE       [--mode standard|thorough|hashes|parity]
cold-archive-cli repair  ARCHIVE
cold-archive-cli rescue  ARCHIVE_ON_FAILING_DRIVE EMPTY_FOLDER_ON_HEALTHY_DRIVE
cold-archive-cli preflight SOURCE [TARGET]
cold-archive-cli plan    ARCHIVE --media BD_25
cold-archive-cli doctor
```

Exit codes: 0 all good, 1 intact but needs attention, 2 problems, 3 could not
run, 4 nothing changed because your decision is needed (removed or changed
files: run again with `--accept-removals` / `--accept-changes`), 130
cancelled. Logs and reports go to `~/Library/Logs/Cold Archive/`, never
inside the archive.

## What "10 % repair data" means

Each folder gets its own PAR2 set sized at 10 % of that folder's data (the
smallest folders get a floor of ten recovery blocks so a small file can be
lost outright and still be rebuilt). Any damage pattern totalling up to that
amount within a folder is fully repairable: scattered bad sectors, a few
damaged files, a deleted file. Damage beyond that in one folder is not, and
neither is the loss of a whole drive; keep a second copy and check both.

Block sizes are chosen per folder so that parity really is about 10 % even
for folders of thousands of small photos (par2's default would silently make
it 60 to 100 %), and folders with more than 4,000 files are covered by
several sets (`PARITY.par2`, `PARITY-2.par2`, ...).

## Safety rules the engine enforces

* Protect stops, changing nothing, when recorded files are missing or when a
  file's contents differ from the records, until you say that was deliberate
  (the app asks, listing the files; the CLI takes `--accept-removals` and
  `--accept-changes`). A person knows whether they edited a file; the tool
  cannot tell an edit from corruption that arrived with a new date (a copy
  through exFAT, rsync or a cloud sync).
* A file whose bytes changed while its size and date did not is treated as
  silent corruption: the records keep the original fingerprint, the folder's
  parity is left alone, and you are told to run Repair.
* Protect never starts over from whatever is on disk. If the manifest is
  unreadable it falls back to `SHA256SUMS` (every difference then counts as
  suspected corruption) or refuses and points at Repair; if the two records
  disagree it refuses; a file it cannot read keeps its previous record; a
  file the records forgot is judged against the folder's existing parity.
* Before any parity set is replaced it is verified; anything it reports as
  damaged that does not match the records blocks the replacement. New parity
  is built in a hidden folder and swapped in only after par2 has verified it
  and it covers exactly the intended files, so a failed build never leaves a
  folder without parity.
* File names that PAR2 cannot store (a backslash, a tab or newline) are
  recorded in `SHA256SUMS` but reported as unprotectable, never dropped
  silently. Names are recorded exactly as spelled on disk so that Linux and
  Windows find them.
* Check reads every parity packet in every mode, notices missing or
  shortened recovery volumes, and reports a set that no longer matches the
  records as out of date. It never says "all good" for parity it did not read.
* Copies are read back and compared before they count as copied; the
  read-back goes past the page cache, so the bytes compared came from the
  medium. Source files carrying names reserved for the archive's own files
  are refused rather than silently left out.
* A renamed or moved file is recognised by its size and date: same contents
  are simply recorded under the new name; different contents are treated as
  corruption, the old record kept and the folder left alone until Repair
  has rebuilt the file. "Rebuild parity" re-reads every file and still
  verifies the old parity before replacing it. A set par2 built around bytes
  that differ from the records is refused, and no failure during the swap
  leaves a folder without parity.
* Repair treats self-consistent records as the truth (an interrupted Protect
  never rolls them back), proves every rebuilt file against `SHA256SUMS`,
  restores its recorded date, keeps a legitimately edited file rather than
  reverting it, keeps a file it cannot judge as `name.1` next to the restored
  one rather than deleting the only copy, removes only the backups it made,
  and rebuilds damaged or out-of-date parity afterwards.

See `docs/DESIGN.md` for the format and the reasoning, and `tests/` for the
scenarios that are exercised against the real par2 binary on real files.

## Development

```
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/cold-archive          # run the app from source
```
