"""
Ogni lancio della CLI (o di altro codice Python) dai test ha una HOME
temporanea.

Il fixture di conftest.py che isola il registro delle prese visione (e,
in generale, ogni monkeypatch) non attraversa i sottoprocessi: una CLI o
uno script del progetto lanciato così leggerebbe e scriverebbe i dati
reali di chi esegue la suite (~/.local/share/klamav-py, ~/.config).
La regola (CONTESTO, sezione 7): env={**os.environ, "HOME": str(tmp_path)}
o equivalente. Controllo statico, come gli altri del progetto.

Lanci riconosciuti (0.1.15; prima solo sys.executable): argv con
sys.executable, il console script klamav-py o klamav-py-gui (letterale o
da shutil.which), python -m klamav_py; anche attraverso una variabile
locale assegnata prima del lancio, e attraverso una funzione dello stesso
file che passa un suo parametro come argv a subprocess (il controllo
dell'env vale allora per il lancio dentro la funzione). Limite: un argv
costruito in un altro modulo o passato attraverso più funzioni non si
vede.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

_LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}

# Testo (ast.unparse) di un argv che lancia la CLI o Python.
_CLI = re.compile(
    r"sys\.executable"
    r"|['\"]klamav-py(?:-gui)?['\"]"
    r"|['\"]-m['\"],\s*['\"]klamav_py(?:\.\w+)*['\"]"
)


def _launches(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LAUNCHERS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
            yield node


def _argv(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "args"), None)


def _assigned_before(name: str, line: int, tree: ast.AST) -> list[ast.AST]:
    """Valori assegnati a `name` prima della riga `line` (qualunque scope:
    un controllo statico prudente, non un'analisi del flusso)."""
    values = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.lineno < line
                and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)):
            values.append(node.value)
    return values


def _source(expr: ast.AST, line: int, tree: ast.AST, depth: int = 3) -> str:
    """Testo dell'espressione, con quello delle variabili locali che usa."""
    parts = [ast.unparse(expr)]
    if depth:
        for name in {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}:
            for value in _assigned_before(name, line, tree):
                parts.append(_source(value, line, tree, depth - 1))
    return "\n".join(parts)


def _is_cli(expr: ast.AST, line: int, tree: ast.AST) -> bool:
    return bool(_CLI.search(_source(expr, line, tree)))


def _env_source(call: ast.Call, tree: ast.AST) -> str:
    """Il testo dell'argomento env, seguendo una variabile locale."""
    env = next((k.value for k in call.keywords if k.arg == "env"), None)
    if env is None:
        return ""
    return _source(env, call.lineno, tree, depth=1)


def _wrapper_param(call: ast.Call, tree: ast.AST) -> tuple[str, int] | None:
    """(funzione, posizione) se l'argv del lancio è un parametro della
    funzione che lo contiene."""
    argv = _argv(call)
    if not isinstance(argv, ast.Name):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.lineno <= call.lineno <= (node.end_lineno or node.lineno):
            names = [a.arg for a in node.args.args]
            if argv.id in names:
                return node.name, names.index(argv.id)
    return None


def cli_launches(tree: ast.AST):
    """Lanci della CLI o di Python: i lanci diretti, e quelli dentro una
    funzione che riceve l'argv da un chiamante dello stesso file che le
    passa un argv della CLI."""
    for call in _launches(tree):
        argv = _argv(call)
        if argv is not None and _is_cli(argv, call.lineno, tree):
            yield call
            continue
        wrapper = _wrapper_param(call, tree)
        if wrapper is None:
            continue
        name, pos = wrapper
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == name and len(node.args) > pos
                    and _is_cli(node.args[pos], node.lineno, tree)):
                yield call
                break


def unguarded(source: str) -> list[int]:
    """Righe dei lanci della CLI senza HOME nell'env."""
    tree = ast.parse(source)
    return [c.lineno for c in cli_launches(tree) if "HOME" not in _env_source(c, tree)]


def test_sottoprocessi_python_con_home_temporanea():
    missing = []
    found = 0
    for path in sorted(TESTS.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        found += sum(1 for _ in cli_launches(ast.parse(source)))
        missing += [f"{path.name}:{line}" for line in unguarded(source)]
    assert found, "nessun sottoprocesso trovato: il controllo non sta guardando nulla"
    assert not missing, "sottoprocessi Python senza HOME temporanea: " + ", ".join(missing)


# -- il controllo vede i lanci della CLI (test fittizi) ---------------------

_FITTIZI_SENZA_HOME = {
    "letterale": 'subprocess.run(["klamav-py", "scan", str(p)])',
    "gui": 'subprocess.Popen(["klamav-py-gui", "--scan-target", p], env=dict(os.environ))',
    "which": 'exe = shutil.which("klamav-py")\nsubprocess.run([exe, "ping"], check=True)',
    "which in linea": 'subprocess.check_output([shutil.which("klamav-py"), "--version"])',
    "modulo": 'subprocess.run([sys.executable, "-m", "klamav_py", "scan", p])',
    "python -m": 'subprocess.run(["python3", "-m", "klamav_py.cli", "ping"])',
    "argv in variabile": 'cmd = ["klamav-py", "ping"]\nsubprocess.call(cmd)',
    "funzione di comodo": (
        "def _cli(argv, tmp):\n"
        "    return subprocess.run(argv, env={'PATH': '/usr/bin'})\n"
        "def test_x(tmp_path):\n"
        "    _cli(['klamav-py', 'ping'], tmp_path)\n"
    ),
}


def test_controllo_vede_ogni_lancio_della_cli_senza_home():
    for name, source in _FITTIZI_SENZA_HOME.items():
        assert unguarded(source), f"lancio della CLI non visto ({name}): {source!r}"


def test_controllo_accetta_la_home_temporanea():
    sources = [
        'subprocess.run(["klamav-py", "ping"], env={**os.environ, "HOME": str(tmp_path)})',
        'env = dict(os.environ, HOME=str(tmp_path))\n'
        'subprocess.run([shutil.which("klamav-py"), "ping"], env=env)',
        'subprocess.run(["git", "status"])',  # non è la CLI
    ]
    for source in sources:
        assert unguarded(source) == [], source
