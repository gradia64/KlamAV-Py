"""
Finto clamd su socket reali per i test (TCP IPv4/IPv6 o Unix): PING,
VERSION, INSTREAM e sessioni IDSESSION, una connessione alla volta.

Modulo di supporto, non di test (niente prefisso test_): lo caricano
test_clamd_endpoint.py e test_cli_tcp.py con load_fake_clamd(), per
percorso, così funziona qualunque sia la modalità di import di pytest.
"""

from __future__ import annotations

import socket
import struct
import threading

# Marcatore riconosciuto dal finto clamd. Di proposito NON la stringa EICAR
# vera: su una macchina con il Real-Time attivo il file di prova verrebbe
# messo in quarantena dall'antivirus stesso mentre il test gira.
EICAR_MARK = b"KLAMAV-PY-FINTO-VIRUS-DI-PROVA"


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        data = conn.recv(n - len(buf))
        if not data:
            raise ConnectionError("chiuso")
        buf += data
    return buf


def _recv_command(conn):
    buf = b""
    while not buf.endswith(b"\0"):
        data = conn.recv(1)
        if not data:
            return None
        buf += data
    # Prefisso "z": comando terminato da NUL, come lo manda ClamdClient.
    cmd = buf[:-1].decode()
    assert cmd.startswith("z"), cmd
    return cmd[1:]


def _recv_stream(conn):
    payload = b""
    while True:
        (size,) = struct.unpack("!L", _recv_exact(conn, 4))
        if size == 0:
            return payload
        payload += _recv_exact(conn, size)


def _verdict(payload):
    return "stream: Eicar-Test-Signature FOUND" if EICAR_MARK in payload else "stream: OK"


class FakeClamd:
    """
    PING, VERSION, INSTREAM e sessioni IDSESSION, una connessione alla volta.

    die_after=N simula clamd che si spegne a scansione iniziata: dopo il
    verdetto dell'N-esimo stream chiude la connessione e smette di
    ascoltare, quindi ogni nuova connessione viene rifiutata.
    """

    def __init__(self, family, address, die_after=None):
        self.die_after = die_after
        self.streams = 0
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.bind(address)
        self.sock.listen()
        self.address = self.sock.getsockname()
        self.commands: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn):
        cmd = _recv_command(conn)
        self.commands.append(cmd)
        if cmd == "PING":
            conn.sendall(b"PONG\0")
        elif cmd == "VERSION":
            conn.sendall(b"ClamAV 1.4.1/27400/Fri Sep 25 10:00:00 2026\0")
        elif cmd == "INSTREAM":
            conn.sendall(_verdict(_recv_stream(conn)).encode() + b"\0")
            self._count_stream()
        elif cmd == "IDSESSION":
            n = 0
            while (sub := _recv_command(conn)) not in (None, "END"):
                self.commands.append(sub)
                n += 1
                conn.sendall(f"{n}: {_verdict(_recv_stream(conn))}".encode() + b"\0")
                if self._count_stream():
                    return

    def _count_stream(self):
        self.streams += 1
        if self.die_after is not None and self.streams >= self.die_after:
            self.close()  # niente più accept: le connessioni nuove sono rifiutate
            return True
        return False

    def close(self):
        # shutdown prima di close: sblocca l'accept() pendente nel thread.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def closed_tcp_port(host: str = "127.0.0.1") -> int:
    """Una porta su cui nessuno ascolta (connessione rifiutata)."""
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port
