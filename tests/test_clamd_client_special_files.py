"""
Test del filtro sulle entry non regolari (symlink pendenti, FIFO,
socket) in clamd_client.

Contesto: os.walk() classifica come "file" tutto ciò che non è una
directory, quindi symlink pendenti, FIFO e socket finivano nell'elenco
dei file da inviare a clamd. Due conseguenze:

  1. i symlink pendenti producevano centinaia di finti ERRORE
     "[Errno 2] File o directory non esistente" su alberi con
     node_modules o store pnpm ripuliti;
  2. open() su una FIFO senza scrittore BLOCCA indefinitamente nel
     kernel — nessuna eccezione, nessun timeout: una sola FIFO
     nell'albero piantava l'intera scansione.

Il punto (2) è il motivo per cui questi test hanno un timeout esplicito:
una regressione si manifesterebbe come blocco, non come fallimento, e
senza timeout la suite resterebbe appesa invece di segnalare l'errore.
"""

import os
import socket as socket_module
import stat
from pathlib import Path

import pytest

from klamav_py.clamd_client import ClamdClient


def _client() -> ClamdClient:
    # Socket inesistente: i test coprono il filtro, che agisce PRIMA di
    # qualsiasi contatto con clamd. Se il filtro non funzionasse, il
    # test fallirebbe comunque (per errore di connessione), il che è
    # esattamente il segnale che vogliamo.
    return ClamdClient(unix_socket="/nonexistent/clamd.ctl")


def test_symlink_pendente_saltato_non_errore(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "dangling").symlink_to(tmp_path / "target-inesistente")

    client = _client()
    results = list(client.scan_stream(root))

    assert results == [], "un symlink pendente non deve produrre risultati"
    assert client.skipped["collegamenti simbolici"] == 1


def test_symlink_valido_saltato_target_scansionato_a_parte(tmp_path: Path) -> None:
    """
    Il symlink non viene inviato, ma il suo target — se è dentro
    l'albero — viene comunque scansionato come file reale: nessuna
    perdita di copertura, solo niente doppioni.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "reale.txt").write_bytes(b"x" * 10)
    (root / "alias.txt").symlink_to(root / "reale.txt")

    client = _client()
    # max_stream_size piccolissimo: il file reale esce come TOO_LARGE
    # dal pre-check, senza mai toccare clamd. Ci interessa solo QUALI
    # entry arrivano al pre-check.
    results = list(client.scan_stream(root, max_stream_size=1))

    paths = {Path(r.path).name for r in results}
    assert paths == {"reale.txt"}
    assert client.skipped["collegamenti simbolici"] == 1


@pytest.mark.timeout(15)
def test_fifo_non_blocca_la_scansione(tmp_path: Path) -> None:
    """
    Regressione critica: senza il filtro, open() sulla FIFO blocca per
    sempre. Se questo test va in timeout invece di fallire, la
    protezione è saltata.
    """
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "tubo")
    (root / "normale.txt").write_bytes(b"y" * 10)

    client = _client()
    results = list(client.scan_stream(root, max_stream_size=1))

    paths = {Path(r.path).name for r in results}
    assert paths == {"normale.txt"}, "la FIFO non deve essere inviata a clamd"
    assert client.skipped["file non regolari (socket, FIFO, device)"] == 1


@pytest.mark.timeout(15)
def test_socket_unix_saltato(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        sock.bind(str(root / "s.sock"))
        client = _client()
        results = list(client.scan_stream(root))
        assert results == []
        assert client.skipped["file non regolari (socket, FIFO, device)"] == 1
    finally:
        sock.close()


def test_open_regular_rifiuta_symlink(tmp_path: Path) -> None:
    """
    Seconda barriera: anche se il filtro su lstat venisse aggirato da
    una race (path sostituito tra il controllo e l'apertura),
    _open_regular fallisce con O_NOFOLLOW invece di aprire il target.
    """
    reale = tmp_path / "reale.txt"
    reale.write_bytes(b"z" * 10)
    link = tmp_path / "link.txt"
    link.symlink_to(reale)

    with pytest.raises(OSError):
        ClamdClient._open_regular(link)


@pytest.mark.timeout(15)
def test_open_regular_non_blocca_su_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "tubo"
    os.mkfifo(fifo)

    with pytest.raises(OSError):
        ClamdClient._open_regular(fifo)


def test_open_regular_apre_file_normale_in_modo_bloccante(tmp_path: Path) -> None:
    """
    O_NONBLOCK serve solo a non farsi bloccare dall'apertura di una
    FIFO: sul file regolare il descrittore deve tornare bloccante,
    altrimenti una lettura futura potrebbe restituire dati parziali.
    """
    f = tmp_path / "normale.txt"
    f.write_bytes(b"contenuto")

    with ClamdClient._open_regular(f) as fh:
        assert os.get_blocking(fh.fileno()) is True
        assert fh.read() == b"contenuto"


def test_file_regolare_non_conteggiato_come_saltato(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a" * 10)

    client = _client()
    list(client.scan_stream(root, max_stream_size=1))

    assert sum(client.skipped.values()) == 0
