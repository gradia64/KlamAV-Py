"""
Codice di uscita per i guasti di I/O (punto 1-bis della 0.1.14).

Un errore di lettura che non è un permesso né un'entry sparita (EIO,
ESTALE, ETIMEDOUT di un disco o di un mount guasto), su un file o su una
sottocartella, rende la scansione non affidabile. Precedenza dei codici:
radice o infrastruttura → 2; almeno un rilevamento → 1, anche con guasti;
almeno un guasto senza rilevamenti → 2; altrimenti 0. Permessi e file
spariti non cambiano il codice. Prima un guasto usciva con 0: sotto il
timer, nessuna notifica.

Percorso di lettura vero: clamd finto su socket Unix (tests/fake_clamd.py),
in sessione IDSESSION e senza. Il guasto si inietta in
ClamdClient._open_regular, all'apertura o a metà lettura.
"""

from __future__ import annotations

import errno
import importlib.util
import os
from pathlib import Path

import pytest

import klamav_py.cli as cli
from klamav_py.clamd_client import ClamdClient


def _load_fake():
    path = Path(__file__).with_name("fake_clamd.py")
    spec = importlib.util.spec_from_file_location("klamav_fake_clamd_io", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = _load_fake()
GUASTO = "guasto.bin"


@pytest.fixture
def clamd(tmp_path, monkeypatch):
    import socket
    sock = tmp_path / "clamd.sock"
    server = fake.FakeClamd(socket.AF_UNIX, str(sock))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    yield str(sock)
    server.close()


class _FailingReader:
    """File aperto davvero, la cui lettura fallisce con `code`."""

    def __init__(self, fh, code):
        self._fh, self._code = fh, code

    def read(self, n):
        raise OSError(self._code, os.strerror(self._code))

    def fileno(self):
        return self._fh.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()


@pytest.fixture
def broken_file(monkeypatch):
    """broken_file(code, dove): il file GUASTO fallisce all'apertura o alla
    lettura con quell'errno."""
    real = ClamdClient._open_regular

    def install(code, where="apertura"):
        def open_regular(target):
            if Path(target).name == GUASTO:
                if where == "apertura":
                    raise OSError(code, os.strerror(code), str(target))
                return _FailingReader(real(target), code)
            return real(target)
        monkeypatch.setattr(ClamdClient, "_open_regular", staticmethod(open_regular))

    return install


@pytest.fixture
def broken_dir(monkeypatch):
    real = os.scandir

    def install(target: Path, code: int):
        def scandir(path="."):
            if Path(os.fsdecode(path)) == target:
                raise OSError(code, os.strerror(code), os.fsdecode(path))
            return real(path)
        monkeypatch.setattr(os, "scandir", scandir)

    return install


def _tree(tmp_path, infected=False):
    root = tmp_path / "radice"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("pulito")
    (root / GUASTO).write_bytes(b"contenuto")
    if infected:
        (root / "virus.bin").write_bytes(fake.EICAR_MARK)
    return root


def _scan(clamd, root, *extra):
    return cli.main(["--socket", clamd, "scan", str(root), "--quiet", *extra])


SESSIONE = [[], ["--no-persistent"]]
IDS = ["sessione", "senza-sessione"]


@pytest.mark.parametrize("extra", SESSIONE, ids=IDS)
@pytest.mark.parametrize("where", ["apertura", "lettura"])
@pytest.mark.parametrize("code", [errno.EIO, errno.ESTALE, errno.ETIMEDOUT],
                         ids=errno.errorcode.get)
def test_guasto_su_un_file_senza_rilevamenti_esce_2(tmp_path, clamd, broken_file, capsys,
                                                    code, where, extra):
    root = _tree(tmp_path)
    broken_file(code, where)
    assert _scan(clamd, root, *extra) == 2
    out = capsys.readouterr()
    assert f"ERRORE su {root / GUASTO}: impossibile leggere il file" in out.err
    assert "sessione clamd interrotta" not in out.err
    last = out.out.strip().splitlines()[-1]
    assert "1 guasti di I/O" in last and "non è stata controllata" in last


def test_guasto_su_una_sottocartella_senza_rilevamenti_esce_2(tmp_path, clamd, broken_dir, capsys):
    root = _tree(tmp_path)
    broken_dir(root / "sub", errno.EIO)
    (root / GUASTO).unlink()
    assert _scan(clamd, root) == 2
    assert "1 guasti di I/O" in capsys.readouterr().out.strip().splitlines()[-1]


@pytest.mark.parametrize("extra", SESSIONE, ids=IDS)
def test_guasto_e_rilevamento_esce_1_con_il_guasto_nel_riepilogo(tmp_path, clamd, broken_file,
                                                                 capsys, extra):
    root = _tree(tmp_path, infected=True)
    broken_file(errno.EIO, "lettura")
    assert _scan(clamd, root, *extra) == 1
    out = capsys.readouterr().out
    assert "INFETTO" in out
    assert "1 guasti di I/O" in out.strip().splitlines()[-1]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.ENOENT], ids=errno.errorcode.get)
def test_permessi_e_file_spariti_non_cambiano_il_codice(tmp_path, clamd, broken_file, capsys, code):
    root = _tree(tmp_path)
    broken_file(code, "apertura")
    assert _scan(clamd, root) == 0
    out = capsys.readouterr()
    assert f"ERRORE su {root / GUASTO}" in out.err  # resta un errore
    assert "guasti di I/O" not in out.out


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOENT], ids=errno.errorcode.get)
def test_cartella_per_permessi_o_sparita_non_cambia_il_codice(tmp_path, clamd, broken_dir,
                                                             capsys, code):
    root = _tree(tmp_path)
    (root / GUASTO).unlink()
    broken_dir(root / "sub", code)
    assert _scan(clamd, root) == 0
    assert "guasti di I/O" not in capsys.readouterr().out


def test_sessione_ricreata_dopo_un_guasto_a_meta_lettura(tmp_path, clamd, broken_file, capsys):
    # L'INSTREAM interrotto lascia la sessione in uno stato incoerente: i
    # file successivi devono comunque essere verificati.
    root = _tree(tmp_path, infected=True)
    broken_file(errno.EIO, "lettura")
    assert _scan(clamd, root) == 1
    out = capsys.readouterr().out
    assert "3 file scansionati, 1 infetti, 1 errori." in out


def test_classificazione():
    from klamav_py.clamd_client import is_io_fault

    def e(code):
        return OSError(code, os.strerror(code))

    for code in (errno.EIO, errno.ESTALE, errno.ETIMEDOUT, errno.ENOSPC):
        assert is_io_fault(e(code)) and is_io_fault(e(code), directory=True)
    for code in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR):
        assert not is_io_fault(e(code)) and not is_io_fault(e(code), directory=True)
    # File sostituito da un symlink o da una cartella: sparito, non guasto.
    # L'esenzione vale solo per i file: su una cartella restano guasti.
    for code in (errno.ELOOP, errno.EISDIR):
        assert not is_io_fault(e(code), directory=False)
        assert is_io_fault(e(code), directory=True)
    assert not is_io_fault(OSError("non è un file regolare"))
