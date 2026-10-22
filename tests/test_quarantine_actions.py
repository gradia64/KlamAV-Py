"""
Ripristino, eliminazione e «Metti in quarantena» fuori dal thread della GUI.

Dalla 0.1.15 recupero e letture della quarantena girano nel worker, ma le
azioni su richiesta no: QuarantinePage._restore_selected e _delete_selected
chiamavano Quarantine.restore e delete nel thread della GUI, e
ScanPage._quarantine_selected spostava i file uno per volta nello stesso
modo. Con la quarantena (o i file da spostare) su un mount lento la
finestra si bloccava.

Il filesystem è strumentato come in tests/test_quarantine_loader.py: ogni
chiamata su un percorso della prova fatta dal thread della GUI viene
registrata. Le operazioni tenute aperte usano un threading.Event, mai
sleep; nel thread della GUI non si bloccano (con il codice vecchio il test
fallirebbe per asserzione, non per un'attesa infinita).
"""

from __future__ import annotations

import builtins
import functools
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem, QMessageBox, QWidget  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.quarantine import Quarantine  # noqa: E402

_OS_CALLS = ("open", "stat", "lstat", "scandir", "listdir", "rename", "replace", "mkdir",
             "unlink", "chmod", "rmdir", "link")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def boxes(monkeypatch):
    """QMessageBox intercettate: (tipo, titolo, testo); question risponde Sì."""
    seen: list[tuple[str, str, str]] = []

    def record(kind, answer=None):
        def box(parent, title, text, *a, **k):
            seen.append((kind, title, text))
            return answer
        return staticmethod(box)

    monkeypatch.setattr(QMessageBox, "information", record("information"))
    monkeypatch.setattr(QMessageBox, "warning", record("warning"))
    monkeypatch.setattr(QMessageBox, "question", record("question", QMessageBox.Yes))
    return seen


@pytest.fixture
def fs_calls(monkeypatch, tmp_path):
    main = threading.main_thread()
    root = str(tmp_path)
    state = SimpleNamespace(on_main=[], active=False)

    def wrap(name, fn):
        @functools.wraps(fn)
        def wrapper(path, *a, **k):
            if state.active and threading.current_thread() is main:
                if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).startswith(root):
                    state.on_main.append((name, os.fsdecode(path)))
            return fn(path, *a, **k)
        return wrapper

    for name in _OS_CALLS:
        monkeypatch.setattr(os, name, wrap(f"os.{name}", getattr(os, name)))
    monkeypatch.setattr(builtins, "open", wrap("open", builtins.open))
    monkeypatch.setattr(io, "open", wrap("io.open", io.open))

    class Watching:
        def __enter__(self):
            state.active = True

        def __exit__(self, *exc):
            state.active = False

    state.watching = Watching
    return state


def _until(predicate, what: str, timeout=10):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"{what}: non terminato")


def _idle(page) -> bool:
    return (page.worker is None and getattr(page, "action_worker", None) is None
            and page.loading_label.isHidden())


def _window(qdir: Path, tmp_path: Path):
    win = QWidget()
    scan = mw.ScanPage(mw.ClamdEndpoint(), qdir, mw.HistoryManager(tmp_path / "h.json"),
                       parent=win)
    page = mw.QuarantinePage(qdir, parent=win)
    page.quarantine_ready.connect(scan.set_quarantine)
    win.quarantine_page = page
    _until(lambda: _idle(page), "caricamento")
    return win, scan, page


def _infetto(tmp_path: Path, name: str = "virus.exe") -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    f = home / name
    f.write_bytes(b"X5O!P%@AP")
    return f


def _with_entry(tmp_path: Path) -> tuple[Path, Path]:
    qdir = tmp_path / "q"
    vittima = _infetto(tmp_path)
    Quarantine(qdir).quarantine_file(vittima, "Eicar-Test-Signature")
    return qdir, vittima


def _scan_idle(scan) -> bool:
    return getattr(scan, "quarantine_worker", None) is None


# -- nessuna chiamata dal thread della GUI, esiti come prima ---------------

def test_ripristino_fuori_dal_thread_della_gui(app, tmp_path, boxes, fs_calls):
    qdir, vittima = _with_entry(tmp_path)
    _win, _scan, page = _window(qdir, tmp_path)
    page.table.selectRow(0)

    with fs_calls.watching():
        page._restore_selected()
        _until(lambda: _idle(page), "ripristino")

    assert fs_calls.on_main == []
    assert vittima.read_bytes() == b"X5O!P%@AP"
    assert page.table.rowCount() == 0
    assert boxes == [("information", "Ripristinato", f"File ripristinato in {vittima}")]


def test_ripristino_fallito_mostrato(app, tmp_path, boxes, fs_calls):
    qdir, vittima = _with_entry(tmp_path)
    _win, _scan, page = _window(qdir, tmp_path)
    vittima.write_bytes(b"rioccupato")  # il percorso originale è di nuovo occupato
    page.table.selectRow(0)

    with fs_calls.watching():
        page._restore_selected()
        _until(lambda: _idle(page), "ripristino")

    assert fs_calls.on_main == []
    ((kind, title, text),) = boxes
    assert (kind, title) == ("warning", "Ripristino fallito") and "esiste già" in text
    assert page.table.rowCount() == 1 and page.status.ready


def test_eliminazione_fuori_dal_thread_della_gui(app, tmp_path, boxes, fs_calls):
    qdir, _ = _with_entry(tmp_path)
    _win, _scan, page = _window(qdir, tmp_path)
    page.table.selectRow(0)
    (dest,) = [p for p in qdir.iterdir() if p.name[0].isdigit()]

    with fs_calls.watching():
        page._delete_selected()
        _until(lambda: _idle(page), "eliminazione")

    assert fs_calls.on_main == []
    assert [b[0] for b in boxes] == ["question"]
    assert not dest.exists() and page.table.rowCount() == 0


def _select(scan, path: Path, infected: bool = True) -> QListWidgetItem:
    item = QListWidgetItem(f"INFETTO — {path}")
    if infected:
        item.setData(Qt.UserRole, {"path": str(path), "signature": "Eicar-Test-Signature"})
    scan.results_list.addItem(item)
    item.setSelected(True)
    return item


def test_metti_in_quarantena_fuori_dal_thread_della_gui(app, tmp_path, boxes, fs_calls):
    qdir = tmp_path / "q"
    _win, scan, page = _window(qdir, tmp_path)
    primo = _infetto(tmp_path, "primo.exe")
    mancante = tmp_path / "home" / "sparito.exe"
    secondo = _infetto(tmp_path, "secondo.exe")
    items = [_select(scan, primo), _select(scan, mancante), _select(scan, secondo),
             _select(scan, tmp_path / "pulito.txt", infected=False)]

    with fs_calls.watching():
        scan._quarantine_selected()
        _until(lambda: _scan_idle(scan) and _idle(page), "quarantena dei selezionati")

    assert fs_calls.on_main == []
    assert not primo.exists() and not secondo.exists()
    assert items[0].text().startswith("IN QUARANTENA — ") and items[0].data(Qt.UserRole) is None
    assert items[2].text().startswith("IN QUARANTENA — ")
    assert items[1].data(Qt.UserRole) is not None  # errore: la riga resta com'era
    ((kind, title, text),) = boxes
    assert (kind, title) == ("warning", "Quarantena fallita") and str(mancante) in text
    assert sorted(page.table.item(r, 0).text() for r in range(page.table.rowCount())) == \
        sorted([str(primo), str(secondo)])


def test_selezione_senza_infetti(app, tmp_path, boxes):
    _win, scan, _page = _window(tmp_path / "q", tmp_path)
    _select(scan, tmp_path / "pulito.txt", infected=False)
    scan._quarantine_selected()
    assert boxes == [("information", "Nessun file infetto selezionato",
                      "La selezione non contiene file infetti da mettere in quarantena.")]
    assert _scan_idle(scan)


# -- operazione in corso --------------------------------------------------

@pytest.fixture
def held_restore(monkeypatch):
    """Quarantine.restore tenuto aperto da un Event nel worker; nel thread
    della GUI (codice vecchio) non si blocca e lo registra."""
    state = SimpleNamespace(started=threading.Event(), release=threading.Event(),
                            on_main=False)
    real = Quarantine.restore

    def restore(self, *a, **k):
        if threading.current_thread() is threading.main_thread():
            state.on_main = True
        else:
            state.started.set()
            assert state.release.wait(10), "ripristino mai sbloccato"
        return real(self, *a, **k)

    monkeypatch.setattr(Quarantine, "restore", restore)
    return state


def test_operazione_in_corso_pulsanti_spenti_e_messaggio(app, tmp_path, boxes, held_restore):
    qdir, vittima = _with_entry(tmp_path)
    _win, scan, page = _window(qdir, tmp_path)
    page.table.selectRow(0)
    altro = _infetto(tmp_path, "altro.exe")
    _select(scan, altro)

    try:
        page._restore_selected()
        assert not held_restore.on_main, "ripristino eseguito nel thread della GUI"
        assert held_restore.started.wait(10)
        assert page.status.kind == "operazione in corso"
        assert not page.restore_button.isEnabled() and not page.delete_button.isEnabled()
        page._restore_selected()   # un secondo clic non avvia nulla
        scan._quarantine_selected()
        assert boxes[-1][1] == "Quarantena occupata" and "attendi" in boxes[-1][2]
        assert altro.exists()
    finally:
        held_restore.release.set()
    _until(lambda: _idle(page), "ripristino")
    assert page.status.ready and vittima.exists()
    assert [b[1] for b in boxes] == ["Quarantena occupata", "Ripristinato"]


def test_chiusura_attende_l_operazione_in_corso(app, tmp_path, boxes, held_restore):
    qdir, vittima = _with_entry(tmp_path)
    _win, scan, page = _window(qdir, tmp_path)
    page.table.selectRow(0)
    page._restore_selected()
    assert not held_restore.on_main, "ripristino eseguito nel thread della GUI"
    worker = getattr(page, "action_worker", None)
    assert worker is not None
    assert held_restore.started.wait(10)
    # requestInterruption è chiamata da _shutdown_workers prima dell'attesa:
    # qui sblocca il ripristino, così solo un'attesa vera lo vede finito.
    worker.requestInterruption = held_restore.release.set

    fake = SimpleNamespace(
        scan_page=scan, quarantine_page=page,
        update_page=SimpleNamespace(worker=None),
        settings_page=SimpleNamespace(_update_check_worker=None),
    )
    try:
        mw.MainWindow._shutdown_workers(fake)
        assert worker.isFinished(), "la chiusura non ha atteso l'operazione"
        assert vittima.exists()
    finally:
        held_restore.release.set()
        worker.wait(10000)
    _until(lambda: _idle(page), "ripristino")
