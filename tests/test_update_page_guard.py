"""
UpdatePage con la regola di db_update_policy: pulsante, spiegazione e
guardia in _start_update, che è anche il percorso dell'aggiornamento
all'avvio (startup_update) e non deve chiedere una password per
riavviare un freshclam che non tocca il database di clamd.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import ClamdEndpoint  # noqa: E402
from klamav_py.db_freshness import DbInfo  # noqa: E402

INFO = DbInfo("1.4.3", 27800, datetime(2026, 9, 27, 9, 0))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workers(app, monkeypatch):
    creati = []

    class FakeWorker:
        def __init__(self, probe):
            creati.append(probe)
            self.progress = self.finished_with = type("S", (), {"connect": lambda *a: None})()

        def start(self):
            pass

    monkeypatch.setattr(mw, "FreshclamRestartWorker", FakeWorker)
    return creati


def test_socket_unix_invariato(workers):
    page = mw.UpdatePage(lambda: ClamdEndpoint())
    page.set_db_info(INFO)
    assert page.update_button.isEnabled() and page.update_blocked_label.isHidden()
    page._start_update()
    assert len(workers) == 1


def test_tcp_remoto_pulsante_disabilitato_e_spiegato(workers):
    page = mw.UpdatePage(lambda: ClamdEndpoint.tcp("10.0.0.5"))
    page.set_db_info(INFO)
    assert not page.update_button.isEnabled()
    assert not page.update_blocked_label.isHidden()
    assert "10.0.0.5" in page.update_blocked_label.text()
    # La versione del DB remoto resta visibile.
    assert "27800" in page.db_status_label.text()


def test_avvio_automatico_non_parte_con_clamd_remoto(workers):
    # startup_update chiama _start_update direttamente, senza pulsante.
    page = mw.UpdatePage(lambda: ClamdEndpoint.tcp("10.0.0.5"))
    page.set_db_info(INFO)
    page._start_update()
    assert workers == []
    assert "non eseguito" in page.log_console.toPlainText()


def test_cambio_endpoint_ricalcolato_alla_visualizzazione(workers):
    corrente = {"ep": ClamdEndpoint()}
    page = mw.UpdatePage(lambda: corrente["ep"])
    page.set_db_info(INFO)
    assert page.update_button.isEnabled()
    corrente["ep"] = ClamdEndpoint.tcp("10.0.0.5")
    page.show()
    assert not page.update_button.isEnabled()
    page.hide()


def test_loopback_con_database_diverso(workers, monkeypatch):
    # La versione locale è un parametro della regola: la pagina usa quella
    # reale, qui la si fissa sostituendo la funzione vista da main_window.
    import klamav_py.db_update_policy as policy

    monkeypatch.setattr(
        mw, "update_availability",
        lambda ep, info: policy.update_availability(ep, info, lambda: 27795),
    )
    page = mw.UpdatePage(lambda: ClamdEndpoint.tcp("127.0.0.1"))
    page.set_db_info(INFO)
    assert not page.update_button.isEnabled()
    assert "27795" in page.update_blocked_label.text()
