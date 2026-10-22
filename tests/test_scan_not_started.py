"""
Scansione che non resta «in corso» quando la preparazione fallisce.

In ScanWorker.run la costruzione del client, di Quarantine e
quarantine_dir.resolve() erano fuori da ogni try: un'eccezione lì
(cartella di quarantena non creabile, endpoint non valido) terminava il
thread senza aborted né finished_scan. La pagina Scansione restava «in
corso» fino al riavvio e la programmata interna non registrava nulla. Ora
ogni eccezione prima della traversata è una scansione non eseguita, con il
motivo, sul percorso «non completata» già esistente.

Il worker gira nel thread del test, ma come un QThread: un'eccezione che
esce da run() viene stampata e il thread finisce, senza raggiungere il
chiamante.
"""

from __future__ import annotations

import functools
import os
import traceback
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import ScanResult  # noqa: E402
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _start_like_qthread(self):
    try:
        self.run()
    except Exception:  # noqa: BLE001 - come QThread: stampata, il thread finisce
        traceback.print_exc()


class OkClient:
    def __init__(self, **kw):
        self.skipped = Counter()

    def scan_stream(self, target, **kw):
        yield ScanResult(str(Path(target) / "a"), "OK")


def _broken_client(self, **kw):
    raise ValueError("endpoint non valido")


@pytest.fixture
def quarantine_file(tmp_path) -> Path:
    occupato = tmp_path / "q-file"
    occupato.write_text("non una cartella")
    return occupato


CASES = {
    "quarantena_file": ("Quarantena non utilizzabile", None),
    "client_che_solleva": ("Client di clamd non utilizzabile", _broken_client),
}


# -- scansione manuale ----------------------------------------------------

@pytest.fixture
def make_page(app, tmp_path, monkeypatch):
    monkeypatch.setattr(ScanWorker, "start", _start_like_qthread)
    reports = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: reports.append(
        (self.windowTitle(), self.text())))
    for name in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(
            lambda parent, title, text, *a, **k: reports.append((title, text))))
    pages = []

    def make(quarantine_dir: Path):
        history = mw.HistoryManager(tmp_path / "data" / "history.json")
        p = mw.ScanPage(mw.ClamdEndpoint(), quarantine_dir, history)
        p.show()
        p.reports = reports
        pages.append(p)
        return p

    yield make
    for p in pages:
        p.close()


@pytest.mark.parametrize("case", CASES)
def test_manuale_non_completata_con_il_motivo(make_page, tmp_path, monkeypatch,
                                             quarantine_file, case):
    motivo, factory = CASES[case]
    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", factory or (lambda self, **k: OkClient()))
    qdir = quarantine_file if factory is None else tmp_path / "q"
    page = make_page(qdir)
    target = tmp_path / "dati"
    target.mkdir()

    page.path_edit.setText(str(target))
    page._start_scan()

    assert page.worker is None, "la pagina resta «in corso»"
    assert page.start_button.isEnabled() and not page.stop_button.isEnabled()
    status = page._status_full_text
    assert status.startswith("Scansione non completata: Scansione non eseguita.") and motivo in status
    ((title, text),) = page.reports
    assert title == "Scansione non completata" and motivo in text
    (entry,) = page.history.get_entries()
    assert entry["type"] == "Manuale (non completata)" and motivo in entry.get("reason", "")


# -- programmata interna --------------------------------------------------

class _Settings:
    def __init__(self, values):
        self.values = values

    def value(self, key, default=None, type=None):
        value = self.values.get(key, default)
        if type is bool:
            return bool(value)
        if type is list:
            return list(value or [])
        return value

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


HANDLERS = (
    "_on_bg_result", "_on_bg_progress", "_on_bg_aborted", "_on_bg_error",
    "_on_bg_unreadable_dir", "_on_bg_acknowledged", "_on_bg_report_only",
    "_on_bg_finished", "_on_bg_quarantine_outcome", "_bg_log_write", "_bg_log_close",
)


def _window(tmp_path, target, quarantine_dir):
    messages = []
    fake = SimpleNamespace(
        settings=_Settings({"schedule_target": str(target), "quarantine_dir": str(quarantine_dir)}),
        tray_icon=SimpleNamespace(showMessage=lambda *a, **k: messages.append(a[1]),
                                  setToolTip=lambda *a: None),
        scan_page=SimpleNamespace(worker=None),
        scheduler_page=SimpleNamespace(update_progress=lambda *a: None),
        history_manager=mw.HistoryManager(tmp_path / "data" / "history.json"),
        history_page=SimpleNamespace(refresh=lambda: None),
        reports_page=SimpleNamespace(add_report=lambda r: None),
        clamd_health=SimpleNamespace(is_down=False),
        bg_worker=None, _bg_log_fh=None, _bg_log_path=None, _bg_aborted=None,
        _schedule_aborted_noted=False, _schedule_skip_noted=False, _schedule_late_noted=False,
        _bg_new_reports=0, _bg_errors=0,
        _clamd_endpoint=lambda: mw.ClamdEndpoint(),
        _reset_tray_tooltip=lambda: None, _update_next_run_label=lambda: None,
        _on_quarantine_changed=lambda *a: None,
    )
    for name in HANDLERS:
        setattr(fake, name, functools.partial(getattr(mw.MainWindow, name), fake))
    return fake, messages


@pytest.mark.parametrize("case", CASES)
def test_programmata_non_completata_con_il_motivo(app, tmp_path, monkeypatch,
                                                 quarantine_file, case):
    motivo, factory = CASES[case]
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(ScanWorker, "start", _start_like_qthread)
    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", factory or (lambda self, **k: OkClient()))
    target = tmp_path / "dati"
    target.mkdir()
    qdir = quarantine_file if factory is None else tmp_path / "q"
    fake, messages = _window(tmp_path, target, qdir)

    mw.MainWindow._run_scheduled_scan(fake)

    assert fake.bg_worker is None, "la programmata resta «in corso»"
    entries = fake.history_manager.get_entries()
    assert len(entries) == 1, "nessuna voce di Cronologia"
    (entry,) = entries
    assert entry["type"] == "Programmata (non completata)" and motivo in entry.get("reason", "")
    assert any(motivo in m for m in messages)
