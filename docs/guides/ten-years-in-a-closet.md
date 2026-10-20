# Ten years in a closet

*What a long-stored archive drive can do when you wake it, and what to do about each.*

This guide is for anyone who finds a Shoebox Project drive, or any drive made
with Cold Archive, after it has spent years on a shelf. You do not need to
know anything about computers to follow it. Read the first section before you
plug anything in.

The drive holds ordinary photo and video files in ordinary folders, plus
extra files that can find damage and repair it. Most drives wake up fine.
When one does not, the right first move is almost always to stop, so that
nothing gets worse.

## Before you plug it in

- **Let it reach room temperature.** If the box was somewhere cold or damp,
  leave the drive out in a dry room for a few hours, or overnight, so that no
  moisture forms inside it.
- **Use the cable from the box, straight into the computer.** Not through a
  keyboard, a monitor or a hub. A portable drive takes all its power from
  that one cable.
- **Never click Initialize, Erase, Format or "Use as Backup Disk".** Any of
  them can wipe the drive. When in doubt, click Ignore, Cancel, Eject or
  Don't Use.
- **If it clicks, grinds, beeps or smells hot, unplug it at once.** Every new
  attempt can make a mechanical fault worse.
- **If this is the only copy and the hardware seems wrong, get help before
  experimenting.** A data-recovery service can often bring back everything
  from a drive that a few home attempts would finish off.

## 1 · The box

**The humidity card shows damp, or the box feels or smells damp.**
Do not plug the drive in yet. Leave it unpacked in a dry room for a day or
two, then carry on.

**The plug or the socket looks green, white or crusty.**
That is corrosion. Do not plug it in, because the electronics may short. A
computer repair shop can clean the connector or move the disk into a new case.

**The cables are missing, or none of your sockets fit.**
The drive has a USB-C socket. Any USB-C cable that carries data will do, with
an adapter if the computer only has the older rectangular USB-A sockets. Some
cheap cables only charge phones and carry no data: with one of those the
drive lights up but never appears on screen.

## 2 · Power and sound

**No light and no sound.**
Try the other cable, another socket, then another computer. If nothing at
all happens, the small circuit board in the drive's case has probably
failed. The disk inside is usually fine, and a repair shop or data-recovery
service can move it to a working case.

**Clicking, ticking, grinding or scraping. Or it spins up, stops, and spins
up again.**
Unplug it now. This is a mechanical fault. Do not try again, tap it, shake
it or put it in a freezer. Take it to a data-recovery service.

**A beep or a buzz, and it never seems to spin.**
After years of rest the parts inside can stick, or the socket may not give it
enough power. Try once more in a different socket directly on the computer,
with the other cable. If it still beeps, stop, and take it to a
data-recovery service.

**It gets hot, or smells of burning.**
Unplug it and leave it unplugged. Data-recovery service.

## 3 · What the computer says

**Nothing appears, but the drive hums quietly as if it were working.**
Give it a minute: the first start after years can be slow. On a Mac, open
Finder and look under Locations in the sidebar. If it is not there, open
Disk Utility (in Applications, then Utilities), choose View > Show All
Devices, select the drive and click Mount. Still nothing: try the other
cable, remembering the charging-only cables above, then another Mac.

**A Mac says "The disk you attached was not readable by this computer."**
Click **Ignore** or **Eject**. Never Initialize. Shoebox drives are formatted
for Macs (APFS), which any Mac running macOS 10.13 High Sierra (2017) or
later can read. If a recent Mac still cannot read it, the drive's filing
system may be damaged. Disk Utility's First Aid can usually fix that, but it
writes to the drive, so if this is the only copy, ask a data-recovery service
first.

**A Windows PC says "You need to format the disk before you can use it."**
Click **Cancel**. Windows cannot read a Mac drive without extra software, and
formatting would erase it. Use a Mac, or see section 5 if there is no Mac
anywhere.

**A Mac asks whether to use the drive with Time Machine.**
Click **Don't Use**. Time Machine can erase a drive to make room for backups.

**Photos or Image Capture opens and offers to import.**
Close it. Importing does no harm, but you do not need it.

**The drive appears with a strange name, a size of zero, or a size far from
what its label says.**
The drive's electronics may be misreporting. Do not format it or "repair"
it. Try another cable and another computer. If it stays wrong, take it to a
data-recovery service.

**It appears, but everything is very slow: folders take ages to open, or
copying stops with an error.**
The disk has weak spots. Do not keep opening photos one at a time, because
each attempt wears it further. In the Cold Archive app, click **Rescue a
failing drive**: it copies everything that can be read onto a healthy drive
and then repairs the copy. See *Recovering a damaged archive*, step 3.

## 4 · The files

**Everything opens and looks right.**
Good. Run a check anyway (*Recovering a damaged archive*, step 2). A drive can
look fine and still hold a few damaged files. Then plan a new copy
(section 6).

**Some photos will not open, show grey or coloured bands, or stop halfway.**
This is what the repair files are for. Run a check, then a repair: see
*Recovering a damaged archive*.

**A whole folder is missing or empty.**
Repair can rebuild up to about a tenth of each folder, not a whole folder.
Look first in case it was moved. If it is really gone, it has to come from
another copy of the archive.

**RAW photos (names ending in .CR3, .ARW, .RAF, .NEF or .DNG) will not open.**
That is not damage. RAW files are a camera's digital negatives and need a
photo app that understands them. On a Mac, Preview and Photos open most of
them. The free apps darktable and RawTherapee open them on any computer, and
Adobe's free DNG Converter turns them into DNG, the most widely readable RAW
format. JPEG and HEIC photos open almost anywhere (Windows may need
Microsoft's free HEIF Image Extensions for HEIC). Videos (.MOV, .MP4) play
in most players; VLC is free.

**Files ending in .xmp, a file ending in .lrcat, or a folder of previews.**
These come from the photo editor (Adobe Lightroom) and hold the edits made to
each photo. Keep them next to the photos. The photos open without them. To
use the catalog in Lightroom, copy its folder to your computer and open the
copy: Lightroom changes a catalog the moment it opens it, and the next check
of the drive would report the change.

**Files called PARITY, SHA256SUMS and cold-archive-manifest.json.**
Those are the repair and fingerprint files. Leave them where they are, and
copy them along with everything else whenever you copy the archive.

**Files ending in .1 next to some photos.**
A repair left them: they are the damaged versions it replaced. Delete them
once a check passes.

**Files called .DS_Store, or names starting with ._**
Mac housekeeping. Ignore them.

## 5 · The software

**There is no Cold Archive app on this Mac.**
It is free: go to **github.com/wwward/cold-archive** and click **Download**,
or use the copy in the drive's TOOLS folder (double-click the file ending in
`-macOS.zip`). Drag **Cold Archive** onto **Applications**. The first time
you open it, click **Open** when your Mac asks about an app from the
internet.

**The Cold Archive app will not open. The Mac says it cannot check it for
malware, or that it is damaged.**
Open System Settings > Privacy & Security, scroll down to the message about
Cold Archive and click **Open Anyway**. On macOS 14 and older, right-click the
app and choose Open instead. Or download the newest version from
github.com/wwward/cold-archive, which opens without this step.

**The app will not run on this Mac at all, because the Mac is too old, too
new, or not a Mac.**
Nothing on the drive depends on the app. Double-click
**Check-This-Archive.command** in the drive's top folder to check every
file. To repair without the app, ask someone comfortable with computers to
follow *For a technical helper*, the last section of *Recovering a damaged
archive*: every file can be checked and repaired with free tools on any
computer.

**There is no Mac anywhere.**
Windows and Linux cannot read a Mac drive by themselves. The simplest answer
is to borrow a Mac for an afternoon. Otherwise a technical helper can read
the drive on Windows or Linux and copy it to a drive every computer reads
(see *For a technical helper*).

**The instructions are lost.**
READ-ME-FIRST.txt, and an .html copy of it, sit in the top folder of the
drive. These guides are online at github.com/wwward/cold-archive.

## 6 · Time

**Nobody has checked the drive in years.**
Check it now. Then copy the whole archive, PARITY files included, to a new
hard drive, and check the copy. Ten years is old for a hard drive. When the
old drive is healthy, the Cold Archive app's *Create a new archive* copies
it and reads every file back in one step.

**You are choosing a new drive for it.**
Use a hard disk, not a memory card, a USB stick or an SSD: flash memory
slowly loses data when it is left unpowered. Copy everything, check the copy,
and keep the two copies in different places.

## Getting professional help

If you take the drive to a data-recovery service:

- Choose one that quotes before starting and charges nothing if nothing
  comes back.
- Tell them it is a portable USB hard disk with a Mac (APFS) volume.
- Tell them every folder carries PAR2 repair files. A partial recovery is
  still worth having: if they bring back most of each folder, the repair
  files can rebuild the rest.
- Ask for the result on a new hard drive, then check and repair it as in
  *Recovering a damaged archive*.

---

THE SHOEBOX PROJECT | SHARED MEMORIES & STORIES
