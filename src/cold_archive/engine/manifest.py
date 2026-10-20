"""The two root-level records of an archive.

``SHA256SUMS``: the authoritative hash list, in the exact text format that
``sha256sum -c`` (GNU coreutils), ``shasum -a 256 -c`` (macOS/Perl) and
BusyBox accept. A trustee needs no software from us to use it.

``cold-archive-manifest.json``: metadata (sizes, mtimes, settings) that makes
incremental re-protection cheap and helps a future reader understand the
archive. It is a convenience: verification and repair must work from
``SHA256SUMS`` and the PAR2 sets alone.

Both are written atomically (temp file + fsync + rename) so a crash mid-write
never leaves a truncated record in place of a good one.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .. import __version__
from .layout import FORMAT_VERSION

MANIFEST_FORMAT = "cold-archive-manifest"


class ManifestError(Exception):
    pass


class ManifestTooNew(ManifestError):
    """The manifest was written by a newer format than this tool understands."""


@dataclass
class FileEntry:
    size: int
    mtime: float
    sha256: str
    md5: str = ""   # par2 identifies files by MD5; kept so parity staleness is judged on content

    def to_json(self) -> dict:
        d = {"size": self.size, "mtime": self.mtime, "sha256": self.sha256}
        if self.md5:
            d["md5"] = self.md5
        return d

    @classmethod
    def from_json(cls, d: dict) -> "FileEntry":
        sha = str(d["sha256"]).lower()
        md5 = str(d.get("md5", "")).lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ManifestError(f"bad sha256 in manifest: {sha!r}")
        if md5 and (len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5)):
            raise ManifestError(f"bad md5 in manifest: {md5!r}")
        return cls(size=int(d["size"]), mtime=float(d["mtime"]), sha256=sha, md5=md5)


@dataclass
class Manifest:
    tool_version: str = __version__
    format_version: int = FORMAT_VERSION
    created: str = ""
    updated: str = ""
    label: str = ""
    media: str = ""
    redundancy_percent: int = 10
    files: Dict[str, FileEntry] = field(default_factory=dict)
    # Our own payload parity files (PARITY*.par2 in each folder), with their
    # hashes, so SHA256SUMS can list them and a lost or corrupt recovery
    # volume is visible to any checksum tool. PARITY-INDEX cannot be listed:
    # it is built from SHA256SUMS.
    parity: Dict[str, FileEntry] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.files.values())

    def to_json(self) -> dict:
        return {
            "format": MANIFEST_FORMAT,
            "format_version": self.format_version,
            "tool": "Cold Archive",
            "tool_version": self.tool_version,
            "created": self.created,
            "updated": self.updated,
            "label": self.label,
            "media": self.media,
            "redundancy_percent": self.redundancy_percent,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "notes": list(self.notes),
            "files": {k: self.files[k].to_json() for k in sorted(self.files)},
            "parity_files": {k: self.parity[k].to_json() for k in sorted(self.parity)},
        }

    @classmethod
    def from_json(cls, d: dict) -> "Manifest":
        if d.get("format") != MANIFEST_FORMAT:
            raise ManifestError("not a Cold Archive manifest")
        fv = int(d.get("format_version", 0))
        if fv > FORMAT_VERSION:
            raise ManifestTooNew(f"manifest format {fv} is newer than this tool understands ({FORMAT_VERSION}); use a newer Cold Archive")
        files = {str(k): FileEntry.from_json(v) for k, v in dict(d.get("files", {})).items()}
        parity = {str(k): FileEntry.from_json(v) for k, v in dict(d.get("parity_files", {})).items()}
        return cls(
            tool_version=str(d.get("tool_version", "")),
            format_version=fv,
            created=str(d.get("created", "")),
            updated=str(d.get("updated", "")),
            label=str(d.get("label", "")),
            media=str(d.get("media", "")),
            redundancy_percent=int(d.get("redundancy_percent", 10)),
            files=files,
            parity=parity,
            notes=[str(n) for n in d.get("notes", [])],
        )

    def content_bytes(self) -> bytes:
        return (json.dumps(self.to_json(), indent=1, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_if_changed(path: str, data: bytes, mode: int = 0o644) -> bool:
    """Write *data* to *path* unless the file already holds exactly that. Returns True if written."""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    atomic_write_bytes(path, data, mode)
    return True


def load_manifest(path: str) -> Optional[Manifest]:
    """Return the manifest, None if absent. Raises ManifestError if unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Manifest.from_json(json.load(f))
    except ManifestError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ManifestError(f"cannot read {path}: {e}") from e


def save_manifest(path: str, manifest: Manifest) -> bool:
    return write_if_changed(path, manifest.content_bytes())


# --- SHA256SUMS -------------------------------------------------------------

def _escape_name(name: str) -> Tuple[str, bool]:
    """GNU coreutils escaping: names with newline or backslash are written with
    those characters escaped and the line prefixed by a backslash."""
    if "\n" in name or "\\" in name or "\r" in name:
        return name.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r"), True
    return name, False


def _unescape_name(name: str) -> str:
    out = []
    i = 0
    while i < len(name):
        c = name[i]
        if c == "\\" and i + 1 < len(name):
            nxt = name[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "r":
                out.append("\r"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
        out.append(c)
        i += 1
    return "".join(out)


def sums_content(files: Iterable[Tuple[str, str]]) -> bytes:
    """Render ``(relpath, sha256)`` pairs as SHA256SUMS text, sorted by path.

    Uses the binary-mode marker (``*``) that both GNU and BSD tools accept and
    that guarantees no newline translation on Windows.
    """
    lines = []
    for rel, digest in sorted(files):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"bad sha256 for {rel!r}")
        name, escaped = _escape_name(rel)
        lines.append(("\\" if escaped else "") + digest + " *" + name + "\n")
    # surrogateescape: a name that is not valid UTF-8 on disk (possible on
    # Linux) round-trips byte for byte instead of crashing the run
    return "".join(lines).encode("utf-8", "surrogateescape")


def parse_sums(text: str) -> Dict[str, str]:
    """Parse SHA256SUMS text into ``{relpath: sha256}``. Tolerates both the
    binary (``*``) and text (two spaces) markers and CRLF line endings.
    Splits on LF only: str.splitlines() would also split on FF, VT, NEL,
    U+2028 and U+2029, which are legal inside a file name."""
    out: Dict[str, str] = {}
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        escaped = line.startswith("\\")
        if escaped:
            line = line[1:]
        if len(line) < 66:
            raise ManifestError(f"malformed SHA256SUMS line: {raw!r}")
        digest, rest = line[:64], line[64:]
        if any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ManifestError(f"malformed SHA256SUMS line: {raw!r}")
        if rest.startswith(" *") or rest.startswith("  "):
            name = rest[2:]
        elif rest.startswith(" "):
            name = rest[1:]
        else:
            raise ManifestError(f"malformed SHA256SUMS line: {raw!r}")
        if escaped:
            name = _unescape_name(name)
        out[name] = digest.lower()
    return out


def read_sums(path: str) -> Dict[str, str]:
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return parse_sums(f.read())
