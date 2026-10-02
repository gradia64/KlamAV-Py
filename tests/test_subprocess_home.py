"""
Ogni sottoprocesso Python lanciato dai test ha una HOME temporanea.

Il fixture di conftest.py che isola il registro delle prese visione (e,
in generale, ogni monkeypatch) non attraversa i sottoprocessi: una CLI o
uno script del progetto lanciato così leggerebbe e scriverebbe i dati
reali di chi esegue la suite (~/.local/share/klamav-py, ~/.config).
La regola (CONTESTO, sezione 7): env={**os.environ, "HOME": str(tmp_path)}
o equivalente. Controllo statico, come gli altri del progetto.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

_LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}


def _python_subprocesses(tree: ast.AST):
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LAUNCHERS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
            continue
        if node.args and "sys.executable" in ast.unparse(node.args[0]):
            yield node


def _env_source(call: ast.Call, tree: ast.AST) -> str:
    """Il testo dell'argomento env, seguendo una variabile locale."""
    env = next((k.value for k in call.keywords if k.arg == "env"), None)
    if env is None:
        return ""
    if isinstance(env, ast.Name):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and node.lineno < call.lineno
                    and any(isinstance(t, ast.Name) and t.id == env.id for t in node.targets)):
                return ast.unparse(node.value)
    return ast.unparse(env)


def test_sottoprocessi_python_con_home_temporanea():
    missing = []
    found = 0
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _python_subprocesses(tree):
            found += 1
            if "HOME" not in _env_source(call, tree):
                missing.append(f"{path.name}:{call.lineno}")
    assert found, "nessun sottoprocesso trovato: il controllo non sta guardando nulla"
    assert not missing, "sottoprocessi Python senza HOME temporanea: " + ", ".join(missing)
