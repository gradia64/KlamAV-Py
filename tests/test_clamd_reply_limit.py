"""
Tetto alla risposta letta da clamd (audit 0.1.8).

Le due letture (_read_all per PING/VERSION/CONTSCAN/INSTREAM e
_read_reply della sessione IDSESSION) accumulavano in memoria senza
limite: un interlocutore che invia senza fermarsi faceva crescere il
buffer fino a esaurire la RAM. Non riguarda il clamd di sistema, ma un
clamd remoto via TCP o un socket in un percorso configurabile.

I test usano un finto clamd su socket Unix: uno che risponde
correttamente (nessuna regressione) e uno che inonda.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from klamav_py.clamd_client import (
    MAX_REPLY_BYTES,
    ClamdClient,
    ClamdError,
    _ClamdSession,
)


class FintoClamd:
    """Server Unix che risponde con `risposta` oppure inonda di byte."""

    def __init__(self, path: Path, risposta: bytes | None = None, inonda: bool = False):
        self.path = str(path)
        self.risposta = risposta
        self.inonda = inonda
        self.byte_inviati = 0
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(1)
        self._srv.settimeout(5)
        self._thread = threading.Thread(target=self._servi, daemon=True)
        self._thread.start()

    def _servi(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                conn.recv(4096)  # il comando, ignorato
                if self.inonda:
                    blocco = b"x" * 65536
                    # Abbondantemente oltre il tetto; si ferma da sé quando
                    # il client chiude, così il test non dipende dal thread.
                    while self.byte_inviati < MAX_REPLY_BYTES * 4:
                        conn.sendall(blocco)
                        self.byte_inviati += len(blocco)
                elif self.risposta is not None:
                    conn.sendall(self.risposta)
            except OSError:
                pass

    def chiudi(self) -> None:
        self._srv.close()
        self._thread.join(5)


@pytest.fixture
def percorso_socket(tmp_path):
    return tmp_path / "clamd.ctl"


@pytest.mark.timeout(60)
def test_ping_normale_funziona(percorso_socket):
    srv = FintoClamd(percorso_socket, risposta=b"PONG\0")
    try:
        assert ClamdClient(unix_socket=str(percorso_socket)).ping() is True
    finally:
        srv.chiudi()


@pytest.mark.timeout(60)
def test_risposta_infinita_interrotta(percorso_socket):
    srv = FintoClamd(percorso_socket, inonda=True)
    try:
        with pytest.raises(ClamdError, match="oltre"):
            ClamdClient(unix_socket=str(percorso_socket)).version()
        # Il client ha smesso di leggere: il server non ha potuto inviare
        # molto più del tetto (resta il margine dei buffer del kernel).
        assert srv.byte_inviati < MAX_REPLY_BYTES * 4
    finally:
        srv.chiudi()


@pytest.mark.timeout(60)
def test_scansione_di_un_file_non_abbatte_tutto(percorso_socket, tmp_path):
    # Una risposta anomala riguarda il singolo file: deve diventare un
    # risultato ERROR, non un'eccezione che interrompe la scansione.
    srv = FintoClamd(percorso_socket, inonda=True)
    bersaglio = tmp_path / "file.txt"
    bersaglio.write_text("contenuto")
    try:
        client = ClamdClient(unix_socket=str(percorso_socket))
        risultato = client._instream_one(bersaglio)
        assert risultato.status == "ERROR"
        assert "oltre" in (risultato.signature or "")
    finally:
        srv.chiudi()


@pytest.mark.timeout(60)
def test_sessione_senza_terminatore_viene_chiusa(percorso_socket):
    srv = FintoClamd(percorso_socket, inonda=True)
    try:
        client = ClamdClient(unix_socket=str(percorso_socket))
        sessione = _ClamdSession(client)
        with pytest.raises(ClamdError, match="terminatore"):
            sessione._read_reply()
        assert sessione.dead is True, "la sessione non è più sincronizzata: va buttata"
    finally:
        srv.chiudi()
