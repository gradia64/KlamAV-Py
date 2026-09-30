"""
Presa visione nella GUI: ScanWorker consulta il registro dopo la policy,
la pagina Segnalazioni offre presa visione ed eliminazione.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.acknowledged import AckRegistry, PendingReport, hash_file  # noqa: E402
from klamav_py.clamd_client import ClamdEndpoint, ScanResult  # noqa: E402
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402
from klamav_py.quarantine_policy import REASON_HEURISTIC, REASON_MAIL_STORE, QuarantinePolicy  # noqa: E402
from klamav_py.scan_totals import ScanTotals  # noqa: E402

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"
TROJAN = "Win.Trojan.Agent-1"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path):
    mail = tmp_path / "mail"
    mail.mkdir()
    return tmp_path, mail, AckRegistry(tmp_path / "data" / "acknowledged.json")


def _client(verdicts):
    class Client:
        def __init__(self, **kw):
            self.skipped = Counter()

        def scan_stream(self, target, **kw):
            for path, sig in verdicts:
                yield ScanResult(str(path), "FOUND", sig)
    return Client


def _run(env, verdicts, auto_quarantine=True):
    tmp, mail, reg = env
    w = ScanWorker(
        endpoint=ClamdEndpoint(), target=tmp, client_factory=_client(verdicts),
        quarantine_dir=tmp / "q", auto_quarantine=auto_quarantine,
        policy=QuarantinePolicy([mail]), acknowledgements=reg,
    )
    got = {"finished": [], "results": [], "ack": [], "reports": [], "outcomes": []}
    w.finished_scan.connect(got["finished"].append)
    w.result_ready.connect(got["results"].append)
    w.acknowledged.connect(lambda p, s: got["ack"].append(p))
    w.report_only_found.connect(got["reports"].append)
    w.quarantine_outcome.connect(lambda p, o, d: got["outcomes"].append(o))
    w.run()
    return got


def test_segnalazione_valutata_non_contata(env):
    tmp, mail, reg = env
    msg = mail / "1"
    msg.write_bytes(b"phishing")
    e = reg.acknowledge(hash_file(msg).sha256, PHISHING, msg)
    got = _run(env, [(msg, PHISHING)])
    assert got["finished"] == [ScanTotals(scanned=1, acknowledged=1)]
    assert got["ack"] == [str(msg)] and got["results"] == [] and got["reports"] == []
    assert msg.exists()
    # Ultimo riscontro aggiornato.
    assert reg.entries()[0].last_seen >= e.last_seen


def test_stesso_hash_da_quarantena_va_in_quarantena(env):
    tmp, mail, reg = env
    virus = tmp / "virus.exe"
    virus.write_bytes(b"phishing")
    reg.acknowledge(hash_file(virus).sha256, TROJAN, virus)
    got = _run(env, [(virus, TROJAN)])
    assert got["finished"] == [ScanTotals(scanned=1, infections=1)]
    assert got["outcomes"] == ["quarantined"] and not virus.exists()
    assert got["ack"] == [] and got["reports"] == []


def test_segnalazione_nuova_offerta_alla_pagina(env):
    tmp, mail, reg = env
    msg = mail / "2"
    msg.write_bytes(b"phishing")
    got = _run(env, [(msg, PHISHING)], auto_quarantine=False)
    assert got["finished"] == [ScanTotals(scanned=1, infections=1)]
    (report,) = got["reports"]
    assert report.path == str(msg) and report.signature == PHISHING
    assert report.identity == hash_file(msg) and report.reason == REASON_HEURISTIC


# -- pagina ------------------------------------------------------------------

@pytest.fixture
def page(app, env, monkeypatch):
    shown = []
    answers = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: shown.append(a[2]) or answers.pop(0)),
    )
    p = mw.ReportsPage(env[2])
    p.shown, p.answers = shown, answers
    return p


def _report(path: Path, sig=PHISHING):
    return PendingReport(str(path), sig, REASON_MAIL_STORE, hash_file(path), 0.0)


def _select(table, *rows):
    table.clearSelection()
    for r in rows:
        table.selectRow(r)


def test_presa_visione_registra_il_contenuto_rilevato(page, env):
    tmp, mail, reg = env
    msg = mail / "1"
    msg.write_bytes(b"phishing")
    copia = mail / "copia"
    copia.write_bytes(b"phishing")
    rilevato = _report(msg)
    page.add_report(rilevato)
    page.add_report(_report(copia))
    msg.write_bytes(b"cambiato dopo il rilevamento")
    _select(page.pending_table, 0)
    page._acknowledge_selected()
    (e,) = reg.entries()
    assert e.sha256 == rilevato.identity.sha256 and e.first_path == str(msg)
    # Stesso contenuto in un altro percorso: valutato anch'esso.
    assert page.pending() == [] and page.ack_table.rowCount() == 1

    _select(page.ack_table, 0)
    page._revoke_selected()
    assert reg.entries() == [] and page.ack_table.rowCount() == 0


def test_eliminazione_con_conferma(page, env):
    msg = env[1] / "1"
    msg.write_bytes(b"phishing")
    page.add_report(_report(msg))
    _select(page.pending_table, 0)
    page.answers.append(QMessageBox.No)
    page._delete_selected()
    assert msg.exists() and len(page.pending()) == 1
    assert "akonadictl fsck" in page.shown[-1]

    page.answers.append(QMessageBox.Yes)
    page._delete_selected()
    assert not msg.exists() and page.pending() == []


def test_eliminazione_rifiutata_se_il_file_e_stato_sostituito(page, env):
    msg = env[1] / "1"
    msg.write_bytes(b"phishing")
    page.add_report(_report(msg))
    nuovo = env[1] / ".tmp"
    nuovo.write_bytes(b"versione nuova")  # prima di togliere il vecchio
    os.replace(nuovo, msg)
    _select(page.pending_table, 0)
    page.answers.append(QMessageBox.Yes)
    page._delete_selected()
    assert msg.read_bytes() == b"versione nuova"
    assert "sostituito" in page.shown[-1] and len(page.pending()) == 1


def test_senza_identita_nessuna_azione(page, env):
    msg = env[1] / "1"
    msg.write_bytes(b"phishing")
    page.add_report(PendingReport(str(msg), PHISHING, REASON_MAIL_STORE, None, 0.0))
    _select(page.pending_table, 0)
    page._delete_selected()
    page._acknowledge_selected()
    assert msg.exists() and env[2].entries() == []
