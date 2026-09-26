"""
--quarantine nella CLI: stessa regola della GUI (quarantine_location), con
le due differenze volute (relativi risolti sulla cwd, directory volatili
bloccanti solo sotto systemd) e i due casi che prima uscivano male:
quarantena che contiene la radice della scansione (uscita 0 senza aver
controllato nulla) e errore nel creare la quarantena (traceback con
uscita 1, cioè "infezioni trovate").
"""

from __future__ import annotations

import os
from collections import Counter
from functools import partial

import pytest

import klamav_py.cli as cli
from klamav_py.clamd_client import ClamdEndpoint
from klamav_py.quarantine_location import decide

MOUNTS_EXT4 = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


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
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "dati"
    target.mkdir()
    volatile = tmp_path / "volatile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    FakeClient.last = None
    # tmp_path sta sotto /tmp: la radice volatile di prova è una sua
    # sottodirectory, così i casi non volatili restano tali.
    monkeypatch.setattr(
        cli, "decide_quarantine_dir",
        partial(decide, unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=[volatile]),
    )
    return home, target, volatile


def run(*argv):
    return cli.main(["scan", *map(str, argv)])


def test_quarantena_normale_esclusa_e_creata(env, capsys):
    home, target, _ = env
    q = home / "q"
    assert run(target, "--quarantine", q) == 0
    assert q.is_dir() and q in FakeClient.last.exclude_dirs
    assert capsys.readouterr().err == ""


def test_relativa_risolta_sulla_cwd(env, monkeypatch):
    home, target, _ = env
    monkeypatch.chdir(home)
    assert run(target, "--quarantine", "q-relativa") == 0
    assert (home / "q-relativa").is_dir()


def test_tilde_espansa(env):
    home, target, _ = env
    assert run(target, "--quarantine", "~/q") == 0
    assert (home / "q").is_dir()


def test_volatile_in_primo_piano_solo_avviso(env, capsys):
    _, target, volatile = env
    assert run(target, "--quarantine", volatile / "q") == 0
    assert "ATTENZIONE" in capsys.readouterr().err
    assert (volatile / "q").is_dir()


def test_volatile_sotto_systemd_bloccante(env, capsys, monkeypatch):
    _, target, volatile = env
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    assert run(target, "--quarantine", volatile / "q") == 2
    assert "non utilizzabile" in capsys.readouterr().err
    assert not (volatile / "q").exists()
    assert FakeClient.last is None  # nessuna scansione avviata


@pytest.mark.parametrize("dentro", [False, True])
def test_quarantena_che_contiene_la_radice(env, capsys, dentro):
    # Prima: radice esclusa per intero, 0 file scansionati, uscita 0.
    _, target, _ = env
    radice = target
    if dentro:
        radice = target / "sotto"
        radice.mkdir()
    assert run(radice, "--quarantine", target) == 2
    assert "verrebbe escluso per intero" in capsys.readouterr().err


def test_quarantena_sotto_la_radice_ammessa(env):
    # Il caso normale della scansione programmata: home scansionata,
    # quarantena al suo interno ed esclusa.
    home, _, _ = env
    q = home / ".local/share/klamav-py/quarantine"
    assert run(home, "--quarantine", q) == 0
    assert q in FakeClient.last.exclude_dirs


def test_file_al_posto_della_directory(env, capsys):
    home, target, _ = env
    f = home / "file"
    f.write_text("x")
    assert run(target, "--quarantine", f) == 2
    assert "non è una directory" in capsys.readouterr().err


def test_errore_nel_creare_la_quarantena_esce_con_2(env, capsys):
    # Prima: traceback, uscita 1, OnFailure che notificava "infezioni".
    home, target, _ = env
    if os.getuid() == 0:
        pytest.skip("da root i permessi non bloccano mkdir")
    bloccata = home / "bloccata"
    bloccata.mkdir()
    bloccata.chmod(0o500)
    try:
        assert run(target, "--quarantine", bloccata / "q") == 2
        assert "non utilizzabile" in capsys.readouterr().err
    finally:
        bloccata.chmod(0o700)


def test_permessi_larghi_segnalati(env, capsys):
    home, target, _ = env
    q = home / "q"
    q.mkdir()
    q.chmod(0o755)
    assert run(target, "--quarantine", q) == 0
    assert "755" in capsys.readouterr().err
    assert q.stat().st_mode & 0o777 == 0o700


def test_tmpfs_avviso(env, capsys, monkeypatch, tmp_path):
    home, target, _ = env
    ram = tmp_path / "ram"
    monkeypatch.setattr(
        cli, "decide_quarantine_dir",
        partial(decide, unit_hidden=(), volatile_roots=(),
                mountinfo=MOUNTS_EXT4 + f"30 22 0:40 / {ram} rw - tmpfs tmpfs rw\n"),
    )
    assert run(target, "--quarantine", ram / "q") == 0
    assert "tmpfs" in capsys.readouterr().err


def test_senza_quarantena_nessun_controllo(env, monkeypatch):
    _, target, _ = env
    monkeypatch.setattr(cli, "decide_quarantine_dir", None)  # non deve essere chiamata
    assert run(target) == 0
