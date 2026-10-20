"""Make a compatibility fixture: a small archive protected by a given build.

Every Cold Archive build must check and repair archives made by every earlier
build (the Shoebox drives are meant to outlive many releases). The fixtures
in tests/compat/archives/ are archives made by real past builds, kept
byte-for-byte; tests/test_compat.py and the build gate (packaging/
compat_gate.py) prove each new build can read, check and repair them.

To add a fixture for a release (do this once per release, with the release's
own installed app or DMG, never with a development checkout):

    python3 tests/compat/make_fixture.py /tmp/fx            # writes the sample tree
    ln -s "/Volumes/Cold Archive/Cold Archive.app/Contents/MacOS/Cold Archive" /tmp/cold-archive-cli
    /tmp/cold-archive-cli protect /tmp/fx --no-tools --label "Compatibility fixture" --media hdd
    cp -Rp /tmp/fx tests/compat/archives/v<version>

The link must be named cold-archive-cli: the app runs its command line when
started under that name. Record the build and par2 version in
tests/compat/README.md. The tree is the same every time (fixed seed), shaped
like a photo archive: dated folders, binary "photos", XMP sidecars, an
accented name, an empty file and a nested folder.
"""
from __future__ import annotations

import os
import random
import sys
import unicodedata


def make_tree(root: str) -> None:
    rnd = random.Random(19980101)
    layout = {
        "2021/2021-12-01": [("IMG_0001.CR3", 48_000), ("IMG_0001.xmp", 2_300), ("IMG_0002.CR3", 52_000)],
        "2022/2022-04-27": [("DSC_1040.ARW", 61_000), ("DSC_1040.xmp", 2_100), ("DSC_1041.JPG", 23_500)],
        "2022/2022-04-27/edits": [("DSC_1040-Edit.tif", 37_000)],
        "Curated": [("best of 2022.jpg", 19_000)],
        "Letters": [],
    }
    for folder, files in layout.items():
        d = os.path.join(root, folder)
        os.makedirs(d, exist_ok=True)
        for name, size in files:
            with open(os.path.join(d, name), "wb") as f:
                f.write(rnd.randbytes(size))
    letters = os.path.join(root, "Letters")
    with open(os.path.join(letters, unicodedata.normalize("NFC", "Café menu.txt")), "w", encoding="utf-8") as f:
        f.write("Café au lait, croissant, déjeuner sur l'herbe.\n" * 40)
    with open(os.path.join(letters, "note to self.txt"), "w", encoding="utf-8") as f:
        f.write("Check the drive once a year.\n" * 20)
    open(os.path.join(letters, "empty.txt"), "wb").close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or os.path.exists(sys.argv[1]):
        sys.exit("usage: make_fixture.py <new folder>")
    make_tree(sys.argv[1])
    print("sample tree written to", sys.argv[1])
