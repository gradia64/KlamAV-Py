"""
Confronto delle versioni del controllo aggiornamenti (GitHub Releases).

Prima i suffissi di pre-release venivano scartati: 0.1.12-rc1 e 0.1.12
risultavano uguali, e chi aveva installato la candidata non veniva
avvisato della versione finale.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from klamav_py.gui.update_check_worker import UpdateCheckWorker  # noqa: E402

compare = UpdateCheckWorker._version_compare


@pytest.mark.parametrize(
    "a, b, atteso",
    [
        ("0.1.12", "0.1.12-rc1", 1),  # la finale supera la candidata
        ("0.1.12-rc1", "0.1.11", 1),
        ("0.1.12-rc2", "0.1.12-rc1", 1),
        ("0.1.12-rc10", "0.1.12-rc9", 1),  # numerico, non lessicografico
        ("0.1.12rc1", "0.1.12-rc1", 0),  # stessa candidata, due grafie
        ("0.1.12-beta1", "0.1.12-alpha3", 1),
        ("0.1.12-rc1", "0.1.12b5", 1),
        ("0.1.12-a1", "0.1.12.dev1", 1),
        ("0.1.12-qualcosa", "0.1.12", -1),  # suffisso sconosciuto: pre-release
        ("0.1.10", "0.1.9", 1),
        ("0.2.0", "0.1.99", 1),
        ("0.1", "0.1.0", 0),  # zeri finali ignorati
        ("v0.1.12", "0.1.12", 0),
        ("0.1.12+deb1", "0.1.12", 0),  # metadati di build
    ],
)
def test_confronto(a, b, atteso):
    assert compare(a, b) == atteso
    assert compare(b, a) == -atteso
