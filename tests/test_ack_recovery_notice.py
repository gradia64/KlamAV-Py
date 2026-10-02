"""
Registro delle prese visione messo da parte: avviso in ogni punto.

Con un registro corrotto, --acknowledge e --unacknowledge e la pagina
Segnalazioni lo mettevano da parte (.corrupt-*) senza dire nulla:
--acknowledge usciva con 0 e «Presa visione registrata», e le prese visione
precedenti sparivano. L'utente vedeva solo le vecchie segnalazioni tornare
nuove. Ora lo stesso avviso di --list-acknowledged e delle scansioni, con
il percorso del file messo da parte e la precisazione che le prese visione
precedenti sono lì, non cancellate. Il codice di uscita non cambia.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.cli as cli  # noqa: E402
import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.acknowledged import AckRegistry, PendingReport, hash_file  # noqa: E402
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ScanResult  # noqa: E402
from klamav_py.quarantine_policy import REASON_MAIL_STORE  # noqa: E402

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"
ROTTO = "{rotto"


class FakeClient:
    def __init__(self, **kw):
        self.skipped = Counter()

    def scan_stream(self, root, exclude_dirs=None, **kw):
        for f in ClamdClient._iter_files(Path(root).resolve(), exclude_dirs):
            yield ScanResult(str(f), "FOUND", PHISHING)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".local/share/local-mail/trash/new").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    return h


def _mail(home, name="1", content=b"phishing"):
    p = home / ".local/share/local-mail/trash/new" / name
    p.write_bytes(content)
    return p


def _corrupt(registry: AckRegistry) -> None:
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text(ROTTO)


def _backup(registry: AckRegistry) -> Path:
    (backup,) = registry.path.parent.glob(registry.path.name + ".corrupt-*")
    assert backup.read_text() == ROTTO  # mai cancellato
    return backup


def _assert_notice(text: str, backup: Path) -> None:
    assert f"messo da parte in {backup}" in text
    assert "non sono state cancellate" in text


# -- CLI ---------------------------------------------------------------------

def test_acknowledge_con_registro_corrotto(home, capsys):
    mail = _mail(home)
    _corrupt(AckRegistry())
    assert cli.main(["--acknowledge", str(mail)]) == 0  # operazione riuscita
    out = capsys.readouterr()
    assert "Presa visione registrata" in out.out
    _assert_notice(out.err, _backup(AckRegistry()))
    (entry,) = AckRegistry().entries()
    assert entry.first_path == str(mail)


@pytest.mark.parametrize("valore", ["percorso", "prefisso"])
def test_unacknowledge_con_registro_corrotto(home, capsys, valore):
    mail = _mail(home)
    _corrupt(AckRegistry())
    arg = str(mail) if valore == "percorso" else "abcdef"
    # Il registro riparte vuoto: nulla da revocare, uscita 2 come sempre.
    assert cli.main(["--unacknowledge", arg]) == 2
    _assert_notice(capsys.readouterr().err, _backup(AckRegistry()))


def test_list_acknowledged_avviso_una_volta(home, capsys):
    _corrupt(AckRegistry())
    assert cli.main(["--list-acknowledged"]) == 0
    err = capsys.readouterr().err
    assert err.count("messo da parte") == 1
    _assert_notice(err, _backup(AckRegistry()))


def test_registro_valido_nessun_avviso(home, capsys):
    assert cli.main(["--acknowledge", str(_mail(home))]) == 0
    assert "messo da parte" not in capsys.readouterr().err


# -- pagina Segnalazioni -----------------------------------------------------

@pytest.fixture
def page(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    registry = AckRegistry(tmp_path / "data" / "acknowledged.json")
    p = mw.ReportsPage(registry)
    return p, registry


def _visible_notice(p) -> str:
    assert not p.recovery_label.isHidden()
    return p.recovery_label.text()


def test_pagina_ricarica(page):
    p, registry = page
    assert p.recovery_label.isHidden()
    _corrupt(registry)
    p.refresh_acknowledged()
    _assert_notice(_visible_notice(p), _backup(registry))


def test_pagina_presa_visione(page, tmp_path):
    p, registry = page
    msg = tmp_path / "msg"
    msg.write_bytes(b"phishing")
    p.add_report(PendingReport(str(msg), PHISHING, REASON_MAIL_STORE, hash_file(msg), 0.0))
    _corrupt(registry)
    p.pending_table.selectRow(0)
    p._acknowledge_selected()
    _assert_notice(_visible_notice(p), _backup(registry))
    assert len(registry.entries()) == 1


def test_pagina_revoca(page):
    p, registry = page
    registry.acknowledge("a" * 64, PHISHING, Path("/a"))
    p.refresh_acknowledged()
    _corrupt(registry)
    p.ack_table.selectRow(0)
    p._revoke_selected()
    _assert_notice(_visible_notice(p), _backup(registry))
