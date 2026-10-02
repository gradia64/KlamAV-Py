"""
Ambito della presa visione, detto all'utente.

La chiave è SHA-256 del contenuto più firma (decisione della 0.1.13, senza
campo «motivo»: per una stessa chiave non può differire). Ne segue che una
copia identica in un altro percorso, con la stessa firma, risulta già
valutata. È voluto, ma l'utente deve saperlo: una frase unica
(acknowledged.SCOPE_NOTE) nella conferma della CLI e della GUI e nella
pagina Segnalazioni, che dice anche di essere «della sessione corrente».
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

import klamav_py.cli as cli  # noqa: E402
import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.acknowledged import SCOPE_NOTE, AckRegistry, PendingReport, hash_file  # noqa: E402
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ScanResult  # noqa: E402
from klamav_py.quarantine_policy import REASON_HEURISTIC  # noqa: E402

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"


class FakeClient:
    def __init__(self, **kw):
        self.skipped = Counter()

    def ping(self):
        return True

    def scan_stream(self, root, exclude_dirs=None, **kw):
        for f in ClamdClient._iter_files(Path(root).resolve(), exclude_dirs):
            yield ScanResult(str(f), "FOUND", PHISHING)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "a").mkdir(parents=True)
    (h / "b").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    return h


def test_cli_conferma_dice_l_ambito_e_la_copia_risulta_valutata(home, capsys):
    originale = home / "a" / "msg"
    originale.write_bytes(b"phishing")
    assert cli.main(["--acknowledge", str(originale)]) == 0
    out = capsys.readouterr().out
    assert "Presa visione registrata" in out and SCOPE_NOTE in out

    copia = home / "b" / "copia"
    copia.write_bytes(b"phishing")
    assert cli.main(["scan", str(home / "b"), "--quiet"]) == 0
    assert f"GIÀ VALUTATO: {copia}" in capsys.readouterr().out


def test_cli_nessuna_nota_se_non_registra_nulla(home, capsys):
    assert cli.main(["--acknowledge", str(home / "niente")]) == 2
    assert SCOPE_NOTE not in capsys.readouterr().out


@pytest.fixture
def page(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    return mw.ReportsPage(AckRegistry(tmp_path / "data" / "acknowledged.json"))


def _texts(page) -> str:
    return "\n".join(label.text() for label in page.findChildren(QLabel))


def test_pagina_dice_ambito_e_sessione(page):
    text = _texts(page)
    assert SCOPE_NOTE in text
    assert "di questa sessione" in text
    assert "journalctl --user -u klamav-scan.service" in text
    assert "klamav-py --acknowledge" in text


def test_pagina_conferma_con_l_ambito(page, tmp_path):
    msg = tmp_path / "msg"
    msg.write_bytes(b"phishing")
    page.add_report(PendingReport(str(msg), PHISHING, REASON_HEURISTIC, hash_file(msg), 0.0))
    assert page.ack_status_label.isHidden()
    page.pending_table.selectRow(0)
    page._acknowledge_selected()
    assert not page.ack_status_label.isHidden()
    assert page.ack_status_label.text() == (
        f"Presa visione registrata per 1 segnalazioni. {SCOPE_NOTE}"
    )
