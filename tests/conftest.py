"""Shared fixtures. Every test that touches par2 runs the real binary on real
files under tmp_path; nothing is mocked at the subprocess or filesystem seam,
because those seams are exactly where the previous version's bugs lived."""
from __future__ import annotations

import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from cold_archive.engine import par2 as p2  # noqa: E402


def _par2_available() -> bool:
    try:
        p2.find_par2()
        return True
    except p2.Par2NotFound:
        return False


needs_par2 = pytest.mark.skipif(not _par2_available(), reason="par2cmdline not installed")


@pytest.fixture(autouse=True)
def _no_tools_by_default(monkeypatch):
    """A source checkout may have the par2 tarball under build/; tests that
    want the TOOLS folder point at their own fixture files explicitly."""
    from cold_archive.engine import resources
    monkeypatch.setattr(resources, "par2_source_tarball", lambda: None)
    monkeypatch.setattr(resources, "par2_source_licence", lambda: None)
    monkeypatch.setattr(resources, "app_bundle", lambda: None)


@pytest.fixture(autouse=True)
def _logs_in_tmp(tmp_path, monkeypatch):
    """Reports and logs go under tmp_path, never in ~/Library/Logs during tests."""
    from cold_archive.engine import logs
    d = tmp_path / "_logs"
    d.mkdir()
    monkeypatch.setattr(logs, "log_dir", lambda: str(d))


def make_tree(root: str, *, seed: int = 1) -> dict:
    """A small but nasty source tree. Returns {rel: bytes} of payload files."""
    import random
    rnd = random.Random(seed)
    files = {
        "Photos/2019/IMG_001.jpg": bytes(rnd.getrandbits(8) for _ in range(400_000)),
        "Photos/2019/IMG_002.jpg": bytes(rnd.getrandbits(8) for _ in range(350_000)),
        "Photos/2019/héllo wörld ✓.jpg": bytes(rnd.getrandbits(8) for _ in range(120_000)),
        "Docs/-leading-dash.txt": b"dash\n" * 1000,
        "Docs/letter.txt": b"Dear trustee,\n" * 3000,
        "Docs/empty.txt": b"",
        "root file.bin": bytes(rnd.getrandbits(8) for _ in range(200_000)),
    }
    for rel, data in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    os.makedirs(os.path.join(root, "Empty folder"), exist_ok=True)
    with open(os.path.join(root, ".DS_Store"), "wb") as f:
        f.write(b"junk")
    with open(os.path.join(root, "Photos", "._IMG_001.jpg"), "wb") as f:
        f.write(b"appledouble")
    os.symlink("Docs/letter.txt", os.path.join(root, "link.txt"))
    return files


def rot(path: str, offset: int = 12345, length: int = 300) -> None:
    """Corrupt bytes in place while keeping size and mtime: what bit-rot looks like."""
    st = os.stat(path)
    with open(path, "r+b") as f:
        f.seek(offset)
        old = f.read(length)
        f.seek(offset)
        f.write(bytes(b ^ 0xFF for b in old))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def edit(path, data: bytes, seconds_later: float = 10.0) -> None:
    """Write new bytes the way a person's edit does: the date moves on. An
    edit that kept the size and landed within DATE_SLACK (2 s) of the recorded
    date would be indistinguishable from rot, by design (layout.same_date)."""
    import os as _os
    t = _os.stat(path).st_mtime
    with open(path, "wb") as f:
        f.write(data)
    _os.utime(path, (t + seconds_later, t + seconds_later))
