"""Create: protect the source folder where it is, then copy it, records and
parity included, to the destination with read-back verification.

The source is protected first (since 0.2.1) for two reasons,
both measured on the Shoebox run of 2026-09-15 (1.00 TB, 73,381 files): the
destination drive wrote at about 36 MB/s, so parity built there would have
been bound by that disk (about 28 s/GB) instead of by the CPU (9.7 s/GB on the
source SSD); and the source keeps its own records, so every later copy of it
can be checked against them and the decision to delete a source can rest on
evidence.

Two things follow from protecting the source rather than a fresh copy:

* The source may already carry records from an earlier run, so the same
  consent gates as a standalone Protect apply: files that changed or vanished
  since are re-recorded only with an explicit yes (``accept_changes`` /
  ``accept_removals``), never by default. Silent corruption (bytes differ,
  size and date intact) is refused by Protect regardless.
* Protect is incremental: a file whose size and date are unchanged is not
  re-read, so corruption that happened since the last run is invisible to
  that step. The copy reads every byte anyway, so each file's hash as read
  during the copy is compared with the source's records afterwards. A
  mismatch means the source rotted since it was recorded (or the drive reads
  unreliably); the copy carries what was read, the records on both sides
  still say what the bytes should be, and Repair can fix either side. The
  result is reported as a problem, never shipped quietly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .copy import CopyOptions, CopyResult, copy_tree
from .layout import MANIFEST_FILE, nfc
from .manifest import load_manifest
from .observer import NullObserver, Observer
from .protect import ProtectOptions, ProtectResult, protect


@dataclass
class CreateResult:
    source: str
    target: str
    protect: ProtectResult
    copy: Optional[CopyResult] = None
    # files that read differently during the copy than the source's records say
    source_mismatches: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def needs_consent(self) -> str:
        return self.protect.needs_consent

    @property
    def ok(self) -> bool:
        return (self.protect.ok and self.copy is not None and self.copy.ok
                and not self.source_mismatches and not self.errors)

    def summary(self) -> str:
        parts = [self.protect.summary()]
        if self.copy is not None:
            parts.append(self.copy.summary())
        if self.source_mismatches or self.errors:
            lines = ["SOURCE PROBLEMS:"]
            for e in self.errors:
                lines.append("  " + e)
            for rel in self.source_mismatches[:20]:
                lines.append("  read differently than recorded: " + rel)
            if len(self.source_mismatches) > 20:
                lines.append(f"  ... and {len(self.source_mismatches) - 20} more")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)


def create_archive(source: str, target: str, options: Optional[ProtectOptions] = None,
                   observer: Optional[Observer] = None, copy_options: Optional[CopyOptions] = None) -> CreateResult:
    """Protect *source* in place, then copy it (records and parity included)
    to *target*. Stops after the Protect step when it needs consent or
    failed; nothing is copied then."""
    obs = observer or NullObserver()
    opts = options or ProtectOptions()
    pres = protect(source, opts, obs)
    res = CreateResult(source=source, target=target, protect=pres)
    if pres.needs_consent or not pres.ok:
        return res

    copts = copy_options or CopyOptions()
    copts.include_archive_files = True
    res.copy = copy_tree(source, target, copts, obs)

    manifest = None
    try:
        manifest = load_manifest(os.path.join(source, MANIFEST_FILE))
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"the source's records could not be read back after the copy ({e}); "
                          "run Check on the source folder")
    if manifest is None:
        if not res.errors:
            res.errors.append("the source's records were not found after the copy; run Check on the source folder")
        return res
    by_nfc = {nfc(rel): e for rel, e in manifest.files.items()}
    for rel, dg in res.copy.hashes.items():
        e = manifest.files.get(rel) or by_nfc.get(nfc(rel))
        if e is not None and e.sha256 and e.sha256 != dg.sha256:
            res.source_mismatches.append(rel)
    res.source_mismatches.sort()
    if res.source_mismatches:
        n = len(res.source_mismatches)
        res.errors.append(f"{n} files read differently during the copy than the source's records say (for example "
                          f"{res.source_mismatches[0]!r}). That is silent corruption in the source since it was recorded, "
                          "or a drive that reads unreliably. The copy carries what was read; the records on both sides "
                          "still say what the bytes should be. Run Check, then Repair, on the source and on the copy, "
                          "then run Create again.")
    return res
