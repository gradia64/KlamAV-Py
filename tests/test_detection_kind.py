"""
Pagina Segnalazioni Euristiche (0.2.0): il nome dice «sospetto», ma la
pagina raccoglie anche un malware riconosciuto lasciato al suo posto
(archivio di posta, file con più nomi). La colonna Tipo lo distingue.
"""

from __future__ import annotations

import os
import time

import pytest

from klamav_py.quarantine_policy import (
    KIND_HARDLINKS, KIND_HEURISTIC, KIND_KNOWN, KIND_MAIL_STORE,
    REASON_HARDLINKS, REASON_HEURISTIC, REASON_MAIL_STORE, detection_kind,
)

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"


@pytest.mark.parametrize("signature, reason, kind", [
    (PHISHING, REASON_HEURISTIC, KIND_HEURISTIC),
    # Un'email sospetta in un archivio di posta resta un sospetto.
    (PHISHING, REASON_MAIL_STORE, KIND_HEURISTIC),
    (PHISHING, REASON_HARDLINKS, KIND_HEURISTIC),
    ("Win.Trojan.Agent-1", REASON_MAIL_STORE, KIND_MAIL_STORE),
    ("Eicar-Test-Signature", REASON_HARDLINKS, KIND_HARDLINKS),
    ("Eicar-Test-Signature", "motivo di una versione futura", KIND_KNOWN),
    ("Eicar-Test-Signature", None, KIND_KNOWN),
    (None, None, KIND_KNOWN),
])
def test_tipo(signature, reason, kind):
    assert detection_kind(signature, reason) == kind


def test_solo_l_euristica_e_un_sospetto():
    for kind in (KIND_MAIL_STORE, KIND_HARDLINKS, KIND_KNOWN):
        assert kind.startswith("Firma nota")
    assert "sospetto" in KIND_HEURISTIC


# -- GUI -------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.acknowledged import AckRegistry, PendingReport  # noqa: E402
from klamav_py.pending_reports import PendingStore  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _page(tmp_path):
    return mw.ReportsPage(AckRegistry(tmp_path / "ack.json"),
                          pending_store=PendingStore(tmp_path / "pending.json"))


def test_colonna_tipo_e_malware_noto_evidenziato(app, tmp_path):
    page = _page(tmp_path)
    header = [page.pending_table.horizontalHeaderItem(c).text()
              for c in range(page.pending_table.columnCount())]
    assert header == ["File", "Firma", "Tipo", "Rilevato"]
    now = time.time()
    page.add_report(PendingReport(str(tmp_path / "a.eml"), PHISHING, REASON_MAIL_STORE, None, now))
    page.add_report(PendingReport(str(tmp_path / "b.eml"), "Win.Trojan.Agent-1",
                                  REASON_MAIL_STORE, None, now))
    tipi = {page.pending_table.item(r, 1).text(): page.pending_table.item(r, 2)
            for r in range(page.pending_table.rowCount())}
    assert tipi[PHISHING].text() == KIND_HEURISTIC and not tipi[PHISHING].font().bold()
    assert tipi["Win.Trojan.Agent-1"].text() == KIND_MAIL_STORE
    assert tipi["Win.Trojan.Agent-1"].font().bold()


def test_titolo_e_voce_di_menu(app, tmp_path):
    page = _page(tmp_path)
    assert "Segnalazioni Euristiche" in [l.text() for l in page.findChildren(QLabel)]
    src = (mw.Path(mw.__file__)).read_text(encoding="utf-8")
    assert '_add_sidebar_item("Segnalazioni Euristiche"' in src
