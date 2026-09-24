"""
Tetti di volume: coda del Real-Time (debounce, deduplica, sovraccarico),
righe della pagina Scansione, log della scansione programmata scritto
man mano. I metodi reali di MainWindow/ScanPage girano su oggetti minimi:
istanziare la finestra intera richiederebbe clamd, QSettings e la tray.
"""
from __future__ import annotations

import os
import stat
from collections import deque
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QListWidget  # noqa: E402

from klamav_py.clamd_client import ScanResult  # noqa: E402
from klamav_py.gui import main_window as mw  # noqa: E402

app = QApplication.instance() or QApplication([])


class _Tray:
    def __init__(self):
        self.messages = []

    def showMessage(self, title, text, *a):
        self.messages.append(title)


class _RtPage:
    def __init__(self):
        self.entries = []

    def add_outcome_entry(self, text, warning):
        self.entries.append(text)


class _Timer:
    def __init__(self):
        self.active = False

    def isActive(self):
        return self.active

    def start(self):
        self.active = True

    def stop(self):
        self.active = False


class _Window:
    _schedule_realtime_scan = mw.MainWindow._schedule_realtime_scan
    _flush_pending_realtime = mw.MainWindow._flush_pending_realtime
    _queue_realtime_scan = mw.MainWindow._queue_realtime_scan
    _note_realtime_overflow = mw.MainWindow._note_realtime_overflow
    _bg_log_write = mw.MainWindow._bg_log_write
    _bg_log_close = mw.MainWindow._bg_log_close

    def __init__(self):
        self._pending_realtime_scans = {}
        self._realtime_debounce_timer = _Timer()
        self._realtime_queue = deque()
        self._realtime_queued = set()
        self._realtime_dropped = 0
        self._realtime_overflow_notified = False
        self.realtime_page = _RtPage()
        self.tray_icon = _Tray()
        self.processed = 0
        self._bg_log_fh = None
        self._bg_log_path = None

    # La coda resta ferma: si misura cosa ci entra.
    def _process_realtime_queue(self):
        self.processed += 1

    def _update_realtime_status_label(self):
        pass


def _files(tmp_path, n):
    out = []
    for i in range(n):
        f = tmp_path / f"f{i}"
        f.write_text("x")
        out.append(str(f))
    return out


# --- coda Real-Time ------------------------------------------------------------

def test_deduplica(tmp_path):
    w = _Window()
    [f] = _files(tmp_path, 1)
    w._queue_realtime_scan(f)
    w._queue_realtime_scan(f)
    assert list(w._realtime_queue) == [f]


def test_file_sparito_non_accodato(tmp_path):
    w = _Window()
    w._queue_realtime_scan(str(tmp_path / "assente"))
    assert not w._realtime_queue


def test_sovraccarico_notificato_una_volta(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "MAX_REALTIME_QUEUE", 3)
    w = _Window()
    for f in _files(tmp_path, 10):
        w._queue_realtime_scan(f)
    assert len(w._realtime_queue) == 3
    assert w._realtime_dropped == 7
    assert len(w.tray_icon.messages) == 1 and len(w.realtime_page.entries) == 1


def test_debounce_un_solo_timer_e_scadenza_spostata(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mw.time, "monotonic", lambda: now[0])
    w = _Window()
    [f] = _files(tmp_path, 1)

    w._schedule_realtime_scan(f)
    assert w._realtime_debounce_timer.isActive()
    now[0] += 2.0
    w._schedule_realtime_scan(f)          # nuova scrittura: scadenza spostata
    now[0] += 2.0
    w._flush_pending_realtime()
    assert not w._realtime_queue           # 2s dall'ultima scrittura: non ancora
    now[0] += 1.5
    w._flush_pending_realtime()
    assert list(w._realtime_queue) == [f]
    assert not w._realtime_debounce_timer.isActive()


def test_debounce_rispetta_il_tetto(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "MAX_REALTIME_QUEUE", 5)
    w = _Window()
    for f in _files(tmp_path, 8):
        w._schedule_realtime_scan(f)
    assert len(w._pending_realtime_scans) == 5
    assert w._realtime_dropped == 3


# --- righe della pagina Scansione -------------------------------------------------

class _ScanPage:
    _on_result = mw.ScanPage._on_result

    def __init__(self):
        self.results_list = QListWidget()
        self._omitted_errors = self._omitted_too_large = 0
        self._non_infected_rows = 0


def test_righe_limitate_ma_infetti_sempre_mostrati(monkeypatch):
    monkeypatch.setattr(mw, "MAX_RESULT_ROWS", 5)
    p = _ScanPage()
    for i in range(20):
        p._on_result(ScanResult(f"/e{i}", "ERROR", "Permission denied"))
    for i in range(3):
        p._on_result(ScanResult(f"/big{i}", "TOO_LARGE"))
    for i in range(4):
        p._on_result(ScanResult(f"/v{i}", "FOUND", "Win.Trojan.A"))

    righe = [p.results_list.item(i).text() for i in range(p.results_list.count())]
    assert sum(r.startswith("INFETTO") for r in righe) == 4
    assert sum(r.startswith("ERRORE") for r in righe) == 5
    assert p._omitted_errors == 15 and p._omitted_too_large == 3


# --- log della scansione programmata ----------------------------------------------

def test_log_programmato_incrementale_e_privato(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    w = _Window()
    w._bg_log_write("INFETTO — /a (X)")
    path = w._bg_log_path
    # Già su disco prima della chiusura: un crash non perde le righe.
    assert path.read_text() == "INFETTO — /a (X)\n"
    w._bg_log_write("ERRORE — /b: y")
    assert w._bg_log_close() == path
    assert path.read_text().splitlines() == ["INFETTO — /a (X)", "ERRORE — /b: y"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_scansione_senza_righe_non_crea_log(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    w = _Window()
    assert w._bg_log_close() is None
    assert not (tmp_path / "logs").exists()
