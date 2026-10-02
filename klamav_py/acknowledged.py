"""
Registro delle segnalazioni già valutate dall'utente («presa visione»).
Senza Qt: lo usano GUI e CLI.

Il caso che l'ha motivato: due email di phishing nel cestino di KMail,
lasciate al loro posto dalla regola «solo segnalazione»
(quarantine_policy.py), venivano segnalate ogni notte dal timer. La CLI
usciva con 1 e OnFailure notificava ogni volta la stessa cosa: una
notifica sempre uguale smette di essere guardata e nasconde quella nuova.
Il codice di uscita non cambia (una segnalazione NUOVA deve notificare);
si toglie la ripetizione di una segnalazione già valutata.

Semantica dei file .fp di ClamAV, al livello dell'utente:
- chiave: SHA-256 del contenuto più la firma, non il percorso. In un
  maildir un messaggio letto passa da new/ a cur/ e cambia nome con i flag
  (":2,S"): una chiave sul percorso si romperebbe al primo accesso. Se il
  contenuto cambia, l'hash cambia e il file torna a essere segnalato;
- si consulta SOLO dopo che la policy ha deciso «solo segnalazione»: una
  presa visione non sopprime mai un rilevamento che andrebbe in
  quarantena, nemmeno con lo stesso hash in un altro percorso;
- l'hash si calcola solo sui file segnalati (rari), rileggendoli dopo il
  rilevamento con O_NOFOLLOW e controllo di file regolare. Un errore di
  lettura vale come segnalazione nuova: si fallisce in modo visibile.

File di stato con la stessa disciplina degli altri: 0600 in directory
0700, scrittura atomica, lock per i read-modify-write (GUI e timer possono
scrivere insieme), parse validato con tetto di dimensione, file invalido
messo da parte (.corrupt-*, mai cancellato) e si riparte da vuoto. La
manomissione da parte dello stesso utente è robustezza, non sicurezza: il
file sta nella sua home.

Le voci senza riscontri da EXPIRY_DAYS giorni si tolgono alla scrittura
successiva e non valgono più già alla lettura.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .private_files import ensure_private_dir, open_private_fd, write_private_text

REGISTRY_NAME = "acknowledged.json"
# Una voce occupa qualche centinaio di byte: 4 MiB sono decine di migliaia
# di prese visione, molto oltre un uso plausibile.
MAX_REGISTRY_BYTES = 4 * 1024 * 1024
EXPIRY_DAYS = 90
# Prefisso dell'hash mostrato da --list-acknowledged e accettato da
# --unacknowledge.
HASH_PREFIX_LEN = 12
_HASH_CHUNK = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")

Key = Tuple[str, str]  # (sha256, firma)


# Ambito di una presa visione, detto all'utente con le stesse parole nella
# CLI, nella pagina Segnalazioni e nella documentazione. Una copia identica
# altrove ha la stessa chiave: è voluto (vedi sopra), ma va saputo.
SCOPE_NOTE = (
    "La presa visione vale per il contenuto e la firma, in qualunque percorso: "
    "una copia identica altrove, con la stessa firma, risulta già valutata."
)


def default_registry_path() -> Path:
    """Accanto alla cronologia della GUI. Letto a ogni chiamata, non
    all'import: HOME può cambiare (test, sudo -u)."""
    return Path.home() / ".local/share/klamav-py" / REGISTRY_NAME


class RegistryError(RuntimeError):
    pass


def recovery_notice(backup: Path, reason: str) -> str:
    """Avviso per un registro messo da parte (AckRegistry.last_recovery),
    con lo stesso testo nella CLI, nelle scansioni e nella pagina
    Segnalazioni. Senza, chi registrava o revocava una presa visione con
    un registro corrotto vedeva solo le vecchie segnalazioni tornare
    nuove, e pensava che le prese visione fossero state cancellate."""
    return (
        f"registro delle prese visione non valido ({reason}), messo da parte in {backup}. "
        "Le prese visione precedenti sono in quel file, non sono state cancellate; "
        "il registro riparte vuoto, quindi quelle segnalazioni tornano nuove."
    )


class _CorruptRegistry(Exception):
    pass


# -- identità del contenuto ----------------------------------------------

@dataclass(frozen=True)
class FileIdentity:
    """Contenuto (sha256) e inode di un file letto dopo il rilevamento.
    L'inode serve all'eliminazione dalla GUI: si cancella solo il file
    mostrato, non uno che ne ha preso il posto."""
    sha256: str
    dev: int
    ino: int


def hash_file(path: Path) -> FileIdentity:
    """SHA-256 di un file regolare, senza seguire un symlink finale.
    OSError per qualunque problema: il chiamante lo tratta come
    segnalazione nuova."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} non è un file regolare")
        os.set_blocking(fd, True)
        digest = hashlib.sha256()
        while chunk := os.read(fd, _HASH_CHUNK):
            digest.update(chunk)
        return FileIdentity(digest.hexdigest(), st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def delete_if_same(path: Path, identity: FileIdentity) -> None:
    """
    Elimina `path` solo se è ancora un file regolare con l'inode di
    `identity` (quello mostrato all'utente). Niente symlink, niente file
    sostituito dopo la visualizzazione: in quei casi RegistryError e non si
    tocca nulla. Il controllo e l'unlink passano dallo stesso descrittore
    della directory, così la finestra di gara resta dentro quella directory.
    """
    path = Path(path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise RegistryError(f"impossibile aprire la cartella di {path}: {exc}") from exc
    try:
        try:
            st = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RegistryError(f"{path} non esiste più") from exc
        if not stat.S_ISREG(st.st_mode):
            raise RegistryError(f"{path} non è più un file regolare: non eliminato")
        if (st.st_dev, st.st_ino) != (identity.dev, identity.ino):
            raise RegistryError(
                f"{path} è stato sostituito dopo il rilevamento: non eliminato"
            )
        os.unlink(path.name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


# -- registro --------------------------------------------------------------

@dataclass(frozen=True)
class Acknowledgement:
    sha256: str
    signature: str
    # Informativo: il file può poi cambiare nome (maildir) o sparire.
    first_path: str
    acknowledged_at: float
    last_seen: float

    @property
    def key(self) -> Key:
        return (self.sha256, self.signature)


_FIELDS = {f.name for f in fields(Acknowledgement)}


def _is_time(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _entry_problem(item) -> Optional[str]:
    if not isinstance(item, dict) or not _FIELDS <= item.keys():
        return "voce malformata"
    if not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
        return "sha256 non valido"
    for key in ("signature", "first_path"):
        value = item[key]
        if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
            return f"{key} non valido"
    for key in ("acknowledged_at", "last_seen"):
        if not _is_time(item[key]):
            return f"{key} non valido"
    return None


class AckRegistry:
    def __init__(self, path: Optional[Path] = None, now: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else default_registry_path()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._now = now
        # Ultimo registro messo da parte da QUESTA istanza: (file, motivo).
        self.last_recovery: Optional[Tuple[Path, str]] = None

    # -- lettura -----------------------------------------------------------

    def _parse(self) -> List[Acknowledgement]:
        """FileNotFoundError se manca, _CorruptRegistry se non è plausibile."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _CorruptRegistry(f"registro non apribile: {exc}") from exc
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise _CorruptRegistry("il registro non è un file regolare")
            if st.st_size > MAX_REGISTRY_BYTES:
                raise _CorruptRegistry(f"registro di dimensione anomala ({st.st_size} byte)")
            data = fh.read(MAX_REGISTRY_BYTES + 1)
        if len(data) > MAX_REGISTRY_BYTES:
            raise _CorruptRegistry("registro cresciuto durante la lettura")
        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _CorruptRegistry(f"JSON non valido: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            raise _CorruptRegistry("struttura non valida")
        entries = {}
        for n, item in enumerate(raw["entries"]):
            problem = _entry_problem(item)
            if problem is not None:
                raise _CorruptRegistry(f"voce {n}: {problem}")
            # Campi sconosciuti ignorati: un registro scritto da una
            # versione più recente resta leggibile dopo un downgrade.
            entry = Acknowledgement(**{k: item[k] for k in _FIELDS})
            entries[entry.key] = entry
        return list(entries.values())

    def _expired(self, entry: Acknowledgement) -> bool:
        return self._now() - entry.last_seen > EXPIRY_DAYS * 86400

    def _load_locked(self) -> List[Acknowledgement]:
        """Per un read-modify-write, con il lock tenuto. Un registro
        corrotto si rinomina (mai cancellato) e si riparte da vuoto."""
        try:
            return self._parse()
        except FileNotFoundError:
            return []
        except _CorruptRegistry as exc:
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-"
                f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            )
            os.rename(self.path, backup)
            self.last_recovery = (backup, str(exc))
            return []

    def entries(self) -> List[Acknowledgement]:
        """Prese visione valide (non scadute), dalla più recente."""
        try:
            entries = self._parse()
        except FileNotFoundError:
            return []
        except _CorruptRegistry:
            with self._lock():
                entries = self._load_locked()
        live = [e for e in entries if not self._expired(e)]
        return sorted(live, key=lambda e: e.acknowledged_at, reverse=True)

    def snapshot(self) -> "AckSnapshot":
        """Stato letto una volta, per una scansione: le ricerche non
        rileggono il file."""
        return AckSnapshot({e.key: e for e in self.entries()})

    # -- scrittura ---------------------------------------------------------

    @contextmanager
    def _lock(self):
        ensure_private_dir(self.path.parent)
        fd = open_private_fd(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _write_locked(self, entries: Iterable[Acknowledgement]) -> None:
        live = [e for e in entries if not self._expired(e)]
        write_private_text(
            self.path,
            json.dumps({"version": 1, "entries": [asdict(e) for e in live]}, indent=2),
        )

    def acknowledge(self, sha256: str, signature: str, path: Path) -> Acknowledgement:
        if not _SHA256.fullmatch(sha256) or not signature:
            raise RegistryError("hash o firma non validi")
        now = self._now()
        entry = Acknowledgement(sha256, signature, str(path), now, now)
        if _entry_problem(asdict(entry)) is not None:
            raise RegistryError(f"percorso non registrabile: {path!r}")
        with self._lock():
            entries = [e for e in self._load_locked() if e.key != entry.key]
            entries.append(entry)
            self._write_locked(entries)
        return entry

    def mark_seen(self, keys: Iterable[Key]) -> None:
        """Aggiorna l'ultimo riscontro delle voci ritrovate da una
        scansione (una scrittura per scansione, non per file)."""
        keys = set(keys)
        if not keys:
            return
        now = self._now()
        with self._lock():
            entries = [
                Acknowledgement(e.sha256, e.signature, e.first_path, e.acknowledged_at, now)
                if e.key in keys else e
                for e in self._load_locked()
            ]
            self._write_locked(entries)

    def revoke(self, keys: Iterable[Key]) -> List[Acknowledgement]:
        keys = set(keys)
        with self._lock():
            entries = self._load_locked()
            removed = [e for e in entries if e.key in keys]
            if removed:
                self._write_locked(e for e in entries if e.key not in keys)
        return removed

    # -- ricerca per --unacknowledge ---------------------------------------

    def match_prefix(self, prefix: str) -> List[Acknowledgement]:
        """Voci il cui hash comincia con `prefix`. RegistryError se il
        prefisso non è esadecimale, non trova nulla o indica più file
        diversi (più firme dello stesso contenuto sono lo stesso file)."""
        prefix = prefix.strip().lower()
        if not prefix or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise RegistryError(f"«{prefix}» non è un prefisso di hash (cifre 0-9 e a-f)")
        found = [e for e in self.entries() if e.sha256.startswith(prefix)]
        if not found:
            raise RegistryError(f"nessuna presa visione con hash che inizia per {prefix}")
        if len({e.sha256 for e in found}) > 1:
            raise RegistryError(
                f"il prefisso {prefix} indica {len({e.sha256 for e in found})} file "
                "diversi: usane uno più lungo"
            )
        return found

    def match_hash(self, sha256: str) -> List[Acknowledgement]:
        return [e for e in self.entries() if e.sha256 == sha256]


class AckSnapshot:
    """Prese visione lette all'inizio di una scansione. Registra i
    riscontri, da passare poi a AckRegistry.mark_seen."""

    def __init__(self, entries: dict):
        self._entries = entries
        self.hits: set = set()

    def check(self, identity: FileIdentity, signature: Optional[str]) -> bool:
        key = (identity.sha256, signature or "")
        if key in self._entries:
            self.hits.add(key)
            return True
        return False


@dataclass(frozen=True)
class PendingReport:
    """Rilevamento «solo segnalazione» nuovo, per la pagina Segnalazioni
    della GUI. identity è il contenuto riletto subito dopo il rilevamento:
    la presa visione registra QUELLO, non il contenuto del file al momento
    del clic; None se non si è potuto rileggere (niente azioni)."""
    path: str
    signature: str
    reason: str
    identity: Optional[FileIdentity]
    found_at: float


class ScanAcknowledgements:
    """
    Consultazione del registro durante una scansione, condivisa da CLI e
    worker della GUI. Da chiamare solo per i rilevamenti che la policy ha
    già deciso di segnalare soltanto.

    Il registro si legge al primo rilevamento del genere, non prima: una
    scansione senza segnalazioni non tocca il file. Un registro che non si
    legge, come un file che non si rilegge per l'hash, vale «nessuna presa
    visione»: la segnalazione resta, e il motivo finisce in `problems`.
    """

    def __init__(self, registry: Optional[AckRegistry] = None):
        self.registry = registry if registry is not None else AckRegistry()
        self._snapshot: Optional[AckSnapshot] = None
        self.problems: List[str] = []

    def identify(self, path: Path) -> Optional[FileIdentity]:
        try:
            return hash_file(path)
        except OSError as exc:
            self.problems.append(f"impossibile rileggere {path} per l'hash: {exc}")
            return None

    def already_evaluated(self, identity: Optional[FileIdentity], signature: Optional[str]) -> bool:
        if identity is None or not signature:
            return False
        if self._snapshot is None:
            try:
                self._snapshot = self.registry.snapshot()
            except OSError as exc:
                self.problems.append(f"registro delle prese visione non leggibile: {exc}")
                self._snapshot = AckSnapshot({})
            if self.registry.last_recovery is not None:
                self.problems.append(recovery_notice(*self.registry.last_recovery))
        return self._snapshot.check(identity, signature)

    def finish(self) -> None:
        """Aggiorna l'ultimo riscontro delle prese visione ritrovate."""
        if self._snapshot is None or not self._snapshot.hits:
            return
        try:
            self.registry.mark_seen(self._snapshot.hits)
        except OSError as exc:
            self.problems.append(f"impossibile aggiornare il registro delle prese visione: {exc}")
