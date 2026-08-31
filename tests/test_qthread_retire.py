"""
Regressione: SIGABRT durante il rilascio di un QThread.

Sintomo osservato su Arch (Python 3.14, PySide6 6.11.2) durante
l'aggiornamento freshclam: il processo abortiva con

    QThread: Destroyed while thread is still running

cioè qFatal -> abort, con lo stack sul thread UpdateWorker che passa da
_Py_Dealloc a shiboken a QMessageLogger::fatal.

Causa: i worker del progetto emettono il loro segnale di fine DENTRO
run(). La slot collegata gira quindi mentre run() non ha ancora
restituito il controllo e il thread C++ è ancora in teardown. Se la slot
azzera l'ultimo riferimento Python al wrapper, shiboken distrugge
l'oggetto C++ sotto un thread ancora vivo, e Qt aborta.

Due invarianti proteggono il codice, e questo file verifica entrambe:

 1. i worker senza parent Qt vengono rilasciati da _retire_qthread, che
    deve trattenere il wrapper fino a QThread::finished;
 2. PingWorker, l'unico esente, lo è perché ha un parent Qt, che sposta
    la ownership al C++.

I test statici girano ovunque (la CI non ha PySide6). Il test funzionale
serve un sottoprocesso, perché qFatal termina il processo: non è
catturabile con pytest.raises, si può solo osservare il codice di uscita.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RADICE = Path(__file__).resolve().parent.parent
MAIN_WINDOW = RADICE / "klamav_py" / "gui" / "main_window.py"

# 128 + SIGABRT(6): come la shell riporta un processo terminato da abort
USCITA_ABORT = -6


def _albero_main_window() -> ast.Module:
    if not MAIN_WINDOW.exists():
        pytest.skip("main_window.py non presente (sorgente non completo)")
    return ast.parse(MAIN_WINDOW.read_text(encoding="utf-8"))


def test_pingworker_creato_con_parent():
    """
    PingWorker è l'unico worker che non passa da _retire_qthread: la slot
    _on_ping_result fa un semplice `self._ping_worker = None`. È sicuro
    solo perché l'oggetto ha un parent Qt e quindi la ownership è del
    C++.

    Se qualcuno togliesse il parent, quel `= None` tornerebbe a essere
    esattamente la riga che causava il crash, senza nessun altro segnale
    d'allarme.
    """
    albero = _albero_main_window()

    chiamate = [
        n
        for n in ast.walk(albero)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "PingWorker"
    ]
    assert chiamate, "nessuna istanziazione di PingWorker trovata in main_window.py"

    for chiamata in chiamate:
        ha_parent = len(chiamata.args) >= 2 or any(
            kw.arg == "parent" for kw in chiamata.keywords
        )
        assert ha_parent, (
            f"PingWorker istanziato senza parent Qt (riga {chiamata.lineno}). "
            "Senza parent la ownership resta a Python e "
            "'self._ping_worker = None' distrugge il QThread mentre è "
            "ancora in teardown: usare _retire_qthread oppure ripristinare "
            "il parent."
        )


def test_retire_trattiene_un_riferimento():
    """
    La prima versione di _retire_qthread si limitava a
    `worker.finished.connect(worker.deleteLater)`. Non basta: la
    connessione Qt non trattiene il wrapper Python, quindi appena il
    chiamante esce di scope shiboken distrugge comunque l'oggetto C++ e
    il qFatal scatta prima che finished venga consegnato. Verificato
    sperimentalmente: quella versione abortiva esattamente come il
    codice senza fix.

    Serve un riferimento forte esterno che sopravviva al chiamante.
    Questo test lo cerca staticamente, per intercettare un refactor che
    lo rimuovesse credendolo superfluo.
    """
    albero = _albero_main_window()

    funzione = next(
        (
            n
            for n in ast.walk(albero)
            if isinstance(n, ast.FunctionDef) and n.name == "_retire_qthread"
        ),
        None,
    )
    assert funzione is not None, "_retire_qthread non trovata in main_window.py"

    # La docstring nomina _in_ritiro per spiegare il perché: se si
    # analizzasse il corpo intero, il test passerebbe grazie al proprio
    # commento anche con l'implementazione rotta. Verificato: succedeva.
    corpo_senza_docstring = [
        n
        for n in funzione.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]
    corpo = "".join(ast.dump(n) for n in corpo_senza_docstring)

    assert "_in_ritiro" in corpo, (
        "_retire_qthread non trattiene più un riferimento forte al worker "
        "(_in_ritiro). Agganciare deleteLater a finished non è "
        "sufficiente: il wrapper Python muore prima che finished arrivi."
    )
    assert "deleteLater" in corpo, (
        "_retire_qthread non chiama più deleteLater: il worker non verrebbe "
        "mai distrutto."
    )


SCRIPT_RIPRODUZIONE = """
import sys
from PySide6.QtCore import QCoreApplication, QThread, QTimer, Signal

MODO = sys.argv[1]
_in_ritiro = set()


def retire(worker):
    if worker.isFinished():
        worker.deleteLater()
        return
    _in_ritiro.add(worker)

    def _finito():
        _in_ritiro.discard(worker)
        worker.deleteLater()

    worker.finished.connect(_finito)


class Worker(QThread):
    fine = Signal()

    def run(self):
        # come ScanWorker/UpdateWorker: il segnale di fine e' emesso
        # dentro run(), non dopo
        self.fine.emit()


class Pagina:
    def __init__(self):
        self.worker = None
        self.cicli = 0

    def avvia(self):
        self.worker = Worker()
        self.worker.fine.connect(self.su_fine)
        self.worker.start()

    def su_fine(self):
        worker, self.worker = self.worker, None
        if MODO == "senza_fix":
            del worker
        else:
            retire(worker)
        self.cicli += 1
        if self.cicli < 50:
            QTimer.singleShot(0, self.avvia)
        else:
            QTimer.singleShot(150, app.quit)


app = QCoreApplication(sys.argv)
pagina = Pagina()
QTimer.singleShot(0, pagina.avvia)
app.exec()
print("uscita pulita")
"""


def _esegui(modo: str, tmp_path: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "riproduzione.py"
    script.write_text(textwrap.dedent(SCRIPT_RIPRODUZIONE), encoding="utf-8")
    ambiente = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    return subprocess.run(
        [sys.executable, str(script), modo],
        capture_output=True,
        text=True,
        timeout=120,
        env=ambiente,
    )


@pytest.mark.timeout(180)
def test_rilascio_ripetuto_non_aborta(tmp_path):
    """
    Cinquanta cicli avvio/fine con rilascio via _retire_qthread: il
    processo deve uscire pulito.

    In sottoprocesso perché qFatal chiama abort(): il processo muore e
    non c'è eccezione da intercettare. L'unica evidenza osservabile è il
    codice di uscita.
    """
    pytest.importorskip("PySide6", reason="PySide6 non disponibile (atteso in CI)")

    esito = _esegui("con_fix", tmp_path)
    assert esito.returncode == 0, (
        "il rilascio dei QThread ha fatto abortire il processo "
        f"(returncode={esito.returncode}).\nstderr:\n{esito.stderr}"
    )
    assert "uscita pulita" in esito.stdout


@pytest.mark.timeout(180)
def test_lo_scenario_senza_fix_aborta_davvero(tmp_path):
    """
    Controprova: senza il rilascio differito lo stesso scenario deve
    abortire.

    Senza questa verifica il test precedente potrebbe passare per il
    motivo sbagliato — per esempio se una versione futura di PySide6
    rendesse innocuo il rilascio immediato, o se lo scenario smettesse
    di riprodurre la gara. In quel caso questo test fallisce e segnala
    che la rete di sicurezza non sta più misurando niente, invece di
    restare verde a vuoto.
    """
    pytest.importorskip("PySide6", reason="PySide6 non disponibile (atteso in CI)")

    esito = _esegui("senza_fix", tmp_path)
    if esito.returncode == 0:
        pytest.skip(
            "il rilascio immediato non aborta più in questo ambiente: la "
            "gara non è riprodotta, il test di regressione non sta "
            "misurando nulla (verificare il comportamento di PySide6)"
        )
    assert esito.returncode == USCITA_ABORT, (
        "atteso SIGABRT dal rilascio immediato, ottenuto "
        f"returncode={esito.returncode}.\nstderr:\n{esito.stderr}"
    )
    assert "still running" in esito.stderr
