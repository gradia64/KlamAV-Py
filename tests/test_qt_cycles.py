"""
Oggetti Qt tenuti in vita solo da un ciclo di riferimenti.

Il garbage collector libera un ciclo nel thread che fa partire la raccolta,
anche quello di un worker: un widget o un QThread distrutto fuori dal
thread della GUI fa crashare Qt (segfault nella CI con Python 3.12, nella
0.1.17). conftest.py controlla dopo ogni test che usa Qt che non ne resti
nessuno (qobjects_in_cycles); qui la guardia su oggetti fittizi, e i tre
punti di produzione in cui un'eccezione tenuta in un locale legava il
frame, e con esso il worker, in un ciclo.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import klamav_py.gui.off_thread as off_thread  # noqa: E402
from conftest import qobjects_in_cycles  # noqa: E402

non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root legge anche le cartelle 0300")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _without_traceback():
    fn = getattr(off_thread, "without_traceback", None)
    assert fn is not None, "manca without_traceback in gui/off_thread.py"
    return fn


# -- la guardia -------------------------------------------------------------

def test_guardia_vede_un_qobject_in_un_ciclo(app):
    obj = QObject()
    obj.me = obj
    del obj
    assert qobjects_in_cycles() == ["QObject"]
    assert qobjects_in_cycles() == []  # liberato dalla prima chiamata


def test_guardia_ignora_oggetti_senza_ciclo_e_qsettings(app, tmp_path):
    tenuto = QObject()
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    settings.me = settings
    del settings
    assert qobjects_in_cycles() == []
    assert tenuto is not None


# -- eccezioni consegnate ad altri thread -----------------------------------

class _Custom(ValueError):
    pass


def _catch(keep: QObject, strip: bool):
    """Un frame che tiene un QObject e intercetta un'eccezione concatenata,
    come il target di run_off_gui_thread."""
    try:
        try:
            raise OSError("interno")
        except OSError as inner:
            raise _Custom("esterno") from inner
    except Exception as exc:  # noqa: BLE001
        result = _without_traceback()(exc) if strip else exc
    return result


def test_eccezione_senza_traceback_nessun_ciclo(app):
    exc = _catch(QObject(), strip=True)
    assert qobjects_in_cycles() == []
    assert type(exc) is _Custom and str(exc) == "esterno"
    assert exc.__traceback__ is None and exc.__cause__.__traceback__ is None
    assert str(exc.__cause__) == "interno"


def test_controprova_eccezione_con_traceback_fa_ciclo(app):
    _without_traceback()
    _catch(QObject(), strip=False)
    assert qobjects_in_cycles() == ["QObject"]


# -- worker di produzione ---------------------------------------------------

def test_caricamento_fallito_della_quarantena_nessun_ciclo(app, tmp_path):
    from klamav_py.gui.quarantine_loader import QuarantineLoadWorker

    occupato = tmp_path / "q"
    occupato.write_text("non una cartella")
    got = []
    worker = QuarantineLoadWorker(occupato)
    worker.loaded.connect(lambda directory, result: got.append(result))
    worker.run()  # sincrono: il segnale arriva in linea
    del worker
    ((result,),) = [got]
    assert isinstance(result, OSError) and str(occupato) in str(result)
    del got, result
    assert qobjects_in_cycles() == []


@non_root
def test_radice_illeggibile_nessun_ciclo(app, tmp_path):
    from klamav_py.clamd_client import ClamdEndpoint
    from klamav_py.gui.scan_worker import ScanWorker

    root = tmp_path / "chiusa"
    root.mkdir()
    os.chmod(root, 0o300)
    try:
        aborted = []
        worker = ScanWorker(endpoint=ClamdEndpoint(), target=root, client_factory=object)
        worker.aborted.connect(aborted.append)
        worker.run()
        del worker
        assert len(aborted) == 1 and "non è leggibile" in aborted[0]
        assert qobjects_in_cycles() == []
    finally:
        os.chmod(root, 0o700)


def test_root_problem_stesso_testo(tmp_path):
    from klamav_py.clamd_client import root_problem

    errore = PermissionError(13, "Permission denied")
    assert root_problem(tmp_path, errore) == f"«{tmp_path}» non è leggibile: Permission denied"
    assert root_problem(tmp_path, OSError("senza strerror")) == \
        f"«{tmp_path}» non è leggibile: senza strerror"
    assert root_problem(Path(tmp_path)) is None
