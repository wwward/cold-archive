"""Find Cold Archives on the drives that are connected, so a person can pick
one with a click instead of navigating a folder dialog.

An archive is a folder holding cold-archive-manifest.json. Each drive's top
folder and the folders directly inside it are looked at; nothing deeper, so
a large drive is not walked. The startup disk (a link to / in /Volumes) and
macOS's own volumes are skipped.
"""
from __future__ import annotations

import os
from typing import List

from .layout import MANIFEST_FILE

VOLUMES = "/Volumes"


def find_archives(volumes_dir: str = VOLUMES) -> List[str]:
    found: List[str] = []
    try:
        names = sorted(os.listdir(volumes_dir))
    except OSError:
        return found
    for name in names:
        if name.startswith(".") or name.startswith("com.apple."):
            continue
        root = os.path.join(volumes_dir, name)
        if os.path.islink(root) or not os.path.isdir(root):
            continue
        candidates = [root]
        try:
            candidates += [os.path.join(root, n) for n in sorted(os.listdir(root))
                           if not n.startswith(".") and os.path.isdir(os.path.join(root, n))]
        except OSError:
            pass
        for c in candidates:
            if os.path.isfile(os.path.join(c, MANIFEST_FILE)):
                found.append(c)
    return found


def describe(path: str, volumes_dir: str = VOLUMES) -> str:
    """'Drive name' or 'Drive name / folder' for a button label."""
    rel = os.path.relpath(path, volumes_dir)
    return rel.replace(os.sep, " / ") if not rel.startswith("..") else os.path.basename(path)
