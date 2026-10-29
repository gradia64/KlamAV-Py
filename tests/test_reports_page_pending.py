"""
Pagina Segnalazioni e registro delle segnalazioni da valutare.

La pagina legge il registro (scritto anche dal timer di sistema) fuori dal
thread della GUI, all'avvio, a ogni apertura e con «Aggiorna», e toglie le
voci già valutate (anche dalla CLI) o il cui file non c'è più. Presa
visione ed eliminazione tolgono la voce anche dal registro. Una lettura
che si sovrappone a una di queste azioni si ripete, così una voce appena
gestita non ricompare; una segnalazione arrivata in diretta durante la
lettura resta.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.acknowledged import AckRegistry, PendingReport, hash_file  # noqa: E402
from klamav_py.quarantine_policy import REASON_MAIL_STORE  # noqa: E402

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(app, tmp_path, monkeypatch):
    assert importlib.util.find_spec("klamav_py.pending_reports") is not None, (
        "nessun registro delle segnalazioni da valutare"
    )
    PendingStore = importlib.import_module("klamav_py.pending_reports").PendingStore

    answers: list = []
    shown: list = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: shown.append(a[2]) or answers.pop(0)))
    mail = tmp_path / "mail"
    mail.mkdir()
    registry = AckRegistry(tmp_path / "data" / "acknowledged.json")
    store = PendingStore(tmp_path / "data" / "pending-reports.json")
    return mail, registry, store, answers


def _page(env, parent=None):
    _mail, registry, store, _answers = env
    assert "pending_store" in inspect.signature(mw.ReportsPage).parameters, (
        "la pagina Segnalazioni non legge le segnalazioni registrate dalle scansioni"
    )
    page = mw.ReportsPage(registry, parent, pending_store=store)
    _wait(page)
    return page


def _wait(page, timeout=10):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if not getattr(page, "_pending_loading", False):
            app.processEvents()
            return
        time.sleep(0.01)
    pytest.fail("lettura delle segnalazioni non terminata")


def _report(path: Path) -> PendingReport:
    return PendingReport(str(path), PHISHING, REASON_MAIL_STORE, hash_file(path), time.time())


def _paths(page) -> list[str]:
    return sorted(r.path for r in page.pending())


def _select(page, row=0):
    page.pending_table.clearSelection()
    page.pending_table.selectRow(row)


def test_segnalazione_del_timer_nella_pagina(env):
    mail, _registry, store, _ = env
    notte = mail / "39356_r0"
    notte.write_bytes(b"phishing")
    store.record(_report(notte))  # come la CLI del timer

    page = _page(env)
    assert _paths(page) == [str(notte)]
    assert page.pending_table.rowCount() == 1


def test_apertura_della_pagina_rilegge(env):
    mail, _registry, store, _ = env
    window = QWidget()
    page = _page(env, window)
    assert page.pending() == []
    dopo = mail / "dopo"
    dopo.write_bytes(b"phishing")
    store.record(_report(dopo))  # il timer gira con la finestra aperta
    window.show()
    try:
        _wait(page)
        assert _paths(page) == [str(dopo)]
    finally:
        window.close()


def test_presa_visione_toglie_dal_registro(env):
    mail, registry, store, _ = env
    a, b = mail / "a", mail / "b"
    a.write_bytes(b"stesso phishing")
    b.write_bytes(b"stesso phishing")
    store.record(_report(a))
    store.record(_report(b))
    page = _page(env)
    _select(page)
    page._acknowledge_selected()
    assert page.pending() == []
    assert store.reports() == [], "la voce torna al prossimo avvio"
    (entry,) = registry.entries()
    assert entry.sha256 == hash_file(a).sha256


def test_eliminazione_toglie_dal_registro(env):
    mail, _registry, store, answers = env
    msg = mail / "1"
    msg.write_bytes(b"phishing")
    store.record(_report(msg))
    page = _page(env)
    _select(page)
    answers.append(QMessageBox.Yes)
    page._delete_selected()
    assert not msg.exists() and page.pending() == [] and store.reports() == []


def test_valutate_altrove_e_file_spariti_tolti_alla_lettura(env):
    mail, registry, store, _ = env
    valutato, sparito, resta = mail / "v", mail / "s", mail / "r"
    for f in (valutato, sparito, resta):
        f.write_bytes(f.name.encode())
        store.record(_report(f))
    # Presa visione dalla CLI (--acknowledge) e messaggio tolto da KMail.
    registry.acknowledge(hash_file(valutato).sha256, PHISHING, valutato)
    sparito.unlink()

    page = _page(env)
    assert _paths(page) == [str(resta)]
    assert [r.path for r in store.reports()] == [str(resta)]


@pytest.fixture
def held_prune(env, monkeypatch):
    """PendingStore.prune tenuto aperto da un Event (lettura in corso)."""
    _mail, _registry, store, _ = env
    started, release = threading.Event(), threading.Event()
    real = type(store).prune
    calls = []

    def prune(self, *a, **k):
        calls.append(1)
        if len(calls) == 2:  # la seconda lettura, avviata dal test
            started.set()
            assert release.wait(10), "lettura mai sbloccata"
        return real(self, *a, **k)

    monkeypatch.setattr(type(store), "prune", prune)
    return started, release


def test_segnalazione_in_diretta_durante_la_lettura_resta(env, held_prune):
    mail, _registry, _store, _ = env
    started, release = held_prune
    page = _page(env)
    page.reload_pending()
    assert started.wait(10)
    live = mail / "live"
    live.write_bytes(b"phishing")
    page.add_report(_report(live))  # dal worker di una scansione della finestra
    release.set()
    _wait(page)
    assert _paths(page) == [str(live)]


def test_voce_gestita_durante_la_lettura_non_ricompare(env, held_prune):
    mail, _registry, store, _ = env
    started, release = held_prune
    msg = mail / "1"
    msg.write_bytes(b"phishing")
    store.record(_report(msg))
    page = _page(env)
    page.reload_pending()
    assert started.wait(10)
    _select(page)
    page._acknowledge_selected()  # mentre la lettura vede ancora la voce
    release.set()
    _wait(page)
    assert page.pending() == [] and store.reports() == []
