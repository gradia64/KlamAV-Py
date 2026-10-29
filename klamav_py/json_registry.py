"""
File di stato JSON dell'utente con la stessa disciplina: 0600 in directory
0700, scrittura atomica, lock (flock) per i read-modify-write, parse
validato con un tetto di dimensione, file non valido messo da parte
(.corrupt-*, mai cancellato) per ripartire da vuoto. Senza Qt.

La usano il registro delle prese visione (acknowledged.py) e quello delle
segnalazioni da valutare (pending_reports.py): GUI, CLI e timer possono
scriverli insieme. La manomissione da parte dello stesso utente è
robustezza, non sicurezza: i file stanno nella sua home.

Formato su disco: {"version": 1, "entries": [...]}. Ogni sottoclasse
valida le proprie voci (_entry_from); i campi sconosciuti si ignorano, così
un file scritto da una versione più recente resta leggibile dopo un
downgrade.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generic, Iterable, List, Optional, Tuple, TypeVar

from .private_files import ensure_private_dir, open_private_fd, write_private_text

T = TypeVar("T")


class CorruptRegistry(Exception):
    """Contenuto non plausibile: il file si mette da parte."""


def is_time(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def is_text(value) -> bool:
    """Stringa non vuota su una riga, come un percorso o una firma."""
    return isinstance(value, str) and bool(value) and "\0" not in value and "\n" not in value


class JsonRegistry(Generic[T]):
    max_bytes: int

    def __init__(self, path: Path, now: Callable[[], float] = time.time):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._now = now
        # Ultimo file messo da parte da QUESTA istanza: (file, motivo).
        self.last_recovery: Optional[Tuple[Path, str]] = None

    # -- da definire nella sottoclasse ---------------------------------------

    def _entry_from(self, item) -> T:
        """La voce da un elemento di "entries"; CorruptRegistry se non è
        valido."""
        raise NotImplementedError

    def _entry_to(self, entry: T) -> dict:
        raise NotImplementedError

    def _keep(self, entry: T) -> bool:
        """False per una voce scaduta: non vale alla lettura e non si
        riscrive."""
        return True

    # -- lettura -----------------------------------------------------------

    def _parse(self) -> List[T]:
        """FileNotFoundError se manca, CorruptRegistry se non è plausibile."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise CorruptRegistry(f"registro non apribile: {exc}") from exc
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise CorruptRegistry("il registro non è un file regolare")
            if st.st_size > self.max_bytes:
                raise CorruptRegistry(f"registro di dimensione anomala ({st.st_size} byte)")
            data = fh.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise CorruptRegistry("registro cresciuto durante la lettura")
        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptRegistry(f"JSON non valido: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            raise CorruptRegistry("struttura non valida")
        entries = []
        for n, item in enumerate(raw["entries"]):
            try:
                entries.append(self._entry_from(item))
            except CorruptRegistry as exc:
                raise CorruptRegistry(f"voce {n}: {exc}") from None
        return entries

    def _load_locked(self) -> List[T]:
        """Per un read-modify-write, con il lock tenuto. Un file corrotto
        si rinomina (mai cancellato) e si riparte da vuoto."""
        try:
            return self._parse()
        except FileNotFoundError:
            return []
        except CorruptRegistry as exc:
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-"
                f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            )
            os.rename(self.path, backup)
            self.last_recovery = (backup, str(exc))
            return []

    def _read(self) -> List[T]:
        """Voci valide, senza lock nel caso normale: la scrittura atomica
        garantisce uno snapshot completo. Il lock serve solo per mettere da
        parte un file corrotto."""
        try:
            entries = self._parse()
        except FileNotFoundError:
            return []
        except CorruptRegistry:
            with self._lock():
                entries = self._load_locked()
        return [e for e in entries if self._keep(e)]

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

    def _write_locked(self, entries: Iterable[T]) -> None:
        live = [self._entry_to(e) for e in entries if self._keep(e)]
        write_private_text(self.path, json.dumps({"version": 1, "entries": live}, indent=2))
