"""
Stato dell'ultima scansione del timer di sistema: il marcatore di inizio e
fine che la GUI legge all'avvio per avvertire di una scansione interrotta.

Una scansione del timer uccisa dallo spegnimento del PC non lascia tracce:
klamav-scan-notify ignora le uscite per segnale e Persistent=true non
ripete l'attivazione, che risulta avvenuta. La CLI scrive qui l'inizio
subito prima della traversata (una scansione mai partita non deve lasciare
marcatori) e la fine nel finally: le uscite con codice passano dal finally,
resta senza fine solo la morte per segnale, ed è quello che la GUI deve
trovare al login.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

import klamav_py.cli as cli
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ClamdUnavailable, ScanResult


def _module():
    assert importlib.util.find_spec("klamav_py.timer_scan_state") is not None, (
        "nessuno stato dell'ultima scansione del timer: una scansione uccisa dallo "
        "spegnimento resta invisibile fino alla successiva riuscita"
    )
    return importlib.import_module("klamav_py.timer_scan_state")


# -- il modulo ----------------------------------------------------------------

def test_ciclo_completo_privato(tmp_path):
    mod = _module()
    path = tmp_path / "data" / mod.STATE_NAME
    inizio = 1_790_000_000.0
    mod.record_start("/home/utente", now=inizio, path=path)
    stato = mod.read_state(path)
    assert stato == mod.TimerScanState(inizio, "/home/utente", None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    mod.record_end(now=inizio + 3600, path=path)
    assert mod.read_state(path) == mod.TimerScanState(inizio, "/home/utente", inizio + 3600)


def test_fine_senza_inizio_non_crea_nulla(tmp_path):
    mod = _module()
    mod.record_end(path=tmp_path / "data" / mod.STATE_NAME)
    assert not (tmp_path / "data").exists()


def test_fine_gia_scritta_non_si_riscrive(tmp_path):
    mod = _module()
    path = tmp_path / mod.STATE_NAME
    mod.record_start("radice", now=100.0, path=path)
    mod.record_end(now=200.0, path=path)
    mod.record_end(now=300.0, path=path)
    assert mod.read_state(path).finished_at == 200.0


def test_nuova_scansione_sovrascrive_lavviso_della_precedente(tmp_path):
    mod = _module()
    path = tmp_path / mod.STATE_NAME
    mod.record_start("radice", now=100.0, path=path)
    mod.record_end(now=200.0, path=path)
    mod.record_start("radice", now=300.0, path=path)
    assert mod.read_state(path) == mod.TimerScanState(300.0, "radice", None)


def test_file_non_plausibile_none(tmp_path):
    mod = _module()
    path = tmp_path / mod.STATE_NAME
    validi = {
        "version": 1,
        "started_at": 100.0,
        "target": "radice",
        "finished_at": None,
    }
    non_plausibili = [
        {},
        {**validi, "version": 2},
        {**validi, "started_at": "ieri"},
        {**validi, "started_at": -1},
        {**validi, "target": ""},
        {**validi, "target": "riga\nnuova"},
        {**validi, "finished_at": 99.0},
        {**validi, "finished_at": "adesso"},
        {"entries": [validi]},
    ]
    for raw in non_plausibili:
        path.write_text(json.dumps(raw))
        assert mod.read_state(path) is None, raw
    path.write_text("non è json")
    assert mod.read_state(path) is None
    assert mod.read_state(tmp_path / "assente.json") is None


def test_file_gigante_o_symlink_none(tmp_path):
    mod = _module()
    path = tmp_path / mod.STATE_NAME
    path.write_text("x" * (mod.MAX_STATE_BYTES + 1))
    assert mod.read_state(path) is None
    altro = tmp_path / "altro"
    altro.write_text(json.dumps({"version": 1}))
    link = tmp_path / "link.json"
    link.symlink_to(altro)
    assert mod.read_state(link) is None


def test_avviso_con_data_e_rimandi(tmp_path):
    mod = _module()
    inizio = 1_790_000_000.0
    stato = mod.TimerScanState(inizio, "/home/utente", None)
    notice = mod.interrupted_notice(stato)
    assert time.strftime("%d/%m/%Y", time.localtime(inizio)) in notice
    assert "non risulta completata" in notice
    assert "Segnalazioni" in notice
    assert "journalctl --user -u klamav-scan.service" in notice


# -- scritto dalla CLI, quindi dal timer --------------------------------------

class FakeClient:
    """Solo file puliti; con `die_after` clamd sparisce dopo quel file, e al
    primo file si osserva lo stato scritto finora: il marcatore d'inizio
    dev'esserci già, la scansione è in corso."""

    die_after: str | None = None
    seen_durante: object = "mai-iterato"

    def __init__(self, **kw):
        self.skipped = Counter()

    def ping(self):
        return True

    def scan_stream(self, root, exclude_dirs=None, **kw):
        for n, f in enumerate(sorted(ClamdClient._iter_files(Path(root).resolve(), exclude_dirs))):
            if n == 0:
                FakeClient.seen_durante = _module().read_state()
            yield ScanResult(str(f), "OK")
            if FakeClient.die_after == f.name:
                raise ClamdUnavailable("connessione rifiutata", "socket di prova")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "documenti").mkdir(parents=True)
    (h / "documenti" / "a").write_bytes(b"pulito")
    (h / "documenti" / "b").write_bytes(b"pulito")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    monkeypatch.setattr(FakeClient, "die_after", None)
    monkeypatch.setattr(FakeClient, "seen_durante", "mai-iterato")
    return h


def _scan(home):
    # Senza --quarantine: il marcatore non c'entra con la quarantena, e con
    # INVOCATION_ID una quarantena sotto /tmp (dove sta la home dei test)
    # verrebbe rifiutata dalla regola sulle directory volatili.
    return cli.main(["scan", str(home), "--quiet"])


def test_cli_sotto_systemd_scrive_inizio_e_fine(home, monkeypatch):
    mod = _module()
    monkeypatch.setenv("INVOCATION_ID", "1")
    assert _scan(home) == 0
    stato = mod.read_state()
    assert stato is not None and stato.finished_at is not None
    assert stato.target == str(home.resolve())
    # Durante la traversata il marcatore c'era già, ed era aperto.
    assert FakeClient.seen_durante is not None
    assert FakeClient.seen_durante.finished_at is None, (
        "il marcatore d'inizio dev'essere scritto prima della traversata, non a fine scansione"
    )


def test_cli_interrotta_da_clamd_registra_comunque_la_fine(home, monkeypatch):
    # Un'uscita con codice (2) passa dal finally e ha la notifica di
    # OnFailure: non dev'essere scambiata per una scansione interrotta.
    mod = _module()
    monkeypatch.setenv("INVOCATION_ID", "1")
    FakeClient.die_after = "b"
    assert _scan(home) == 2
    stato = mod.read_state()
    assert stato is not None and stato.finished_at is not None


def test_cli_mai_partita_non_lascia_marcatori(home, monkeypatch):
    mod = _module()
    monkeypatch.setenv("INVOCATION_ID", "1")
    assert cli.main(["scan", str(home / "inesistente"),
                     "--quarantine", str(home / "q"), "--quiet"]) == 2
    assert mod.read_state() is None, (
        "le validazioni bloccanti vengono prima del marcatore: una scansione mai "
        "partita non deve sembrare interrotta"
    )


def test_cli_in_primo_piano_non_scrive_nulla(home):
    mod = _module()
    assert _scan(home) == 0
    assert mod.read_state() is None, (
        "il marcatore vale solo per il timer di sistema: una scansione manuale "
        "interrotta è visibile nel terminale e non deve avvisare la GUI"
    )


# -- l'avviso nella GUI -------------------------------------------------------

class _Tray:
    """Registra le notifiche: bastano isVisible e showMessage."""

    def __init__(self):
        self.messages = []

    def isVisible(self):
        return True

    def showMessage(self, title, text, icon=None, msecs=0):
        self.messages.append((title, text))


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def gui(app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    from PySide6.QtCore import QSettings
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(config))
    import klamav_py.gui.main_window as mw
    page = mw.SchedulerPage()
    fake = SimpleNamespace(
        settings=QSettings(mw.APP_NAME, mw.APP_NAME),
        scheduler_page=page,
        tray_icon=_Tray(),
    )
    return SimpleNamespace(mw=mw, fake=fake, page=page, tray=fake.tray_icon)


def _run_startup(gui):
    """I due metodi dell'avvio di MainWindow sull'oggetto minimo, cercati
    con getattr: senza il controllo la controprova fallisce su
    un'asserzione, non su AttributeError."""
    check = getattr(gui.mw.MainWindow, "_check_interrupted_timer_scan", None)
    done = getattr(gui.mw.MainWindow, "_timer_scan_checked", None)
    assert check is not None and done is not None, (
        "nessun controllo della scansione del timer interrotta all'avvio della GUI"
    )
    gui.fake._timer_scan_checked = lambda result: done(gui.fake, result)
    check(gui.fake)


def test_avviso_allavvio_notifica_una_volta_per_interruzione(gui, monkeypatch, capsys):
    mod = _module()
    stato = mod.TimerScanState(1_790_000_000.0, "/home/utente", None)
    monkeypatch.setattr(gui.mw, "run_off_gui_thread", lambda fn, cb: cb(stato))
    _run_startup(gui)
    assert len(gui.tray.messages) == 1
    assert "non risulta completata" in gui.tray.messages[0][1]
    assert "non risulta completata" in gui.page.timer_scan_label.text()
    assert "non risulta completata" in capsys.readouterr().err

    # Stessa interruzione, secondo avvio: l'etichetta resta, la notifica no.
    _run_startup(gui)
    assert len(gui.tray.messages) == 1
    assert "non risulta completata" in gui.page.timer_scan_label.text()

    # Una nuova interruzione (altra ora di avvio) viene notificata di nuovo.
    stato2 = mod.TimerScanState(1_790_010_000.0, "/home/utente", None)
    monkeypatch.setattr(gui.mw, "run_off_gui_thread", lambda fn, cb: cb(stato2))
    _run_startup(gui)
    assert len(gui.tray.messages) == 2


def test_niente_da_dire_nessun_avviso(gui, monkeypatch):
    monkeypatch.setattr(gui.mw, "run_off_gui_thread", lambda fn, cb: cb(None))
    _run_startup(gui)
    assert gui.tray.messages == []
    assert gui.page.timer_scan_label.text() == ""


def test_check_distingue_interrotta_da_in_corso(gui, monkeypatch):
    mod = _module()
    fn = getattr(gui.mw, "check_interrupted_timer_scan", None)
    assert fn is not None, "nessun controllo condiviso per avvio e pagina"
    aperta = mod.TimerScanState(100.0, "radice", None)
    chiusa = mod.TimerScanState(100.0, "radice", 200.0)
    casi = [
        (None, None, None),      # nessun marcatore
        (chiusa, None, None),    # scansione arrivata alla fine
        (aperta, True, None),    # in corso adesso: il buco lo copre lei
        (aperta, False, aperta),  # interrotta
        (aperta, None, aperta),  # systemctl assente: vale interrotta
    ]
    for stato, attiva, atteso in casi:
        monkeypatch.setattr(gui.mw, "read_state", lambda s=stato: s)
        monkeypatch.setattr(gui.mw, "service_active", lambda a=attiva: a)
        assert fn() == atteso, (stato, attiva)


def test_pagina_aggiorna_lavviso_ad_ogni_apertura(gui, monkeypatch):
    mod = _module()
    stato = mod.TimerScanState(1_790_000_000.0, "/home/utente", None)

    def _inline(fn, callback):
        try:
            callback(fn())
        except Exception as exc:  # come run_off_gui_thread
            callback(exc)

    monkeypatch.setattr(gui.mw, "run_off_gui_thread", _inline)
    monkeypatch.setattr(gui.mw, "timer_enabled", lambda: True)
    monkeypatch.setattr(gui.mw, "foreign_overrides", lambda **kw: [])
    monkeypatch.setattr(gui.mw, "check_interrupted_timer_scan", lambda: stato)
    gui.page.refresh_system_timer()
    assert "non risulta completata" in gui.page.timer_scan_label.text()

    # Sparisce appena una scansione arriva alla fine.
    monkeypatch.setattr(gui.mw, "check_interrupted_timer_scan", lambda: None)
    gui.page.refresh_system_timer()
    assert gui.page.timer_scan_label.text() == ""
