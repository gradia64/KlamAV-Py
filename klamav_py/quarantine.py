"""
Gestione quarantena, separata dalla UI (a differenza di kuarantine.cpp
in klamav 0.22, che mescolava logica di spostamento file, dialog Qt e
lettura/scrittura KConfig nella stessa classe).

I metadata sono in un JSON accanto ai file quarantenati: niente database
esterno da mantenere per un caso d'uso così semplice.

File di servizio nella directory (tutti 0600, directory 0700):
  index.json                  l'indice
  index.json.lock             lock flock, mai troncato né riscritto
  .index.json.<uuid>.tmp      temporaneo della scrittura atomica
  index.json.corrupt-<...>    indice illeggibile messo da parte (mai
                              cancellato: serve a recuperare a mano)
  .<nome>.intent              intento di una quarantena in corso (vedi
                              _write_intent e recover_interrupted)
  .<nome>.copy                la stessa quarantena procede per copia
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import List, Optional, Tuple

from .private_files import ensure_private_dir, open_private_fd, write_private_text

# Un indice legittimo occupa qualche centinaio di byte per voce: 16 MiB
# sono decine di migliaia di file in quarantena. Oltre, il file non è un
# indice plausibile e non va caricato in memoria per intero.
MAX_INDEX_BYTES = 16 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
INTENT_SUFFIX = ".intent"
# Accanto all'intento, creato prima di iniziare una copia fra filesystem:
# dice al recupero quale delle due strade era stata presa. Il dispositivo
# non basta: un rename fra due mount dello stesso filesystem (bind mount)
# dà EXDEV con lo stesso st_dev.
COPY_MARK_SUFFIX = ".copy"
# Una quarantena per copia (EXDEV) crea la copia 0600 e la porta a 0400
# solo dopo fsync: 0400 è il segno che la copia è completa.
_COMPLETE_COPY_MODE = 0o400


@dataclass
class QuarantineEntry:
    original_path: str
    quarantined_path: str
    signature: Optional[str]
    timestamp: float
    # Permessi POSIX del file al momento della quarantena (es. 0o755),
    # da ripristinare al momento del restore. Optional con default None
    # per compatibilità con index.json scritti da versioni precedenti,
    # che non avevano questo campo.
    original_mode: Optional[int] = None


_ENTRY_FIELDS = {f.name for f in fields(QuarantineEntry)}
_REQUIRED_FIELDS = {"original_path", "quarantined_path", "timestamp"}


class QuarantineError(RuntimeError):
    pass


class _CorruptIndex(Exception):
    pass


class Quarantine:
    """
    Directory di quarantena con permessi 0700, un JSON di indice
    (`index.json`) e i file rinominati con un identificatore non
    prevedibile (timestamp + UUID) per evitare sia collisioni tra file
    omonimi provenienti da directory diverse (uno dei problemi elencati
    nel TODO originale di klamav: "allow multiple instances of same
    filename in quarantine"), sia la conservazione su disco del nome
    file originale in chiaro.
    """

    def __init__(self, quarantine_dir: Path):
        self.dir = Path(quarantine_dir)
        # ensure_private_dir invece di mkdir + chmod: la chmod passa da un
        # descrittore aperto con O_NOFOLLOW e verifica il proprietario.
        # Con Path.chmod(), che segue i symlink, una "directory di
        # quarantena" sostituita da un link avrebbe reso 0700 il bersaglio
        # e poi ci avremmo scritto dentro i file infetti.
        ensure_private_dir(self.dir)
        self.index_path = self.dir / "index.json"
        self.lock_path = self.index_path.with_name(self.index_path.name + ".lock")
        # Ultimo recupero da indice corrotto eseguito da QUESTA istanza:
        # (file messo da parte, motivo). La GUI non deve dipendere da
        # questo attributo (il recupero può averlo fatto un'altra istanza,
        # es. il worker Real-Time): usa corrupt_backups().
        self.last_recovery: Optional[Tuple[Path, str]] = None
        # Operazioni riprese da recover_interrupted() in questa istanza:
        # (file in quarantena, esito) per la diagnostica.
        self.recovered: List[Tuple[Path, str]] = []
        with self._index_lock():
            if not self.index_path.exists():
                self._write_index([])
            self._recover_interrupted_locked()

    # -- lock e indice ---------------------------------------------------

    @contextmanager
    def _index_lock(self):
        """Lock esclusivo (fcntl.flock) sulle sequenze read-modify-write
        di index.json.

        GUI, CLI e worker del timer utente systemd possono modificare la
        quarantena contemporaneamente. La scrittura atomica protegge i
        LETTORI, non il read-modify-write: due processi che leggono lo
        stesso indice e riscrivono ognuno con la propria voce perdono
        quella dell'altro. fcntl è POSIX: questo progetto è Linux-only per
        costruzione (clamd, systemd, pkexec).

        Il lock file è creato 0600 senza seguire symlink e senza
        troncamento (open(..., "w") lo troncava a ogni apertura e ne
        lasciava i permessi all'umask: 0664 con l'umask 0002 di Debian).
        """
        fd = open_private_fd(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _parse_index(self) -> List[QuarantineEntry]:
        """Legge e valida l'indice. FileNotFoundError se manca,
        _CorruptIndex per qualunque contenuto non plausibile."""
        try:
            fd = os.open(self.index_path,
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _CorruptIndex(f"indice non apribile: {exc}") from exc
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise _CorruptIndex("l'indice non è un file regolare")
            if st.st_size > MAX_INDEX_BYTES:
                raise _CorruptIndex(f"indice di dimensione anomala ({st.st_size} byte)")
            data = fh.read(MAX_INDEX_BYTES + 1)
        if len(data) > MAX_INDEX_BYTES:
            raise _CorruptIndex("indice cresciuto durante la lettura")

        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _CorruptIndex(f"JSON non valido: {exc}") from exc
        if not isinstance(raw, list):
            raise _CorruptIndex("la radice dell'indice non è una lista")

        entries = []
        for n, item in enumerate(raw):
            if not isinstance(item, dict) or not _REQUIRED_FIELDS <= item.keys():
                raise _CorruptIndex(f"voce {n} malformata")
            # Campi sconosciuti ignorati invece di far fallire tutto: un
            # indice scritto da una versione più recente resta leggibile
            # dopo un downgrade.
            known = {k: v for k, v in item.items() if k in _ENTRY_FIELDS}
            known.setdefault("signature", None)
            if not (isinstance(known["original_path"], str)
                    and isinstance(known["quarantined_path"], str)
                    and isinstance(known["timestamp"], (int, float))
                    and isinstance(known["signature"], (str, type(None)))
                    and isinstance(known.get("original_mode"), (int, type(None)))):
                raise _CorruptIndex(f"voce {n}: tipi non validi")
            entries.append(QuarantineEntry(**known))
        return entries

    def _load_index(self) -> List[QuarantineEntry]:
        """Indice per un read-modify-write. Da chiamare con il lock tenuto.

        Degradazione controllata invece di un'eccezione che blocca ogni
        operazione: un indice corrotto (disco pieno, crash di una versione
        vecchia senza scrittura atomica) viene RINOMINATO, mai cancellato,
        e si riparte da vuoto. I file in quarantena restano dove sono e
        compaiono in orphans(), recuperabili a mano con l'indice salvato.
        """
        try:
            return self._parse_index()
        except FileNotFoundError:
            self._write_index([])
            return []
        except _CorruptIndex as exc:
            backup = self.dir / (
                f"{self.index_path.name}.corrupt-"
                f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            )
            os.rename(self.index_path, backup)
            self._write_index([])
            self.last_recovery = (backup, str(exc))
            return []

    def _write_index(self, entries: List[QuarantineEntry]) -> None:
        # Scrittura atomica e privata (temporaneo 0600 con nome univoco,
        # fsync, os.replace): chi legge vede sempre l'indice vecchio o il
        # nuovo per intero. Va chiamata con _index_lock() tenuto:
        # l'atomicità protegge i lettori, non gli scrittori.
        write_private_text(
            self.index_path, json.dumps([asdict(e) for e in entries], indent=2)
        )

    def _require_inside(self, quarantined_path: str) -> Path:
        """Difesa in profondità per delete/restore: il percorso viene
        dall'indice, e un indice manomesso non deve poter far cancellare o
        spostare file fuori dalla quarantena."""
        p = Path(quarantined_path)
        if p.parent.resolve() != self.dir.resolve():
            raise QuarantineError(
                f"{quarantined_path} non è nella directory di quarantena: "
                "voce dell'indice non valida, operazione rifiutata"
            )
        return p

    # -- intento e recupero ----------------------------------------------

    def _intent_path(self, dest: Path) -> Path:
        return self.dir / f".{dest.name}{INTENT_SUFFIX}"

    def _copy_mark_path(self, dest: Path) -> Path:
        return self.dir / f".{dest.name}{COPY_MARK_SUFFIX}"

    def _write_intent(self, dest: Path, record: dict) -> int:
        """
        Registra una quarantena in corso PRIMA di toccare il file, e ritorna
        il descrittore che ne tiene il lock (flock) fino alla fine.

        Senza intento, un crash fra lo spostamento (o la copia) e la
        scrittura dell'indice lasciava il file in quarantena senza voce,
        cioè non ripristinabile dalla UI; con la copia fra filesystem
        poteva restare anche l'originale infetto, sotto un nome nascosto
        nella sua directory. recover_interrupted() completa o annulla
        l'operazione. Il lock distingue un'operazione in corso (in un altro
        processo: GUI, CLI, timer) da una interrotta: il kernel lo rilascia
        alla morte del processo.
        """
        path = self._intent_path(dest)
        fd = open_private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = json.dumps(record).encode("utf-8")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        return fd

    def _clear_intent(self, dest: Path, fd: int) -> None:
        # Prima l'unlink, poi la chiusura (che rilascia il lock): nessun
        # altro processo vede l'intento senza lock mentre esiste ancora.
        self._copy_mark_path(dest).unlink(missing_ok=True)
        self._intent_path(dest).unlink(missing_ok=True)
        os.close(fd)

    def recover_interrupted(self) -> List[Tuple[Path, str]]:
        """Completa o annulla le quarantene interrotte (vedi _write_intent).
        Eseguita anche alla creazione dell'istanza."""
        with self._index_lock():
            return self._recover_interrupted_locked()

    def _recover_interrupted_locked(self) -> List[Tuple[Path, str]]:
        done = []
        for intent in sorted(self.dir.glob(f".*{INTENT_SUFFIX}")):
            try:
                fd = os.open(intent, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue  # operazione in corso in un altro processo
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                dest_name = intent.name[1:-len(INTENT_SUFFIX)]
                mark = self.dir / f".{dest_name}{COPY_MARK_SUFFIX}"
                outcome = self._recover_one(os.read(fd, 64 * 1024), copying=mark.exists())
                mark.unlink(missing_ok=True)
                intent.unlink(missing_ok=True)
                if outcome is not None:
                    done.append(outcome)
            finally:
                os.close(fd)
        self.recovered.extend(done)
        return done

    def _recover_one(self, raw: bytes, copying: bool) -> Optional[Tuple[Path, str]]:
        """Esito del recupero di un intento, o None se non c'era niente da
        fare (l'operazione non aveva ancora toccato il file)."""
        try:
            record = json.loads(raw.decode("utf-8"))
            dest = self.dir / Path(record["quarantined_path"]).name
            original = Path(record["original_path"])
            staging = Path(record["staging"])
            identity = (int(record["dev"]), int(record["ino"]))
            entry = QuarantineEntry(
                original_path=str(original),
                quarantined_path=str(dest),
                signature=record.get("signature"),
                timestamp=float(record["timestamp"]),
                original_mode=record.get("original_mode"),
            )
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            # Intento scritto a metà: il crash è avvenuto prima del fsync,
            # quindi prima di toccare il file.
            return None

        try:
            dest_st = os.lstat(dest)
        except FileNotFoundError:
            return None  # interrotta prima dello spostamento o già annullata
        if not stat.S_ISREG(dest_st.st_mode):
            return None

        def same(p: Path) -> bool:
            try:
                st = os.lstat(p)
            except OSError:
                return False
            return (st.st_dev, st.st_ino) == identity

        if not copying:
            # dest esiste solo se il rename è avvenuto. Un inode diverso è
            # la sostituzione gestita da _handle_substitution, interrotta:
            # nessuna voce, resta visibile in orphans().
            if (dest_st.st_dev, dest_st.st_ino) != identity:
                return (dest, "lasciato per il recupero manuale: non è il file verificato")
            # Manca al più il passaggio a sola lettura.
            fd = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                os.fchmod(fd, 0o400)
            finally:
                os.close(fd)
        else:
            if stat.S_IMODE(dest_st.st_mode) != _COMPLETE_COPY_MODE or same(original):
                # Copia incompleta, oppure completa ma l'originale non è
                # ancora stato tolto: si annulla e l'originale resta dov'è,
                # come se la quarantena non fosse partita (la prossima
                # scansione lo rileva di nuovo). Rifare qui lo spostamento
                # dell'originale vorrebbe dire ripeterne le verifiche.
                dest.unlink(missing_ok=True)
                return (dest, "annullata: il file originale è rimasto al suo posto")
            if same(staging):
                # Originale già rinominato col nome temporaneo ma non
                # cancellato: la copia completa è in quarantena.
                staging.unlink()

        entries = self._load_index()
        if not any(e.quarantined_path == str(dest) for e in entries):
            entries.append(entry)
            self._write_index(entries)
        return (dest, "completata")

    # -- quarantena ------------------------------------------------------

    @staticmethod
    def _put_back(moved: Path, original: Path) -> bool:
        """Rimette al suo posto una voce spostata per errore, SENZA
        sovrascrivere: link() fallisce con EEXIST se il nome originale è
        stato nel frattempo rioccupato. follow_symlinks=False: se la voce
        è un symlink si ricrea il link, non un hardlink al suo bersaglio."""
        try:
            os.link(moved, original, follow_symlinks=False)
        except OSError:
            return False
        os.unlink(moved)
        return True

    def _handle_substitution(self, moved: Path, original: Path, moved_st) -> None:
        """L'oggetto spostato non è quello verificato: lo si rimette a posto.

        Prima veniva cancellato. Nello scenario d'attacco (symlink verso un
        hardlink) era innocuo, ma nel caso innocuo lo stesso ramo scatta
        quando un'applicazione salva con "scrivi temporaneo + rinomina"
        mentre il file è in quarantena: la voce spostata è la versione
        APPENA SALVATA dall'utente, e cancellarla era perdita di dati.
        """
        if self._put_back(moved, original):
            detail = "l'elemento spostato è stato rimesso al suo posto"
        elif not stat.S_ISREG(moved_st.st_mode):
            # Symlink o altro oggetto senza contenuto proprio e il nome
            # originale è occupato: rimuoverlo non perde dati.
            try:
                moved.unlink()
            except OSError:
                pass
            detail = "l'elemento spostato non era un file ed è stato rimosso"
        else:
            # File regolare e nome originale occupato: non si cancella
            # niente, resta nella directory di quarantena senza voce
            # nell'indice (visibile in orphans()).
            detail = f"l'elemento spostato è stato lasciato in {moved} per il recupero"
        raise QuarantineError(
            f"{original} è stato sostituito durante la quarantena "
            f"(possibile tentativo di evasione): operazione annullata, {detail}"
        )

    def _copy_across_filesystems(self, src_fd: int, st, path: Path, dest: Path, staging: Path) -> None:
        """Quarantena di un file su un altro filesystem (EXDEV).

        Stesse garanzie del rename:
          - il contenuto si legge dal descrittore già aperto e verificato,
            non dal path;
          - l'originale viene prima rinominato con un nome univoco nella
            SUA directory (stesso filesystem, atomico), poi si confronta
            l'inode con lstat, e solo allora lo si elimina. Così non si
            cancella mai per percorso un oggetto diverso da quello letto.
        """
        os.close(open_private_fd(self._copy_mark_path(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600)
        try:
            os.set_blocking(src_fd, True)
            os.lseek(src_fd, 0, os.SEEK_SET)
            while chunk := os.read(src_fd, COPY_CHUNK):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view):]
            os.fsync(out)
            # Dopo il fsync: 0400 segna la copia completa per il recupero.
            os.fchmod(out, _COMPLETE_COPY_MODE)
        except BaseException:
            os.close(out)
            dest.unlink(missing_ok=True)
            raise
        os.close(out)

        try:
            os.rename(path, staging)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise QuarantineError(
                f"copia in quarantena riuscita ma impossibile rimuovere {path}: {exc}"
            ) from exc

        staged_st = os.lstat(staging)
        if (staged_st.st_dev, staged_st.st_ino) != (st.st_dev, st.st_ino):
            dest.unlink(missing_ok=True)
            self._handle_substitution(staging, path, staged_st)
        os.unlink(staging)

    def quarantine_file(self, path: Path, signature: Optional[str] = None) -> QuarantineEntry:
        path = Path(path)

        # Un file infetto è, per definizione, un contenuto potenzialmente
        # ostile: il modello di minaccia NON è "un aggressore esterno", ma
        # il file stesso (o un processo che lo controlla) che tenta di
        # evadere sostituendosi con un symlink fra un check e l'operazione
        # successiva (TOCTOU). Si apre subito con O_NOFOLLOW e si tiene il
        # descrittore per tutta l'operazione, per la verifica post-rename.
        # O_NONBLOCK: su una FIFO l'open in lettura non si blocca.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise QuarantineError(
                    f"{path} è un collegamento simbolico: rifiuto di quarantenarlo"
                ) from exc
            raise QuarantineError(f"impossibile aprire {path}: {exc}") from exc

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise QuarantineError(f"{path} non è un file regolare")

            # Rifiuto esplicito di ri-quarantenare un file già gestito (una
            # scansione che copre la quarantena lo ri-rileverebbe a ogni
            # ciclo): guard strutturale a prescindere dal chiamante.
            if path.resolve().is_relative_to(self.dir.resolve()):
                raise QuarantineError(
                    f"{path} è già dentro la directory di quarantena: "
                    "rifiuto di ri-quarantenare un file già gestito"
                )

            # Mode originale dal descrittore (non dal path), salvata PRIMA
            # di renderlo 0400, per ripristinarla al restore.
            original_mode = stat.S_IMODE(st.st_mode)

            timestamp = time.time()
            # Nome non prevedibile e senza il nome originale in chiaro.
            dest = self.dir / f"{int(timestamp)}_{uuid.uuid4().hex[:16]}"
            # Nome temporaneo dell'originale nella quarantena per copia:
            # deciso qui perché finisca nell'intento.
            staging = path.with_name(f".klamav-quarantena-{uuid.uuid4().hex}")
            entry = QuarantineEntry(
                original_path=str(path),
                quarantined_path=str(dest),
                signature=signature,
                timestamp=timestamp,
                original_mode=original_mode,
            )
            intent_fd = self._write_intent(dest, {
                **asdict(entry), "staging": str(staging), "dev": st.st_dev, "ino": st.st_ino,
            })
        except BaseException:
            os.close(fd)
            raise

        try:
            self._move_into_quarantine(fd, st, path, dest, staging)
            with self._index_lock():
                entries = self._load_index()
                entries.append(entry)
                self._write_index(entries)
        finally:
            os.close(fd)
            self._clear_intent(dest, intent_fd)
        return entry

    def _move_into_quarantine(self, fd: int, st, path: Path, dest: Path, staging: Path) -> None:
        """Spostamento (o copia fra filesystem) con verifica dell'inode.
        Un'eccezione lascia tutto com'era, salvo quanto documentato in
        _handle_substitution."""
        try:
            # rename() opera sulla voce di directory: se fra l'open e
            # qui 'path' è stato sostituito, sposta la voce nuova. Da
            # qui la verifica dell'inode subito dopo.
            os.rename(path, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise QuarantineError(
                    f"impossibile spostare {path} in quarantena: {exc}"
                ) from exc
            self._copy_across_filesystems(fd, st, path, dest, staging)
            return
        # lstat(), NON stat(): stat() segue i symlink e rendeva il
        # controllo aggirabile con un symlink verso un hardlink del
        # file infetto (stesso inode). Vedi
        # tests/test_quarantine_evasion.py.
        moved_st = os.lstat(dest)
        if (moved_st.st_dev, moved_st.st_ino) != (st.st_dev, st.st_ino):
            self._handle_substitution(dest, path, moved_st)
        # Read-only, dal descrittore: Path.chmod() seguirebbe un
        # eventuale symlink fuori dalla quarantena.
        os.fchmod(fd, 0o400)

    def restore(self, quarantined_path: str, destination: Optional[Path] = None) -> Path:
        with self._index_lock():
            entries = self._load_index()
            match = next((e for e in entries if e.quarantined_path == quarantined_path), None)
            if match is None:
                raise QuarantineError(f"{quarantined_path} non è in indice")
            source = self._require_inside(match.quarantined_path)

            # Descrittore sul file in quarantena PRIMA dello spostamento:
            # l'inode resta lo stesso dopo os.replace, quindi i permessi
            # originali si applicano con fchmod senza passare dal path di
            # destinazione (Path.chmod seguirebbe un symlink creato lì nel
            # frattempo).
            try:
                src_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as exc:
                raise QuarantineError(
                    f"file in quarantena non disponibile ({source}): {exc}"
                ) from exc

            try:
                target = Path(destination) if destination else Path(match.original_path)
                target.parent.mkdir(parents=True, exist_ok=True)

                # Se il percorso originale è stato rioccupato, NON
                # sovrascrivere: la destinazione si riserva atomicamente con
                # O_CREAT|O_EXCL invece di exists() + move (finestra di gara).
                try:
                    reserve_fd = os.open(
                        target,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                    )
                except FileExistsError:
                    raise QuarantineError(
                        f"Impossibile ripristinare: {target} esiste già. "
                        "Sposta o rinomina il file esistente, poi riprova."
                    )

                try:
                    try:
                        # Il nome è già nostro: os.replace sovrascrive il
                        # segnaposto appena creato, non un file di altri.
                        os.replace(source, target)
                        mode_fd = src_fd  # stesso inode, ora in target
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        # Il file veniva da un altro filesystem (quarantena
                        # per copia): si ripristina per copia nel segnaposto
                        # già riservato, poi si elimina la copia in quarantena.
                        os.set_blocking(src_fd, True)
                        while chunk := os.read(src_fd, COPY_CHUNK):
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(reserve_fd, view):]
                        os.fsync(reserve_fd)
                        source.unlink()
                        mode_fd = reserve_fd

                    if match.original_mode is not None:
                        try:
                            os.fchmod(mode_fd, match.original_mode)
                        except OSError:
                            # Il contenuto è recuperato: i permessi sono
                            # secondari.
                            pass
                except BaseException:
                    # Niente segnaposto vuoto al posto dell'originale se il
                    # ripristino non è andato a buon fine.
                    if source.exists():
                        target.unlink(missing_ok=True)
                    raise
                finally:
                    os.close(reserve_fd)
            finally:
                os.close(src_fd)

            remaining = [e for e in entries if e.quarantined_path != quarantined_path]
            self._write_index(remaining)
        return target

    def delete(self, quarantined_path: str) -> None:
        with self._index_lock():
            entries = self._load_index()
            match = next((e for e in entries if e.quarantined_path == quarantined_path), None)
            if match is None:
                raise QuarantineError(f"{quarantined_path} non è in indice")
            self._require_inside(match.quarantined_path).unlink(missing_ok=True)
            remaining = [e for e in entries if e.quarantined_path != quarantined_path]
            self._write_index(remaining)

    def list_entries(self) -> List[QuarantineEntry]:
        # Lettura senza lock nel caso normale: la scrittura atomica
        # garantisce uno snapshot completo. Il lock serve solo se l'indice
        # va recuperato (rinomina + riscrittura).
        try:
            return self._parse_index()
        except (FileNotFoundError, _CorruptIndex):
            with self._index_lock():
                return self._load_index()

    # -- diagnostica per la UI -------------------------------------------

    def corrupt_backups(self) -> List[Path]:
        """Indici corrotti messi da parte (da qualunque istanza)."""
        return sorted(self.dir.glob(f"{self.index_path.name}.corrupt-*"))

    def orphans(self) -> List[Path]:
        """File nella directory di quarantena senza voce nell'indice: dopo
        un recupero da indice corrotto, o un elemento lasciato da
        _handle_substitution. Non vengono mai toccati automaticamente."""
        indexed = {Path(e.quarantined_path).name for e in self.list_entries()}
        service = {self.index_path.name, self.lock_path.name}
        result = []
        for p in sorted(self.dir.iterdir()):
            name = p.name
            if name in service or name in indexed:
                continue
            if name.startswith(f"{self.index_path.name}.corrupt-"):
                continue
            if name.startswith(f".{self.index_path.name}.") and name.endswith(".tmp"):
                continue
            if name.startswith(".") and name.endswith((INTENT_SUFFIX, COPY_MARK_SUFFIX)):
                continue
            if p.is_file() and not p.is_symlink():
                result.append(p)
        return result
