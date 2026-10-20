"""Log and report locations. Never inside an archive: a log that lives in the
archive changes on every run, gets hashed, and invalidates its own parity."""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import os
import sys

from .. import APP_NAME


def log_dir() -> str:
    if sys.platform == "darwin":
        d = os.path.join(os.path.expanduser("~"), "Library", "Logs", APP_NAME)
    elif os.name == "nt":
        d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME, "logs")
    else:
        d = os.path.join(os.path.expanduser("~"), ".cold-archive", "logs")
    os.makedirs(d, exist_ok=True)
    return d


def log_file() -> str:
    return os.path.join(log_dir(), "cold-archive.log")


def setup_logging(console: bool = True, level: int = logging.INFO) -> str:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = log_file()
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(ch)
    return path


def report_path(kind: str, archive_root: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = os.path.basename(os.path.abspath(archive_root)) or "archive"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base)[:60]
    return os.path.join(log_dir(), f"{kind}-{safe}-{stamp}.txt")


def write_report(kind: str, archive_root: str, text: str) -> str:
    p = report_path(kind, archive_root)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
    return p
