"""
Una sola costruzione della policy fra scan e --acknowledge (cli.build_policy).

cmd_scan costruiva la regola «solo segnalazione» con le cartelle
predefinite più le --report-only dell'utente e con --quarantine-all,
--acknowledge con le sole cartelle predefinite. Un file sotto una
--report-only personalizzata era «solo segnalazione» in scansione, ma
--acknowledge lo rifiutava come «rilevamento da quarantena» senza modo di
indicare la cartella. Ora --report-only e --quarantine-all valgono anche
fra le opzioni globali, e si passano a --acknowledge come alla scansione.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import klamav_py.cli as cli
from klamav_py.acknowledged import AckRegistry
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ScanResult

TROJAN = "Win.Trojan.Agent-1"
PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"


class FakeClient:
    """b"TROJAN" è una firma non euristica, b"PHISH" euristica."""

    def __init__(self, **kw):
        self.skipped = Counter()

    def ping(self):
        return True

    def scan_stream(self, root, exclude_dirs=None, **kw):
        for f in ClamdClient._iter_files(Path(root).resolve(), exclude_dirs):
            data = f.read_bytes()
            if b"TROJAN" in data:
                yield ScanResult(str(f), "FOUND", TROJAN)
            elif b"PHISH" in data:
                yield ScanResult(str(f), "FOUND", PHISHING)
            else:
                yield ScanResult(str(f), "OK")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "Archivio").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    return h


def _scan(home, *extra, globali=()):
    return cli.main([*globali, "scan", str(home), "--quarantine",
                     str(home / ".local/share/klamav-py/quarantine"), "--quiet", *extra])


@pytest.fixture
def archivio(home):
    f = home / "Archivio" / "vecchio.exe"
    f.write_bytes(b"TROJAN")
    return f


def test_report_only_personalizzata_flusso_completo(home, archivio, capsys):
    cartella = str(home / "Archivio")
    assert _scan(home, "--report-only", cartella) == 1
    out = capsys.readouterr().out
    assert "NON messo in quarantena" in out and archivio.exists()

    assert cli.main(["--report-only", cartella, "--acknowledge", str(archivio)]) == 0
    assert "Presa visione registrata" in capsys.readouterr().out
    (entry,) = AckRegistry().entries()
    assert entry.signature == TROJAN

    assert _scan(home, "--report-only", cartella) == 0
    assert f"GIÀ VALUTATO: {archivio}" in capsys.readouterr().out


def test_senza_report_only_rifiutata_con_il_motivo(home, archivio, capsys):
    assert cli.main(["--acknowledge", str(archivio)]) == 2
    err = capsys.readouterr().err
    assert "rilevamento da quarantena" in err
    assert "passa le stesse cartelle anche a --acknowledge" in err
    assert AckRegistry().entries() == []


def test_quarantine_all_rifiuta_anche_le_euristiche(home, capsys):
    f = home / "phish.eml"
    f.write_bytes(b"PHISH")
    assert cli.main(["--quarantine-all", "--acknowledge", str(f)]) == 2
    assert "con --quarantine-all nessun rilevamento è solo segnalato" in capsys.readouterr().err
    # Senza, la stessa euristica si registra.
    assert cli.main(["--acknowledge", str(f)]) == 0


def test_scan_accetta_le_opzioni_anche_in_forma_globale(home, archivio, capsys):
    assert _scan(home, globali=("--report-only", str(home / "Archivio"))) == 1
    assert "NON messo in quarantena" in capsys.readouterr().out and archivio.exists()


def test_globali_e_di_scan_si_sommano(home, archivio, capsys):
    altro = home / "Altro"
    altro.mkdir()
    (altro / "b.exe").write_bytes(b"TROJAN")
    assert _scan(home, "--report-only", str(altro),
                 globali=("--report-only", str(home / "Archivio"))) == 1
    assert capsys.readouterr().out.count("NON messo in quarantena") == 2
    assert archivio.exists() and (altro / "b.exe").exists()


def test_build_policy_unica(home):
    parser = cli.build_parser()
    scan = cli.build_policy(parser.parse_args(
        ["scan", str(home), "--report-only", str(home / "Archivio")]))
    ack = cli.build_policy(parser.parse_args(
        ["--report-only", str(home / "Archivio"), "--acknowledge", "x"]))
    assert scan.report_only_dirs == ack.report_only_dirs
    assert not cli.build_policy(parser.parse_args(["--quarantine-all", "--list-acknowledged"])).enabled
    assert not cli.build_policy(parser.parse_args(["scan", "x", "--quarantine-all"])).enabled


def test_ping_rifiuta_le_opzioni_della_policy(home):
    with pytest.raises(SystemExit):
        cli.main(["--report-only", str(home), "ping"])
