"""
Messaggi di error nella scansione manuale: contano per l'esito (0.1.17).

La programmata interna contava i messaggi di error (_bg_errors: registro
delle prese visione, quarantena fallita) e li passava a scan_outcome come
problemi, con l'icona di avviso. La manuale li mostrava in lista come
«ERRORE SISTEMA» ma esito e icona restavano «Completata senza problemi»
con la spunta. Ora la regola è la stessa: «Completata con errori», icona
di avviso in notifica e referto, e una riga nel referto.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import ScanResult  # noqa: E402
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402

PROBLEMA = "Prese visione: registro non valido, messo da parte"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class OkClient:
    def __init__(self, **kw):
        self.skipped = Counter()

    def scan_stream(self, target, **kw):
        yield ScanResult(str(Path(target) / "a"), "OK")


@pytest.fixture
def page(app, tmp_path, monkeypatch):
    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", lambda self, **k: OkClient())
    monkeypatch.setattr(mw, "_icon", lambda *names: names[0])
    reports = []

    def exec_(box):
        reports.append((box.windowTitle(), box.text(), box.icon()))

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    for name in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(
            lambda parent, title, text, *a, **k: reports.append((title, text, None))))
    win = QWidget()
    win.tray_icon = MagicMock()
    history = mw.HistoryManager(tmp_path / "data" / "history.json")
    p = mw.ScanPage(mw.ClamdEndpoint(), tmp_path / "q", history, parent=win)
    win.show()
    p.show()
    p.reports = reports
    p.tray = win.tray_icon
    yield p
    win.close()


def _worker_with_messages(monkeypatch, *messages):
    class Worker(ScanWorker):
        def start(self):
            for message in messages:
                self.error.emit(message)
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", Worker)


def _scan(page, target: Path):
    page.path_edit.setText(str(target))
    page._start_scan()
    assert page.worker is None


def _notification_icon(page) -> str:
    ((args, _kw),) = [(c.args, c.kwargs) for c in page.tray.showMessage.call_args_list]
    return args[2]


def test_messaggio_di_error_senza_errori_di_file(page, tmp_path, monkeypatch):
    _worker_with_messages(monkeypatch, PROBLEMA)
    _scan(page, tmp_path)

    ((title, text, icon),) = page.reports
    assert title == "Scansione completata"
    assert "Errori: 0" in text
    assert "Esito: Completata con errori" in text, text
    assert "1 problemi durante la scansione, vedi l'elenco" in text
    assert icon == QMessageBox.Warning
    assert _notification_icon(page) == "dialog-warning"


def test_senza_messaggi_esito_invariato(page, tmp_path, monkeypatch):
    _worker_with_messages(monkeypatch)
    _scan(page, tmp_path)
    ((title, text, icon),) = page.reports
    assert "Esito: Completata senza problemi" in text and "problemi durante" not in text
    assert icon == QMessageBox.Information
    assert _notification_icon(page) == "emblem-checked"


def test_motivo_di_non_completata_non_contato_due_volte(page, tmp_path, monkeypatch):
    # aborted e error con lo stesso testo (scan_worker): un esito solo.
    _worker_with_messages(monkeypatch)
    fifo = tmp_path / "coda"
    os.mkfifo(fifo)
    _scan(page, fifo)
    ((title, text, _icon),) = page.reports
    assert title == "Scansione non completata" and "problemi durante" not in text
    # E il conteggio non passa alla scansione successiva.
    page.reports.clear()
    page.tray.reset_mock()
    _scan(page, tmp_path)
    ((_title, text, _icon),) = page.reports
    assert "Esito: Completata senza problemi" in text
