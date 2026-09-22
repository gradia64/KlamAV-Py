"""
Creazione di file e directory che contengono dati privati dell'utente:
cronologia delle scansioni, log delle scansioni programmate, log degli
errori della CLI. Tutti contengono percorsi dei file scansionati, nomi
delle firme e file infetti.

Storia (audit 0.1.8): questi file erano creati con i permessi di
default, filtrati dallo umask. Con il tipico umask 022 le directory
nascevano 0755 e i file 0644. Su un sistema con home 0755 (default
storico, ancora frequente su installazioni aggiornate e server
multi-utente) un altro utente locale poteva leggerli. Il log di --log-errors
era peggio: il README suggeriva /tmp come destinazione, e in una
directory condivisa un altro utente può creare il file per primo con
permessi 0666 e leggere tutto ciò che la vittima ci scrive (bloccato dal
kernel solo se fs.protected_regular è attivo).

Due primitive, usate da GUI e CLI (per questo il modulo sta alla radice
del pacchetto e non importa nulla di Qt):

- ensure_private_dir(): directory 0700, riportata a 0700 se esiste con
  permessi più larghi. Applicata a ~/.local/share/klamav-py basta da
  sola a proteggere tutto quello che ci sta sotto, indipendentemente dai
  permessi dei singoli file e della home.
- write_private_text() / open_private_for_write(): file 0600 creati
  senza seguire symlink e verificando di esserne i proprietari.
"""

from __future__ import annotations

import io
import os
import stat
import uuid
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class PrivateFileError(OSError):
    pass


def ensure_private_dir(path: Path) -> Path:
    """
    Crea path (e i genitori mancanti) e la rende 0700.

    Solo l'ultimo componente viene forzato a 0700: i genitori (tipicamente
    ~/.local/share) appartengono all'ambiente dell'utente e non sono
    affar nostro. Una directory esistente con permessi più larghi viene
    ristretta, purché sia nostra e non sia un symlink: la chmod passa da
    un descrittore aperto con O_NOFOLLOW, non dal percorso.
    """
    path = Path(path)
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)

    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise PrivateFileError(
            f"{path} non è una directory utilizzabile (symlink o altro tipo): {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise PrivateFileError(f"{path} non appartiene all'utente corrente")
        if stat.S_IMODE(st.st_mode) != PRIVATE_DIR_MODE:
            os.fchmod(fd, PRIVATE_DIR_MODE)
    finally:
        os.close(fd)
    return path


def _open_owned(path: Path, flags: int) -> int:
    """os.open con 0600, senza seguire symlink, verificando il proprietario."""
    try:
        # O_NONBLOCK: se al posto del file c'è una FIFO (pre-creata da un
        # altro utente in una directory condivisa), O_WRONLY senza questo
        # flag bloccherebbe finché qualcuno non la apre in lettura. Con il
        # flag l'apertura fallisce subito (ENXIO) o, se c'è un lettore,
        # riesce e viene rifiutata dal controllo S_ISREG qui sotto. Sui
        # file regolari non ha alcun effetto.
        fd = os.open(
            path,
            flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            PRIVATE_FILE_MODE,
        )
    except OSError as exc:
        raise PrivateFileError(f"impossibile creare {path} in modo sicuro: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PrivateFileError(f"{path} non è un file regolare")
        if st.st_uid != os.getuid():
            # Il file esisteva già ed è di qualcun altro (es. pre-creato in
            # /tmp): scriverci dentro gli consegnerebbe i dati.
            raise PrivateFileError(f"{path} esiste già e appartiene a un altro utente")
        if stat.S_IMODE(st.st_mode) != PRIVATE_FILE_MODE:
            os.fchmod(fd, PRIVATE_FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def ensure_private_file(path: Path, create: bool = True) -> bool:
    """
    Porta a 0600 un file scritto da CODICE NON NOSTRO, senza toccarne il
    contenuto. Ritorna True se il file esiste (o è stato creato).

    Serve per ~/.config/KlamAV-Py/KlamAV-Py.conf, scritto da QSettings e
    quindi fuori dalla portata di write_private_text(): Qt lo crea con
    0666 & ~umask (0644 con umask 022) e contiene le cartelle monitorate
    dal Real-Time, il target delle scansioni pianificate e il percorso di
    quarantena — gli stessi dati che altrove teniamo a 0600. Verificato
    con PySide6 6.11.2: QSettings usa QSaveFile, che PRESERVA i permessi
    di un file esistente, quindi basta stringerli una volta all'avvio e
    restano tali a ogni sync() successivo.

    create=False per il file legacy (~/.config/KlamAV/KlamAV.conf): va
    ristretto se c'è, ma crearlo vuoto genererebbe una directory legacy
    su ogni installazione nuova che non ne ha mai avuta una.
    """
    flags = os.O_WRONLY | (os.O_CREAT if create else 0)
    try:
        # Niente O_TRUNC: il file ci interessa solo per i permessi.
        fd = _open_owned(Path(path), flags)
    except PrivateFileError as exc:
        if not create and isinstance(exc.__cause__, FileNotFoundError):
            return False
        raise
    os.close(fd)
    return True


def write_private_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """
    Scrittura ATOMICA di un file privato: file temporaneo 0600 nella
    stessa directory, fsync, os.replace.

    Oltre ai permessi, chiude un problema di affidabilità della vecchia
    open(path, "w"): il file veniva troncato PRIMA di essere riscritto, e
    un crash in quel momento lasciava la cronologia vuota o a metà. Con
    os.replace chi legge vede sempre il contenuto vecchio o quello nuovo
    per intero. Il file finale eredita i permessi 0600 del temporaneo,
    quindi anche un file preesistente 0644 viene sostituito da uno 0600.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = _open_owned(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_private_for_write(path: Path, encoding: str = "utf-8") -> io.TextIOWrapper:
    """
    Apre in scrittura (troncando) un file privato che viene riempito
    man mano, come il log degli errori della CLI durante la scansione.

    Rifiuta i symlink e i file preesistenti di un altro utente; un file
    nostro preesistente con permessi larghi viene riportato a 0600 prima
    di scriverci.
    """
    fd = _open_owned(Path(path), os.O_WRONLY | os.O_CREAT)
    try:
        # O_TRUNC solo DOPO le verifiche: troncare un file di altri,
        # anche senza scriverci, sarebbe già un effetto collaterale.
        os.ftruncate(fd, 0)
        return os.fdopen(fd, "w", encoding=encoding)
    except BaseException:
        os.close(fd)
        raise
