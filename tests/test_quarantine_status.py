"""
Stato della pagina Quarantena e pulsanti coerenti (0.1.16).

Dalla 0.1.15 la pagina carica nel worker, ma dopo un errore del
costruttore Ripristina ed Elimina tornavano attivi senza fare nulla al
clic, e «Metti in quarantena» nella pagina Scansione rispondeva «riprova
tra poco» anche a caricamento fallito, che non riparte da solo. Ora la
pagina ha uno stato (caricamento, pronta, fallita con il motivo) da cui
dipendono pulsanti e messaggi.

Il caricamento in corso è tenuto aperto da un load_quarantine fermo su un
threading.Event, senza sleep.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem, QMessageBox, QWidget  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
import klamav_py.gui.quarantine_loader as ql  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def messages(monkeypatch):
    """QMessageBox.information intercettata: (titolo, testo)."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda parent, title, text, *a, **k: seen.append((title, text))),
    )
    return seen


def _wait(page, timeout=10):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if page.worker is None and page.loading_label.isHidden():
            return
        time.sleep(0.01)
    pytest.fail("caricamento della quarantena non terminato")


def _window(directory: Path, tmp_path: Path):
    """Le due pagine sotto una finestra minima, collegate come in
    MainWindow: la pagina Scansione trova la pagina Quarantena con
    window().quarantine_page."""
    win = QWidget()
    scan = mw.ScanPage(mw.ClamdEndpoint(), directory, mw.HistoryManager(tmp_path / "h.json"),
                       parent=win)
    page = mw.QuarantinePage(directory, parent=win)
    page.quarantine_ready.connect(scan.set_quarantine)
    win.quarantine_page = page
    return win, scan, page


def _select_infected(scan, path: Path) -> None:
    item = QListWidgetItem(f"INFETTO — {path}")
    item.setData(Qt.UserRole, {"path": str(path), "signature": "Eicar-Test-Signature"})
    scan.results_list.addItem(item)
    item.setSelected(True)


def _buttons(page) -> tuple[bool, bool]:
    return page.restore_button.isEnabled(), page.delete_button.isEnabled()


def _status_kind(page) -> str | None:
    status = getattr(page, "status", None)
    return getattr(status, "kind", None)


def test_costruttore_fallito_pulsanti_spenti_e_motivo_nella_scansione(
        app, tmp_path, messages):
    occupato = tmp_path / "file"
    occupato.write_text("non una cartella")
    win, scan, page = _window(occupato, tmp_path)
    _wait(page)

    assert _buttons(page) == (False, False), "Ripristina ed Elimina attivi senza quarantena"
    assert _status_kind(page) == "fallita"
    assert str(occupato) in page.status.reason

    _select_infected(scan, tmp_path / "virus.exe")
    scan._quarantine_selected()
    assert len(messages) == 1
    title, text = messages[0]
    assert str(occupato) in text, text  # il motivo
    assert "Aggiorna" in text, text      # e il rimedio
    assert "riprova tra poco" not in text


def test_caricamento_in_corso_pulsanti_spenti_e_attendi(app, tmp_path, messages, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    real = ql.load_quarantine

    def blocked(directory, quarantine=None):
        started.set()
        assert release.wait(10), "caricamento mai sbloccato"
        return real(directory, quarantine)

    monkeypatch.setattr(ql, "load_quarantine", blocked)
    qdir = tmp_path / "q"
    win, scan, page = _window(qdir, tmp_path)
    try:
        assert started.wait(10)
        assert _status_kind(page) == "caricamento"
        assert _buttons(page) == (False, False)
        _select_infected(scan, tmp_path / "virus.exe")
        scan._quarantine_selected()
        title, text = messages[-1]
        assert "attendi" in text, text
    finally:
        release.set()
    _wait(page)
    assert _status_kind(page) == "pronta" and _buttons(page) == (True, True)


def test_aggiorna_dopo_un_errore_riattiva_tutto(app, tmp_path, messages):
    occupato = tmp_path / "q"
    occupato.write_text("non una cartella")
    win, scan, page = _window(occupato, tmp_path)
    _wait(page)
    assert _buttons(page) == (False, False)
    assert scan.quarantine is None

    occupato.unlink()  # l'utente sistema la cartella e preme «Aggiorna»
    page.refresh()
    _wait(page)
    assert _status_kind(page) == "pronta"
    assert _buttons(page) == (True, True)
    assert page.quarantine is not None
    assert page.health_label.isHidden(), "errore precedente ancora mostrato"
    assert scan.quarantine is page.quarantine, "quarantena non passata alla pagina Scansione"


def test_messaggio_della_scansione_per_stato():
    status_cls = getattr(mw, "QuarantineStatus", None)
    message = getattr(mw, "quarantine_unavailable_message", None)
    assert status_cls is not None and message is not None, "nessuno stato della quarantena"
    _, loading = message(status_cls(status_cls.LOADING))
    _, failed = message(status_cls(status_cls.FAILED, "Impossibile preparare: X"))
    assert "attendi" in loading and "Aggiorna" not in loading
    assert "Impossibile preparare: X" in failed and "Aggiorna" in failed
    # Senza pagina Quarantena (finestra minima): come il caricamento.
    assert message(None) == message(status_cls(status_cls.LOADING))


# -- guardie con un caricamento in corso (0.1.15, test nella 0.1.16) ----------

class _Gate:
    """load_quarantine trattenuto su un Event finché il test non lo apre:
    il caricamento resta in corso senza sleep."""

    def __init__(self, monkeypatch):
        self.real = ql.load_quarantine
        self.calls: list[Path] = []
        self.open = threading.Event()
        self.started = threading.Event()
        monkeypatch.setattr(ql, "load_quarantine", self._load)

    def _load(self, directory, quarantine=None):
        self.calls.append(Path(directory))
        self.started.set()
        assert self.open.wait(10), "caricamento mai sbloccato"
        return self.real(directory, quarantine)


def _quarantined(qdir: Path, home: Path, name: str):
    """Una voce in quarantena in qdir, scritta da un altro oggetto."""
    from klamav_py.quarantine import Quarantine

    home.mkdir(parents=True, exist_ok=True)
    victim = home / name
    victim.write_bytes(name.encode())
    Quarantine(qdir).quarantine_file(victim, "Sig." + name)


def _names(page) -> list[str]:
    return sorted(Path(page.table.item(r, 0).text()).name for r in range(page.table.rowCount()))


def test_esito_di_una_cartella_superata_scartato(app, tmp_path):
    """_on_loaded con la directory di prima: niente elenco né oggetto di
    quella cartella, e un nuovo caricamento per quella attuale."""
    a, b = tmp_path / "a", tmp_path / "b"
    _quarantined(a, tmp_path / "home", "da_a.exe")
    _quarantined(b, tmp_path / "home", "da_b.exe")
    win, scan, page = _window(b, tmp_path)
    _wait(page)
    assert _names(page) == ["da_b.exe"]

    page._on_loaded(a, ql.load_quarantine(a))  # arrivato in ritardo
    assert _names(page) != ["da_a.exe"]
    assert page.quarantine.dir == b
    _wait(page)
    assert _names(page) == ["da_b.exe"] and page.quarantine.dir == b
    assert scan.quarantine.dir == b


def test_cambio_di_cartella_durante_il_caricamento(app, tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    _quarantined(a, tmp_path / "home", "da_a.exe")
    _quarantined(b, tmp_path / "home", "da_b.exe")
    gate = _Gate(monkeypatch)
    win, scan, page = _window(a, tmp_path)
    assert gate.started.wait(10)
    scan.set_quarantine_dir(b)
    page.load(b)  # Impostazioni: nuova cartella mentre a è in caricamento
    gate.open.set()
    _wait(page)
    assert gate.calls == [a, b]
    assert _names(page) == ["da_b.exe"] and page.quarantine.dir == b
    assert scan.quarantine is not None and scan.quarantine.dir == b


def test_aggiorna_durante_il_caricamento_ripetuto_alla_fine(app, tmp_path, monkeypatch):
    qdir = tmp_path / "q"
    _quarantined(qdir, tmp_path / "home", "primo.exe")
    gate = _Gate(monkeypatch)
    win, scan, page = _window(qdir, tmp_path)
    assert gate.started.wait(10)
    # Una voce nuova mentre il caricamento è in corso, poi «Aggiorna».
    _quarantined(qdir, tmp_path / "home", "secondo.exe")
    page.refresh()
    assert gate.calls == [qdir], "un secondo caricamento in parallelo al primo"
    gate.open.set()
    _wait(page)
    assert gate.calls == [qdir, qdir]
    assert _names(page) == ["primo.exe", "secondo.exe"]


def test_errore_di_lettura_dell_elenco(app, tmp_path, monkeypatch):
    """list_entries che solleva OSError: QuarantineSnapshot.error, la pagina
    lo mostra come «fallita», l'oggetto arriva comunque alla pagina
    Scansione."""
    from klamav_py.quarantine import Quarantine

    def broken(self):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Quarantine, "list_entries", broken)
    qdir = tmp_path / "q"
    snapshot = ql.load_quarantine(qdir)
    assert snapshot.error and "Input/output error" in snapshot.error
    assert snapshot.entries == []

    win, scan, page = _window(qdir, tmp_path)
    _wait(page)
    assert "Impossibile leggere la quarantena" in page.health_label.text()
    assert "Input/output error" in page.health_label.text()
    assert page.table.rowCount() == 0 and _buttons(page) == (False, False)
    assert page.quarantine is not None and scan.quarantine is page.quarantine


def test_quarantena_di_una_cartella_precedente_ignorata(app, tmp_path):
    from klamav_py.quarantine import Quarantine

    scan = mw.ScanPage(mw.ClamdEndpoint(), tmp_path / "a", mw.HistoryManager(tmp_path / "h.json"))
    old = Quarantine(tmp_path / "a")
    scan.set_quarantine_dir(tmp_path / "b")
    scan.set_quarantine(old)  # pronta per a, arrivata dopo il cambio
    assert scan.quarantine is None
    new = Quarantine(tmp_path / "b")
    scan.set_quarantine(new)
    assert scan.quarantine is new
