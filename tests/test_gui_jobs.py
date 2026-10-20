"""The GUI's job plumbing, offscreen (no window is ever shown on a desk).

Two measured PySide6 traps (2026-09-10): a Signal(int) is a C int, so byte
totals above 2 GiB wrap or arrive as 0; and a QThread freed by deleteLater
while the window still holds it makes the next isRunning() raise RuntimeError,
which limited the app to one job per launch."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from conftest import make_tree  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, until, timeout=20.0):
    t0 = time.time()
    while not until() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.01)
    assert until(), "timed out"


def test_progress_signals_carry_multi_terabyte_totals(qapp):
    from cold_archive.gui.worker import QtObserver
    o = QtObserver()
    got = []
    o.phase_changed.connect(lambda name, total, unit: got.append(("phase", total)))
    o.progress_changed.connect(lambda done, total, detail: got.append(("progress", done, total)))
    o.phase("Copying", total=4 * 2 ** 40, unit="bytes")
    o.progress(3 * 2 ** 40, 4 * 2 ** 40, "big.iso")
    qapp.processEvents()
    assert ("phase", 4 * 2 ** 40) in got and ("progress", 3 * 2 ** 40, 4 * 2 ** 40) in got, got


def test_two_jobs_in_one_launch(qapp, tmp_path):
    from cold_archive.engine.verify import VerifyOptions, verify
    from cold_archive.gui.app import MainWindow
    make_tree(str(tmp_path))
    win = MainWindow()
    seen = []
    orig = win._job_done
    win._job_done = lambda payload: (seen.append(payload[1].verdict), orig(payload))   # type: ignore[method-assign]
    for i in range(2):
        win.run_job(f"job {i}", str(tmp_path), lambda obs: ("verify", verify(str(tmp_path), VerifyOptions(mode="hashes"), obs)))
        _pump(qapp, lambda: len(seen) == i + 1)
    assert seen == ["problem", "problem"]          # never protected: the verdict is not the point, the second run is
    assert win.worker is None and not win._job_active
    win.close()


def test_create_asks_and_reruns_on_consent(qapp, tmp_path, monkeypatch):
    """Create on a source recorded before, with a file edited since: the job
    stops, the consent dialog is asked, and a Yes re-runs Create with exactly
    the listed files accepted (never a blanket True)."""
    import os
    from cold_archive.engine.layout import SUMS_FILE
    from cold_archive.engine.manifest import read_sums
    from cold_archive.engine.protect import ProtectOptions
    from cold_archive.gui.app import MainWindow
    from conftest import needs_par2  # noqa: F401
    from cold_archive.engine import par2 as p2
    try:
        p2.find_par2()
    except p2.Par2NotFound:
        pytest.skip("par2cmdline not installed")

    src, dst, dst2 = tmp_path / "src", tmp_path / "dst", tmp_path / "dst2"
    src.mkdir()
    make_tree(str(src))
    win = MainWindow()
    shown = []
    win.result.show_result = lambda kind, folder, payload: shown.append((kind, payload))   # type: ignore[method-assign]
    win.start_create(str(src), str(dst), ProtectOptions(label="T"))
    _pump(qapp, lambda: len(shown) == 1, timeout=60)
    assert shown[0][1].ok, shown[0][1].summary()
    old_hash = read_sums(str(src / SUMS_FILE))["Docs/letter.txt"]

    p = src / "Docs" / "letter.txt"
    st = os.stat(p)
    p.write_bytes(b"rewritten on purpose\n" * 3000)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 90 * 10 ** 9))

    asked = []
    win.ask_consent = lambda res: (asked.append(list(res.files_updated)), True)[1]   # type: ignore[method-assign]
    win.start_create(str(src), str(dst2), ProtectOptions(label="T"))
    _pump(qapp, lambda: len(shown) == 2, timeout=60)
    assert asked == [["Docs/letter.txt"]]
    assert win.last_create is not None and win.last_create[2].accept_changes == {"Docs/letter.txt"}
    assert win.last_create[2].accept_removals is False
    r = shown[1][1]
    assert r.ok and not r.needs_consent, r.summary()
    assert read_sums(str(src / SUMS_FILE))["Docs/letter.txt"] != old_hash
    assert read_sums(str(dst2 / SUMS_FILE))["Docs/letter.txt"] == read_sums(str(src / SUMS_FILE))["Docs/letter.txt"]
    assert win.worker is None and not win._job_active


def test_rescue_then_repair_the_copy_with_one_click(qapp, tmp_path, monkeypatch):
    """The rescue result offers 'Repair the copy now', and it repairs the COPY
    (the job's destination), never the failing drive."""
    from cold_archive.engine import par2 as p2
    from cold_archive.engine import rescue as rs
    from cold_archive.engine.protect import ProtectOptions, protect
    from cold_archive.gui import app as gui
    try:
        p2.find_par2()
    except p2.Par2NotFound:
        pytest.skip("par2cmdline not installed")
    src, dst = tmp_path / "failing", tmp_path / "healthy"
    src.mkdir()
    dst.mkdir()
    make_tree(str(src))
    assert protect(str(src), ProtectOptions()).ok
    real = rs.rescue_copy
    monkeypatch.setattr(gui, "rescue_copy", lambda s, d, obs: real(s, d, obs, allow_same_drive=True))
    win = gui.MainWindow()
    win.show_rescue()
    win.rescue_page.source.set_path(str(src))
    win.rescue_page.target.set_path(str(dst))
    win.rescue_page.on_start()
    _pump(qapp, lambda: win.stack.currentWidget() is win.result and not win._job_active, timeout=60)
    assert win.result.folder == str(dst)
    assert win.result.repair_now.text() == "Repair the copy now" and not win.result.repair_now.isHidden()
    repaired = []
    orig = win.result.show_result
    win.result.show_result = lambda kind, folder, payload: (repaired.append((kind, folder, payload.verdict)), orig(kind, folder, payload))   # type: ignore[method-assign]
    win.result.on_repair_now()
    _pump(qapp, lambda: len(repaired) == 1, timeout=60)
    assert repaired[0][:2] == ("repair", str(dst)) and repaired[0][2] == "ok"


def test_the_mac_is_kept_awake_only_while_a_job_runs(qapp, tmp_path, monkeypatch):
    from cold_archive.engine.verify import VerifyOptions, verify
    from cold_archive.gui import worker
    from cold_archive.gui.app import MainWindow
    events = []

    class FakeProc:
        def __init__(self, args, **kw):
            events.append(("start", args))

        def terminate(self):
            events.append(("stop",))

        def wait(self, timeout=None):
            return 0

    make_tree(str(tmp_path))
    monkeypatch.setattr(worker.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(worker.KeepAwake, "CAFFEINATE", "/bin/sh")      # any program that exists
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    win = MainWindow()
    done = []
    orig = win._job_done
    win._job_done = lambda payload: (done.append(1), orig(payload))   # type: ignore[method-assign]
    win.run_job("check", str(tmp_path), lambda obs: ("verify", verify(str(tmp_path), VerifyOptions(mode="hashes"), obs)))
    assert events and events[0][0] == "start" and "-i" in events[0][1] and "-w" in events[0][1]
    _pump(qapp, lambda: done)
    assert events[-1] == ("stop",) and win.keep_awake.proc is None


def test_an_archive_on_a_connected_drive_is_one_click_away(qapp, tmp_path, monkeypatch):
    from cold_archive.gui import app as gui
    arc = tmp_path / "Volumes" / "Shoebox"
    arc.mkdir(parents=True)
    monkeypatch.setattr(gui, "find_archives", lambda: [str(arc)])
    win = gui.MainWindow()
    win.check_page.folder.edit.setText("")
    win.show_check()
    assert win.check_page.folder.path() == str(arc)          # one archive found: filled in already
    buttons = [w for w in win.check_page.shortcuts.findChildren(QtWidgets.QPushButton)]
    assert len(buttons) == 1 and buttons[0].toolTip() == str(arc)
