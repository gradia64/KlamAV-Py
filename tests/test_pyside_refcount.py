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

La misura gira in un sottoprocesso che non può lasciare core dump: quando
il difetto c'è, quell'interprete abortisce (durante le emissioni o in
chiusura), e nel processo di pytest questo trasformerebbe un fallimento
leggibile in un crash di Python (con relativa notifica nel desktop).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

EMISSIONI = 1000

MISURA = """
import ctypes
try:
    # PR_SET_DUMPABLE = 4: niente core dump e niente notifica di crash
    # (systemd-coredump, DrKonqi) per un abort voluto.
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
except (OSError, AttributeError):
    pass
import os, sys
from PySide6.QtCore import QCoreApplication, QObject, Signal

class Emettitore(QObject):
    senza_argomenti = Signal()
    con_argomento = Signal(object)

app = QCoreApplication([])
o = Emettitore()
o.senza_argomenti.connect(lambda: None)
o.con_argomento.connect(lambda x: None)
prima = sys.getrefcount(True)
for i in range(int(sys.argv[1])):
    o.senza_argomenti.emit()
    o.con_argomento.emit(i)
print(prima - sys.getrefcount(True), flush=True)
# Niente chiusura dell'interprete: con il difetto abortirebbe (bool_dealloc).
os._exit(0)
"""


def test_emettere_un_segnale_non_consuma_riferimenti_a_true(tmp_path):
    PySide6 = pytest.importorskip("PySide6")

    esito = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(MISURA), str(EMISSIONI)],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=str(tmp_path)),
    )
    difetto = (
        f"PySide6 {PySide6.__version__} toglie un riferimento a True a ogni "
        "emissione: con Python < 3.12 la GUI abortisce (bool_dealloc). Con "
        "Python < 3.12 serve PySide6 < 6.12 (requirements.txt, pyproject.toml; "
        "PYSIDE-3474)."
    )
    # Con pochi riferimenti iniziali a True il figlio abortisce già durante
    # le emissioni, prima di poter stampare la misura.
    assert "bool_dealloc" not in esito.stderr, difetto
    assert esito.returncode == 0, (
        f"la misura è terminata con returncode={esito.returncode}.\n"
        f"stderr:\n{esito.stderr}"
    )
    persi = int(esito.stdout.split()[-1])
    # Margine per riferimenti creati o rilasciati da altro nel frattempo:
    # il difetto ne toglie uno per emissione, cioè 2000.
    assert persi < EMISSIONI // 2, f"{persi} riferimenti persi: {difetto}"
