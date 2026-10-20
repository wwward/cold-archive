"""Cold Archive: the Mac application.

A single window with five tasks a non-technical person can run with clicks
alone: create a new archive, check an archive, repair an archive, rescue a
failing drive, protect a folder in place. Archives on connected drives are
offered as buttons, and the Mac is kept awake while a job runs.
Everything long-running happens on a worker thread with a progress page and
a Cancel button; results land on a page with a plain-language verdict, the
full report, and buttons to reveal the archive or open the report.
"""
from __future__ import annotations

import dataclasses

import os
import sys
import time
from typing import Tuple, Any, Callable, Optional

from PySide6.QtCore import QSettings, QSize, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSpinBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, BUNDLE_ID, __version__
from ..engine import par2 as p2
from ..engine import resources
from ..engine.create import create_archive
from ..engine.layout import human_bytes
from ..engine.logs import log_dir, setup_logging, write_report
from ..engine.observer import Observer
from ..engine.preflight import PreflightReport, run_preflight
from ..engine.protect import ProtectOptions, protect
from ..engine.repair import RepairOptions, repair
from ..engine.rescue import rescue_copy
from ..engine.verify import VerifyOptions, verify
from ..engine.volumes import describe as describe_volume_path
from ..engine.volumes import find_archives
from .worker import EngineWorker, KeepAwake, QtObserver

MEDIA_CHOICES = [
    ("External hard drive (HDD)", "hdd"),
    ("SSD or flash drive", "ssd"),
    ("Blu-ray or M-DISC", "bluray"),
    ("DVD", "dvd"),
    ("LTO tape", "tape"),
    ("Cloud or network storage", "cloud"),
    ("Other / not sure", ""),
]

GREEN, ORANGE, RED = "#1e8e3e", "#c26a00", "#c5221f"


def _resource_path(name: str) -> Optional[str]:
    """A file shipped with the app (icon), whether frozen or run from source."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "resources", name))
        candidates.append(os.path.join(os.path.dirname(base), "Resources", name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "resources", name))
    candidates.append(os.path.join(here, "..", "..", "..", "packaging", name))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


class FolderPicker(QWidget):
    """A path field with a Choose... button; folder-only, remembers the last choice."""

    def __init__(self, settings_key: str, placeholder: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings_key = settings_key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.button = QPushButton("Choose…")
        self.button.clicked.connect(self.choose)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.button)
        last = QSettings().value(settings_key, "")
        if isinstance(last, str) and os.path.isdir(last):
            self.edit.setText(last)

    def choose(self) -> None:
        start = self.edit.text() if os.path.isdir(self.edit.text()) else os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "Choose a folder", start,
                                                QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if path:
            self.edit.setText(path)
            QSettings().setValue(self.settings_key, path)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, path: str) -> None:
        self.edit.setText(path)
        QSettings().setValue(self.settings_key, path)


def _find_archives_quickly(timeout: float = 2.0) -> list:
    """find_archives on a helper thread with a time limit: listing a struggling
    or sleeping drive can hang, and the window must never freeze over it."""
    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(find_archives)
    try:
        return fut.result(timeout=timeout)
    except Exception:  # noqa: BLE001 - a timeout or an unreadable /Volumes just means no shortcuts
        return []
    finally:
        ex.shutdown(wait=False)


class ArchiveShortcuts(QWidget):
    """One button per Cold Archive found on a connected drive; a click fills
    the folder field, so nobody has to find the right folder in a dialog."""

    def __init__(self, picker: FolderPicker, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.picker = picker
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.found: list = []

    def refresh(self) -> None:
        while self.lay.count():
            item = self.lay.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.found = _find_archives_quickly()
        if not self.found:
            self.lay.addWidget(_hint("No archive found on the connected drives. Connect the drive, or click Choose…"))
        for path in self.found[:4]:
            b = QPushButton(describe_volume_path(path))
            b.setToolTip(path)
            b.clicked.connect(lambda _checked=False, p=path: self.picker.set_path(p))
            self.lay.addWidget(b)
        self.lay.addStretch(1)
        if len(self.found) == 1 and not os.path.isdir(self.picker.path()):
            self.picker.set_path(self.found[0])


def _title(text: str) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setPointSize(f.pointSize() + 8)
    f.setBold(True)
    lbl.setFont(f)
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: palette(mid);")
    return lbl


def _app_copy_checkbox() -> QCheckBox:
    """Every archive gets the par2 source in TOOLS/; a copy of this app is optional
    (about 50 MB, Mac only) and only possible when running from the installed .app."""
    cb = QCheckBox("Also put a copy of this app in the archive's TOOLS folder (about 50 MB, Mac only)")
    if resources.app_bundle() is None:
        cb.setEnabled(False)
        cb.setToolTip("Available when Cold Archive runs from the installed app.")
    return cb


def _app_copy_checkbox_on_for_new_archives() -> QCheckBox:
    """For a new archive, carrying the app is the default: whoever receives the
    drive can then check and repair it with clicks, even if downloads are gone."""
    cb = _app_copy_checkbox()
    cb.setChecked(cb.isEnabled())
    return cb


def _big_button(title: str, subtitle: str, on_click: Callable[[], None]) -> QPushButton:
    b = QPushButton(f"{title}\n{subtitle}")
    b.setMinimumHeight(64)
    b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    b.clicked.connect(on_click)
    return b


class HomePage(QWidget):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(12)
        lay.addWidget(_title(APP_NAME))
        lay.addWidget(_hint("Keeps a folder of files safe for the long term: every file gets a fingerprint, every folder "
                            "gets repair data, and a READ-ME-FIRST explains it all to whoever opens the archive years from now."))
        lay.addSpacing(8)
        lay.addWidget(_big_button("Create a new archive", "Copy a folder onto a drive or disc folder and protect it", win.show_create))
        lay.addWidget(_big_button("Check an archive", "Find out whether every file is still exactly as it was", win.show_check))
        lay.addWidget(_big_button("Repair an archive", "Rebuild damaged or missing files from the repair data", win.show_repair))
        lay.addWidget(_big_button("Rescue a failing drive", "Copy everything readable onto a healthy drive, then repair the copy", win.show_rescue))
        lay.addWidget(_big_button("Protect a folder in place", "Add fingerprints and repair data to files that are already where you want them", win.show_protect))
        lay.addStretch(1)
        self.status = _hint("")
        lay.addWidget(self.status)
        self.refresh_status()

    _version_cache: dict = {}

    def refresh_status(self) -> None:
        try:
            exe = p2.find_par2()
            where = "bundled with the app" if getattr(sys, "_MEIPASS", None) and exe.startswith(os.path.dirname(sys._MEIPASS)) else exe
            # par2 -V is a subprocess with a 20 s timeout: run it once per binary, not on every return to this page
            if exe not in self._version_cache:
                self._version_cache[exe] = p2.par2_version(exe)
            self.status.setText(f"Repair engine: {self._version_cache[exe]} ({where}). Version {__version__}.")
        except p2.Par2NotFound:
            self.status.setText("The par2 repair engine was not found. Creating, protecting and repairing need it; "
                                "checking fingerprints still works. Install it with: brew install par2")
            self.status.setStyleSheet(f"color: {RED};")


class TaskPage(QWidget):
    """Base for the four setup pages: a form, Back and Start."""

    def __init__(self, win: "MainWindow", title: str, hint: str) -> None:
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.addWidget(_title(title))
        lay.addWidget(_hint(hint))
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        lay.addLayout(self.form)
        lay.addStretch(1)
        row = QHBoxLayout()
        back = QPushButton("Back")
        back.clicked.connect(win.show_home)
        self.start = QPushButton("Start")
        self.start.setDefault(True)
        self.start.clicked.connect(self.on_start)
        row.addWidget(back)
        row.addStretch(1)
        row.addWidget(self.start)
        lay.addLayout(row)

    def on_start(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def need_folder(self, picker: FolderPicker, what: str) -> Optional[str]:
        p = picker.path()
        if not p or not os.path.isdir(p):
            QMessageBox.warning(self, APP_NAME, f"Please choose {what}.")
            return None
        return p


def _media_combo() -> QComboBox:
    c = QComboBox()
    for label, key in MEDIA_CHOICES:
        c.addItem(label, key)
    return c


def _redundancy_spin() -> QSpinBox:
    s = QSpinBox()
    s.setRange(1, 50)
    s.setValue(int(QSettings().value("redundancy", 10)))
    s.setSuffix(" %")
    return s


class CreatePage(TaskPage):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win, "Create a new archive",
                         "Fingerprints, repair data and a READ-ME-FIRST are first written into the folder where it is, "
                         "so it becomes a checkable archive itself; then everything is copied to the destination and every "
                         "file is read back and compared. Your files are never modified. If the folder was recorded "
                         "before and files have changed since, you are asked before anything is re-recorded.")
        self.source = FolderPicker("last_source", "The folder you want to archive")
        self.target = FolderPicker("last_target", "An empty folder on the drive or disc image that will hold the archive")
        self.label = QLineEdit()
        self.label.setPlaceholderText("A name for this archive, for example: Family photos 1998-2025")
        self.media = _media_combo()
        self.redundancy = _redundancy_spin()
        self.form.addRow("Folder to archive:", self.source)
        self.form.addRow("Destination:", self.target)
        self.form.addRow("Archive name:", self.label)
        self.form.addRow("Stored on:", self.media)
        self.form.addRow("Repair data:", self.redundancy)
        self.form.addRow("", _hint("10 % means each folder can lose up to a tenth of its contents, in any pattern, and still be "
                                   "rebuilt. More costs more space and time; it does not protect against losing a whole drive. "
                                   "Keep a second copy."))
        self.include_app = _app_copy_checkbox_on_for_new_archives()
        self.form.addRow("", self.include_app)
        self.form.addRow("", _hint("The archive always carries the source code of the par2 repair program in a TOOLS folder, "
                                   "so it can be rebuilt on any computer years from now."))
        self.start.setText("Check and start")

    def on_start(self) -> None:
        src = self.need_folder(self.source, "the folder to archive")
        if not src:
            return
        dst = self.target.path()
        if not dst:
            QMessageBox.warning(self, APP_NAME, "Please choose a destination folder.")
            return
        QSettings().setValue("redundancy", self.redundancy.value())
        red = self.redundancy.value()
        label = self.label.text().strip() or os.path.basename(src.rstrip(os.sep))
        media = self.media.currentData()
        include_app = self.include_app.isChecked()

        # The source is protected where it is, then copied (engine/create.py).
        # Nothing is accepted by default: if the source was recorded before and
        # files changed since, the job stops and the person is asked.
        opts = ProtectOptions(redundancy_percent=red, label=label, media_note=media,
                              accept_removals=False, accept_changes=False, include_app=include_app)
        self.win.preflight_then(src, dst, red, self, lambda: self.win.start_create(src, dst, opts))


class ProtectPage(TaskPage):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win, "Protect a folder in place",
                         "Adds fingerprints, repair data and a READ-ME-FIRST to a folder where it already is. Run it again "
                         "after adding files: only what changed is redone. Files are never modified.")
        self.folder = FolderPicker("last_protect", "The folder to protect")
        self.label = QLineEdit()
        self.label.setPlaceholderText("A name for this archive (kept from last time if left empty)")
        self.media = _media_combo()
        self.redundancy = _redundancy_spin()
        self.full_rehash = QCheckBox("Re-read every file, even unchanged ones (slow; finds silent corruption)")
        self.rebuild = QCheckBox("Rebuild all repair data from scratch (only after a thorough Check passed)")
        self.include_app = _app_copy_checkbox()
        self.form.addRow("Folder:", self.folder)
        self.form.addRow("Archive name:", self.label)
        self.form.addRow("Stored on:", self.media)
        self.form.addRow("Repair data:", self.redundancy)
        self.form.addRow("", self.full_rehash)
        self.form.addRow("", self.rebuild)
        self.form.addRow("", self.include_app)

    def on_start(self) -> None:
        folder = self.need_folder(self.folder, "the folder to protect")
        if not folder:
            return
        QSettings().setValue("redundancy", self.redundancy.value())
        opts = ProtectOptions(redundancy_percent=self.redundancy.value(), label=self.label.text().strip(),
                              media_note=self.media.currentData(), accept_removals=False,
                              accept_changes=False, include_app=self.include_app.isChecked(),
                              full_rehash=self.full_rehash.isChecked(), rebuild_parity=self.rebuild.isChecked())
        self.win.last_protect = (folder, opts)
        self.win.preflight_then(folder, None, self.redundancy.value(), self,
                                lambda: self.win.run_job("Protecting the folder", folder, lambda obs: ("protect", protect(folder, opts, obs))))


class CheckPage(TaskPage):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win, "Check an archive",
                         "Reads every file and compares it with its recorded fingerprint, then checks the repair data. "
                         "Nothing is changed. Do this once a year or two, and before trusting a copy.")
        self.folder = FolderPicker("last_check", "The top folder of the archive (where READ-ME-FIRST.txt is)")
        self.shortcuts = ArchiveShortcuts(self.folder)
        self.form.addRow("Archive:", self.folder)
        self.form.addRow("On connected drives:", self.shortcuts)
        self.standard = QRadioButton("Standard: every file and every parity file (recommended)")
        self.thorough = QRadioButton("Thorough: standard, plus a second independent check of every folder by par2 (twice as long)")
        self.quick = QRadioButton("Quick: fingerprints only")
        self.standard.setChecked(True)
        for r in (self.standard, self.thorough, self.quick):
            self.form.addRow("", r)

    def on_start(self) -> None:
        folder = self.need_folder(self.folder, "the archive folder")
        if not folder:
            return
        mode = "thorough" if self.thorough.isChecked() else ("hashes" if self.quick.isChecked() else "standard")
        self.win.run_job("Checking the archive", folder, lambda obs: ("check", verify(folder, VerifyOptions(mode=mode), obs)))


class RepairPage(TaskPage):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win, "Repair an archive",
                         "Finds damaged or missing files, rebuilds them from the repair data, and checks the result. "
                         "If the drive itself is failing, copy the archive to a healthy disk first and repair the copy.")
        self.folder = FolderPicker("last_repair", "The top folder of the archive")
        self.shortcuts = ArchiveShortcuts(self.folder)
        self.keep_backups = QCheckBox("Keep the damaged originals next to the repaired files (name ending in .1)")
        self.form.addRow("Archive:", self.folder)
        self.form.addRow("On connected drives:", self.shortcuts)
        self.form.addRow("", self.keep_backups)

    def on_start(self) -> None:
        folder = self.need_folder(self.folder, "the archive folder")
        if not folder:
            return
        opts = RepairOptions(remove_backups=not self.keep_backups.isChecked())
        self.win.run_job("Repairing the archive", folder, lambda obs: ("repair", repair(folder, opts, obs)))


class RescuePage(TaskPage):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win, "Rescue a failing drive",
                         "For a drive that is slow, shows errors, or makes odd sounds but still appears. Everything that "
                         "can be read is copied onto a healthy drive, including the repair data; parts that cannot be "
                         "read are skipped and noted. Nothing on the failing drive is changed. Then repair the copy. "
                         "If the drive clicks or grinds, stop: unplug it and take it to a data-recovery service.")
        self.source = FolderPicker("last_rescue_source", "The archive on the failing drive (the folder with READ-ME-FIRST.txt)")
        self.shortcuts = ArchiveShortcuts(self.source)
        self.target = FolderPicker("last_rescue_target", "An empty folder on a healthy drive, at least as large")
        self.form.addRow("Rescue:", self.source)
        self.form.addRow("On connected drives:", self.shortcuts)
        self.form.addRow("Copy to:", self.target)
        self.form.addRow("", _hint("Make the empty folder on the healthy drive first: in Finder, open the drive and choose "
                                   "File > New Folder. If the rescue is interrupted, start it again with the same choices; "
                                   "what was already copied is kept."))

    def on_start(self) -> None:
        src = self.need_folder(self.source, "the archive on the failing drive")
        if not src:
            return
        dst = self.need_folder(self.target, "an empty folder on a healthy drive")
        if not dst:
            return
        self.win.run_job("Rescuing the drive", dst, lambda obs: ("rescue", rescue_copy(src, dst, obs)))


class RunningPage(QWidget):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 24)
        self.title = _title("Working…")
        lay.addWidget(self.title)
        self.phase = QLabel("")
        f = self.phase.font()
        f.setPointSize(f.pointSize() + 2)
        self.phase.setFont(f)
        lay.addWidget(self.phase)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.detail = _hint("")
        lay.addWidget(self.detail)
        self.eta = _hint("")
        lay.addWidget(self.eta)
        lay.addWidget(_hint("Your Mac stays awake while this runs. Keep a laptop plugged in with its lid open, "
                            "and leave the drive connected."))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFont(QFont("Menlo", 11))
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(win.cancel_job)
        row.addWidget(self.cancel)
        lay.addLayout(row)
        self._unit = ""
        self._t0 = 0.0
        self._phase_t0 = 0.0

    def reset(self, title: str) -> None:
        self.title.setText(title)
        self.phase.setText("Starting…")
        self.bar.setRange(0, 0)
        self.detail.setText("")
        self.eta.setText("")
        self.log.clear()
        self.cancel.setEnabled(True)
        self.cancel.setText("Cancel")
        self._t0 = time.monotonic()

    def on_phase(self, name: str, total: int, unit: str) -> None:
        self.phase.setText(name)
        self._unit = unit
        self._phase_t0 = time.monotonic()
        self.bar.setRange(0, 1000 if total else 0)
        self.bar.setValue(0)
        self.detail.setText("")
        self.log.appendPlainText(f"== {name}")

    def on_progress(self, done: int, total: int, detail: str) -> None:
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(done * 1000 / total)))
            what = f"{human_bytes(done)} of {human_bytes(total)}" if self._unit == "bytes" else f"{done} of {total}"
            elapsed = time.monotonic() - self._phase_t0
            eta = ""
            if done > 0 and elapsed > 2 and done < total:
                remaining = elapsed * (total - done) / done
                eta = f"  ·  about {_fmt_seconds(remaining)} left in this step"
            self.detail.setText(f"{what}{eta}")
        else:
            self.detail.setText(detail)
        if detail:
            self.eta.setText(detail[:120])

    def on_log(self, level: str, message: str) -> None:
        prefix = {"warning": "warning: ", "error": "ERROR: "}.get(level, "")
        self.log.appendPlainText(prefix + message)


def _fmt_seconds(s: float) -> str:
    s = int(s)
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{s // 60} minutes"
    return f"{s / 3600:.1f} hours"


class ResultPage(QWidget):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 24)
        self.verdict = _title("")
        self.verdict.setWordWrap(True)
        lay.addWidget(self.verdict)
        self.explain = _hint("")
        lay.addWidget(self.explain)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Menlo", 11))
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.reveal = QPushButton("Show in Finder")
        self.reveal.clicked.connect(self.on_reveal)
        self.open_report = QPushButton("Open report")
        self.open_report.clicked.connect(self.on_open_report)
        self.repair_now = QPushButton("Repair now")
        self.repair_now.clicked.connect(self.on_repair_now)
        self.protect_now = QPushButton("Protect now")
        self.protect_now.clicked.connect(self.on_protect_now)
        done = QPushButton("Done")
        done.setDefault(True)
        done.clicked.connect(win.show_home)
        row.addWidget(self.reveal)
        row.addWidget(self.open_report)
        row.addStretch(1)
        row.addWidget(self.repair_now)
        row.addWidget(self.protect_now)
        row.addWidget(done)
        lay.addLayout(row)
        self.folder = ""
        self.report_path = ""

    def show_result(self, kind: str, folder: str, payload: Any) -> None:
        self.folder = folder
        text, verdict, headline, explain = _describe(kind, payload)
        self.text.setPlainText(text)
        color = {"ok": GREEN, "attention": ORANGE}.get(verdict, RED)
        self.verdict.setText(headline)
        self.verdict.setStyleSheet(f"color: {color};")
        self.explain.setText(explain)
        self.report_path = write_report(kind, folder, text)
        rescued = kind == "rescue" and not getattr(payload, "refused", "") and getattr(payload, "is_archive", False)
        self.repair_now.setText("Repair the copy now" if rescued else "Repair now")
        self.repair_now.setVisible(rescued or (kind == "check" and verdict == "problem" and _repairable(payload)))
        self.protect_now.setVisible(kind in ("check", "repair") and verdict == "attention")

    def show_error(self, kind: str, folder: str, tb: str) -> None:
        self.folder = folder
        self.verdict.setText("Something went wrong")
        self.verdict.setStyleSheet(f"color: {RED};")
        self.explain.setText("The job stopped with an error. Nothing that was already written has been removed. "
                             "The details below are also in the log.")
        self.text.setPlainText(tb)
        self.report_path = write_report(kind + "-error", folder, tb)
        self.repair_now.setVisible(False)
        self.protect_now.setVisible(False)

    def show_cancelled(self, kind: str, folder: str) -> None:
        self.folder = folder
        self.verdict.setText("Cancelled")
        self.verdict.setStyleSheet(f"color: {ORANGE};")
        self.explain.setText("The job was stopped. The archive is in a consistent but incomplete state; "
                             "running the same task again will pick up where it left off.")
        self.text.setPlainText("")
        self.report_path = ""
        self.repair_now.setVisible(False)
        self.protect_now.setVisible(False)

    def on_reveal(self) -> None:
        if self.folder and os.path.isdir(self.folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder))

    def on_open_report(self) -> None:
        if self.report_path and os.path.isfile(self.report_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.report_path))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir()))

    def on_repair_now(self) -> None:
        folder = self.folder
        self.win.run_job("Repairing the archive", folder, lambda obs: ("repair", repair(folder, RepairOptions(), obs)))

    def on_protect_now(self) -> None:
        folder = self.folder
        self.win.run_job("Protecting the folder", folder, lambda obs: ("protect", protect(folder, ProtectOptions(), obs)))


def _repairable(payload: Any) -> bool:
    v = payload
    return any(s.status == "repairable" for s in getattr(v, "sets", [])) and not any(
        s.status in ("unrepairable", "unusable") for s in getattr(v, "sets", []))


def _consented(opts: ProtectOptions, res: Any) -> ProtectOptions:
    """The options for a re-run after the person said yes: exactly the files
    the stopped run listed are accepted (unioned with any earlier answer),
    never a blanket True."""
    new_removals = set(res.files_removed) if res.files_removed else False
    if isinstance(opts.accept_removals, set):
        new_removals = new_removals.union(opts.accept_removals) if new_removals else opts.accept_removals
    new_changes = set(res.files_updated) if res.files_updated else False
    if isinstance(opts.accept_changes, set):
        new_changes = new_changes.union(opts.accept_changes) if new_changes else opts.accept_changes
    return dataclasses.replace(opts, accept_removals=opts.accept_removals if opts.accept_removals is True else new_removals,
                               accept_changes=opts.accept_changes if opts.accept_changes is True else new_changes)


def _describe(kind: str, payload: Any):
    """(report text, verdict, headline, explanation) for the result page."""
    if kind == "create":
        r = payload
        p, c = r.protect, r.copy
        if r.needs_consent:
            return (r.summary(), "attention", "Nothing was written: the source folder has changed since it was last recorded",
                    "Some files differ from the folder's records or are gone, and nothing was copied. If you changed them "
                    "on purpose, run Create again and answer Yes when asked. If you did not, run Check on the source "
                    "folder, then Repair.")
        if c is None:
            return (r.summary(), "problem", "The protection step did not complete",
                    "Could not finish calculating fingerprints and parity on the source folder, so nothing was copied. "
                    "Read the report, fix the cause and run Create again.")
        if not c.ok:
            return (r.summary(), "problem", "The copy did not complete",
                    "Some files could not be copied or did not read back correctly. The source folder is protected; "
                    "fix the cause (space, a bad cable, a failing drive) and run Create again; files already copied are skipped.")
        if r.source_mismatches:
            return (r.summary(), "problem", "The source folder has silent corruption",
                    f"{len(r.source_mismatches)} files read differently during the copy than their fingerprints say. "
                    "The copy carries what was read. Run Check, then Repair, on the source folder and on the copy, "
                    "then run Create again.")
        if r.ok:
            return (r.summary(), "ok", "Your archive is ready",
                    f"{p.files_total:,} files ({human_bytes(p.bytes_total)}) were fingerprinted and given repair data where they "
                    "are, then copied and read back. Before handing the drive over: eject it, plug it in again and run "
                    "Check on it. Open READ-ME-FIRST.txt in the archive to see what a future reader will see.")
        return (r.summary(), "problem", "The archive is not fully protected or copied", "Read the report.")
    if kind == "protect":
        p = payload
        if p.needs_consent:
            return (p.summary(), "attention", "Nothing was changed: your decision is needed",
                    "Some files differ from the records or are gone. If that was you, run Protect again with the matching "
                    "\"on purpose\" box ticked. If it was not you, run Check, then Repair.")
        if p.ok:
            if p.files_removed or p.files_updated:
                return (p.summary(), "attention", "The folder is protected (records were updated)",
                        f"Accepted changes: {len(p.files_removed)} files removed, {len(p.files_updated)} changed. "
                        f"{p.count('created')} parity sets built, {p.count('kept')} were already current.")
            return (p.summary(), "ok", "The folder is protected",
                    f"{p.count('created')} parity sets built, {p.count('kept')} were already current.")
        return (p.summary(), "problem", "The folder is not fully protected",
                "Nothing was laundered: folders with missing or suspect files kept their old repair data. Read the report.")
    if kind == "check":
        v = payload
        head = {"ok": "Everything is exactly as it was", "attention": "All files are intact, but something needs attention",
                "problem": "Damage was found"}[v.verdict]
        expl = {"ok": f"{v.files_checked:,} files checked; every fingerprint and every parity set is intact.",
                "attention": "The files are fine. The repair data or the records need refreshing: run Protect on this folder.",
                "problem": ("Repair can fix this: press Repair now." if _repairable(v) else
                            "Some damage is beyond what the repair data can rebuild. Restore those folders from another copy.")}[v.verdict]
        return (v.summary(), v.verdict, head, expl)
    if kind == "rescue":
        r = payload
        if r.refused:
            return (r.summary(), "problem", "The rescue did not start", r.refused)
        if not r.is_archive:
            return (r.summary(), "attention", "Copied what could be read",
                    "This folder has no Cold Archive fingerprints or repair data, so damaged files cannot be rebuilt. "
                    "The copy holds everything that could be read; the report lists what could not.")
        if not r.lost_anything:
            return (r.summary(), "ok", "Everything was copied",
                    f"{r.files_copied + r.files_skipped:,} files are on the healthy drive. Next, click Repair the copy now: "
                    "it checks every file against its fingerprint and fixes anything that was read wrongly.")
        return (r.summary(), "attention", "Copied, with damage: repair the copy next",
                f"{len(r.partial):,} files had unreadable parts and {len(r.unreadable):,} could not be read at all. "
                "Click Repair the copy now: it rebuilds them from the repair data, as long as no folder lost more than "
                "its repair data can cover. Keep the failing drive unplugged; a data-recovery service may still need it.")
    if kind == "repair":
        r = payload
        head = {"ok": "Repaired: everything is exactly as it was", "attention": "Repaired, but something needs attention",
                "problem": "Not everything could be repaired"}[r.verdict]
        expl = {"ok": f"{len(r.repaired)} files were rebuilt and verified against their fingerprints." if r.repaired else "Nothing needed repair.",
                "attention": ("Some files may have been changed on purpose after the last Protect; the recorded version was restored and "
                              "your version kept next to it (name ending in .1). Read the report." if r.changed_kept else
                              "The files are fine now. Read the report for what still needs attention."),
                "problem": "Files listed as not repaired must be restored from another copy of the archive."}[r.verdict]
        text = r.summary() + ("\n\nState after repair:\n" + r.after.summary() if r.after is not None else "")
        return (text, r.verdict, head, expl)
    return (str(payload), "attention", "Finished", "")


class MainWindow(QMainWindow):
    last_protect: Optional[Tuple[str, ProtectOptions]] = None
    last_create: Optional[Tuple[str, str, ProtectOptions]] = None

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(760, 560)
        self.setMinimumSize(QSize(640, 480))
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.home = HomePage(self)
        self.create_page = CreatePage(self)
        self.protect_page = ProtectPage(self)
        self.check_page = CheckPage(self)
        self.repair_page = RepairPage(self)
        self.rescue_page = RescuePage(self)
        self.running = RunningPage(self)
        self.result = ResultPage(self)
        for w in (self.home, self.create_page, self.protect_page, self.check_page, self.repair_page, self.rescue_page,
                  self.running, self.result):
            self.stack.addWidget(w)
        self.keep_awake = KeepAwake()
        self.worker: Optional[EngineWorker] = None
        self._job_active = False
        self.observer: Optional[QtObserver] = None
        self._job_kind = ""
        self._job_folder = ""
        self._preflight: tuple = (None, None)
        self._build_menus()
        self.show_home()

    # ---- navigation
    def show_home(self) -> None:
        self.home.refresh_status()
        self.stack.setCurrentWidget(self.home)

    def show_create(self) -> None:
        self.stack.setCurrentWidget(self.create_page)

    def show_protect(self) -> None:
        self.stack.setCurrentWidget(self.protect_page)

    def show_check(self) -> None:
        self.check_page.shortcuts.refresh()
        self.stack.setCurrentWidget(self.check_page)

    def show_repair(self) -> None:
        self.repair_page.shortcuts.refresh()
        self.stack.setCurrentWidget(self.repair_page)

    def show_rescue(self) -> None:
        self.rescue_page.shortcuts.refresh()
        self.stack.setCurrentWidget(self.rescue_page)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        app_menu = mb.addMenu(APP_NAME)
        about = QAction(f"About {APP_NAME}", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self.show_about)
        app_menu.addAction(about)
        quit_action = QAction("Quit " + APP_NAME, self)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        app_menu.addAction(quit_action)
        help_menu = mb.addMenu("Help")
        logs_action = QAction("Show log folder", self)
        logs_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir())))
        help_menu.addAction(logs_action)
        home_action = QAction("Home", self)
        home_action.setShortcut("Ctrl+H")
        home_action.triggered.connect(self.show_home)
        help_menu.addAction(home_action)

    def show_about(self) -> None:
        try:
            exe = p2.find_par2()
            engine = f"{p2.par2_version(exe)}\n{exe}"
        except p2.Par2NotFound:
            engine = "par2 not found"
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<b>{APP_NAME} {__version__}</b><br>Long-term integrity protection for archived files: "
                          f"SHA-256 fingerprints plus PAR2 repair data, in open formats any computer can read.<br><br>"
                          f"Repair engine: {engine}<br>Logs and reports: {log_dir()}<br><br>"
                          "Cold Archive is free software under the Apache License 2.0. It bundles par2cmdline "
                          "(GPL, with its source), Qt and PySide6 (LGPL) and Python (PSF); the licences and "
                          "where to get each part's source are in the app's Resources/licenses folder, "
                          "THIRD-PARTY-NOTICES.md.")

    # ---- preflight
    def confirm_preflight(self, rep: PreflightReport) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Before we start")
        lay = QVBoxLayout(dlg)
        head = QLabel("Ready to go." if rep.ok else "This cannot start yet.")
        f = head.font()
        f.setBold(True)
        head.setFont(f)
        head.setStyleSheet(f"color: {GREEN if rep.ok else RED};")
        lay.addWidget(head)
        txt = QPlainTextEdit(rep.summary())
        txt.setReadOnly(True)
        txt.setFont(QFont("Menlo", 11))
        txt.setMinimumSize(620, 300)
        lay.addWidget(txt)
        buttons = QDialogButtonBox()
        if rep.ok:
            buttons.addButton("Start", QDialogButtonBox.ButtonRole.AcceptRole)
            buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        else:
            buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        return rep.ok and dlg.exec() == QDialog.DialogCode.Accepted

    def preflight_then(self, source: str, target: Optional[str], redundancy: int, page: QWidget, then: Callable[[], None]) -> None:
        """Run preflight on the worker thread (it scans the whole source), show
        the readiness dialog, and only then start the real job."""
        self._preflight = (page, then)
        self.run_job("Checking readiness", target or source,
                     lambda obs: ("preflight", run_preflight(source, target, redundancy, observer=obs)))

    # ---- jobs
    def run_job(self, title: str, folder: str, job: Callable[[Observer], Any]) -> None:
        if self._job_active:
            return
        old = self.worker
        if old is not None:
            # MEASURED (PySide6 6.11): a QThread whose C++ object was freed by
            # deleteLater makes every later isRunning() raise RuntimeError, so
            # the previous worker is only released here, after it has fully
            # exited, and the reference is dropped before the object goes.
            old.wait()
            self.worker = None
            old.deleteLater()
        self._job_active = True
        self._job_kind = title
        self._job_folder = folder
        self.observer = QtObserver()
        self.observer.phase_changed.connect(self.running.on_phase)
        self.observer.progress_changed.connect(self.running.on_progress)
        self.observer.log_line.connect(self.running.on_log)
        w = EngineWorker(job, self.observer, self)
        self.worker = w
        w.finished_ok.connect(self._job_done)
        w.finished_error.connect(self._job_error)
        w.finished_cancelled.connect(self._job_cancelled)
        w.finished.connect(lambda: self._worker_finished(w))   # a long session must not accumulate dead threads
        self.running.reset(title)
        self.stack.setCurrentWidget(self.running)
        self.keep_awake.start()
        w.start()

    def _worker_finished(self, w: EngineWorker) -> None:
        # Runs after the job's own finished_* slot (queued in emit order). Drop
        # the reference first, then let Qt free the thread object.
        if self.worker is w:
            self.worker = None
            w.deleteLater()

    def cancel_job(self) -> None:
        if self.worker is None or not self._job_active:
            return
        if QMessageBox.question(self, APP_NAME, "Stop the current job? Whatever is finished stays; the rest can be resumed later.") \
                == QMessageBox.StandardButton.Yes:
            self.running.cancel.setEnabled(False)
            self.running.cancel.setText("Stopping…")
            self.worker.cancel()

    def _job_done(self, payload: Any) -> None:
        self._job_active = False
        self.keep_awake.stop()
        kind = payload[0] if isinstance(payload, tuple) else "job"
        if kind == "preflight":
            page, then = self._preflight
            self._preflight = (None, None)
            self.stack.setCurrentWidget(page)
            if self.confirm_preflight(payload[1]):
                then()
            return
        data = payload[1] if isinstance(payload, tuple) else payload
        if kind == "protect" and getattr(data, "needs_consent", "") and self.last_protect is not None:
            folder, opts = self.last_protect
            if self.ask_consent(data):
                opts2 = _consented(opts, data)
                self.last_protect = (folder, opts2)
                self.run_job("Protecting the folder", folder, lambda obs: ("protect", protect(folder, opts2, obs)))
                return
        if kind == "create" and getattr(data, "needs_consent", "") and self.last_create is not None:
            src, dst, opts = self.last_create
            if self.ask_consent(data.protect):
                self.start_create(src, dst, _consented(opts, data.protect))
                return
        self.result.show_result(kind, self._job_folder, data)
        self.stack.setCurrentWidget(self.result)
        self._notify_done()

    def start_create(self, src: str, dst: str, opts: ProtectOptions) -> None:
        """Run Create with these options; remembered so a consent answer can re-run it."""
        self.last_create = (src, dst, opts)
        self.run_job("Creating the archive", dst, lambda obs: ("create", create_archive(src, dst, opts, obs)))

    def ask_consent(self, res: Any) -> bool:
        """Protect stopped because files were removed or changed. Only the person
        knows whether that was on purpose; ask, listing exactly which files."""
        parts = []
        if res.files_removed:
            parts.append(f"{len(res.files_removed)} recorded files are no longer in the folder:\n  " + "\n  ".join(res.files_removed[:12])
                         + ("\n  ..." if len(res.files_removed) > 12 else ""))
        if res.files_updated:
            parts.append(f"{len(res.files_updated)} files have different contents than the records:\n  " + "\n  ".join(res.files_updated[:12])
                         + ("\n  ..." if len(res.files_updated) > 12 else ""))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(APP_NAME)
        box.setText("Did you make these changes on purpose?")
        box.setInformativeText("\n\n".join(parts) + "\n\nIf yes, the records will be updated to match. If you did not change these files, "
                               "choose No and run Check: the difference may be damage that Repair can undo.")
        yes = box.addButton("Yes, I did: update the records", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("No, check the archive first", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def _job_error(self, tb: str) -> None:
        self._job_active = False
        self.keep_awake.stop()
        self.result.show_error(self._job_kind or "job", self._job_folder, tb)
        self.stack.setCurrentWidget(self.result)
        self._notify_done()

    def _job_cancelled(self) -> None:
        self._job_active = False
        self.keep_awake.stop()
        page, _then = self._preflight
        if page is not None:
            # the readiness check was stopped: nothing was started, so there
            # is no "incomplete archive" to talk about; back to the form
            self._preflight = (None, None)
            self.stack.setCurrentWidget(page)
            return
        self.result.show_cancelled(self._job_kind or "job", self._job_folder)
        self.stack.setCurrentWidget(self.result)

    def _notify_done(self) -> None:
        QApplication.alert(self)  # bounce the Dock icon if we are in the background

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None and self._job_active:
            if QMessageBox.question(self, APP_NAME, "A job is still running. Stop it and quit?") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(15000)
        event.accept()


def main() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("Billie Grace Ward")
    QApplication.setOrganizationDomain("billie.nyc")
    QApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    setup_logging(console=False)
    icon = _resource_path("ColdArchive.png")
    if icon:
        app.setWindowIcon(QIcon(QPixmap(icon)))
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
