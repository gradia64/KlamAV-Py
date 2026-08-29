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

import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

CHUNK_SIZE = 8192

# Soglia del pre-check dimensionale, allineata al default di
# StreamMaxLength in clamd.conf (25MB). Chi chiama può passare un valore
# diverso (se ha alzato StreamMaxLength nella propria configurazione) o
# None per disattivare il pre-check: in quel caso TOO_LARGE viene
# riconosciuto solo quando clamd risponde letteralmente
# "INSTREAM size limit exceeded".
DEFAULT_MAX_STREAM_SIZE = 25 * 1024 * 1024


class ClamdError(RuntimeError):
    """Errore di comunicazione con clamd o risposta inattesa."""


@dataclass
class ScanResult:
    path: str
    status: str  # "OK", "FOUND", "ERROR", "TOO_LARGE"
    signature: Optional[str] = None

    @property
    def infected(self) -> bool:
        return self.status == "FOUND"

    @property
    def too_large(self) -> bool:
        return self.status == "TOO_LARGE"


class ClamdClient:
    """
    Connessione a clamd via socket Unix (default) o TCP.

    Esempio:
        client = ClamdClient(unix_socket="/run/clamav/clamd.ctl")
        client.ping()
        for result in client.scan_stream(Path("/home/utente/scaricati")):
            if result.infected:
                print(result.path, result.signature)

    Nota sullo stato: la sessione IDSESSION persistente vive
    sull'istanza (self._session), non nel generatore — è ciò che
    rende reset_session() raggiungibile dall'esterno (il worker GUI
    la usa dopo le pause lunghe). Conseguenza: UNA scan_stream per
    client alla volta. Per scansioni concorrenti (es. GUI + CLI,
    worker manuale + worker Real-Time) istanziare client separati —
    è ciò che il progetto già fa.
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
        # Stato della sessione persistente: attributo dell'istanza (non
        # variabile locale del generatore) perché reset_session() debba
        # potere essere chiamato dall'esterno (ScanWorker, dopo pause
        # più lunghe dell'IdleTimeout di clamd).
        self._session: Optional[_ClamdSession] = None
        self._scanned_in_session = 0

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

    @staticmethod
    def _iter_files(path: Path, exclude_dirs: Optional[Iterable[Path]]) -> Iterator[Path]:
        """
        Traversata ricorsiva con PRUNING: le directory escluse non vengono
        nemmeno attraversate — a differenza di un filtro post-hoc su
        rglob, che le leggerebbe comunque (e con esse i file di quarantena,
        ri-rilevandoli a ogni scansione home-wide). Il vecchio
        `sorted(path.rglob("*"))` costruiva inoltre in memoria la lista
        COMPLETA dei path (su una home con 300k+ file: centinaia di
        migliaia di oggetti Path prima ancora di scansionare nulla):
        qui l'attraversamento è in streaming, memoria costante. L'ordine
        di attraversamento cambia (per-directory invece che globale
        lessicografico): nessun consumatore dipende dall'ordine.

        I percorsi in exclude_dirs devono essere assoluti ed espansi
        (niente '~' non espanso). I symlink-directory non vengono seguiti
        (followlinks=False), quindi un'esclusione non è aggirabile
        attraverso un symlink dentro l'albero scansionato.
        """
        exclude = [Path(e) for e in (exclude_dirs or [])]

        if path.is_file():
            yield path
            return

        # Il top stesso non deve stare dentro un'esclusione (es. per
        # errore si chiede la scansione della directory di quarantena:
        # non produrre risultati piuttosto che ri-rilevare i file già
        # gestiti).
        if any(path == e or path.is_relative_to(e) for e in exclude):
            return

        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames.sort()
            base = Path(dirpath)
            # Pruning: rimuovere una dir da dirnames (modifica in-place,
            # convenzione documentata di os.walk) impedisce a os.walk di
            # scendervi — la directory esclusa non viene nemmeno letta.
            if exclude:
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not any(
                        (base / d) == e or (base / d).is_relative_to(e)
                        for e in exclude
                    )
                ]
            for name in sorted(filenames):
                yield base / name

    def scan_stream(
        self,
        path: Path,
        persistent: bool = True,
        session_batch_size: int = 500,
        on_file_start: Optional[Callable[[Path], None]] = None,
        exclude_dirs: Optional[Iterable[Path]] = None,
        max_stream_size: Optional[int] = DEFAULT_MAX_STREAM_SIZE,
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

        exclude_dirs: directory da escludere dall'attraversamento ricorsivo
        (non vengono né attraversate né lette). È il filtro giusto per la
        directory di quarantena e i dati dell'applicazione: i file lì
        dentro sono già stati gestiti e ri-rilevarli a ogni scansione
        gonfia per sempre infetti/errori con "fantasmi".

        max_stream_size: pre-check dimensionale PRIMA dell'invio (default
        DEFAULT_MAX_STREAM_SIZE, allineato al default di StreamMaxLength
        in clamd.conf). Un file oltre soglia esce subito come TOO_LARGE
        ("non verificato") invece di venire interrotto a metà invio con
        una pipe interrotta e costare la ricreazione della sessione.
        None disattiva il pre-check.

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
        # Risoluzione della RADICE dell'attraversamento, una volta sola,
        # per TUTTI i punti di ingresso (CLI, scansione GUI manuale,
        # IPC/Dolphin, e futuri Real-Time/programmata): se il percorso da
        # scansionare è un symlink-directory, os.walk() lo segue comunque
        # quando è il punto di partenza (followlinks=False blocca solo i
        # symlink INTERNI all'albero, non la radice). Senza risolvere qui,
        # i risultati verrebbero riportati sotto il percorso del symlink
        # invece che sotto quello reale effettivamente letto. Per un
        # antivirus conta il contenuto reale controllato, non l'alias da
        # cui ci si è arrivati: risolvere rende il referto coerente con
        # ciò che è stato davvero scansionato e normalizza eventuali "..".
        #
        # Perché QUI e non in _iter_files: _iter_files produce il path di
        # OGNI file durante l'attraversamento (centinaia di migliaia su una
        # home) — risolvere lì significherebbe una syscall per file su un
        # percorso hot, e altererebbe la logica di esclusione della
        # quarantena (un file interno che è a sua volta un symlink verso la
        # quarantena verrebbe valutato sul target risolto). Risolvere solo
        # la radice ha costo trascurabile (una volta) e lascia intatto sia
        # lo streaming a memoria costante sia l'esclusione, che continua a
        # lavorare sui path non risolti dei file interni.
        #
        # È coerente con scan_file() (CONTSCAN), che già risolve, e con
        # scan_worker, che già risolve la radice della quarantena.
        path = Path(path).resolve()
        try:
            if not persistent:
                for target in self._iter_files(path, exclude_dirs):
                    if on_file_start:
                        on_file_start(target)
                    skip = self._size_limit_result(target, max_stream_size)
                    if skip is not None:
                        yield skip
                        continue
                    try:
                        yield self._instream_one(target, max_stream_size)
                    except OSError as exc:
                        yield self._stream_failure_result(target, exc, max_stream_size)
                return

            for target in self._iter_files(path, exclude_dirs):
                if on_file_start:
                    on_file_start(target)

                skip = self._size_limit_result(target, max_stream_size)
                if skip is not None:
                    yield skip
                    continue

                if self._session is None:
                    try:
                        self._session = _ClamdSession(self)
                    except OSError as exc:
                        yield ScanResult(
                            path=str(target),
                            status="ERROR",
                            signature=f"impossibile aprire sessione con clamd: {exc}",
                        )
                        continue
                    self._scanned_in_session = 0

                try:
                    result = self._session.scan_one(target)
                except (ClamdError, OSError) as exc:
                    # La sessione è da buttare in ogni caso: clamd può
                    # aver chiuso la connessione (rifiuto per size limit,
                    # timeout, riavvio del demone). Viene ricreata da
                    # zero al prossimo file; la classificazione del
                    # risultato distingue il caso "file oltre soglia"
                    # (TOO_LARGE, non è un malfunzionamento) dagli errori
                    # veri.
                    self.reset_session()
                    yield self._stream_failure_result(target, exc, max_stream_size)
                    continue

                yield result
                self._scanned_in_session += 1
                if self._session is not None and (
                    self._session.dead
                    or self._scanned_in_session >= session_batch_size
                ):
                    # dead=True: clamd ha chiuso la connessione al termine
                    # di QUESTO stream (rifiuto per size limit: dopo la
                    # risposta chiude la connessione). Ricreare la sessione
                    # PRIMA del file successivo evita che quel file muoia
                    # di EPIPE su una connessione già chiusa — il pattern
                    # delle "vittime collaterali" a cascata osservato sui
                    # file grandi.
                    self.reset_session()
        finally:
            # Eseguito anche se il consumatore abbandona il generatore
            # prima della fine (es. "Interrompi" nella GUI): senza questo,
            # la sessione IDSESSION resterebbe aperta fino al garbage
            # collection del generatore.
            self.reset_session()

    def reset_session(self) -> None:
        """
        Chiude l'eventuale sessione IDSESSION aperta e la segna per la
        ricreazione al prossimo file.

        Usato dal worker GUI dopo una pausa più lunga dell'IdleTimeout
        di clamd (~30s di default: il demone chiude le sessioni inattive,
        quindi alla ripresa il primo file su una sessione vecchia
        produrrebbe un errore finto). La sessione è stato dell'istanza
        proprio per rendere possibile questa chiamata dall'esterno.
        """
        session = self._session
        self._session = None
        self._scanned_in_session = 0
        if session is not None:
            session.close()

    @staticmethod
    def _size_limit_result(
        target: Path, max_stream_size: Optional[int]
    ) -> Optional[ScanResult]:
        """
        Pre-check dimensionale: None se il file va inviato (o se il
        pre-check è disattivato o non valutabile), ScanResult(TOO_LARGE)
        se è oltre soglia. Race nota e accettata: un file che cresce
        oltre soglia tra il pre-check e lo stream viene comunque gestito
        dal fallback _stream_failure_result.
        """
        if max_stream_size is None:
            return None
        try:
            size = target.stat().st_size
        except OSError:
            # File sparito/illegibile: nessun pre-check, il flusso
            # normale produrrà l'errore opportuno.
            return None
        if size <= max_stream_size:
            return None
        return ScanResult(
            path=str(target),
            status="TOO_LARGE",
            signature=(
                f"dimensione {size} byte supera StreamMaxLength "
                f"({max_stream_size} byte): file non inviato a clamd"
            ),
        )

    @staticmethod
    def _stream_failure_result(
        target: Path, exc: Exception, max_stream_size: Optional[int]
    ) -> ScanResult:
        """
        Classificazione di un fallimento di stream. Se il file è sopra la
        soglia, un'interruzione (pipe interrotta, connessione chiusa) è
        quasi certamente clamd che rifiuta per StreamMaxLength: TOO_LARGE
        ("non verificato"), non ERROR — è il fallback che copre la race
        del pre-check e i casi in cui la risposta di clamd non riesce a
        essere letta prima della chiusura della connessione.
        """
        if max_stream_size is not None:
            try:
                if target.stat().st_size >= max_stream_size:
                    return ScanResult(
                        path=str(target),
                        status="TOO_LARGE",
                        signature=(
                            f"clamd ha interrotto lo stream su un file "
                            f"oltre la soglia: {exc}"
                        ),
                    )
            except OSError:
                pass
        return ScanResult(
            path=str(target),
            status="ERROR",
            signature=f"sessione clamd interrotta: {exc}",
        )

    def _instream_one(self, target: Path, max_stream_size: Optional[int] = None) -> ScanResult:
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
            return self._stream_failure_result(target, exc, max_stream_size)

        if not raw:
            if max_stream_size is not None:
                try:
                    if target.stat().st_size >= max_stream_size:
                        return ScanResult(
                            path=str(target),
                            status="TOO_LARGE",
                            signature=(
                                "nessuna risposta da clamd: rifiuto "
                                "probabile per StreamMaxLength superato"
                            ),
                        )
                except OSError:
                    pass
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
            # clamd usa lo stesso suffisso "ERROR" sia per errori generici
            # (permessi, I/O) sia per il rifiuto esplicito di uno stream
            # troppo grande (StreamMaxLength in clamd.conf, tipicamente
            # 25MB di default). Sono concettualmente cose diverse per chi
            # legge il risultato — "errore" suggerisce un malfunzionamento,
            # un file "troppo grande" semplicemente non è stato verificato
            # — quindi li distinguiamo con uno status dedicato invece di
            # infilarli tutti nel bucket generico "errori".
            if "size limit exceeded" in raw.lower():
                return ScanResult(path=fallback_path, status="TOO_LARGE", signature=raw)
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

    Il flag `dead` segnala che clamd ha chiuso la connessione (tipico
    dopo un rifiuto per StreamMaxLength): scan_stream la vede e ricrea
    la sessione PRIMA del file successivo, invece di mandarlo in una
    connessione già chiusa (EPIPE a cascata).
    """

    def __init__(self, client: "ClamdClient") -> None:
        self._client = client
        self._sock = client._connect()
        self._buffer = b""
        self.dead = False
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
            try:
                self._sock.sendall(b"zINSTREAM\0")
                while chunk := fh.read(CHUNK_SIZE):
                    self._sock.sendall(struct.pack("!L", len(chunk)) + chunk)
                self._sock.sendall(struct.pack("!L", 0))  # chunk di lunghezza zero = fine stream
            except (BrokenPipeError, ConnectionResetError):
                # clamd ha chiuso la connessione mentre inviavamo —
                # tipicamente rifiuto per StreamMaxLength. La risposta
                # ("INSTREAM size limit exceeded. ERROR") può essere già
                # nel buffer: proviamo a leggerla PRIMA di dichiarare
                # morta la sessione. Se anche la lettura fallisce, la
                # sessione è davvero morta: l'eccezione sale a
                # scan_stream, che la ricrea e classifica il risultato
                # con la dimensione del file.
                pass

        raw = self._read_reply()
        rest = self._client._strip_session_id(raw)
        result = self._client._parse_result_line(rest, fallback_path=str(target))
        if "size limit exceeded" in rest.lower():
            # Dopo un rifiuto per size limit clamd chiude la connessione:
            # la sessione non è più riutilizzabile.
            self.dead = True
        return result

    def _read_reply(self) -> str:
        while b"\0" not in self._buffer:
            data = self._sock.recv(CHUNK_SIZE)
            if not data:
                self.dead = True
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
