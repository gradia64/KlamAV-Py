"""
Gestione quarantena, separata dalla UI (a differenza di kuarantine.cpp
in klamav 0.22, che mescolava logica di spostamento file, dialog Qt e
lettura/scrittura KConfig nella stessa classe).

I metadata sono in un JSON accanto ai file quarantenati: niente database
esterno da mantenere per un caso d'uso così semplice.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class QuarantineEntry:
    original_path: str
    quarantined_path: str
    signature: Optional[str]
    timestamp: float


class QuarantineError(RuntimeError):
    pass


class Quarantine:
    """
    Directory di quarantena con permessi 0700, un JSON di indice
    (`index.json`) e i file rinominati con un timestamp per evitare
    collisioni tra file omonimi provenienti da directory diverse
    (uno dei problemi elencati nel TODO originale di klamav: "allow
    multiple instances of same filename in quarantine").
    """

    def __init__(self, quarantine_dir: Path):
        self.dir = Path(quarantine_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        self.index_path = self.dir / "index.json"
        if not self.index_path.exists():
            self._write_index([])

    def _read_index(self) -> List[QuarantineEntry]:
        raw = json.loads(self.index_path.read_text())
        return [QuarantineEntry(**item) for item in raw]

    def _write_index(self, entries: List[QuarantineEntry]) -> None:
        self.index_path.write_text(
            json.dumps([asdict(e) for e in entries], indent=2)
        )
        self.index_path.chmod(0o600)

    def quarantine_file(self, path: Path, signature: Optional[str] = None) -> QuarantineEntry:
        path = Path(path).resolve()
        if not path.is_file():
            raise QuarantineError(f"{path} non è un file regolare")

        timestamp = time.time()
        safe_name = f"{int(timestamp)}_{path.name}"
        dest = self.dir / safe_name
        counter = 1
        while dest.exists():
            dest = self.dir / f"{int(timestamp)}_{counter}_{path.name}"
            counter += 1

        shutil.move(str(path), str(dest))
        dest.chmod(0o600)

        entry = QuarantineEntry(
            original_path=str(path),
            quarantined_path=str(dest),
            signature=signature,
            timestamp=timestamp,
        )
        entries = self._read_index()
        entries.append(entry)
        self._write_index(entries)
        return entry

    def restore(self, quarantined_path: str, destination: Optional[Path] = None) -> Path:
        entries = self._read_index()
        match = next((e for e in entries if e.quarantined_path == quarantined_path), None)
        if match is None:
            raise QuarantineError(f"{quarantined_path} non è in indice")

        target = Path(destination) if destination else Path(match.original_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(match.quarantined_path, str(target))

        remaining = [e for e in entries if e.quarantined_path != quarantined_path]
        self._write_index(remaining)
        return target

    def delete(self, quarantined_path: str) -> None:
        entries = self._read_index()
        match = next((e for e in entries if e.quarantined_path == quarantined_path), None)
        if match is None:
            raise QuarantineError(f"{quarantined_path} non è in indice")
        Path(match.quarantined_path).unlink(missing_ok=True)
        remaining = [e for e in entries if e.quarantined_path != quarantined_path]
        self._write_index(remaining)

    def list_entries(self) -> List[QuarantineEntry]:
        return self._read_index()
