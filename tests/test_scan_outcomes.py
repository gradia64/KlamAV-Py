"""
Esiti delle scansioni della GUI che non riflettevano l'interruzione.

3a. ScanPage non collegava ScanWorker.aborted: una scansione manuale di
    una radice non valida scriveva la riga d'errore, ma stato, referto e
    voce di Cronologia dicevano «Completata senza problemi». Ora dicono
    che non è stata completata e perché, con il testo della pianificazione
    interna per lo stesso caso.

3b. La pianificazione interna non collegava ScanWorker.error: i problemi
    del registro delle prese visione (e ogni altro messaggio che la
    scansione manuale mostra in lista) non arrivavano al log della
    programmata.
"""

from __future__ import annotations

import functools
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.acknowledged import AckRegistry  # noqa: E402
from klamav_py.clamd_client import ScanResult  # noqa: E402
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402
from klamav_py.quarantine import Quarantine  # noqa: E402
from klamav_py.quarantine_policy import QuarantinePolicy  # noqa: E402

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"
non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root legge anche le cartelle 0300")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# -- 3a: pagina Scansione ----------------------------------------------------

@pytest.fixture
def page(app, tmp_path, monkeypatch):
    # Il worker gira nel thread del test: i segnali arrivano in linea.
    monkeypatch.setattr(ScanWorker, "start", lambda self: self.run())
    reports = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: reports.append(
        (self.windowTitle(), self.text())))
    history = mw.HistoryManager(tmp_path / "data" / "history.json")
    p = mw.ScanPage(mw.ClamdEndpoint(), Quarantine(tmp_path / "q"), history)
    p.show()
    p.reports = reports
    yield p
    p.close()


def _scan(page, target: Path):
    page.path_edit.setText(str(target))
    page._start_scan()
    assert page.worker is None  # finita


def _assert_not_completed(page, motivo: str):
    status = page._status_full_text
    assert status.startswith("Scansione non completata: Scansione non eseguita.")
    assert motivo in status
    ((title, text),) = page.reports
    assert title == "Scansione non completata"
    assert "Esito: Non completata: Scansione non eseguita." in text and motivo in text
    assert "Completata senza problemi" not in text
    (entry,) = page.history.get_entries()
    assert entry["type"] == "Manuale (non completata)"


def test_manuale_radice_non_directory(page, tmp_path):
    # Nessun clamd serve: la traversata solleva UnreadableRoot subito.
    fifo = tmp_path / "coda"
    os.mkfifo(fifo)
    _scan(page, fifo)
    _assert_not_completed(page, "non è una directory né un file regolare")


@non_root
def test_manuale_radice_illeggibile(page, tmp_path):
    root = tmp_path / "chiusa"
    root.mkdir()
    os.chmod(root, 0o300)
    try:
        _scan(page, root)
    finally:
        os.chmod(root, 0o700)
    _assert_not_completed(page, "non è leggibile")


def test_manuale_completata_invariata(page, tmp_path, monkeypatch):
    class Client:
        def __init__(self, **kw):
            self.skipped = Counter()

        def scan_stream(self, target, **kw):
            yield ScanResult(str(target / "a"), "OK")

    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", lambda self, **k: Client())
    _scan(page, tmp_path)
    ((title, text),) = page.reports
    assert title == "Scansione completata" and "Esito: Completata senza problemi" in text
    assert page.history.get_entries()[0]["type"] == "Manuale"


# -- 3b: pianificazione interna ------------------------------------------------

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


def _window(tmp_path, target):
    """Una MainWindow ridotta: stato e collaboratori finti, ma metodi veri
    della pianificazione interna, compreso _run_scheduled_scan che collega
    i segnali del worker."""
    messages, entries = [], []
    fake = SimpleNamespace(
        settings=_Settings({"schedule_target": str(target), "quarantine_dir": str(tmp_path / "q")}),
        tray_icon=SimpleNamespace(showMessage=lambda *a, **k: messages.append(a[1]),
                                  setToolTip=lambda *a: None),
        scan_page=SimpleNamespace(worker=None),
        scheduler_page=SimpleNamespace(update_progress=lambda *a: None),
        history_manager=SimpleNamespace(add_entry=lambda *a, **k: entries.append((a, k))),
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
    return fake, messages, entries


def test_programmata_problema_del_registro_nel_log(app, tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", logs)
    mail = tmp_path / "home" / "mail"
    mail.mkdir(parents=True)
    msg = mail / "1"
    msg.write_bytes(b"phishing")
    registry = AckRegistry(tmp_path / "data" / "acknowledged.json")
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text("{rotto")

    class Client:
        def __init__(self, **kw):
            self.skipped = Counter()

        def scan_stream(self, target, **kw):
            yield ScanResult(str(msg), "FOUND", PHISHING)

    class SyncWorker(ScanWorker):
        def __init__(self, **kw):
            super().__init__(client_factory=Client, acknowledgements=registry,
                             policy=QuarantinePolicy([mail]), **kw)

        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    fake, messages, entries = _window(tmp_path, tmp_path / "home")
    mw.MainWindow._run_scheduled_scan(fake)

    (log,) = logs.glob("scheduled-*.log")
    text = log.read_text()
    assert "ERRORE SISTEMA — Prese visione: registro delle prese visione non valido" in text
    assert "non sono state cancellate" in text
    assert "1 problemi durante la scansione" in messages[-1]
    ((args, kwargs),) = entries
    assert args[0] == "Programmata" and kwargs["log_file"] == str(log)


def test_programmata_non_completata_motivo_scritto_una_volta(app, tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", logs)

    class SyncWorker(ScanWorker):
        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    fake, messages, entries = _window(tmp_path, tmp_path / "sparita")
    mw.MainWindow._run_scheduled_scan(fake)
    (log,) = logs.glob("scheduled-*.log")
    lines = log.read_text().splitlines()
    # aborted ed error portano lo stesso testo: una riga sola.
    assert len(lines) == 1 and lines[0].startswith("ERRORE — Scansione non eseguita.")
    assert messages[-1].startswith("Scansione programmata non completata")
