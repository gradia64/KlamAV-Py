"""
Client minimale per il demone clamd (ClamAV), protocollo nativo su socket
Unix o TCP. Nessuna dipendenza esterna: usa direttamente il protocollo
INSTREAM/CONTSCAN/PING di clamd invece di invocare i binari clamscan/
clamdscan tramite shell (è esattamente il problema di sicurezza che
aveva klamav 0.22 in scanviewer.cpp: qui non c'è nessuna shell di mezzo,
i path non vengono mai interpolati in una stringa di comando).

Riferimento protocollo: clamd(8), sezione "COMMANDS".
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

CHUNK_SIZE = 8192


class ClamdError(RuntimeError):
    """Errore di comunicazione con clamd o risposta inattesa."""


@dataclass
class ScanResult:
    path: str
    status: str  # "OK", "FOUND", "ERROR"
    signature: Optional[str] = None

    @property
    def infected(self) -> bool:
        return self.status == "FOUND"


class ClamdClient:
    """
    Connessione a clamd via socket Unix (default) o TCP.

    Esempio:
        client = ClamdClient(unix_socket="/run/clamav/clamd.ctl")
        client.ping()
        for result in client.scan_stream(Path("/home/utente/scaricati")):
            if result.infected:
                print(result.path, result.signature)
    """

    def __init__(
        self,
        unix_socket: Optional[str] = "/run/clamav/clamd.ctl",
        tcp_host: Optional[str] = None,
        tcp_port: int = 3310,
        timeout: float = 30.0,
    ) -> None:
        if not unix_socket and not tcp_host:
            raise ValueError("Serve unix_socket oppure tcp_host")
        self.unix_socket = unix_socket
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        if self.unix_socket:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.unix_socket)
        else:
            sock = socket.create_connection(
                (self.tcp_host, self.tcp_port), timeout=self.timeout
            )
        return sock

    def _send_simple_command(self, command: str) -> str:
        with self._connect() as sock:
            sock.sendall(f"z{command}\0".encode("utf-8"))
            return self._read_all(sock)

    @staticmethod
    def _read_all(sock: socket.socket) -> str:
        chunks = []
        while True:
            data = sock.recv(CHUNK_SIZE)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace").strip("\0\n ")

    def ping(self) -> bool:
        return self._send_simple_command("PING") == "PONG"

    def version(self) -> str:
        return self._send_simple_command("VERSION")

    def reload(self) -> str:
        return self._send_simple_command("RELOAD")

    def scan_file(self, path: Path) -> ScanResult:
        """
        Scansiona un singolo file già presente sul filesystem raggiungibile
        da clamd, usando CONTSCAN (path assoluto passato come argomento a
        clamd, non a una shell).
        """
        path = Path(path).resolve()
        with self._connect() as sock:
            sock.sendall(f"zCONTSCAN {path}\0".encode("utf-8"))
            raw = self._read_all(sock)
        return self._parse_result_line(raw, fallback_path=str(path))

    def scan_stream(
        self,
        path: Path,
        persistent: bool = True,
        session_batch_size: int = 500,
        on_file_start: Optional[Callable[[Path], None]] = None,
    ) -> Iterator[ScanResult]:
        """
        Invia il contenuto di un file (o ricorsivamente di una directory)
        a clamd via INSTREAM: i byte viaggiano sul socket, non serve che
        clamd possa leggere il path (utile se gira in un container/chroot
        diverso da chi lancia lo scan).

        on_file_start, se passato, viene chiamato con il Path del file
        appena prima di iniziare a leggerlo/inviarlo — utile per una UI
        che vuole mostrare "sto scansionando X" invece di scoprirlo solo
        a risultato ottenuto (specie su file grossi che richiedono un
        po' per essere letti e inviati).

        persistent=True (default): riusa una singola connessione tramite
        il protocollo IDSESSION di clamd invece di aprirne una nuova per
        ogni file — su alberi con decine di migliaia di file evita
        l'overhead di connect/accept ripetuto per ciascuno. La sessione
        viene comunque richiusa e riaperta ogni `session_batch_size` file
        (limita l'impatto di eventuali limiti non documentati su sessioni
        molto lunghe) e viene ricreata da zero ogni volta che qualcosa va
        storto, cosicché un file problematico non comprometta il resto
        della scansione — esattamente come nel percorso non persistente.

        Un file singolo che va in errore (socket chiuso da clamd a metà
        invio, permessi negati in lettura, file sparito nel frattempo...)
        non deve interrompere la scansione degli altri: viene riportato
        come ScanResult(status="ERROR") e si continua con il prossimo.
        """
        path = Path(path)
        targets = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())

        if not persistent:
            for target in targets:
                if on_file_start:
                    on_file_start(target)
                try:
                    yield self._instream_one(target)
                except OSError as exc:
                    yield ScanResult(path=str(target), status="ERROR", signature=str(exc))
            return

        session: Optional[_ClamdSession] = None
        scanned_in_session = 0
        for target in targets:
            if on_file_start:
                on_file_start(target)

            if session is None:
                try:
                    session = _ClamdSession(self)
                except OSError as exc:
                    yield ScanResult(
                        path=str(target),
                        status="ERROR",
                        signature=f"impossibile aprire sessione con clamd: {exc}",
                    )
                    continue
                scanned_in_session = 0

            try:
                result = session.scan_one(target)
            except (ClamdError, OSError) as exc:
                session.close()
                session = None
                yield ScanResult(
                    path=str(target),
                    status="ERROR",
                    signature=f"sessione clamd interrotta: {exc}",
                )
                continue

            yield result
            scanned_in_session += 1
            if scanned_in_session >= session_batch_size:
                session.close()
                session = None

        if session is not None:
            session.close()

    def _instream_one(self, target: Path) -> ScanResult:
        try:
            with self._connect() as sock:
                sock.sendall(b"zINSTREAM\0")
                try:
                    with open(target, "rb") as fh:
                        while chunk := fh.read(CHUNK_SIZE):
                            sock.sendall(struct.pack("!L", len(chunk)) + chunk)
                    sock.sendall(struct.pack("!L", 0))  # chunk di lunghezza zero = fine stream
                except (BrokenPipeError, ConnectionResetError):
                    # clamd ha chiuso la connessione mentre stavamo ancora
                    # scrivendo — tipicamente perché il file supera
                    # StreamMaxLength (clamd.conf) e ha già rifiutato lo
                    # stream. Proviamo comunque a leggere: la risposta di
                    # errore potrebbe essere arrivata prima della chiusura.
                    pass
                raw = self._read_all(sock)
        except (BrokenPipeError, ConnectionResetError) as exc:
            return ScanResult(
                path=str(target),
                status="ERROR",
                signature=f"connessione con clamd interrotta: {exc}",
            )

        if not raw:
            return ScanResult(
                path=str(target),
                status="ERROR",
                signature="nessuna risposta da clamd (probabile limite dimensione/StreamMaxLength superato)",
            )
        return self._parse_result_line(raw, fallback_path=str(target))

    @staticmethod
    def _parse_result_line(raw: str, fallback_path: str) -> ScanResult:
        # Formati tipici di risposta:
        #   "/percorso: OK"
        #   "/percorso: Eicar-Test-Signature FOUND"
        #   "/percorso: <messaggio errore> ERROR"
        #   "stream: OK" / "stream: <firma> FOUND"  (con INSTREAM: clamd non
        #   conosce il path reale, letteralmente risponde con la parola
        #   "stream" — qui usiamo sempre fallback_path in quel caso, dato
        #   che noi il path reale lo conosciamo sempre)
        if raw.endswith("OK"):
            path = raw[: -len(" OK")].rstrip(": ")
            return ScanResult(path=path if path and path != "stream" else fallback_path, status="OK")
        if raw.endswith("FOUND"):
            body = raw[: -len(" FOUND")]
            path, _, signature = body.rpartition(": ")
            return ScanResult(
                path=path if path and path != "stream" else fallback_path,
                status="FOUND",
                signature=signature.strip(),
            )
        if raw.endswith("ERROR"):
            return ScanResult(path=fallback_path, status="ERROR", signature=raw)
        raise ClamdError(f"Risposta clamd non riconosciuta: {raw!r}")

    @staticmethod
    def _strip_session_id(raw: str) -> str:
        """
        Le risposte dentro una sessione IDSESSION sono prefissate con
        '<id>: ', es. '3: stream: OK'. Qui togliamo solo il prefisso
        numerico, il resto va comunque a _parse_result_line.
        """
        prefix, sep, rest = raw.partition(": ")
        if sep and prefix.isdigit():
            return rest
        return raw


class _ClamdSession:
    """
    Wrapper attorno a una connessione clamd aperta con IDSESSION: più
    comandi INSTREAM in sequenza sullo stesso socket invece di uno per
    connessione. Non pipeline (aspetta ogni risposta prima di mandare il
    comando successivo), quindi l'ID di risposta coincide sempre con
    quello atteso — niente bisogno di gestire riordinamenti.
    """

    def __init__(self, client: "ClamdClient") -> None:
        self._client = client
        self._sock = client._connect()
        self._buffer = b""
        try:
            self._sock.sendall(b"zIDSESSION\0")
        except OSError:
            self._sock.close()
            raise

    def scan_one(self, target: Path) -> ScanResult:
        try:
            fh = open(target, "rb")
        except OSError as exc:
            # Non abbiamo mandato nessun comando a clamd: la sessione resta
            # valida, riportiamo solo l'errore di lettura locale.
            return ScanResult(
                path=str(target),
                status="ERROR",
                signature=f"impossibile leggere il file: {exc}",
            )

        with fh:
            self._sock.sendall(b"zINSTREAM\0")
            while chunk := fh.read(CHUNK_SIZE):
                self._sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            self._sock.sendall(struct.pack("!L", 0))  # chunk di lunghezza zero = fine stream

        raw = self._read_reply()
        rest = self._client._strip_session_id(raw)
        return self._client._parse_result_line(rest, fallback_path=str(target))

    def _read_reply(self) -> str:
        while b"\0" not in self._buffer:
            data = self._sock.recv(CHUNK_SIZE)
            if not data:
                raise ClamdError("clamd ha chiuso la connessione durante la sessione IDSESSION")
            self._buffer += data
        reply, _, self._buffer = self._buffer.partition(b"\0")
        return reply.decode("utf-8", errors="replace").strip("\n ")

    def close(self) -> None:
        try:
            self._sock.sendall(b"zEND\0")
        except OSError:
            pass
        finally:
            self._sock.close()
