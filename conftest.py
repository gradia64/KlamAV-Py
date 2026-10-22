# La sola presenza di questo file fa sì che pytest metta la radice del
# progetto in testa a sys.path (rootdir come prima entry). Senza, con il
# pacchetto klamav-py installato nel sistema e il venv non attivo, i test
# importerebbero /usr/lib/python3/dist-packages/klamav_py invece del
# sorgente: risultati falsi in entrambe le direzioni (moduli nuovi
# "mancanti", oppure test verdi su codice vecchio).

import gc
import sys

import pytest


@pytest.fixture(autouse=True)
def _registro_prese_visione_isolato(tmp_path_factory, monkeypatch):
    """Nessun test legge o scrive il registro delle prese visione reale
    (~/.local/share/klamav-py/acknowledged.json) di chi lancia la suite:
    worker e CLI lo consultano per ogni rilevamento «solo segnalazione»."""
    import klamav_py.acknowledged as acknowledged

    path = tmp_path_factory.mktemp("prese-visione") / acknowledged.REGISTRY_NAME
    monkeypatch.setattr(acknowledged, "default_registry_path", lambda: path)


# -- oggetti Qt tenuti in vita da un ciclo (0.1.17) ----------------------
#
# Un QObject che solo il garbage collector può liberare viene distrutto nel
# thread che fa partire la raccolta, anche quello di un worker: per un
# widget o un QThread vuol dire un crash di Qt in un punto lontano dalla
# causa (segfault nella CI con Python 3.12, dentro processEvents di un test
# successivo). Le cause trovate: un'eccezione tenuta in un locale del frame
# che l'ha intercettata (result = exc), un mock con side_effect=Crash()
# che conserva l'istanza con il traceback. QSettings è esclusa: è
# rientrante e non riceve eventi, distruggerla altrove non fa danni.

_QT_MODULES: dict[str, bool] = {}


def _uses_qt(module) -> bool:
    """Il modulo di test usa Qt (PySide6 o la GUI): solo lì vale la pena
    di pagare una raccolta completa per ogni test."""
    name = module.__name__
    if name not in _QT_MODULES:
        prefixes = ("PySide6", "klamav_py.gui")
        _QT_MODULES[name] = any(
            str(getattr(value, "__module__", None) or "").startswith(prefixes)
            or str(getattr(value, "__name__", None) or "").startswith(prefixes)
            for value in vars(module).values()
        )
    return _QT_MODULES[name]


def qobjects_in_cycles() -> list[str]:
    """Tipi dei QObject (QSettings esclusa) che solo il garbage collector
    può liberare. Li libera subito, nel thread chiamante."""
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None:
        return []
    old = gc.get_debug()
    gc.set_debug(old | gc.DEBUG_SAVEALL)
    try:
        gc.collect()
    finally:
        gc.set_debug(old)
    found = sorted(
        type(o).__name__ for o in gc.garbage
        if isinstance(o, qtcore.QObject) and not isinstance(o, qtcore.QSettings)
    )
    gc.garbage.clear()
    gc.collect()
    return found


@pytest.fixture(autouse=True)
def _nessun_oggetto_qt_nei_cicli(request):
    yield
    if request.module is None or not _uses_qt(request.module):
        return
    found = qobjects_in_cycles()
    if found:
        pytest.fail(
            f"oggetti Qt tenuti in vita solo da un ciclo di riferimenti: {found}. "
            "Il garbage collector li distruggerebbe nel thread che capita (vedi conftest.py)",
            pytrace=False,
        )
