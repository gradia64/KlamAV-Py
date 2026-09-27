"""
--exclude nella CLI: stessa regola delle Impostazioni della GUI
(scan_exclusions), con i relativi risolti sulla cwd come per --quarantine.

I due casi che prima passavano in silenzio: un'esclusione che contiene
il percorso da scansionare (uscita 0 senza aver controllato nulla) e
un'esclusione di file (ignorata da _iter_files, che sfoltisce solo
directory). Ora escono con 2.
"""

from __future__ import annotations

from collections import Counter

import pytest

import klamav_py.cli as cli
from klamav_py.clamd_client import ClamdEndpoint


class FakeClient:
    """Nessun clamd: registra le esclusioni e non restituisce risultati."""

    last: "FakeClient | None" = None

    def __init__(self, *a, **k):
        self.skipped = Counter()
        self.exclude_dirs = None
        FakeClient.last = self

    def ping(self):
        return True

    def scan_stream(self, root, *, exclude_dirs, **k):
        self.exclude_dirs = exclude_dirs
        return iter(())


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "dati").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    FakeClient.last = None
    return h


def run(*argv):
    return cli.main(["scan", *map(str, argv)])


def test_esclusione_normale_risolta(home, capsys):
    (home / "vm").mkdir()
    (home / "link-vm").symlink_to(home / "vm")
    assert run(home, "--exclude", home / "link-vm") == 0
    assert FakeClient.last.exclude_dirs == [(home / "vm").resolve()]
    assert capsys.readouterr().err == ""


def test_relativa_risolta_sulla_cwd(home, monkeypatch):
    (home / "vm").mkdir()
    monkeypatch.chdir(home)
    assert run(home, "--exclude", "vm") == 0
    assert FakeClient.last.exclude_dirs == [(home / "vm").resolve()]


@pytest.mark.parametrize("quale", ["uguale", "genitore"])
def test_contiene_la_radice(home, capsys, quale):
    esclusa = home / "dati" if quale == "uguale" else home
    assert run(home / "dati", "--exclude", esclusa) == 2
    assert FakeClient.last is None  # nessuna scansione avviata
    assert "esclusa per intero" in capsys.readouterr().err


def test_file_rifiutato(home, capsys):
    (home / "disco.iso").write_bytes(b"")
    assert run(home, "--exclude", home / "disco.iso") == 2
    assert "non è una directory" in capsys.readouterr().err


def test_inesistente_solo_avviso(home, capsys):
    assert run(home, "--exclude", home / "non-ancora") == 0
    assert "non esiste" in capsys.readouterr().err


def test_fuori_dalla_radice_solo_avviso(home, capsys):
    (home / "altro").mkdir()
    assert run(home / "dati", "--exclude", home / "altro") == 0
    assert "non ha effetto" in capsys.readouterr().err


def test_con_file_come_radice_niente_confronti(home, capsys):
    # Scansione di un singolo file: nessuna radice, quindi nessun avviso
    # "fuori da" per un'esclusione che comunque non si applicherebbe.
    f = home / "dati" / "f.txt"
    f.write_text("x")
    (home / "vm").mkdir()
    assert run(f, "--exclude", home / "vm") == 0
    assert capsys.readouterr().err == ""


def test_quarantena_aggiunta_dopo_le_esclusioni(home, monkeypatch):
    from functools import partial

    from klamav_py.quarantine_location import decide

    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(
        cli, "decide_quarantine_dir",
        partial(decide, unit_hidden=(), mountinfo="22 1 8:1 / / rw - ext4 /dev/sda1 rw\n", volatile_roots=()),
    )
    (home / "vm").mkdir()
    q = home / "q"
    assert run(home, "--exclude", home / "vm", "--quarantine", q) == 0
    assert FakeClient.last.exclude_dirs == [(home / "vm").resolve(), q.resolve()]
