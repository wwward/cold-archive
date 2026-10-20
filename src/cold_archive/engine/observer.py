"""Progress, logging and cancellation plumbing shared by CLI and GUI."""
from __future__ import annotations

import logging
import threading
from typing import Optional


class Cancelled(Exception):
    """Raised inside an engine operation when the observer requests a stop."""


class Observer:
    """Base observer. Engine code calls these; front ends override them.

    ``phase`` announces a new stage; ``progress`` reports work within it (in
    the unit given to ``phase``); ``log`` carries human-readable lines;
    ``check_cancel`` must raise :class:`Cancelled` to stop the operation.
    """

    def phase(self, name: str, total: Optional[int] = None, unit: str = "") -> None:  # noqa: D401
        pass

    def progress(self, done: int, total: Optional[int] = None, detail: str = "") -> None:
        pass

    def log(self, level: str, message: str) -> None:
        pass

    def check_cancel(self) -> None:
        pass

    # Convenience wrappers
    def info(self, message: str) -> None:
        self.log("info", message)

    def warning(self, message: str) -> None:
        self.log("warning", message)

    def error(self, message: str) -> None:
        self.log("error", message)


class NullObserver(Observer):
    pass


class LoggingObserver(Observer):
    """Routes observer events to the ``logging`` module; cancel via an Event."""

    def __init__(self, logger: Optional[logging.Logger] = None, cancel_event: Optional[threading.Event] = None):
        self.logger = logger or logging.getLogger("cold_archive")
        self.cancel_event = cancel_event or threading.Event()
        self._phase = ""
        self._last_pct = -1

    def phase(self, name: str, total: Optional[int] = None, unit: str = "") -> None:
        self._phase = name
        self._last_pct = -1
        self.logger.info("== %s", name)

    def progress(self, done: int, total: Optional[int] = None, detail: str = "") -> None:
        if total:
            pct = int(done * 100 / total)
            # Log at 10 % steps to keep the log readable.
            if pct // 10 != self._last_pct // 10:
                self._last_pct = pct
                self.logger.info("%s: %d%% %s", self._phase, pct, detail)

    def log(self, level: str, message: str) -> None:
        getattr(self.logger, level if level in ("info", "warning", "error", "debug") else "info")(message)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()
