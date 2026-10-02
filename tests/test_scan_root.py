"""
Regola unica per la radice di una scansione (clamd_client.root_problem).

Prima esistevano due copie: la sonda ignorava tutto ciò che non era una
directory, e l'onerror di _iter_files trasformava ogni errore sulla radice
in «non è leggibile». `klamav-py scan /dev/null` o una FIFO uscivano
correttamente con 2, ma con «non è leggibile: Not a directory», e un
symlink rotto passava per «percorso inesistente». Ora quattro casi
distinti, tutti con uscita 2: inesistente, collegamento rotto (con la
destinazione), né directory né file regolare, non leggibile. Un file
regolare, anche tramite symlink, resta una radice valida.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import klamav_py.cli as cli  # noqa: E402
from klamav_py.clamd_client import (  # noqa: E402
    ClamdClient,
    ClamdEndpoint,
    ScanResult,
    UnreadableRoot,
    root_problem,
)
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402

non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root legge anche le cartelle 0000")


class CliClient:
    """Traversata vera, nessun clamd: ogni file è pulito."""

    pinged = False

    def __init__(self, **kw):
        self.skipped = Counter()

    def ping(self):
        CliClient.pinged = True
        return True

    def scan_stream(self, target, *, exclude_dirs=None, on_unreadable_dir=None, **kw):
        for f in ClamdClient._iter_files(Path(target).resolve(), exclude_dirs, on_unreadable_dir):
            yield ScanResult(str(f), "OK")


@pytest.fixture
def cli_env(monkeypatch):
    CliClient.pinged = False
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: CliClient())


@pytest.fixture
def closed_dir(tmp_path):
    d = tmp_path / "chiusa"
    d.mkdir()
    os.chmod(d, 0o000)
    yield d
    os.chmod(d, 0o700)


def _fifo(tmp_path: Path) -> Path:
    fifo = tmp_path / "coda"
    os.mkfifo(fifo)
    return fifo


def _broken_link(tmp_path: Path) -> Path:
    link = tmp_path / "rotto"
    link.symlink_to(tmp_path / "sparito")
    return link


# -- la regola -------------------------------------------------------------

def test_inesistente(tmp_path):
    assert root_problem(tmp_path / "niente") == f"«{tmp_path / 'niente'}» non esiste"


def test_symlink_rotto_non_e_inesistente(tmp_path):
    link = _broken_link(tmp_path)
    assert root_problem(link) == (
        f"«{link}» è un collegamento simbolico rotto: «{tmp_path / 'sparito'}» non esiste"
    )


@pytest.mark.parametrize("caso", ["/dev/null", "fifo"])
def test_ne_directory_ne_file_regolare(tmp_path, caso):
    path = Path("/dev/null") if caso == "/dev/null" else _fifo(tmp_path)
    assert root_problem(path) == f"«{path}» non è una directory né un file regolare"


@non_root
def test_directory_non_leggibile(closed_dir):
    assert root_problem(closed_dir).startswith(f"«{closed_dir}» non è leggibile: ")


def test_file_regolare_anche_tramite_symlink_valido(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    link = tmp_path / "al-file"
    link.symlink_to(f)
    assert root_problem(f) is None
    assert root_problem(link) is None
    assert root_problem(tmp_path) is None


# -- la traversata usa la stessa regola ------------------------------------

@pytest.mark.parametrize("caso", ["/dev/null", "fifo"])
def test_traversata_stesso_messaggio_della_sonda(tmp_path, caso):
    path = Path("/dev/null") if caso == "/dev/null" else _fifo(tmp_path)
    with pytest.raises(UnreadableRoot) as info:
        list(ClamdClient._iter_files(path, []))
    assert str(info.value) == root_problem(path)


def test_traversata_radice_sparita(tmp_path):
    sparita = tmp_path / "sparita"
    with pytest.raises(UnreadableRoot, match="non esiste"):
        list(ClamdClient._iter_files(sparita, []))


# -- CLI: uscita 2 e messaggio specifico -------------------------------------

@pytest.mark.parametrize("caso, atteso", [
    ("/dev/null", "non è una directory né un file regolare"),
    ("fifo", "non è una directory né un file regolare"),
    ("symlink rotto", "è un collegamento simbolico rotto"),
    ("inesistente", "non esiste"),
])
def test_cli_radice_non_valida(tmp_path, cli_env, capsys, caso, atteso):
    path = {
        "/dev/null": lambda: Path("/dev/null"),
        "fifo": lambda: _fifo(tmp_path),
        "symlink rotto": lambda: _broken_link(tmp_path),
        "inesistente": lambda: tmp_path / "niente",
    }[caso]()
    assert cli.main(["scan", str(path)]) == 2
    err = capsys.readouterr().err
    assert f"Percorso «{path}» {atteso}" in err
    assert "Not a directory" not in err
    assert CliClient.pinged is False  # prima della connessione a clamd


@non_root
def test_cli_radice_0000(closed_dir, cli_env, capsys):
    assert cli.main(["scan", str(closed_dir)]) == 2
    assert f"Percorso «{closed_dir}» non è leggibile" in capsys.readouterr().err
    assert CliClient.pinged is False


def test_cli_file_tramite_symlink_scansionato(tmp_path, cli_env, capsys):
    f = tmp_path / "f.txt"
    f.write_text("x")
    link = tmp_path / "al-file"
    link.symlink_to(f)
    assert cli.main(["scan", str(link)]) == 0
    assert "1 file scansionati" in capsys.readouterr().out


# -- GUI: il messaggio arriva da UnreadableRoot ----------------------------

def test_worker_fifo_messaggio_specifico(tmp_path):
    fifo = _fifo(tmp_path)
    w = ScanWorker(endpoint=ClamdEndpoint(), target=fifo, client_factory=CliClient)
    aborted = []
    w.aborted.connect(aborted.append)
    w.run()
    assert aborted == [
        f"Scansione non eseguita. Il percorso da scansionare «{fifo}» "
        "non è una directory né un file regolare"
    ]
