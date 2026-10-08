"""
PySide6 6.12 su Python < 3.12: ogni emissione di un segnale toglie un
riferimento a True.

Con Python 3.10 e 3.11 True non è immortale: dopo qualche centinaio di
segnali il suo refcount arriva a zero e l'interprete abortisce
(«bool_dealloc: deallocating True or False»), durante l'esecuzione e non
solo all'uscita. La GUI emette segnali a ogni file scansionato, quindi
abortiva alla prima scansione. Dalla 3.12 True è immortale e la perdita
non ha effetto (e qui non si vede: il refcount di un immortale non cambia).

Trovato nella CI della 0.1.15 (test_qthread_retire falliva su 3.10 e 3.11
dopo il passaggio da 6.11.2 a 6.12.0). Segnalato a monte come
https://qt-project.atlassian.net/browse/PYSIDE-3474: per Qt è una
conseguenza voluta della build con gli header di Python 3.12 (PYSIDE-3424),
non un difetto da correggere, quindi vale per tutta la 6.12.x.
requirements.txt e pyproject.toml restano alla 6.11.x con Python < 3.12;
questo test dice subito, e con il motivo giusto, se la versione installata
ha il difetto (per esempio un limite tolto troppo presto).
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal  # noqa: E402

EMISSIONI = 1000


class _Emettitore(QObject):
    senza_argomenti = Signal()
    con_argomento = Signal(object)


def test_emettere_un_segnale_non_consuma_riferimenti_a_true():
    import PySide6

    QCoreApplication.instance() or QCoreApplication([])
    o = _Emettitore()
    o.senza_argomenti.connect(lambda: None)
    o.con_argomento.connect(lambda x: None)
    prima = sys.getrefcount(True)
    for i in range(EMISSIONI):
        o.senza_argomenti.emit()
        o.con_argomento.emit(i)
    persi = prima - sys.getrefcount(True)
    # Margine per riferimenti creati o rilasciati da altro nel frattempo:
    # il difetto ne toglie uno per emissione, cioè 2000.
    assert persi < EMISSIONI // 2, (
        f"PySide6 {PySide6.__version__} ha tolto {persi} riferimenti a True in "
        f"{2 * EMISSIONI} emissioni: con Python < 3.12 la GUI abortirebbe "
        "(bool_dealloc). Con Python < 3.12 serve PySide6 < 6.12 "
        "(requirements.txt, pyproject.toml; PYSIDE-3474)."
    )
