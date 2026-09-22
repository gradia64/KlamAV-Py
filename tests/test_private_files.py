"""
Test delle primitive per file privati (audit 0.1.8: cronologia, log
delle scansioni programmate e log errori della CLI creati con i permessi
di default dello umask, leggibili da altri utenti con una home 0755).
Vedi klamav_py/private_files.py.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import pytest

from klamav_py import private_files
from klamav_py.private_files import (
    PrivateFileError,
    ensure_private_dir,
    open_private_for_write,
    write_private_text,
)

RADICE = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def umask_permissivo():
    # Lo scenario reale del reperto: umask 022, il default più comune.
    vecchio = os.umask(0o022)
    yield
    os.umask(vecchio)


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


# --- directory ---------------------------------------------------------

def test_directory_nuova_0700(tmp_path):
    d = ensure_private_dir(tmp_path / "a" / "klamav-py")
    assert _mode(d) == 0o700


def test_directory_esistente_larga_viene_ristretta(tmp_path):
    d = tmp_path / "klamav-py"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    ensure_private_dir(d)
    assert _mode(d) == 0o700


def test_directory_symlink_rifiutata(tmp_path):
    vera = tmp_path / "vera"
    vera.mkdir()
    os.chmod(vera, 0o755)
    link = tmp_path / "klamav-py"
    link.symlink_to(vera)
    with pytest.raises(PrivateFileError):
        ensure_private_dir(link)
    assert _mode(vera) == 0o755, "la chmod non deve seguire il symlink"


# --- scrittura atomica -------------------------------------------------

def test_scrittura_atomica_crea_0600(tmp_path):
    f = tmp_path / "history.json"
    write_private_text(f, "[]")
    assert f.read_text() == "[]"
    assert _mode(f) == 0o600


def test_scrittura_atomica_sostituisce_file_0644(tmp_path):
    f = tmp_path / "history.json"
    f.write_text("vecchio")
    os.chmod(f, 0o644)
    write_private_text(f, "nuovo")
    assert f.read_text() == "nuovo"
    assert _mode(f) == 0o600


def test_scrittura_atomica_non_lascia_temporanei(tmp_path):
    write_private_text(tmp_path / "x.log", "a")
    assert [p.name for p in tmp_path.iterdir()] == ["x.log"]


def test_scrittura_atomica_non_segue_symlink(tmp_path):
    bersaglio = tmp_path / "bersaglio"
    bersaglio.write_text("intatto")
    link = tmp_path / "history.json"
    link.symlink_to(bersaglio)
    write_private_text(link, "dati privati")
    # os.replace sostituisce la voce di directory: il symlink diventa un
    # file normale, il bersaglio non viene toccato.
    assert bersaglio.read_text() == "intatto"
    assert not link.is_symlink() and link.read_text() == "dati privati"


# --- file aperto in scrittura progressiva (log errori CLI) -------------

def test_log_nuovo_0600(tmp_path):
    f = tmp_path / "errori.log"
    with open_private_for_write(f) as fh:
        fh.write("riga\n")
    assert _mode(f) == 0o600 and f.read_text() == "riga\n"


def test_log_proprio_preesistente_troncato_e_ristretto(tmp_path):
    f = tmp_path / "errori.log"
    f.write_text("vecchio contenuto lungo\n")
    os.chmod(f, 0o644)
    with open_private_for_write(f) as fh:
        fh.write("nuovo\n")
    assert f.read_text() == "nuovo\n" and _mode(f) == 0o600


def test_log_symlink_rifiutato(tmp_path):
    bersaglio = tmp_path / "bersaglio"
    bersaglio.write_text("intatto")
    link = tmp_path / "errori.log"
    link.symlink_to(bersaglio)
    with pytest.raises(PrivateFileError):
        open_private_for_write(link)
    assert bersaglio.read_text() == "intatto"


@pytest.mark.timeout(5)
def test_log_fifo_rifiutata_senza_bloccarsi(tmp_path):
    fifo = tmp_path / "errori.log"
    os.mkfifo(fifo)
    with pytest.raises(PrivateFileError):
        open_private_for_write(fifo)


def test_log_di_altro_utente_rifiutato_e_non_troncato(tmp_path, monkeypatch):
    f = tmp_path / "errori.log"
    f.write_text("contenuto dell'altro utente\n")
    os.chmod(f, 0o666)
    # Simula "il file appartiene a qualcun altro" senza servire root.
    monkeypatch.setattr(private_files.os, "getuid", lambda: os.geteuid() + 1)
    with pytest.raises(PrivateFileError):
        open_private_for_write(f)
    monkeypatch.undo()
    assert f.read_text() == "contenuto dell'altro utente\n", "non deve essere troncato"


# --- i chiamanti usano davvero le primitive ----------------------------

def _chiamate_scrittura_insicure(nodo: ast.AST) -> list[str]:
    trovate = []
    for n in ast.walk(nodo):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in ("write_text", "mkdir"):
            trovate.append(f.attr)
        nome = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        # open(path, "w") ha la modalità come 2° argomento, Path.open("w")
        # come 1°: si controllano entrambe le posizioni.
        posizione = slice(1, 2) if isinstance(f, ast.Name) else slice(0, 1)
        modi = [a.value for a in n.args[posizione] if isinstance(a, ast.Constant)]
        modi += [k.value.value for k in n.keywords
                 if k.arg == "mode" and isinstance(k.value, ast.Constant)]
        if nome == "open" and any(isinstance(m, str) and "w" in m for m in modi):
            trovate.append("open(w)")
    return trovate


def test_cronologia_e_log_programmati_usano_file_privati():
    tree = ast.parse((RADICE / "klamav_py/gui/main_window.py").read_text(encoding="utf-8"))
    bersagli = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == "HistoryManager":
            bersagli.append(("HistoryManager", n))
        if isinstance(n, ast.FunctionDef) and n.name == "_on_bg_finished":
            bersagli.append(("_on_bg_finished", n))
    assert len(bersagli) == 2
    for nome, nodo in bersagli:
        assert not _chiamate_scrittura_insicure(nodo), (
            f"{nome} scrive file con i permessi di default: usare private_files"
        )


def test_cli_log_errori_usa_file_privato():
    tree = ast.parse((RADICE / "klamav_py/cli.py").read_text(encoding="utf-8"))
    assert not _chiamate_scrittura_insicure(tree)


# --- file scritti da codice non nostro (QSettings) ---------------------

def test_ensure_private_file_restringe_senza_troncare(tmp_path):
    from klamav_py.private_files import ensure_private_file
    f = tmp_path / "KlamAV-Py.conf"
    f.write_text("[General]\nsocket=/run/clamav/clamd.ctl\n")
    os.chmod(f, 0o644)
    assert ensure_private_file(f) is True
    assert _mode(f) == 0o600
    assert "clamd.ctl" in f.read_text(), "il contenuto non va toccato"


def test_ensure_private_file_crea_vuoto_a_0600(tmp_path):
    from klamav_py.private_files import ensure_private_file
    f = tmp_path / "KlamAV-Py.conf"
    assert ensure_private_file(f) is True
    assert _mode(f) == 0o600 and f.read_text() == ""


def test_ensure_private_file_senza_create_non_crea_nulla(tmp_path):
    from klamav_py.private_files import ensure_private_file
    f = tmp_path / "KlamAV.conf"
    assert ensure_private_file(f, create=False) is False
    assert not f.exists()


def test_ensure_private_file_rifiuta_symlink(tmp_path):
    from klamav_py.private_files import ensure_private_file, PrivateFileError
    bersaglio = tmp_path / "bersaglio"
    bersaglio.write_text("intatto")
    os.chmod(bersaglio, 0o644)
    link = tmp_path / "KlamAV-Py.conf"
    link.symlink_to(bersaglio)
    with pytest.raises(PrivateFileError):
        ensure_private_file(link)
    assert _mode(bersaglio) == 0o644
