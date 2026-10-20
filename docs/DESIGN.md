# Cold Archive: format and design notes

This is the reasoning behind the layout, for whoever maintains the tool. The
layout itself is documented for end users in the `READ-ME-FIRST.txt` that is
written into every archive.

## Goals

1. A trustee with no software from us, on any operating system, in 2036, can
   verify every file and repair damage using tools that already exist.
2. Silent corruption (bytes change, size and date do not) is detected, never
   laundered into the records.
3. The tool must never make an archive *less* recoverable: no operation
   replaces parity that might still be needed.
4. Everything the tool claims to have done is verified by reading it back.

## Layout (format version 1)

```
<archive root>/
  READ-ME-FIRST.txt, READ-ME-FIRST.html   instructions
  Check-This-Archive.command              bash script: shasum/sha256sum -c, then par2 verify per set
  SHA256SUMS                              "<sha256> *<relpath>\n", sorted, forward slashes, names spelled
                                          exactly as on disk; lists payload, the records, and every
                                          folder's PARITY files (not PARITY-INDEX, which is built from it)
  cold-archive-manifest.json              {format, version, created, updated, label, media,
                                           redundancy_percent, files: {rel: {size, mtime, sha256, md5}},
                                           parity_files: {rel: {size, mtime, sha256}}}
  PARITY-INDEX.par2 + .volN+M.par2        covers SHA256SUMS, manifest, READMEs, command (>=50 %)
  TOOLS/                                  par2cmdline-<ver>.tar.bz2, LICENSE-par2cmdline.txt,
                                          HOW-TO-BUILD-par2.txt; optionally Cold-Archive-<ver>-macOS.zip.
                                          Ordinary payload (listed, given its own PARITY set).
  <any folder>/
     PARITY.par2 + PARITY.volN+M.par2      covers that folder's non-empty payload files
     PARITY-2.par2 ...                     further sets when the folder has > 4,000 files
```

Things that are deliberately *not* archived: `.DS_Store` and the other
OS litter (see `layout.JUNK_NAMES`), AppleDouble `._*` sidecars, symbolic
links (reported, never followed), and our own files. Foreign `.par2` files
are treated as ordinary payload.

### Compatibility promise: every build repairs every earlier archive

Archives handed out on drives are meant to outlive many releases, so any
build must check and repair archives made by any earlier one. This is
enforced, not hoped for:

- `tests/compat/archives/` holds archives made by real past releases (0.2.1,
  the release the first hand-off drives were made with, and 0.2.2), kept
  byte-for-byte. `tests/test_compat.py` checks them, repairs a rotted and a
  missing file, rebuilds damaged records from PARITY-INDEX, runs the
  archive's own check script with today's par2, and protects them again.
- `packaging/compat_gate.py` runs the same cycle with the *built* app's
  command line and each bundled par2 engine; `build_app.sh` refuses to sign
  a release that fails it.
- The path `Contents/Frameworks/bin/par2` is part of the contract: every
  archive's `Check-This-Archive.command` looks for the installed app's par2
  there. The gate refuses a bundle without it.
- A change to the layout means a new format version. `load_manifest` raises
  `ManifestTooNew` for a version it does not know, so an old build refuses a
  newer archive instead of misreading it; a new build keeps reading every
  older version. Add a fixture from each release with that release's own
  app (`tests/compat/make_fixture.py`), and never regenerate an old one.

Beyond our own builds, any PAR2 program and any `sha256sum` reads every
archive (goal 1), which is the guarantee that does not depend on us.

### Why names are recorded as spelled on disk (no NFC)

macOS looks file names up normalisation-insensitively, so on a Mac an NFC
path in `SHA256SUMS` finds an NFD-spelled file. Linux and Windows compare
bytes, and macOS writes decomposed names onto exFAT and FAT: a trustee on
those systems with an NFC list could not find any accented file. Recording
the on-disk spelling works everywhere. NFC is used only to compare names
with what par2cmdline prints and stores (`layout.nfc`).

### Why the parity files are listed in SHA256SUMS

A deleted or corrupt recovery volume used to be invisible: `par2 verify`
exits 0 whenever the data files are intact, whatever the state of the
recovery packets. Listing every folder's `PARITY*` files makes their loss
show up in `sha256sum -c` for a trustee and in Check for us (Check
additionally validates every packet's MD5 and compares recovery-block counts
with the volume names and the plan). `PARITY-INDEX` cannot be listed, since
it is built from `SHA256SUMS`; Check counts its blocks instead.

### Names PAR2 cannot store

Measured on par2cmdline 1.3.0: a source name containing a backslash is
silently dropped from the set (exit 0), and a name containing a control
character is stored raw but looked up as `%XX`, so the set is broken from
birth. `layout.name_problem` refuses both: such files are hashed into
`SHA256SUMS`, reported as unprotectable, and Protect is not "ok" until they
are renamed. After every create the new index's file table is compared with
the requested names, so any other silent omission fails the build.

### Why the archive carries the par2 source (and optionally the app)

Decided 2026-09-04: the links in READ-ME-FIRST may be dead in ten years, so
every archive carries the unmodified par2cmdline source release the bundled
`par2` was built from (GPL requires the corresponding source to travel with
the binary anyway) plus a one-page build note; it compiles with a C++
compiler on any Unix-like system. A zip of the app itself is optional (about
50 MB, Mac only, will be refused by a future Gatekeeper without a right-click
Open) and is offered as a checkbox. `TOOLS/` is deliberately ordinary payload
rather than a tool file: it is written *before* the scan, so the same Protect
run hashes it, lists it in `SHA256SUMS` and gives it a `PARITY` set. A missing
tarball is put back silently (no removal to consent to); one of the right size
is never overwritten, so a rotted copy is caught by the records like any other
file. When the source cannot be found next to the program (a source checkout
without a build), Protect warns and carries on.

### Why SHA256SUMS and not only JSON

`sha256sum -c`, `shasum -a 256 -c`, BusyBox and PowerShell one-liners all
consume that text format today, and it is simple enough to re-implement in a
few lines. The JSON manifest carries what the text format cannot (sizes,
dates, MD5s, settings) and makes incremental re-protection cheap; verify and
repair work without it.

### Why PAR2

Parchive 2.0 is a published specification (2003) with several independent,
maintained implementations (par2cmdline, par2cmdline-turbo, MultiPar,
QuickPar, libpar2, and re-implementations in other languages). Every packet
is MD5-checksummed and critical packets are duplicated across volume files,
so parity survives damage to itself. It repairs whole-file loss as well as
scattered sectors. Alternatives considered: ZFS/btrfs (needs the filesystem
to survive, not a file-level format), dvdisaster (optical only, ISO images),
zfec/Tahoe (no maintained end-user tooling), custom Reed-Solomon (nobody
else could read it in ten years).

### Why per-folder sets

* Recovery only needs the folder present, not the whole archive: a single
  folder can be copied off a dying disc and repaired on its own.
* PAR2 has a 32,768 source-block limit per set; whole-archive sets would need
  enormous blocks.
* Incremental re-protection touches only the folders that changed.

The cost is that a folder's files must stay together (the spanning planner
keeps folders whole for that reason).

### Block size policy (`par2.plan_blocks`)

PAR2 pads every file to whole blocks and every recovery block is a full
block. With par2cmdline's default 2,000-block target, a 100 GB folder of
20,000 five-megabyte photos gets 50 MB blocks, 20,000 source blocks, 2,000
recovery blocks, and therefore *100 GB of parity for "10 %"*. The planner
walks a ladder of block sizes, keeps those whose parity is within 25 % of
nominal, and among them picks the block count closest to 2,000 (creation
time is linear in recovery blocks: measured 11 s, 19 s, 26 s per GB at 200,
400, 800). Folders with more than 4,000 non-empty files are split into
several sets so block size can stay near file size. Tiny sets get at least
ten recovery blocks (capped at the block count) so a small folder can lose a
whole small file and still be rebuilt.

Recovery volumes are uniform (`-u`, up to eight files) so that losing any
one volume file costs a bounded fraction of the recovery data, rather than
par2's default doubling sizes where the last file holds half of it.

### Staleness (`protect._set_state`)

A set is current when the file table inside its index (name, size, MD5 from
the FileDesc packets) equals the manifest's records for the folder, the
block size equals what the policy would choose today, and every packet in
every parity file validates with the planned number of recovery blocks
intact (counting only packets that carry this set's id: a same-named volume
from another set is parity par2 cannot use here). Judging on content rather
than on names and dates is what makes a same-size edit, or an edit whose
parity rebuild was blocked, visible.

When the disagreement is on a file whose digest was merely copied forward
from the records (size and date unchanged), Protect reads the file before
deciding: SHA-256 still matching the record means the record's MD5 was wrong
(a flipped digit in the manifest) and it is corrected; a mismatch means the
file rotted and the folder is blocked. Without this, a flipped MD5 digit
rebuilt the set on every run while reporting PROTECTED.

Check judges every set of a split folder (`PARITY`, `PARITY-2`, ...) by
reproducing Protect's chunking (`protect.chunk_rows`), and `PARITY-INDEX`
against its own policy (50 %, at least 20 recovery blocks).

### Consent before re-recording

The records are only worth anything if they are never quietly rewritten.
Protect therefore stops before touching anything when recorded files are
missing (`accept_removals`) or when a file's contents differ from the
records (`accept_changes`). The second gate is what stops the trustee
scenario: an archive copied through exFAT or rsync gets new dates, so
corruption on the way looks like an edit; only the person can say. The GUI
asks with the list of files; Create passes both flags because a verified
copy of the source supersedes whatever an older archive in the target
recorded.

### What "previous truth" means

Protect's old truth for a file is the manifest entry, cross-checked with
`SHA256SUMS`; a payload file only `SHA256SUMS` lists is previous truth too
(size and date unknown), and a parity-file hash the two disagree on is
re-hashed from disk. When either record is missing or unreadable and a
`PARITY-INDEX` exists, Protect refuses and points at Repair, which restores
the record exactly; only without one does it fall back: to `SHA256SUMS`
(every difference is then suspected corruption, since nothing supports
"edit"), or to the manifest alone with every file re-read. When both are
damaged or disagree, it refuses. A file that cannot be read keeps its
previous entry, on every path; files recorded under a folder the scan
cannot enter are unreadable, not removed. A file the records do not know but
the folder's existing PAR2 set does (a renamed folder, a lost record) is
judged against the set's size and MD5, read from any of its files so a lost
index does not erase the witness.

A file the records do not know whose size and date equal those of a record
that vanished is that file, renamed or moved. Identical contents: recorded
under the new name, reported as moved. Different contents: a renamed file
that rotted; the old record is kept (Repair rebuilds it under its old name),
the new name is not recorded, the folder is blocked. Without this rule a
batch rename under `--accept-removals` recorded the rotted bytes as truth and
destroyed the only parity that could have rebuilt them.

### The safety check before replacing parity

Replacing a set is the one destructive thing Protect does, so before it does
it runs `par2 verify` on the old set. Names par2 prints are NFC; they are
matched to on-disk files through `nfc()` and opened by their real spelling.
For every file par2 calls damaged or missing: if the change was accepted
this run, fine; a file the records did not know (added) is blocked whatever
consent was given, since that consent was for other files; otherwise the
file is hashed now and compared with the manifest. A match means the parity
is merely stale (an earlier edit), a mismatch means the file has rotted and
the old parity is the only thing that can rebuild it, so the folder is
blocked and Repair is recommended. If par2 reports damage but names no file
(an output shape we do not parse), the folder is blocked too: a check that
cannot attribute must not approve.

`--rebuild-parity` is not a way around any of this: it implies a full
re-read of every file (so same-size, same-date rot is found first) and the
verify above still runs on every set that is not current.

After par2 has written the new set, its index is read back and the
(name, size, MD5) rows are compared with the records; par2 read every byte,
so a table that differs means the bytes on disk are not what the records
describe, and the set is refused with the old one left in place. Then the
swap: the new files are moved into the folder under temporary names first
(a rename, no copy), then renamed over the old names, volumes first and the
index last, and finally old files the new set did not replace are removed.
A failure in the first phase leaves the old set untouched; in the second,
the old index with whatever old volumes remain, which par2 can still use.

Measured par2cmdline output shapes are listed in `par2.py`; the one that
bit during development is `File: "x" - no data found.`, which replaces the
`Target:` line entirely when not a single block of a file survives.

### Space estimates

par2 repeats its per-file packets (FileDesc, and IFSC with 20 bytes per
block) in every recovery volume, several times over. Measured at 10 %:
2,000 files of 5 KB cost 2.55x the recovery bytes, 3,000 of 1 KB 2.86x, 200
of 100 KB 1.5x, 20 of 5 MB 1.15x. Preflight and the spanning planner use
`par2.estimate_set_bytes`, which reproduces those within about a third and
errs high, instead of "payload times 10 %".

Since Create protects the source first (0.2.1), the parity, records and
tools are written to the *source* volume before anything reaches the
destination, so preflight (0.2.2) judges two pools of free space: the
destination needs the payload plus a copy of all three; the source volume
needs the three themselves, less any parity an earlier Protect already left
there. When the two folders share a volume the needs are added and judged
against the one free figure. The case that made this necessary: a 12.76 TB photo library on a volume
with 1.50 TB free (measured 2026-10-19), where 0.2.1 would have started, run for hours, and failed
on the source before the first file was copied.

Preflight only reads the source. In place it asks whether the folder can be
written (`access(2)`, and `statvfs` for a read-only volume) instead of
making a test file, because a file made and removed still changes the
folder's date; until 0.2.2 it did make one, and the trace was found on a live
Lightroom library on 2026-10-19. With a destination, preflight makes and
removes small test files there (writability, and whether names that differ
only in case or Unicode spelling fold together), since only a real write
answers for SMB shares, ACLs and odd mounts, and the job writes there
anyway. For an estimate that writes nowhere you care about, pass an empty
scratch folder as the destination. Measured that way on a large photo
library (2026-10-19): 418,791 files in 4,409 folders, 12.76 TB, largest
file 24.94 GB; parity about 1.42 TB at 10 % and about 152 GB at 1 %,
records about 304 MB.

### Repair: records first, then files, then parity

1. Records. `SHA256SUMS` and the manifest are authoritative when they are
   self-consistent (they agree on every file and `SHA256SUMS` carries the
   manifest's own fingerprint). A `PARITY-INDEX` that merely predates them
   (an interrupted Protect) is rebuilt, never used to roll them back. Only
   inconsistent or unreadable records are restored from `PARITY-INDEX`, and
   that is reported, with a note that files changed since the last completed
   Protect will show as changed.
2. Files. par2 repair rewrites every file that differs from the set, so
   Repair verifies first and sorts the damaged files: one that matches the
   records was edited after the parity was built (its current version is put
   back afterwards); one that matches neither, with unchanged size and date,
   is corruption (rebuilt, date restored, par2's backup removed once the file
   proves against `SHA256SUMS`); one that matches neither and has a
   different size or date may be a change the owner made after the last
   Protect (the recorded version is restored, the current one is KEPT as
   `name.1` and reported). Only backups that this run created are ever
   removed.
   The same "keep the current bytes as `name.1`" rule applies when there is
   no manifest to judge by, and to a file the records do not know at all
   but a folder's set does (a lost record): standard Check gives such a set
   par2's own verdict, Repair restores the parity's version and keeps the
   current one. Nothing is ever the only copy that gets deleted.
3. Parity. Folders whose sets are damaged, incomplete, stale, or missing an
   index get fresh parity through Protect's own guarded path, the records
   list the new parity files, and `PARITY-INDEX` is rebuilt.

### Why Create protects the source first (0.2.1), and what that costs

Until 0.2.0, Create copied the folder with read-back, then ran Protect on the
copy with the hashes the copy had already computed. Since 0.2.1 Create runs Protect on the *source* where it is, then copies
everything, records and parity included, with read-back (`engine/create.py`).

Measured on the two real runs of the same 1.00 TB, 73,381-file folder on a
Mac Studio (reports in `~/Library/Logs/Cold Archive`):

| run | step | time | rate |
|---|---|---|---|
| 2026-09-12, target-first (0.2.0), SSD to SSD | copy with read-back | 7,862 s | 7.9 s/GB |
| | protect on the target SSD (17,013 sets) | 8,778 s | 8.8 s/GB |
| | thorough check of the target | 6,763 s | 6.8 s/GB |
| 2026-09-15, source-first (0.2.1), SSD to the Shoebox hard disk | protect on the source SSD | 9,682 s | 9.7 s/GB |
| | copy with read-back, parity included (1.12 TB) | 31,417 s | 28 s/GB |
| | thorough check of the hard disk | 29,436 s | 29 s/GB |

So parity creation is CPU-bound at roughly 9-10 s/GB when the data is on an
SSD, but the hard disk used for the Shoebox moved data at about 36 MB/s
(28 s/GB), slower than the CPU. Building parity there would have been bound
by the disk, and Create would have taken about 8 hours longer. The source-first
order is faster whenever the destination is slower than about 100 MB/s, and
not slower otherwise.

The integrity argument is the stronger one. The source keeps its own records
and parity, so it is an archive too: every later copy of it can be checked
against the records, a copy whose check passes is known to equal the source
at the time the source was recorded, and the decision to delete a source can
rest on that evidence rather than on a listing.

Two consequences, both handled in `create.py`:

* The source may already carry records. Create therefore applies the same
  consent gates as Protect: a file that changed or vanished since the records
  is re-recorded only with `--accept-changes` / `--accept-removals` (the app
  asks, listing the files), never by default. 0.2.1 set both to true for the
  source, which let an edited file be re-recorded unasked (silent corruption
  with the date intact was still refused); fixed in 0.2.2.
* Protect is incremental: a file whose size and date are unchanged is not
  re-read, so corruption since the last run is invisible to that step. The
  copy reads every byte anyway, so each file's hash as read during the copy
  is compared with the source's records afterwards. A mismatch is reported
  as a problem (exit 2, the "silent corruption in the source" result page);
  the copy carries what was read, the records on both sides still say what
  the bytes should be, and Repair fixes either side. A Create onto fresh
  media is therefore also a full check of the old one.

The time budget set for the hand-off drives (2026-10-19): integrity comes before wall-clock
time as long as the whole preparation stays under 72 hours. At the Shoebox
rates that is roughly 3 TB per hand-off drive of that class (create and
thorough check together), or 6 TB or more to a drive that sustains 100 MB/s.

### Ordering on a Protect run

1. scan; load the previous truth (see above)
2. hash what changed; classify: added, changed (consent), rot (blocked)
3. stop here if consent is needed: nothing has been written
4. build or keep each folder's sets (new sets are built in a hidden
   `.cold-archive-build-*` folder and swapped in after verification)
5. write the READMEs and command, the manifest, then `SHA256SUMS` (which
   lists the manifest and the parity files), then the index set

If the run is interrupted before step 5 the old records still describe the
old state and the next run resumes; if it is interrupted during the index
build the records are newer than their parity, which Check reports as out
of date and Repair fixes by rebuilding the index, never by rolling back.

## What the tests exercise (real par2, real files)

Lifecycle create/check/damage/repair/re-protect; silent corruption with the
date intact; deletion and content changes refused without consent; a folder
with both rot and a legitimate edit; same-size edits; NFD-spelled names;
an unreadable or missing manifest; records that disagree or are truncated;
an unreadable file; split sets; empty files and folders; loss of the
records themselves; missing, corrupt and truncated recovery volumes in every
mode; a lost index file; an interrupted Protect (records newer than their
index); names par2 cannot store; payload named like parity; a change made
after Protect surviving Repair; a user's own `name.1`; the check script
against a Mac's BSD `sha256sum` and against a tool that skips lines; the
preflight estimate against what par2 really writes; the Finder lock flag;
damage beyond repair; copies whose read-back lies; every par2 verdict line
shape; CLI exit codes; preflight capacities; the GUI's pages and a job run
through the worker thread (offscreen). See `tests/test_review_fixes.py`.

## Known limits

* The bundled `par2` is built from source as a universal binary for macOS
  13 and later, single-threaded; a threaded `par2-fast` (Homebrew's build)
  is used only on a macOS at least as new as the one it was built for, since
  Homebrew bottles carry `minos` of the build host (measured: 26.0). Creating
  parity single-threaded is about 4x slower (45 s/GB vs 11 s/GB measured);
  checking is the same speed.
* The app itself needs the macOS its Python bindings were built for: the
  PySide6 6.11 wheels are tagged for 13.0 but their binaries carry
  `minos 15.0` (measured with `otool -l`), and dyld refuses a binary built
  for a newer macOS than the running one. `build_app.sh` sets
  `LSMinimumSystemVersion` to the highest `minos` of any bundled binary and
  prints it, so the plist never promises what the bundle cannot do. Pinning
  an older PySide6 would lower it; the archives themselves need no app.
  Measured 2026-10-19: the PySide6 6.12.0 wheels carry `minos 26.0`, and a
  rebuilt `.venv` with the dependency unpinned produced a 0.2.2 bundle that
  only macOS 26 could open, with the build reporting success. `pyproject.toml`
  now pins `PySide6<6.12`, and `build_app.sh` refuses any bundle whose floor
  rises above `APP_MAX_FLOOR` (15.0) instead of noting it.
* A copy's read-back bypasses the page cache (`F_NOCACHE`) and the drive is
  asked to flush at the end (`F_FULLFSYNC`), so what is compared came from
  the medium; nothing can catch a medium that returns the right bytes today
  and wrong ones next year (that is what Check is for).
* Releases are notarized with the maintainer's Developer ID
  (`packaging/build_app.sh` with `NOTARY_PROFILE`).
  Apple refuses submissions with a 403 whenever its developer agreement
  needs accepting again; that is the Account Holder's click, not a
  credential problem.
* Recorded dates and dates on disk are compared with 2 seconds of slack
  (`layout.same_date`). FAT32 keeps dates to 2 s and exFAT to 10 ms, so any
  copy onto such a drive moves every date a little; with an exact comparison
  (until 0.3.0) Repair took damage there for an edit and Protect asked
  whether rot was intentional. The price: an edit that keeps the file's size
  and lands within 2 s of its recorded date looks like rot to Protect and
  Repair. Check, which reads every byte, still reports it.
* Symlinks are skipped, not archived.
* Extended attributes and ACLs are not preserved (an archive is plain files).
* A case-sensitive source (case-sensitive HFS+ or APFS, Linux) can hold two
  names that differ only by case, and a case-insensitive destination folds
  them onto one file. Preflight refuses the copy rather than let one
  overwrite the other; the fix is a rename at the source, not a flag.
  Measured 2026-10-19 (read-only): a Lightroom masters tree on a
  case-sensitive HFS+ volume holds 1,799 such pairs, for example
  `IMG_7040-2.JPG` beside `IMG_7040-2.jpg` in one day folder, and every
  pair differs in size, so they are distinct renditions, not duplicates;
  the fix is to rename them inside Lightroom before Create.
* On a live Lightroom tree, a parity set is per folder and a rebuild
  re-reads the whole folder, so one rewritten sidecar costs that folder's
  full read. With "Automatically write changes into XMP" on, every edit
  session touches the parity of every day folder it visited; batch the
  Protect runs, or write metadata once at era close. Keep the `.lrcat` and
  `Previews.lrdata` outside the protected tree; they change constantly.
  Lightroom Classic catalogs are SQLite in WAL mode: header bytes 18-19 read
  `02 02` for every catalog measured on 2026-10-19, the 6.4 GB
  master (`Adobe_DBVersion` 1500000) as well as the 15.4 ones (1504001).
  `PRAGMA journal_mode` is not a reliable reading here: under an immutable
  open it reports `delete` even for a WAL file. Any ordinary open creates `-wal` and `-shm` files beside
  the catalog, and a Protect that walks the catalog folder while Lightroom
  has it open records files that vanish a moment later. A catalog that is
  archived (the Shoebox drive carries one) changes the moment someone opens
  it in Lightroom, and the next Check reports it changed; open a copy.
* Source-first Create needs room on the source volume for the source's own
  repair data and records (about the parity estimate preflight prints: at
  least 10 % of the data, more for folders of many small files). A nearly
  full source volume cannot be archived with Create as it stands; the ways
  out are to free space there first (in one measured library, about 1.3 TB of
  import-collision copies, deletable once the records confirm byte
  identity), or to Protect at a low redundancy first so the
  hashes exist, delete what the hashes prove redundant, and then Protect
  again at 10 %. There is no target-first path in 0.2.x; preflight refuses
  rather than start a run that would fail on the source hours in.
* par2cmdline 1.3 is not the fastest implementation; par2cmdline-turbo is a
  drop-in that this tool would use if found first on PATH.
