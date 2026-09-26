"""
clamd irraggiungibile: prima della scansione e A SCANSIONE INIZIATA.

Prima, il fallimento di connessione diventava un ScanResult ERROR per ogni
file, e una scansione che non aveva verificato nulla finiva "pulita"
(uscita 0, nessuna notifica della scansione programmata). Ora il client
solleva ClamdUnavailable, dopo alcuni tentativi che assorbono un riavvio
breve di clamd; gli errori sul singolo file restano risultati ERROR.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

import klamav_py.cli as cli
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ClamdUnavailable


def load_fake_clamd():
    """Carica tests/fake_clamd.py per percorso (vedi il suo docstring)."""
    import importlib.util

    path = Path(__file__).with_name("fake_clamd.py")
    spec = importlib.util.spec_from_file_location("klamav_fake_clamd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = load_fake_clamd()
# Letto prima che la fixture no_waits lo azzeri.
DEFAULT_DELAYS = ClamdClient.RECONNECT_DELAYS


def test_attese_predefinite_ragionevoli():
    # Abbastanza da coprire un riavvio di clamd, non tanto da far sembrare
    # bloccata una scansione con clamd davvero spento.
    assert DEFAULT_DELAYS and 3 <= sum(DEFAULT_DELAYS) <= 30


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    # Nessuna attesa reale fra i tentativi, salvo i test che la verificano.
    monkeypatch.setattr(ClamdClient, "RECONNECT_DELAYS", (0.0, 0.0))


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "dati"
    d.mkdir()
    for i in range(6):
        (d / f"file{i}.txt").write_bytes(b"contenuto %d" % i)
    return d


def _closed():
    return ClamdEndpoint.tcp("127.0.0.1", fake.closed_tcp_port())


# -- prima della scansione -----------------------------------------------

def test_ping_porta_chiusa(monkeypatch):
    ep = _closed()
    with pytest.raises(ClamdUnavailable) as exc:
        ep.new_client(timeout=2).ping()
    assert exc.value.where == ep.describe()
    assert isinstance(exc.value.__cause__, ConnectionRefusedError)


def test_socket_unix_inesistente(tmp_path):
    with pytest.raises(ClamdUnavailable) as exc:
        ClamdEndpoint.unix(str(tmp_path / "no.sock")).new_client().ping()
    assert isinstance(exc.value.__cause__, FileNotFoundError)


def test_ping_non_ritenta(monkeypatch):
    # I tentativi valgono solo per la scansione: un ping deve rispondere
    # subito, anche per non bloccare il controllo periodico della GUI.
    sleeps = []
    monkeypatch.setattr("klamav_py.clamd_client.time.sleep", sleeps.append)
    monkeypatch.setattr(ClamdClient, "RECONNECT_DELAYS", (2.0, 5.0))
    with pytest.raises(ClamdUnavailable):
        _closed().new_client(timeout=2).ping()
    assert sleeps == []


@pytest.mark.parametrize("persistent", [True, False])
def test_scansione_senza_clamd_solleva_senza_risultati(files, persistent):
    results = []
    with pytest.raises(ClamdUnavailable):
        for r in _closed().new_client(timeout=2).scan_stream(files, persistent=persistent):
            results.append(r)
    assert results == []  # niente righe ERROR "impossibile aprire sessione"


def test_attese_fra_i_tentativi(files, monkeypatch):
    sleeps = []
    monkeypatch.setattr("klamav_py.clamd_client.time.sleep", sleeps.append)
    monkeypatch.setattr(ClamdClient, "RECONNECT_DELAYS", (2.0, 5.0))
    with pytest.raises(ClamdUnavailable):
        list(_closed().new_client(timeout=2).scan_stream(files))
    assert sleeps == [2.0, 5.0]


# -- durante la scansione ------------------------------------------------

@pytest.mark.parametrize("persistent", [True, False])
def test_clamd_che_muore_a_meta(files, persistent):
    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0), die_after=2)
    try:
        client = ClamdEndpoint.tcp("127.0.0.1", server.address[1]).new_client(timeout=5)
        results = []
        with pytest.raises(ClamdUnavailable):
            for r in client.scan_stream(files, persistent=persistent):
                results.append(r)
        ok = [r for r in results if r.status == "OK"]
        assert len(ok) == 2 and len(results) < 6
    finally:
        server.close()


def test_riavvio_breve_assorbito(files, monkeypatch):
    # clamd rifiuta due connessioni e poi torna: la scansione prosegue.
    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0))
    try:
        client = ClamdEndpoint.tcp("127.0.0.1", server.address[1]).new_client(timeout=5)
        real_connect = client._connect
        failures = iter([True, True])

        def flaky():
            if next(failures, False):
                raise ClamdUnavailable("rifiutata", client.describe())
            return real_connect()

        monkeypatch.setattr(client, "_connect", flaky)
        results = list(client.scan_stream(files, persistent=False))
        assert len(results) == 6 and all(r.status == "OK" for r in results)
    finally:
        server.close()


def test_riavvio_troppo_lungo_interrompe(files, monkeypatch):
    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0))
    try:
        client = ClamdEndpoint.tcp("127.0.0.1", server.address[1]).new_client(timeout=5)

        def always_down():
            raise ClamdUnavailable("rifiutata", client.describe())

        monkeypatch.setattr(client, "_connect", always_down)
        with pytest.raises(ClamdUnavailable):
            list(client.scan_stream(files))
    finally:
        server.close()


# -- CLI -----------------------------------------------------------------

def test_cli_clamd_che_muore_a_meta_esce_con_2(files, capsys):
    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0), die_after=3)  # PING non conta
    try:
        code = cli.main(["--tcp", f"127.0.0.1:{server.address[1]}", "scan", str(files)])
    finally:
        server.close()
    err = capsys.readouterr().err
    assert code == 2
    assert "Scansione incompleta" in err and f"TCP 127.0.0.1:{server.address[1]}" in err


# -- GUI -----------------------------------------------------------------

def test_scan_worker_un_solo_errore_invece_di_n_righe(files):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from klamav_py.gui.scan_worker import ScanWorker

    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0), die_after=2)
    try:
        ep = ClamdEndpoint.tcp("127.0.0.1", server.address[1])
        worker = ScanWorker(
            endpoint=ep,
            target=files,
            client_factory=lambda **kw: ep.new_client(timeout=5),
        )
        errors, rows, finished = [], [], []
        worker.error.connect(errors.append)
        worker.result_ready.connect(rows.append)
        worker.finished_scan.connect(lambda *c: finished.append(c))
        worker.run()  # sincrono: niente thread, i segnali arrivano diretti
    finally:
        server.close()
    assert len(errors) == 1 and finished
    assert not any("impossibile aprire sessione" in (r.signature or "") for r in rows)
