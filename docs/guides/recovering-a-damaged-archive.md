# Recovering a damaged archive

*How to check a Cold Archive drive, and bring back damaged or missing files.*

Every folder on the drive carries two kinds of extra files. The fingerprint
files (SHA256SUMS and cold-archive-manifest.json) record exactly what each
file should be, so a check can tell a perfect file from a damaged one. The
repair files (the ones named PARITY) can rebuild up to about a tenth of each
folder. The Cold Archive app uses both, and every step below is a click in
the app. Set aside an afternoon; most of it is waiting.

## What you need

- The drive and its cable.
- A Mac with the Cold Archive app. It is free: go to
  **github.com/wwward/cold-archive**, click **Download**, open the file that
  arrives, and drag **Cold Archive** onto **Applications**. Drives made with
  Cold Archive usually carry a copy too, in their TOOLS folder: double-click
  the file whose name ends in `-macOS.zip`, then drag the app onto
  Applications.
- The first time you open the app, your Mac asks whether to open an app
  downloaded from the internet: click **Open**. If it refuses instead, open
  System Settings > Privacy & Security, scroll down to the message about
  Cold Archive, and click **Open Anyway**.
- If the drive is struggling (slow, errors, odd noises): a second, healthy
  hard drive at least as large.

## 1 · Is the drive healthy?

- **Healthy:** it appears promptly, folders open at normal speed, and it makes
  no unusual sounds. Go to step 2.
- **Struggling:** it is slow, or copying stops with errors. Go to step 3
  first; it copies the drive and then repairs the copy, not the old drive.
- **Clicking, grinding or beeping:** stop. See *Ten years in a closet*,
  section 2.

## 2 · Check

1. Open Cold Archive and click **Check an archive**.
2. Under *On connected drives*, click the drive's name. (If it is not
   listed, click **Choose…** and pick the drive's top folder, the one with
   READ-ME-FIRST.txt in it.)
3. Leave **Standard** selected and click **Start**.
4. Wait. A check reads every file: on a portable hard disk, allow about four
   hours for each terabyte. The app keeps the Mac awake; keep a laptop
   plugged in with its lid open.

The result is one of three:

- **Everything is exactly as it was.** Nothing to do. Eject the drive and put
  it away.
- **All files are intact, but something needs attention.** Your files are
  fine; some repair data or records are damaged or out of date. Click
  **Protect now** to rebuild them. That is safe here, because the check has
  just confirmed every file.
- **Damage was found.** If it adds *Repair can fix this*, click
  **Repair now** and go to step 4. Otherwise read step 4's last result.

Without the app, double-click **Check-This-Archive.command** in the drive's
top folder: a window lists every file and ends with a line that starts with
RESULT.

## 3 · Rescue a struggling drive first

1. Connect a healthy hard drive at least as large as the old one. Open it in
   Finder and make an empty folder for the rescue: **File > New Folder**.
2. In Cold Archive, click **Rescue a failing drive**.
3. Under *On connected drives*, click the struggling drive. Next to
   *Copy to*, click **Choose…** and pick the empty folder on the healthy
   drive.
4. Click **Start**. Everything that can be read is copied, including the
   repair files, and parts that cannot be read are noted. Nothing on the
   struggling drive is changed. If it stops partway, start it again with the
   same choices; what was already copied is kept.
5. When it finishes, click **Repair the copy now** and read the results in
   step 4.
6. Put the old drive away unplugged, in case a data-recovery service is
   needed later.

## 4 · Repair

1. Click **Repair an archive** (or **Repair now** on the check result, or
   **Repair the copy now** after a rescue).
2. Under *On connected drives*, click the drive, or choose its top folder.
   Leave the box unticked unless you want to keep the damaged versions.
3. Click **Start**. Repair rebuilds every damaged or missing file it can,
   restores the files' dates, and checks the result.

The result is one of three:

- **Repaired: everything is exactly as it was.** Go to step 5.
- **Repaired, but something needs attention.** Click **Open report**.
  Usually a file had been changed on purpose after the archive was made: the
  original was restored, and the changed version kept beside it with .1 at
  the end of its name.
- **Not everything could be repaired.** The folders listed in the report lost
  more than their repair files can rebuild. Those files have to come from
  another copy of the archive, or from a data-recovery service; whatever the
  service gets back, repair can then complete.

## 5 · Check again, then make a fresh copy

1. Run the check again (step 2). It should now say *Everything is exactly as
   it was*.
2. A drive that has started to fail will fail further. Copy the repaired
   archive to a new hard drive: in the app, click **Create a new archive**,
   choose the repaired top folder as *Folder to archive* and an empty folder
   on the new drive as *Destination*, and click **Start**. It copies
   everything and reads every file back.
3. Eject the new drive, plug it in again, check it, and keep it somewhere
   other than the first copy.

## What repair can and cannot do

- It can rebuild up to about a tenth of each folder, in any pattern:
  scattered bad spots, a few whole photos, or files that are missing.
- It cannot rebuild a folder that lost more than that, or one that is gone
  entirely. Only another copy can.
- If all of a folder's PARITY files are lost, its photos can still be checked
  but not repaired.
- The fingerprint files are protected too, by the PARITY-INDEX files in the
  top folder.
- A data-recovery service and the repair files work well together: whatever
  the service recovers, repair can finish, as long as no folder is more than
  a tenth short.

## For a technical helper

Everything above can also be done without the app, on any computer, with
free tools. READ-ME-FIRST.txt on the drive has the full details.

- **Check:** in the drive's top folder, run `shasum -a 256 -c SHA256SUMS`
  (Mac) or `sha256sum -c SHA256SUMS` (Linux); READ-ME-FIRST.txt gives a
  PowerShell line for Windows. If Check-This-Archive.command will not open
  by double-clicking, run `bash Check-This-Archive.command` in Terminal.
- **Repair:** get par2 (Mac: `brew install par2` after installing Homebrew
  from brew.sh; Linux: the `par2` package; Windows: MultiPar). In each folder
  that has damage, run `par2 repair PARITY.par2`, and the same for
  PARITY-2.par2 and onward if present; for the records in the top folder,
  `par2 repair PARITY-INDEX.par2`. par2 keeps each damaged original as a
  `.1` file; delete those after a clean check. The TOOLS folder holds the
  par2 source code if no build exists any more.
- **Rescue from the command line:** the app's own command line does the same
  rescue: `"/Applications/Cold Archive.app/Contents/MacOS/Cold Archive"`
  started through a link named `cold-archive-cli`, then
  `cold-archive-cli rescue <archive> <empty folder on another drive>`. For a
  drive that is failing badly, `ddrescue` to an image file first is the
  professional route.
- **A Mac drive on another computer:** Shoebox drives are formatted APFS.
  Linux reads them with apfs-fuse; Windows needs a commercial APFS reader.
  Copy everything to an exFAT drive, then check and repair as above.

---

THE SHOEBOX PROJECT | SHARED MEMORIES & STORIES
