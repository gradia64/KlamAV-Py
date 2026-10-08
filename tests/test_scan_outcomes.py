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
    # Un avviso modale bloccherebbe il test: si registra come un referto.
    for name in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(
            lambda parent, title, text, *a, **k: reports.append((title, text))))
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


# -- guasti di I/O (punto 1-bis): mai «senza problemi» ------------------------

def _ok_client():
    from klamav_py.clamd_client import ClamdClient

    class Client(ClamdClient):
        def __init__(self, **kw):
            super().__init__(unix_socket="/nonexistent")

        def _instream_one(self, target, max_stream_size):
            return ScanResult(str(target), "OK")

        def scan_stream(self, path, **kw):
            kw["persistent"] = False
            return super().scan_stream(path, **kw)

    return Client


@pytest.fixture
def eio_tree(tmp_path, monkeypatch):
    import errno

    root = tmp_path / "radice"
    (root / "guasta").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    real = os.scandir

    def scandir(path="."):
        if Path(os.fsdecode(path)) == root / "guasta":
            raise OSError(errno.EIO, os.strerror(errno.EIO), os.fsdecode(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return root


def test_manuale_guasto_di_io_non_e_senza_problemi(page, eio_tree, monkeypatch):
    client = _ok_client()
    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", lambda self, **k: client())
    _scan(page, eio_tree)
    ((title, text),) = page.reports
    assert "Completata senza problemi" not in text and "Esito: Completata con errori" in text
    assert "Errori: 1" in text
    assert page.history.get_entries()[0]["errors"] == 1


def test_programmata_guasto_di_io_non_e_senza_problemi(app, tmp_path, eio_tree, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    client = _ok_client()

    class SyncWorker(ScanWorker):
        def __init__(self, **kw):
            super().__init__(client_factory=client, **kw)

        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    fake, messages, entries = _window(tmp_path, eio_tree)
    mw.MainWindow._run_scheduled_scan(fake)
    assert "senza problemi" not in messages[-1] and "1 errori" in messages[-1]
    ((args, _),) = entries
    assert args[2].errors == 1
    (log,) = (tmp_path / "logs").glob("scheduled-*.log")
    assert "cartella non letta" in log.read_text()


# -- radice nella GUI: una sola regola (post-review 0.1.14) -------------------

class _Recorder:
    """Client che registra le destinazioni scansionate (nessun clamd)."""

    seen: list = []

    def __init__(self, **kw):
        self.skipped = Counter()

    def scan_stream(self, target, **kw):
        _Recorder.seen.append(Path(target))
        yield ScanResult(str(target), "OK")


def test_manuale_symlink_rotto_messaggio_dedicato(page, tmp_path):
    link = tmp_path / "rotto"
    link.symlink_to(tmp_path / "sparito")
    _scan(page, link)
    # Il percorso scelto dall'utente, non quello risolto.
    _assert_not_completed(page, f"«{link}» è un collegamento simbolico rotto")


def test_programmata_symlink_rotto_messaggio_dedicato(app, tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    link = tmp_path / "rotto"
    link.symlink_to(tmp_path / "sparito")

    class SyncWorker(ScanWorker):
        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    fake, messages, entries = _window(tmp_path, link)
    mw.MainWindow._run_scheduled_scan(fake)
    assert f"«{link}» è un collegamento simbolico rotto" in messages[-1]
    assert entries[0][0][0] == "Programmata (non completata)"


def test_fifo_fermata_dalla_sonda_prima_della_traversata(tmp_path):
    fifo = tmp_path / "coda"
    os.mkfifo(fifo)
    _Recorder.seen = []
    w = ScanWorker(endpoint=mw.ClamdEndpoint(), target=fifo, client_factory=_Recorder)
    aborted = []
    w.aborted.connect(aborted.append)
    w.run()
    assert _Recorder.seen == []
    assert aborted == [f"Scansione non eseguita. Il percorso da scansionare «{fifo}» "
                       "non è una directory né un file regolare"]


def test_selezione_multipla_con_destinazione_non_valida_nessun_file(tmp_path):
    buono = tmp_path / "a.txt"
    buono.write_text("x")
    rotto = tmp_path / "rotto"
    rotto.symlink_to(tmp_path / "sparito")
    _Recorder.seen = []
    w = ScanWorker(endpoint=mw.ClamdEndpoint(), target=[buono, rotto, tmp_path],
                   client_factory=_Recorder)
    aborted = []
    w.aborted.connect(aborted.append)
    w.run()
    # Nessuna scansione parziale: nemmeno la prima destinazione, valida.
    assert _Recorder.seen == []
    assert len(aborted) == 1 and f"«{rotto}»" in aborted[0]


def test_realtime_file_sparito_non_analizzato_senza_cronologia(app, tmp_path, monkeypatch):
    from collections import deque

    class SyncWorker(ScanWorker):
        def __init__(self, **kw):
            super().__init__(client_factory=_Recorder, **kw)

        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    entries = []
    page = mw.RealTimePage()
    sparito = tmp_path / "scaricato.part"
    fake = SimpleNamespace(
        realtime_worker=None, clamd_health=SimpleNamespace(is_down=False),
        _realtime_queue=deque([str(sparito)]), _realtime_queued=set(),
        _realtime_overflow_notified=False, _current_realtime_target="",
        _realtime_aborted=None, realtime_page=page,
        settings=_Settings({"quarantine_dir": str(tmp_path / "q")}),
        _clamd_endpoint=lambda: mw.ClamdEndpoint(),
        reports_page=SimpleNamespace(add_report=lambda r: None),
        history_manager=SimpleNamespace(add_entry=lambda *a, **k: entries.append(a)),
        history_page=SimpleNamespace(refresh=lambda: None),
        tray_icon=SimpleNamespace(showMessage=lambda *a, **k: None),
    )
    for name in ("_process_realtime_queue", "_on_realtime_result", "_on_realtime_finished",
                 "_on_realtime_acknowledged", "_on_realtime_quarantine_outcome",
                 "_on_realtime_aborted", "_on_realtime_error"):
        if hasattr(mw.MainWindow, name):
            setattr(fake, name, functools.partial(getattr(mw.MainWindow, name), fake))
    fake._on_quarantine_changed = lambda *a: None
    _Recorder.seen = []
    mw.MainWindow._process_realtime_queue(fake)

    rows = [page.log_list.item(i).text() for i in range(page.log_list.count())]
    assert any("Non analizzato: scaricato.part" in r and "non esiste" in r for r in rows), rows
    assert entries == [] and _Recorder.seen == []


# -- guasti di I/O nominati nella GUI (0.1.15) --------------------------------
#
# Nella 0.1.14 un guasto contava fra gli errori («Completata con errori»), ma
# la GUI non lo distingueva da un file illeggibile per permessi: la CLI lo
# dice nell'ultima riga del riepilogo, la GUI no. Ora stato, referto,
# notifica, log e Cronologia lo nominano con il testo della CLI.

FAULT_TEXT = "guasti di I/O (elencati fra gli errori): parte dell'albero non è stata controllata"


@pytest.fixture
def denied_tree(tmp_path, monkeypatch):
    """Come eio_tree, ma la sottocartella ha i permessi negati (EACCES)."""
    import errno

    root = tmp_path / "radice"
    (root / "chiusa").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    real = os.scandir

    def scandir(path="."):
        if Path(os.fsdecode(path)) == root / "chiusa":
            raise OSError(errno.EACCES, os.strerror(errno.EACCES), os.fsdecode(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return root


def _history_column(history, header: str) -> list[str]:
    """Valori di una colonna della pagina Cronologia, cercata per titolo."""
    hp = mw.HistoryPage(history)
    headers = [hp.table.horizontalHeaderItem(c).text() for c in range(hp.table.columnCount())]
    assert header in headers, headers
    col = headers.index(header)
    return [hp.table.item(r, col).text() for r in range(hp.table.rowCount())]


def test_manuale_guasto_nominato_in_stato_referto_e_cronologia(page, eio_tree, monkeypatch):
    client = _ok_client()
    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", lambda self, **k: client())
    _scan(page, eio_tree)
    assert "1 " + FAULT_TEXT in page._status_full_text
    ((title, text),) = page.reports
    assert title == "Scansione completata" and "Esito: Completata con errori" in text
    assert "1 " + FAULT_TEXT in text
    (entry,) = page.history.get_entries()
    assert entry.get("io_faults") == 1 and entry["errors"] == 1
    assert _history_column(page.history, "Guasti di I/O") == ["1"]


def test_programmata_guasto_nominato_in_notifica_log_e_cronologia(app, tmp_path, eio_tree, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    client = _ok_client()

    class SyncWorker(ScanWorker):
        def __init__(self, **kw):
            super().__init__(client_factory=client, **kw)

        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    fake, messages, entries = _window(tmp_path, eio_tree)
    mw.MainWindow._run_scheduled_scan(fake)
    assert "1 errori" in messages[-1] and "1 " + FAULT_TEXT in messages[-1]
    ((args, _),) = entries
    assert getattr(args[2], "io_faults", 0) == 1
    (log,) = (tmp_path / "logs").glob("scheduled-*.log")
    assert log.read_text().splitlines()[-1].startswith("ATTENZIONE: 1 " + FAULT_TEXT)


def test_solo_permessi_nessun_guasto(page, app, tmp_path, denied_tree, monkeypatch):
    client = _ok_client()
    monkeypatch.setattr(mw.ClamdEndpoint, "new_client", lambda self, **k: client())
    _scan(page, denied_tree)
    ((_, text),) = page.reports
    assert "guast" not in page._status_full_text and "guast" not in text
    assert page.history.get_entries()[0].get("io_faults", 0) == 0

    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")

    class SyncWorker(ScanWorker):
        def __init__(self, **kw):
            super().__init__(client_factory=client, **kw)

        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    fake, messages, _ = _window(tmp_path, denied_tree)
    mw.MainWindow._run_scheduled_scan(fake)
    assert "cartelle non leggibili" in messages[-1] and "guast" not in messages[-1]


def test_cronologia_della_0_1_14_si_legge(app, tmp_path):
    """Voci scritte dalla 0.1.14: senza io_faults, valgono zero guasti."""
    import json

    path = tmp_path / "history.json"
    path.write_text(json.dumps([
        {"timestamp": "2026-10-01 10:00:00", "type": "Manuale", "target": "/home/u",
         "scanned": 10, "infections": 0, "errors": 1, "too_large": 0,
         "unreadable_dirs": 0, "acknowledged": 0},
        {"timestamp": "2026-10-01 11:00:00", "type": "Programmata (non completata)",
         "target": "/home/u", "scanned": 0, "infections": 0, "errors": 0},
    ]))
    history = mw.HistoryManager(path)
    assert _history_column(history, "Guasti di I/O") == ["0", "0"]


# -- motivo nella voce di Cronologia (0.1.15) ---------------------------------
#
# La voce diceva «Manuale (non completata)» o «Programmata (non completata)»
# con il percorso; il motivo stava solo nello stato e nel referto, che non
# sopravvivono alla sessione. «Dopo il riavvio» qui è un HistoryManager e
# una pagina Cronologia nuovi sullo stesso file.

def _reasons_after_restart(path) -> list[str]:
    return _history_column(mw.HistoryManager(path), "Motivo")


def test_manuale_non_completata_motivo_in_cronologia(page, tmp_path):
    fifo = tmp_path / "coda"
    os.mkfifo(fifo)
    _scan(page, fifo)
    (entry,) = page.history.get_entries()
    assert "non è una directory né un file regolare" in entry.get("reason", "")
    (reason,) = _reasons_after_restart(page.history.file_path)
    assert reason.startswith("Scansione non eseguita.") and str(fifo) in reason


def test_programmata_non_completata_motivo_in_cronologia(app, tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")

    class SyncWorker(ScanWorker):
        def start(self):
            self.run()

    monkeypatch.setattr(mw, "ScanWorker", SyncWorker)
    sparita = tmp_path / "sparita"
    fake, _, _ = _window(tmp_path, sparita)
    fake.history_manager = mw.HistoryManager(tmp_path / "data" / "history.json")
    mw.MainWindow._run_scheduled_scan(fake)

    (entry,) = fake.history_manager.get_entries()
    assert entry["type"] == "Programmata (non completata)"
    (reason,) = _reasons_after_restart(fake.history_manager.file_path)
    assert reason.startswith("Scansione non eseguita.") and "non esiste" in reason


def test_completata_senza_motivo_e_voci_vecchie_leggibili(app, tmp_path):
    import json

    path = tmp_path / "history.json"
    path.write_text(json.dumps([
        {"timestamp": "2026-10-01 10:00:00", "type": "Manuale (non completata)",
         "target": "/x", "scanned": 0, "infections": 0, "errors": 0},
        {"timestamp": "2026-10-01 11:00:00", "type": "Manuale", "target": "/x",
         "reason": ["non", "una", "stringa"]},
    ]))
    history = mw.HistoryManager(path)
    history.add_entry("Manuale", "/y", mw.ScanTotals(scanned=1))
    assert "reason" not in history.get_entries()[-1]
    assert _reasons_after_restart(path) == ["", "", ""]
