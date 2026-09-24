"""
Percorso e lato client del socket IPC single-instance.

Storia (issue di sicurezza sul socket IPC): il socket usava il nome non
assoluto "klamav_py_ipc", che Qt risolve in QDir::tempPath(), cioè
/tmp/klamav_py_ipc, in una directory condivisa da tutti gli utenti. Un
altro utente locale poteva occupare quel nome per primo e:

- ricevere i percorsi dei file che la vittima mandava in scansione
  (il client si fidava di chiunque rispondesse);
- impedire del tutto l'avvio della GUI della vittima (il client
  interpretava la connessione riuscita come "altra istanza attiva" e
  usciva con 0: niente real-time, niente scansioni programmate);
- in alternativa, lasciando un file socket morto, far fallire in
  silenzio listen() e disattivare il single-instance.

UserAccessOption sul server non bastava: protegge chi si può connettere
al NOSTRO socket, non impedisce di connettersi al socket di qualcun altro.

Due difese indipendenti:

1. Il socket vive nella runtime directory dell'utente
   (QStandardPaths.RuntimeLocation: $XDG_RUNTIME_DIR, tipicamente
   /run/user/<uid>). Qt la accetta solo se è una directory vera (non un
   symlink), di proprietà dell'utente e con permessi 0700, altrimenti
   restituisce una stringa vuota: comportamento verificato con due utenti
   reali, directory pre-creata da un altro utente, symlink e
   XDG_RUNTIME_DIR puntata a una directory altrui. Nessun altro utente
   (root escluso) può creare o occupare file lì dentro.

2. Il client verifica con SO_PEERCRED che il processo in ascolto sia
   dello stesso utente PRIMA di scrivere qualunque cosa. Con la difesa 1
   in piedi non dovrebbe mai scattare: è difesa in profondità.

Il lato server resta in app.py/main_window.py (QLocalServer).
"""

from __future__ import annotations

import os
import socket
import struct
import sys

from PySide6.QtCore import QStandardPaths

IPC_SOCKET_NAME = "klamav-py-ipc"

# Payload IPC: uno o più percorsi UTF-8 separati da NUL, l'unico byte che
# non può comparire in un percorso Linux. Un payload senza NUL è il
# formato precedente (un solo percorso): resta valido. 256 KiB bastano per
# qualche migliaio di file selezionati in Dolphin con %F; oltre, i
# percorsi in eccesso non vengono inviati e l'utente lo viene a sapere.
IPC_SEPARATOR = b"\0"
IPC_MAX_PAYLOAD_BYTES = 256 * 1024


def encode_targets(paths, max_bytes: int = IPC_MAX_PAYLOAD_BYTES) -> tuple[bytes, int]:
    """
    Codifica i percorsi per l'IPC. Ritorna (payload, esclusi): il payload
    resta SEMPRE strettamente sotto max_bytes e contiene solo percorsi
    completi, così il server può trattare "letti max_bytes" come
    sovraccarico senza rischiare di interpretare un percorso troncato.
    """
    paths = list(paths)
    parts: list[bytes] = []
    size = 0
    for p in paths:
        enc = os.fsencode(str(p))
        if not enc or IPC_SEPARATOR in enc:
            continue
        extra = len(enc) + (1 if parts else 0)
        if size + extra >= max_bytes:
            break
        parts.append(enc)
        size += extra
    return IPC_SEPARATOR.join(parts), len(paths) - len(parts)

# Timeout del client: la connessione è locale, un'istanza viva risponde
# in pochi millisecondi. Lo stesso ordine di grandezza del vecchio
# waitForConnected(500).
CLIENT_TIMEOUT_SECONDS = 0.5


def ipc_socket_path() -> str | None:
    """
    Percorso assoluto del socket IPC, oppure None se non esiste una
    runtime directory sicura (Qt l'ha rifiutata: vedi docstring del
    modulo). None significa "niente single-instance": il chiamante deve
    proseguire senza IPC e avvisare l'utente, non ripiegare su /tmp.
    """
    base = QStandardPaths.writableLocation(QStandardPaths.RuntimeLocation)
    if not base:
        return None
    return os.path.join(base, IPC_SOCKET_NAME)


def _peer_uid(sock: socket.socket) -> int | None:
    """uid del processo all'altro capo del socket, o None se non determinabile."""
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    fmt = "3i"  # struct ucred: pid, uid, gid
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize(fmt))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack(fmt, raw)
    return uid


def notify_running_instance(sock_path: str, payload: bytes | None) -> bool:
    """
    Cerca un'istanza già attiva dello STESSO utente e, se la trova, le
    passa payload (il target di scansione, se c'è).

    True  = istanza valida trovata e notificata: il chiamante può uscire.
    False = nessuna istanza valida: il chiamante deve diventare la prima
            istanza. Include il caso di socket morto (istanza precedente
            crashata) e quello di peer di un altro utente, per il quale
            NON viene scritto nulla.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CLIENT_TIMEOUT_SECONDS)
    try:
        try:
            sock.connect(sock_path)
        except OSError:
            # FileNotFoundError, ConnectionRefusedError (socket morto),
            # timeout: in tutti i casi nessuna istanza raggiungibile.
            return False

        peer = _peer_uid(sock)
        if peer != os.getuid():
            print(
                f"KlamAV-Py: il socket IPC {sock_path} appartiene a un altro "
                f"utente (uid {peer}); ignorato, nessun dato inviato.",
                file=sys.stderr,
            )
            return False

        if payload:
            try:
                sock.sendall(payload)
            except OSError:
                return False
        return True
    finally:
        sock.close()
