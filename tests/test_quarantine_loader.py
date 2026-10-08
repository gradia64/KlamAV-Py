"""
Recupero della quarantena fuori dal thread della GUI (0.1.15).

Fino alla 0.1.14 la GUI costruiva Quarantine nel thread principale,
all'avvio e al cambio di cartella: il costruttore prepara la directory e
recupera le operazioni interrotte, e la pagina Quarantena leggeva poi
indice, indici messi da parte e orfani. Con una quarantena su un
filesystem lento la finestra si bloccava. Ora lo fa QuarantineLoadWorker.

Due verifiche: l'esito è identico a quello delle stesse chiamate fatte
come prima (indice corrotto, orfani, intento interrotto), e nessuna
chiamata al filesystem su un percorso della prova parte dal thread della
GUI (os e open strumentati) durante caricamento, aggiornamento e cambio
di cartella.
"""

from __future__ import annotations

import builtins
import functools
import inspect
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.quarantine import Quarantine  # noqa: E402


class Crash(BaseException):
    pass


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _prepare(base: Path) -> Path:
    """Una quarantena con le tre situazioni del recupero: una voce
    indicizzata, un'operazione interrotta prima dell'indice (intento) e,
    dopo, un indice corrotto che rende orfana anche la prima voce."""
    qdir = base / "q"
    home = base / "home"
    home.mkdir(parents=True)
    q = Quarantine(qdir)
    first = home / "primo.exe"
    first.write_bytes(b"uno")
    q.quarantine_file(first, "Sig.Uno")
    second = home / "secondo.exe"
    second.write_bytes(b"due")
    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)), \
            patch.object(Quarantine, "_write_index", side_effect=Crash()):
        with pytest.raises(Crash):
            q.quarantine_file(second, "Sig.Due")
    (qdir / "index.json").write_text("{rotto")
    return qdir


def _summary(qdir: Path, entries, backups, orphans) -> dict:
    """Esito confrontabile fra due copie: i nomi in quarantena hanno un
    identificatore casuale, quindi si confrontano origini e conteggi."""
    names = sorted(p.name for p in qdir.iterdir())
    return {
        "entries": sorted((Path(e.original_path).name, e.signature) for e in entries),
        "backups": len(backups),
        "orphans": len(orphans),
        "corrupt_on_disk": sum(n.startswith("index.json.corrupt-") for n in names),
        "intents_on_disk": sum(n.endswith(".intent") for n in names),
    }


def _page(directory: Path):
    params = inspect.signature(mw.QuarantinePage).parameters
    assert "quarantine_dir" in params, (
        "QuarantinePage riceve una Quarantine già costruita, cioè recuperata nel thread della GUI"
    )
    return mw.QuarantinePage(directory)


def _wait(page, timeout=10):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if page.worker is None and page.loading_label.isHidden():
            return
        time.sleep(0.01)
    pytest.fail("caricamento della quarantena non terminato")


def _rows(page) -> list[tuple[str, str]]:
    return sorted(
        (Path(page.table.item(r, 0).text()).name, page.table.item(r, 1).text())
        for r in range(page.table.rowCount())
    )


def test_esito_identico_a_quello_sincrono(app, tmp_path):
    reference = _prepare(tmp_path / "a")
    q = Quarantine(reference)  # come faceva la GUI fino alla 0.1.14
    expected = _summary(reference, q.list_entries(), q.corrupt_backups(), q.orphans())
    # Il recupero dell'intento ha completato la seconda voce; l'indice
    # corrotto è stato messo da parte e la prima voce è orfana.
    assert expected == {"entries": [("secondo.exe", "Sig.Due")], "backups": 1, "orphans": 1,
                        "corrupt_on_disk": 1, "intents_on_disk": 0}

    qdir = _prepare(tmp_path / "b")
    page = _page(qdir)
    _wait(page)
    assert _summary(qdir, page.quarantine.list_entries(), page.quarantine.corrupt_backups(),
                    page.quarantine.orphans()) == expected
    assert _rows(page) == expected["entries"]
    health = page.health_label.text()
    assert "messo da parte" in health and "1 file nella cartella" in health


def test_errore_del_costruttore_mostrato_nella_pagina(app, tmp_path):
    occupato = tmp_path / "file"
    occupato.write_text("non una cartella")
    page = _page(occupato)
    _wait(page)
    assert page.quarantine is None and page.table.rowCount() == 0
    assert "Impossibile preparare la quarantena" in page.health_label.text()


# -- nessuna lettura nel thread della GUI --------------------------------

_OS_CALLS = ("open", "stat", "lstat", "scandir", "listdir", "rename", "replace", "mkdir",
             "unlink", "chmod", "rmdir")


@pytest.fixture
def fs_calls(monkeypatch, tmp_path):
    """Registra le chiamate al filesystem su percorsi sotto tmp_path fatte
    dal thread della GUI. Attivo solo dentro `with fs_calls.watching():`."""
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


def test_avvio_aggiornamento_e_cambio_cartella_fuori_dal_thread_della_gui(
        app, tmp_path, fs_calls, monkeypatch):
    qdir = _prepare(tmp_path / "a")
    nuova = _prepare(tmp_path / "b")
    config = tmp_path / "config"
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(config))
    history = mw.HistoryManager(tmp_path / "history.json")
    settings = QSettings(mw.APP_NAME, mw.APP_NAME)
    settings.setValue("quarantine_dir", str(nuova))

    with fs_calls.watching():
        # Avvio: come MainWindow.__init__.
        scan_page = mw.ScanPage(mw.ClamdEndpoint(), qdir, history)
        page = _page(qdir)
        page.quarantine_ready.connect(scan_page.set_quarantine)
        assert not page.loading_label.isHidden()  # stato di caricamento
        _wait(page)
        assert scan_page.quarantine is page.quarantine and page.quarantine.dir == qdir
        # Aggiorna (e dopo una quarantena dalla scansione).
        page.refresh()
        _wait(page)
        # Cambio di cartella dalle Impostazioni.
        fake = SimpleNamespace(settings=settings, scan_page=scan_page, quarantine_page=page)
        mw.MainWindow._apply_quarantine_dir(fake)
        assert scan_page.quarantine is None
        _wait(page)
        assert scan_page.quarantine is page.quarantine and page.quarantine.dir == nuova

    assert _rows(page) == [("secondo.exe", "Sig.Due")]
    assert fs_calls.on_main == []
