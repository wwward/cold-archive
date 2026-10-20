"""Bridge between the engine's Observer and Qt: runs one engine job on a
QThread, forwards progress as signals, and turns Cancel into a Cancelled
exception at the next check point."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..engine.observer import Cancelled, Observer


class QtObserver(QObject, Observer):
    phase_changed = Signal(str, object, str)        # name, total (0 = unknown), unit
    progress_changed = Signal(object, object, str)     # done, total, detail
    log_line = Signal(str, str)                  # level, message

    def __init__(self) -> None:
        QObject.__init__(self)
        self.cancel_event = threading.Event()
        self._last_emit = 0.0
        self._last_pct = -1

    def phase(self, name: str, total: Optional[int] = None, unit: str = "") -> None:
        self._last_pct = -1
        self._last_emit = 0.0
        self.phase_changed.emit(name, int(total or 0), unit)

    def progress(self, done: int, total: Optional[int] = None, detail: str = "") -> None:
        # Engines report very often; ~10 updates a second is plenty for a human.
        now = time.monotonic()
        pct = int(done * 1000 / total) if total else -1
        if now - self._last_emit < 0.1 and pct == self._last_pct and done != total:
            return
        self._last_emit = now
        self._last_pct = pct
        self.progress_changed.emit(int(done), int(total or 0), detail)

    def log(self, level: str, message: str) -> None:
        self.log_line.emit(level, message)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()


class EngineWorker(QThread):
    """Runs ``job(observer)`` off the UI thread."""
    finished_ok = Signal(object)      # the engine result object
    finished_error = Signal(str)      # traceback text
    finished_cancelled = Signal()

    def __init__(self, job: Callable[[Observer], Any], observer: QtObserver, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.job = job
        self.observer = observer
        self.result: Any = None

    def run(self) -> None:  # noqa: D401
        try:
            self.result = self.job(self.observer)
        except Cancelled:
            self.finished_cancelled.emit()
            return
        except BaseException:  # noqa: BLE001 - shown to the user, never swallowed
            self.finished_error.emit(traceback.format_exc())
            return
        self.finished_ok.emit(self.result)

    def cancel(self) -> None:
        self.observer.cancel_event.set()


class KeepAwake:
    """While a job runs, stop the Mac from idle-sleeping and its disks from
    spinning down; a check of a 1 TB drive takes hours, and a sleeping Mac
    simply pauses it, which nobody expects. Uses /usr/bin/caffeinate, which
    also ends by itself if the app quits (-w). A closed laptop lid still
    sleeps; the screen says so."""

    CAFFEINATE = "/usr/bin/caffeinate"

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self.proc is not None or sys.platform != "darwin" or not os.path.exists(self.CAFFEINATE):
            return
        try:
            self.proc = subprocess.Popen([self.CAFFEINATE, "-i", "-m", "-w", str(os.getpid())],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.terminate()
            p.wait(2)
        except Exception:  # noqa: BLE001 - best effort; caffeinate also exits with the app
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
