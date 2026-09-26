"""
--tcp nella CLI: parsing, mutua esclusione con --socket, messaggi d'errore
che dicono DOVE si è cercato clamd (describe()), e percorso completo
CLI → TCP → protocollo contro il finto clamd di fake_clamd.py.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

import klamav_py.cli as cli
from klamav_py.clamd_client import DEFAULT_SOCKET, ClamdEndpoint


def load_fake_clamd():
    """Carica tests/fake_clamd.py per percorso (vedi il suo docstring)."""
    import importlib.util

    path = Path(__file__).with_name("fake_clamd.py")
    spec = importlib.util.spec_from_file_location("klamav_fake_clamd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = load_fake_clamd()


@pytest.fixture
def server():
    s = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0))
    yield s
    s.close()


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "dati"
    d.mkdir()
    (d / "pulito.txt").write_bytes(b"ciao")
    (d / "infetto.txt").write_bytes(b"x " + fake.EICAR_MARK)
    return d


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


# -- parsing -------------------------------------------------------------

def test_default_socket_unix():
    assert parse("ping").endpoint == ClamdEndpoint.unix(DEFAULT_SOCKET)


def test_socket_esplicito():
    assert parse("--socket", "/run/x.sock", "ping").endpoint == ClamdEndpoint.unix("/run/x.sock")


@pytest.mark.parametrize("value, atteso", [
    ("localhost", ClamdEndpoint.tcp("localhost")),
    ("10.0.0.5:3311", ClamdEndpoint.tcp("10.0.0.5", 3311)),
    ("[::1]:3311", ClamdEndpoint.tcp("::1", 3311)),
])
def test_tcp(value, atteso):
    assert parse("--tcp", value, "ping").endpoint == atteso


def test_socket_e_tcp_mutuamente_esclusivi(capsys):
    with pytest.raises(SystemExit) as exc:
        parse("--socket", "/run/x.sock", "--tcp", "localhost", "ping")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--tcp" in err and "--socket" in err


@pytest.mark.parametrize("value, motivo", [
    ("host:0", "fuori intervallo"),
    ("host:NaN", "porta non valida"),
    ("/run/clamav/clamd.ctl", "--socket"),
])
def test_tcp_non_valido_mostra_il_motivo(capsys, value, motivo):
    # ArgumentTypeError: il motivo arriva all'utente, non "invalid value".
    with pytest.raises(SystemExit) as exc:
        parse("--tcp", value, "ping")
    assert exc.value.code == 2 and motivo in capsys.readouterr().err


def test_socket_vuoto_rifiutato_senza_traceback(capsys):
    with pytest.raises(SystemExit) as exc:
        parse("--socket", "", "ping")
    assert exc.value.code == 2


def test_opzioni_globali_prima_del_comando():
    with pytest.raises(SystemExit):
        parse("ping", "--tcp", "localhost")


# -- ping ----------------------------------------------------------------

def test_ping_tcp(server, capsys):
    port = server.address[1]
    assert cli.main(["--tcp", f"127.0.0.1:{port}", "ping"]) == 0
    assert f"TCP 127.0.0.1:{port}" in capsys.readouterr().out
    assert server.commands == ["PING"]


def test_ping_tcp_porta_chiusa_dice_dove(capsys):
    port = fake.closed_tcp_port()
    assert cli.main(["--tcp", f"127.0.0.1:{port}", "ping"]) == 2
    assert f"TCP 127.0.0.1:{port}" in capsys.readouterr().err


def test_ping_socket_inesistente_dice_dove(capsys, tmp_path):
    missing = tmp_path / "nessuno.sock"
    assert cli.main(["--socket", str(missing), "ping"]) == 2
    assert f"socket {missing}" in capsys.readouterr().err


# -- scan ----------------------------------------------------------------

@pytest.mark.parametrize("persistent", [True, False])
def test_scan_tcp(server, files, capsys, persistent):
    argv = ["--tcp", f"127.0.0.1:{server.address[1]}", "scan", str(files)]
    if not persistent:
        argv.append("--no-persistent")
    assert cli.main(argv) == 1  # un infetto
    out = capsys.readouterr().out
    assert "INFETTO" in out and "infetto.txt" in out and "1 infetti" in out
    assert server.commands[0] == "PING"  # controllo preliminare


@pytest.mark.parametrize("extra", [[], ["--no-persistent"]])
def test_scan_socket_inesistente_esce_con_2(files, capsys, tmp_path, extra):
    # Regressione: scan_stream trasforma "clamd irraggiungibile" in un ERROR
    # per file, e senza il PING preliminare l'uscita era 0 ("pulito"): la
    # scansione programmata con clamd spento non notificava nulla.
    missing = tmp_path / "nessuno.sock"
    assert cli.main(["--socket", str(missing), "scan", str(files), *extra]) == 2
    assert f"socket {missing}" in capsys.readouterr().err


def test_scan_ping_senza_pong_esce_con_2(files, capsys, monkeypatch):
    monkeypatch.setattr("klamav_py.clamd_client.ClamdClient.ping", lambda self: False)
    assert cli.main(["scan", str(files)]) == 2
    assert "non ha risposto correttamente" in capsys.readouterr().err


def test_scan_tcp_porta_chiusa_esce_con_2_e_dice_dove(files, capsys):
    port = fake.closed_tcp_port()
    assert cli.main(["--tcp", f"127.0.0.1:{port}", "scan", str(files)]) == 2
    assert f"TCP 127.0.0.1:{port}" in capsys.readouterr().err


def test_scan_host_non_risolvibile_esce_con_2(files, capsys):
    # .invalid è riservato (RFC 2606): non si risolve mai. Prima un
    # socket.gaierror usciva come traceback con codice 1 ("infezioni").
    assert cli.main(["--tcp", "clamd.invalid", "scan", str(files)]) == 2
    assert "TCP clamd.invalid:3310" in capsys.readouterr().err


def test_scan_socket_senza_permessi_esce_con_2(files, capsys, tmp_path):
    # Utente fuori dal gruppo clamav: PermissionError sul connect. Prima
    # usciva come traceback con codice 1, come se avesse trovato infezioni.
    if os.getuid() == 0:
        pytest.skip("da root i permessi del socket non contano")
    path = tmp_path / "clamd.sock"
    s = fake.FakeClamd(socket.AF_UNIX, str(path))
    try:
        path.chmod(0o000)
        assert cli.main(["--socket", str(path), "scan", str(files)]) == 2
        assert f"socket {path}" in capsys.readouterr().err
    finally:
        path.chmod(0o600)
        s.close()
