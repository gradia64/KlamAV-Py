"""
Gestione quarantena, separata dalla UI (a differenza di kuarantine.cpp
in klamav 0.22, che mescolava logica di spostamento file, dialog Qt e
lettura/scrittura KConfig nella stessa classe).

I metadata sono in un JSON accanto ai file quarantenati: niente database
esterno da mantenere per un caso d'uso così semplice.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class QuarantineEntry:
    original_path: str
    quarantined_path: str
    signature: Optional[str]
    timestamp: float
    # Permessi POSIX del file al momento della quarantena (es. 0o755),
    # da ripristinare al momento del restore. Optional con default None
    # per compatibilità con index.json scritti da versioni precedenti,
    # che non avevano questo campo: senza il default, ricostruire quelle
    # voci con QuarantineEntry(**item) solleverebbe un TypeError.
    original_mode: Optional[int] = None


class QuarantineError(RuntimeError):
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
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        self.index_path = self.dir / "index.json"
        with self._index_lock():
            if not self.index_path.exists():
                self._write_index([])

    @contextmanager
    def _index_lock(self):
        """Lock esclusivo (fcntl.flock) sulle sequenze read-modify-write
        di index.json.

        GUI, CLI e worker del timer utente systemd possono modificare la
        quarantena contemporaneamente. La scrittura atomica (tmp +
        os.replace) garantisce che un LETTORE veda sempre un JSON
        completo, ma NON protegge il read-modify-write: due processi
        che leggono lo stesso indice, ognuno aggiunge la propria entry e
        riscrive → l'ultima scrittura cancella l'entry dell'altro. Il
        lock serializza anche le scritture del file temporaneo (che
        condividono lo stesso nome). Il lock file vive dentro la
        directory di quarantena, già 0700: non è leggibile da altri
        utenti e non serve chmod dedicato. fcntl è POSIX: questo
        progetto è Linux-only per costruzione (clamd, systemd, pkexec).
        """
        lock_path = self.index_path.with_name(self.index_path.name + ".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _read_index(self) -> List[QuarantineEntry]:
        raw = json.loads(self.index_path.read_text())
        return [QuarantineEntry(**item) for item in raw]

    def _write_index(self, entries: List[QuarantineEntry]) -> None:
        # Scrittura atomica: index.json è il punto di incontro tra CLI e
        # GUI (possono girare contemporaneamente, es. timer systemd +
        # finestra aperta). Scrivere direttamente sul file finale
        # lascerebbe una finestra in cui un crash o una lettura
        # concorrente vede un JSON troncato/corrotto. tmp + os.replace()
        # è atomico su un filesystem POSIX: o si vede il contenuto
        # vecchio per intero, o quello nuovo per intero, mai una via di
        # mezzo. Va sempre chiamata con _index_lock() tenuto (vedi i
        # chiamanti): l'atomicità protegge i lettori, non i scrittori.
        tmp_path = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp_path.write_text(json.dumps([asdict(e) for e in entries], indent=2))
        tmp_path.chmod(0o600)
        os.replace(tmp_path, self.index_path)

    def quarantine_file(self, path: Path, signature: Optional[str] = None) -> QuarantineEntry:
        path = Path(path).resolve()
        if not path.is_file():
            raise QuarantineError(f"{path} non è un file regolare")

        # Rifiuto esplicito di ri-quarantenare: se un percorso di
        # scansione copre anche la directory di quarantena (es. scansione
        # di tutta la home), un file già quarantenato viene ri-rilevato e,
        # senza questo guard, verrebbe spostato di nuovo: nuova entry
        # nell'indice, vecchia entry orfana, e a ogni ciclo la quarantena
        # si moltiplicherebbe. È un guard strutturale a prescindere da
        # chi chiama (worker con auto-quarantena, pulsante manuale, CLI).
        if path.is_relative_to(self.dir.resolve()):
            raise QuarantineError(
                f"{path} è già dentro la directory di quarantena: "
                "rifiuto di ri-quarantenare un file già gestito"
            )

        # shutil.move() da solo non neutralizza nulla: un file infetto
        # con mode 0755 arriverebbe in quarantena ancora eseguibile. La
        # mode originale va salvata PRIMA dello spostamento per poterla
        # ripristinare al restore (altrimenti il file restaurato resta
        # bloccato a 0400 per sempre, es. binari diventati illeggibili
        # dalle app che li usavano).
        original_mode = stat.S_IMODE(path.stat().st_mode)

        timestamp = time.time()
        # Nome non prevedibile e senza il nome file originale in chiaro:
        # il nome originale resta comunque in index.json (serve per il
        # restore), ma non è più ricavabile guardando la sola directory.
        unique_name = f"{int(timestamp)}_{uuid.uuid4().hex[:16]}"
        dest = self.dir / unique_name

        shutil.move(str(path), str(dest))
        # Read-only per il proprietario: non eseguibile, non modificabile
        # per errore mentre è in quarantena.
        dest.chmod(0o400)

        entry = QuarantineEntry(
            original_path=str(path),
            quarantined_path=str(dest),
            signature=signature,
            timestamp=timestamp,
            original_mode=original_mode,
        )
        with self._index_lock():
            entries = self._read_index()
            entries.append(entry)
            self._write_index(entries)
        return entry

    def restore(self, quarantined_path: str, destination: Optional[Path] = None) -> Path:
        with self._index_lock():
            entries = self._read_index()
            match = next((e for e in entries if e.quarantined_path == quarantined_path), None)
            if match is None:
                raise QuarantineError(f"{quarantined_path} non è in indice")

            target = Path(destination) if destination else Path(match.original_path)

            # Politica esplicita: se il percorso originale è stato
            # rioccupato nel frattempo (es. un altro file creato con lo
            # stesso nome), NON sovrascrivere silenziosamente — shutil.move
            # su un file di destinazione esistente farebbe proprio questo
            # su POSIX (rename() sostituisce senza chiedere). Meglio un
            # errore esplicito che perdere dati di qualcun altro.
            if target.exists():
                raise QuarantineError(
                    f"Impossibile ripristinare: {target} esiste già. "
                    "Sposta o rinomina il file esistente, poi riprova."
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(match.quarantined_path, str(target))

            if match.original_mode is not None:
                try:
                    target.chmod(match.original_mode)
                except OSError:
                    # Il ripristino del file è comunque riuscito: i permessi
                    # sono un dettaglio secondario rispetto ad avere
                    # recuperato il contenuto.
                    pass

            remaining = [e for e in entries if e.quarantined_path != quarantined_path]
            self._write_index(remaining)
        return target

    def delete(self, quarantined_path: str) -> None:
        with self._index_lock():
            entries = self._read_index()
            match = next((e for e in entries if e.quarantined_path == quarantined_path), None)
            if match is None:
                raise QuarantineError(f"{quarantined_path} non è in indice")
            Path(match.quarantined_path).unlink(missing_ok=True)
            remaining = [e for e in entries if e.quarantined_path != quarantined_path]
            self._write_index(remaining)

    def list_entries(self) -> List[QuarantineEntry]:
        # Sola lettura: niente lock necessario. L'atomicità della
        # scrittura (tmp + os.replace) garantisce che si veda sempre uno
        # snapshot completo, vecchio o nuovo che sia.
        return self._read_index()
