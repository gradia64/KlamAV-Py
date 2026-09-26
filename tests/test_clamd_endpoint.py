"""
ClamdEndpoint: parsing, validazione, descrizione e costruzione del client.

La parte di integrazione usa un finto clamd su socket reali (TCP su
127.0.0.1 e ::1, Unix in una directory temporanea), come
test_single_instance.py: copre l'intero percorso endpoint → connessione →
protocollo (PING, INSTREAM, IDSESSION) senza bisogno del demone.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from klamav_py.clamd_client import (
    DEFAULT_SOCKET,
    DEFAULT_TCP_PORT,
    ClamdClient,
    ClamdEndpoint,
    ClamdUnavailable,
)


def load_fake_clamd():
    """Carica tests/fake_clamd.py per percorso (vedi il suo docstring)."""
    import importlib.util

    path = Path(__file__).with_name("fake_clamd.py")
    spec = importlib.util.spec_from_file_location("klamav_fake_clamd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



# -- parse_cli -----------------------------------------------------------

@pytest.mark.parametrize("value, host, port", [
    ("localhost", "localhost", DEFAULT_TCP_PORT),
    ("localhost:3311", "localhost", 3311),
    ("10.0.0.5:1", "10.0.0.5", 1),
    ("clamd.lan:65535", "clamd.lan", 65535),
    ("[::1]", "::1", DEFAULT_TCP_PORT),
    ("[::1]:3311", "::1", 3311),
    ("::1", "::1", DEFAULT_TCP_PORT),  # IPv6 senza parentesi: tutto host
    ("fe80::1%eth0", "fe80::1%eth0", DEFAULT_TCP_PORT),
])
def test_parse_cli_validi(value, host, port):
    ep = ClamdEndpoint.parse_cli(value)
    assert (ep.transport, ep.tcp_host, ep.tcp_port) == ("tcp", host, port)


@pytest.mark.parametrize("value", [
    "", ":3310", "host:", "host:0", "host:65536", "host:99999999",
    "host:NaN", "host: 80", "host:+80", "host:0x50", "host:٨٠", "host:-1",
    "[::1", "[::1]3310", "[]:3310", "[::1]:",
    "ho st", "host\n", "/run/clamav/clamd.ctl",
    "1.2.3.4:5:6", "host:80:90", "[127.0.0.1:3310]",
])
def test_parse_cli_rifiutati(value):
    with pytest.raises(ValueError):
        ClamdEndpoint.parse_cli(value)


@pytest.mark.parametrize("host", ["127.0.0.1:3310", "clamd.lan:3310", "a:b"])
def test_host_con_porta_rifiutato_con_indicazione(host):
    # Regressione: "127.0.0.1:3310" nel campo host era accettato, mostrato
    # come "[127.0.0.1:3310]:3310" e falliva solo alla risoluzione del nome.
    with pytest.raises(ValueError, match="porta va indicata a parte"):
        ClamdEndpoint.tcp(host)


@pytest.mark.parametrize("host", ["::1", "2001:db8::1", "fe80::1%eth0", "::ffff:127.0.0.1"])
def test_ipv6_validi_accettati(host):
    assert ClamdEndpoint.tcp(host).tcp_host == host


def test_percorso_al_posto_dell_host_suggerisce_socket():
    with pytest.raises(ValueError, match="--socket"):
        ClamdEndpoint.parse_cli("/run/clamav/clamd.ctl")


# -- validazione ---------------------------------------------------------

def test_default_e_unix():
    ep = ClamdEndpoint()
    assert ep.transport == "unix" and ep.unix_socket == DEFAULT_SOCKET and not ep.is_tcp


@pytest.mark.parametrize("kwargs", [
    dict(transport="udp"),
    dict(transport="TCP", tcp_host="h"),  # nessuna normalizzazione implicita
    dict(transport="tcp"),  # host mancante: niente TCP "dedotto"
    dict(transport="tcp", tcp_host="h", tcp_port=0),
    dict(transport="tcp", tcp_host="h", tcp_port=65536),
    dict(transport="tcp", tcp_host="h", tcp_port=True),
    dict(transport="tcp", tcp_host="h", tcp_port="3310"),
    dict(transport="tcp", tcp_host="h", tcp_port=3310.0),
    dict(transport="unix", unix_socket=""),
    dict(transport="unix", unix_socket="/run/a\nb"),
])
def test_costruzioni_rifiutate(kwargs):
    with pytest.raises(ValueError):
        ClamdEndpoint(**kwargs)


def test_host_ignorato_con_transport_unix():
    # tcp_host valorizzato non rende TCP un endpoint unix: conta solo
    # transport (le Impostazioni conservano host e porta anche quando
    # l'utente torna al socket).
    ep = ClamdEndpoint(transport="unix", tcp_host="10.0.0.5")
    assert not ep.is_tcp and ep.new_client().unix_socket == DEFAULT_SOCKET


def test_immutabile():
    with pytest.raises(AttributeError):
        ClamdEndpoint().transport = "tcp"


# -- describe ------------------------------------------------------------

@pytest.mark.parametrize("ep, atteso", [
    (ClamdEndpoint.unix("/run/clamav/clamd.ctl"), "socket /run/clamav/clamd.ctl"),
    (ClamdEndpoint.tcp("10.0.0.5"), "TCP 10.0.0.5:3310"),
    (ClamdEndpoint.tcp("::1", 3311), "TCP [::1]:3311"),
])
def test_describe(ep, atteso):
    assert ep.describe() == atteso


@pytest.mark.parametrize("value", ["clamd.lan", "clamd.lan:3311", "[::1]:3311", "[fe80::1]"])
def test_describe_rileggibile_da_parse_cli(value):
    ep = ClamdEndpoint.parse_cli(value)
    assert ClamdEndpoint.parse_cli(ep.describe().removeprefix("TCP ")) == ep


@pytest.mark.parametrize("ep", [
    ClamdEndpoint.tcp("clamd.lan"),
    ClamdEndpoint.tcp("10.0.0.5", 3311),
    ClamdEndpoint.tcp("::1", 1),
    ClamdEndpoint.tcp("fe80::1%eth0", 65535),
])
def test_to_cli_round_trip(ep):
    assert ClamdEndpoint.parse_cli(ep.to_cli()) == ep


def test_to_cli_unix():
    assert ClamdEndpoint.unix("/run/x.sock").to_cli() == "/run/x.sock"


# -- new_client ----------------------------------------------------------

def test_new_client_unix():
    c = ClamdEndpoint.unix("/tmp/x.sock").new_client(timeout=5)
    assert (c.unix_socket, c.tcp_host, c.timeout) == ("/tmp/x.sock", None, 5)


def test_new_client_tcp_non_usa_il_socket_di_default():
    # ClamdClient ha unix_socket con un default: se new_client non lo
    # azzerasse, _connect() userebbe il socket e ignorerebbe l'host.
    c = ClamdEndpoint.tcp("10.0.0.5", 3311).new_client()
    assert (c.unix_socket, c.tcp_host, c.tcp_port) == (None, "10.0.0.5", 3311)


fake = load_fake_clamd()
FakeClamd = fake.FakeClamd
EICAR_MARK = fake.EICAR_MARK


@pytest.fixture
def tcp4():
    server = FakeClamd(socket.AF_INET, ("127.0.0.1", 0))
    yield server, ClamdEndpoint.tcp("127.0.0.1", server.address[1])
    server.close()


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "dati"
    d.mkdir()
    (d / "pulito.txt").write_bytes(b"ciao" * 5000)  # più chunk da 8 KiB
    (d / "eicar.txt").write_bytes(b"prima " + EICAR_MARK + b" dopo")
    return d


def _verdetti(client, root, **kw):
    return {Path(r.path).name: r.status for r in client.scan_stream(root, **kw)}


def test_tcp_ping_e_version(tcp4):
    server, ep = tcp4
    client = ep.new_client(timeout=5)
    assert client.ping()
    assert client.version().startswith("ClamAV")
    assert server.commands == ["PING", "VERSION"]


@pytest.mark.parametrize("persistent", [False, True])
def test_tcp_instream(tcp4, files, persistent):
    server, ep = tcp4
    verdetti = _verdetti(ep.new_client(timeout=5), files, persistent=persistent)
    assert verdetti == {"pulito.txt": "OK", "eicar.txt": "FOUND"}
    assert ("IDSESSION" in server.commands) is persistent


def test_tcp_ipv6(files):
    if not socket.has_ipv6:
        pytest.skip("IPv6 non disponibile")
    try:
        server = FakeClamd(socket.AF_INET6, ("::1", 0))
    except OSError:
        pytest.skip("::1 non disponibile qui")
    try:
        ep = ClamdEndpoint.parse_cli(f"[::1]:{server.address[1]}")
        assert ep.new_client(timeout=5).ping()
        assert _verdetti(ep.new_client(timeout=5), files, persistent=False)["eicar.txt"] == "FOUND"
    finally:
        server.close()


def test_unix(tmp_path, files):
    path = tmp_path / "clamd.sock"
    server = FakeClamd(socket.AF_UNIX, str(path))
    try:
        ep = ClamdEndpoint.unix(str(path))
        assert ep.new_client(timeout=5).ping()
        assert _verdetti(ep.new_client(timeout=5), files)["eicar.txt"] == "FOUND"
    finally:
        server.close()


def test_tcp_porta_chiusa_errore_visibile():
    # Porta libera ma senza server: l'errore deve arrivare, non un ripiego
    # silenzioso sul socket Unix predefinito.
    port = fake.closed_tcp_port()
    with pytest.raises(ClamdUnavailable) as exc:
        ClamdEndpoint.tcp("127.0.0.1", port).new_client(timeout=2).ping()
    assert isinstance(exc.value.__cause__, ConnectionRefusedError)
    assert exc.value.where == f"TCP 127.0.0.1:{port}"


def test_client_diretto_invariato():
    # Refactor puro: chi costruisce ancora ClamdClient direttamente (fino
    # alla migrazione dei worker) ottiene lo stesso default di prima.
    assert ClamdClient().unix_socket == DEFAULT_SOCKET
