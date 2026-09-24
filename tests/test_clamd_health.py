"""Salute di clamd: isteresi, transizioni e limite ai ping forzati."""
import ast
from pathlib import Path

import pytest

from klamav_py.clamd_health import ClamdHealth, ClamdState, Throttle, Transition

RADICE = Path(__file__).resolve().parent.parent
MAIN_WINDOW = RADICE / "klamav_py" / "gui" / "main_window.py"


def test_single_failure_is_not_down():
    h = ClamdHealth()
    assert h.observe(True) is None
    assert h.observe(False) is None
    assert h.state is ClamdState.UP


def test_two_failures_go_down_once():
    h = ClamdHealth()
    h.observe(True)
    h.observe(False)
    assert h.observe(False) is Transition.WENT_DOWN
    assert h.is_down
    # Ulteriori fallimenti non ripetono la transizione (niente notifiche a raffica).
    assert h.observe(False) is None


def test_success_resets_failure_count():
    h = ClamdHealth()
    h.observe(False)
    h.observe(True)
    assert h.observe(False) is None  # il contatore è ripartito da zero


def test_came_back():
    h = ClamdHealth()
    h.observe(False, immediate=True)
    assert h.observe(True) is Transition.CAME_BACK
    assert h.state is ClamdState.UP
    assert h.observe(True) is None


def test_immediate_startup_failure():
    h = ClamdHealth()
    assert h.state is ClamdState.UNKNOWN
    assert h.observe(False, immediate=True) is Transition.WENT_DOWN


def test_first_success_from_unknown_is_silent():
    assert ClamdHealth().observe(True) is None


def test_invalid_threshold():
    with pytest.raises(ValueError):
        ClamdHealth(fail_threshold=0)


def test_throttle():
    now = [100.0]
    t = Throttle(10.0, clock=lambda: now[0])
    assert t.ready()
    now[0] = 105.0
    assert not t.ready()
    now[0] = 110.0
    assert t.ready()


def _funzione(nome):
    if not MAIN_WINDOW.exists():
        pytest.skip("main_window.py non presente")
    albero = ast.parse(MAIN_WINDOW.read_text(encoding="utf-8"))
    for n in ast.walk(albero):
        if isinstance(n, ast.FunctionDef) and n.name == nome:
            return n
    pytest.fail(f"{nome} non trovata in main_window.py")


def test_ping_periodici_non_si_accumulano():
    """
    Un ping al minuto con parent Qt: senza deleteLater a thread finito,
    ogni PingWorker resterebbe figlio della finestra per tutta la
    sessione (1440 oggetti al giorno).
    """
    corpo = ast.dump(_funzione("_start_ping"))
    assert "deleteLater" in corpo and "finished" in corpo


def test_un_ping_alla_volta():
    """Il guard sul ping in corso deve precedere la creazione del worker."""
    funzione = _funzione("_start_ping")
    primo = next(n for n in funzione.body
                 if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)))
    assert isinstance(primo, ast.If) and "_ping_worker" in ast.dump(primo.test)
