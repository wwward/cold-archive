"""Plan how to split an archive across several discs or drives.

Whole folders are kept together wherever possible, because each folder's
PAR2 set only works when the folder's files are all present in one place. A
folder larger than a single volume is split by files, and the plan says so.

Capacities are the real byte counts of the media, not round numbers: a plan
that overfills a disc is a data-loss event when it is burned.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import par2 as p2
from .layout import TOOLS_DIR, human_bytes
from .observer import NullObserver, Observer
from .scan import scan_archive

# Real capacities in bytes (2048-byte sectors).
MEDIA_LIMITS: Dict[str, int] = {
    "CD": 737_280_000,           # 700 MB CD-R (80 min)
    "DVD": 4_700_372_992,        # DVD-5 (4.7 GB)
    "DVD_DL": 8_543_666_176,     # DVD-9 (8.5 GB)
    "BD_25": 25_025_314_816,     # BD-R 25 GB
    "BD_50": 50_050_629_632,     # BD-R DL 50 GB
    "BD_100": 100_103_356_416,   # BDXL 100 GB
    "BD_128": 128_001_769_472,   # BDXL 128 GB
}
SECTOR = 2048
FS_RESERVE_FRACTION = 0.02   # UDF/ISO directory structures, sector padding, safety
FS_RESERVE_MIN = 16 << 20


@dataclass
class DirUnit:
    rel: str
    files: int
    bytes: int        # payload + estimated parity, sector-rounded


@dataclass
class Volume:
    volume_id: int
    capacity: int
    used: int = 0
    units: List[DirUnit] = field(default_factory=list)

    @property
    def free(self) -> int:
        return self.capacity - self.used

    def fits(self, size: int) -> bool:
        return self.used + size <= self.capacity

    def add(self, unit: DirUnit) -> None:
        self.units.append(unit)
        self.used += unit.bytes


@dataclass
class SpanPlan:
    media_type: str
    capacity: int
    usable: int
    redundancy_percent: int
    volumes: List[Volume] = field(default_factory=list)
    split_dirs: List[str] = field(default_factory=list)
    oversized_files: List[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Spanning plan for {self.media_type} ({human_bytes(self.capacity)} per disc, {human_bytes(self.usable)} usable "
                 f"after filesystem reserve), {self.redundancy_percent}% repair data included:",
                 f"  {len(self.volumes)} volumes needed"]
        for v in self.volumes:
            lines.append(f"  Volume {v.volume_id}: {human_bytes(v.used)} of {human_bytes(v.capacity)} ({v.used * 100 // max(1, v.capacity)}%), "
                         f"{len(v.units)} folders")
            for u in v.units[:50]:
                lines.append(f"      {u.rel or '(top folder)'}: {u.files} files, {human_bytes(u.bytes)}")
            if len(v.units) > 50:
                lines.append(f"      ... and {len(v.units) - 50} more folders")
        if self.split_dirs:
            lines.append(f"  {len(self.split_dirs)} folders are larger than one volume and had to be split across volumes; "
                         "their repair data only works once the pieces are back together in one folder:")
            for d in self.split_dirs:
                lines.append("      " + d)
        if self.oversized_files:
            lines.append(f"  {len(self.oversized_files)} single files do not fit on one volume at all:")
            for f in self.oversized_files:
                lines.append("      " + f)
        return "\n".join(lines)


def _sector_round(n: int) -> int:
    return ((n + SECTOR - 1) // SECTOR) * SECTOR


def _parity_estimate(sizes: List[int], redundancy_percent: int) -> int:
    """Parity bytes for one folder, from the real block policy and par2's
    packet overhead (see par2.estimate_set_bytes); 0 for nothing to protect."""
    sizes = sorted(s for s in sizes if s > 0)
    total = 0
    for i in range(0, len(sizes), p2.MAX_FILES_PER_SET):
        chunk = sizes[i:i + p2.MAX_FILES_PER_SET]
        try:
            plan = p2.plan_blocks(chunk, redundancy_percent)
        except ValueError:
            continue
        total += p2.estimate_set_bytes(chunk, plan)
    return total


class Spanner:
    def __init__(self, root: str, media_type: str = "BD_25", custom_capacity: Optional[int] = None,
                 redundancy_percent: int = 10):
        self.root = os.path.abspath(root)
        self.media_type = media_type.upper()
        self.redundancy_percent = redundancy_percent
        if custom_capacity:
            self.capacity = int(custom_capacity)
            self.media_type = "CUSTOM"
        else:
            if self.media_type not in MEDIA_LIMITS:
                raise ValueError(f"unknown media type {media_type}; choose one of {', '.join(MEDIA_LIMITS)}")
            self.capacity = MEDIA_LIMITS[self.media_type]
        # Reserve for the filesystem's own structures: 2 %, at least 16 MB on
        # real media, but never more than a tenth of a (small, custom) capacity.
        reserve = max(min(FS_RESERVE_MIN, self.capacity // 10), int(self.capacity * FS_RESERVE_FRACTION))
        self.usable = self.capacity - reserve

    def plan(self, observer: Optional[Observer] = None) -> SpanPlan:
        obs = observer or NullObserver()
        scan = scan_archive(self.root, obs)
        plan = SpanPlan(self.media_type, self.capacity, self.usable, self.redundancy_percent)
        from .preflight import estimate_records_bytes, estimate_tools_bytes
        units: List[DirUnit] = []
        # the records, the kit and PARITY-INDEX live in the top folder and are
        # not payload the scanner lists; neither is TOOLS/ before the first
        # Protect has written it
        extra_top = estimate_records_bytes(len(scan.files), len(scan.dirs), self.redundancy_percent)
        if not any(sd.rel == TOOLS_DIR for sd in scan.dirs):
            tools = estimate_tools_bytes(self.redundancy_percent)
            if tools:
                units.append(DirUnit(TOOLS_DIR + " (written by Protect)", 3, _sector_round(tools)))
        units.append(DirUnit("(records, READ-ME-FIRST, PARITY-INDEX)", 0, _sector_round(extra_top)))
        for sd in scan.dirs:
            if not sd.files:
                continue
            payload = sum(_sector_round(f.size) for f in sd.files)
            parity = _parity_estimate([f.size for f in sd.files], self.redundancy_percent)
            size = payload + parity
            if size <= self.usable:
                units.append(DirUnit(sd.rel, len(sd.files), size))
                continue
            # split a huge folder by files (each file with its share of parity)
            plan.split_dirs.append(sd.rel or "(top folder)")
            for f in sorted(sd.files, key=lambda x: x.size, reverse=True):
                fsize = _sector_round(f.size)
                fsize += _parity_estimate([f.size], self.redundancy_percent)
                if fsize > self.usable:
                    plan.oversized_files.append(f.rel)
                    continue
                units.append(DirUnit((sd.rel + "/" if sd.rel else "") + f.name + " (part of split folder)", 1, fsize))
        # First-fit decreasing over folder units
        units.sort(key=lambda u: u.bytes, reverse=True)
        for u in units:
            for v in plan.volumes:
                if v.fits(u.bytes):
                    v.add(u)
                    break
            else:
                v = Volume(len(plan.volumes) + 1, self.usable)
                v.add(u)
                plan.volumes.append(v)
        for v in plan.volumes:
            v.units.sort(key=lambda u: u.rel)
        return plan
