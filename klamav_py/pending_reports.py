"""
Segnalazioni da valutare, persistenti fra le scansioni e i processi.
Senza Qt: lo scrivono CLI (quindi il timer di sistema) e worker della GUI,
lo legge la pagina Segnalazioni.

Prima la pagina conosceva solo le segnalazioni delle scansioni della
finestra, nella sessione corrente. Quelle del timer di sistema restavano
nel journal: la notifica del mattino diceva «file infetti», e per sapere
quali bisognava cercarli con journalctl. Ora ogni rilevamento «solo
segnalazione» nuovo (la policy non lo sposta e non c'è una presa visione)
si scrive qui appena trovato, quindi resta anche se la scansione viene
interrotta dopo, per esempio dallo spegnimento del PC.

Una voce per percorso e firma: un nuovo rilevamento dello stesso file la
sostituisce, con l'identità appena riletta. Una voce esce quando l'utente
ne prende visione o elimina il file (pagina Segnalazioni o CLI), quando il
file non c'è più o non è più lo stesso inode (prune), e dopo EXPIRY_DAYS
senza nuovi rilevamenti. Un file che torna a essere rilevato torna qui.

Stessa disciplina dei file di stato (json_registry.py); un file non valido
si mette da parte e si riparte da vuoto: le segnalazioni tornano alla
scansione successiva.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .acknowledged import _SHA256, FileIdentity, PendingReport
from .json_registry import CorruptRegistry, JsonRegistry, is_text, is_time

REGISTRY_NAME = "pending-reports.json"
# Qualche centinaio di byte per voce: 4 MiB sono decine di migliaia di
# segnalazioni, molto oltre un uso plausibile.
MAX_REGISTRY_BYTES = 4 * 1024 * 1024
EXPIRY_DAYS = 90

Location = Tuple[str, str]  # (percorso, firma)


def default_pending_path() -> Path:
    """Accanto al registro delle prese visione. Letto a ogni chiamata, non
    all'import: HOME può cambiare (test, sudo -u)."""
    return Path.home() / ".local/share/klamav-py" / REGISTRY_NAME


def recovery_notice(backup: Path, reason: str) -> str:
    """Avviso per un registro delle segnalazioni messo da parte."""
    return (
        f"registro delle segnalazioni da valutare non valido ({reason}), messo da parte in "
        f"{backup}. Le segnalazioni precedenti tornano alla prossima scansione dei file."
    )


def location(report: PendingReport) -> Location:
    return (report.path, report.signature)


def _is_index(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _report_from(item) -> PendingReport:
    if not isinstance(item, dict):
        raise CorruptRegistry("voce malformata")
    for key in ("path", "signature"):
        if not is_text(item.get(key)):
            raise CorruptRegistry(f"{key} non valido")
    reason = item.get("reason", "")
    if not isinstance(reason, str) or "\0" in reason:
        raise CorruptRegistry("reason non valido")
    if not is_time(item.get("found_at")):
        raise CorruptRegistry("found_at non valido")
    sha, dev, ino = item.get("sha256"), item.get("dev"), item.get("ino")
    if sha is None and dev is None and ino is None:
        identity = None
    elif isinstance(sha, str) and _SHA256.fullmatch(sha) and _is_index(dev) and _is_index(ino):
        identity = FileIdentity(sha, dev, ino)
    else:
        raise CorruptRegistry("identità non valida")
    return PendingReport(item["path"], item["signature"], reason, identity, item["found_at"])


def _report_to(report: PendingReport) -> dict:
    identity = report.identity
    return {
        "path": report.path,
        "signature": report.signature,
        "reason": report.reason,
        "found_at": report.found_at,
        "sha256": identity.sha256 if identity else None,
        "dev": identity.dev if identity else None,
        "ino": identity.ino if identity else None,
    }


def same_file(report: PendingReport) -> bool:
    """Il percorso è ancora il file rilevato: un file regolare, e con lo
    stesso inode se l'identità è nota. Un messaggio spostato da KMail (new/
    → cur/) cambia nome: la voce vecchia esce, e il prossimo rilevamento
    scrive quella nuova. Tocca il filesystem: fuori dal thread della GUI."""
    try:
        st = os.lstat(report.path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if report.identity is None:
        return True
    return (st.st_dev, st.st_ino) == (report.identity.dev, report.identity.ino)


@dataclass(frozen=True)
class PruneResult:
    reports: List[PendingReport]
    removed: int


class PendingStore(JsonRegistry[PendingReport]):
    def __init__(self, path: Optional[Path] = None, now: Callable[[], float] = time.time):
        super().__init__(path if path is not None else default_pending_path(), now)

    @property
    def max_bytes(self) -> int:
        return MAX_REGISTRY_BYTES

    def _entry_from(self, item) -> PendingReport:
        return _report_from(item)

    def _entry_to(self, entry: PendingReport) -> dict:
        return _report_to(entry)

    def _keep(self, entry: PendingReport) -> bool:
        return self._now() - entry.found_at <= EXPIRY_DAYS * 86400

    def _parse(self) -> List[PendingReport]:
        # Una voce per percorso e firma: l'ultima vince.
        return list({location(r): r for r in super()._parse()}.values())

    # -- lettura -----------------------------------------------------------

    def reports(self) -> List[PendingReport]:
        """Segnalazioni da valutare, dalla meno recente."""
        return sorted(self._read(), key=lambda r: r.found_at)

    # -- scrittura ---------------------------------------------------------

    def record(self, report: PendingReport) -> None:
        """Aggiunge o sostituisce la voce di quel percorso e firma.
        Una scrittura per rilevamento: sono rari, e così una scansione
        interrotta subito dopo non lo perde."""
        if not is_text(report.path) or not is_text(report.signature):
            raise ValueError(f"segnalazione non registrabile: {report.path!r}")
        with self._lock():
            entries = [r for r in self._load_locked() if location(r) != location(report)]
            entries.append(report)
            self._write_locked(entries)

    def discard(self, locations: Iterable[Location] = (),
                contents: Iterable[Tuple[str, str]] = ()) -> int:
        """Toglie le voci di quei percorsi e firme (file eliminato) e quelle
        con quel contenuto e firma (presa visione: vale in ogni percorso).
        Ritorna quante ne ha tolte."""
        locations, contents = set(locations), set(contents)
        if not locations and not contents:
            return 0

        def gone(r: PendingReport) -> bool:
            if location(r) in locations:
                return True
            return r.identity is not None and (r.identity.sha256, r.signature) in contents

        with self._lock():
            entries = self._load_locked()
            kept = [r for r in entries if not gone(r)]
            if len(kept) != len(entries):
                self._write_locked(kept)
        return len(entries) - len(kept)

    def prune(self, acknowledged: Iterable[Tuple[str, str]] = (),
              still_there: Callable[[PendingReport], bool] = same_file) -> PruneResult:
        """Toglie le voci già valutate (chiavi del registro delle prese
        visione, anche registrate altrove, per esempio dalla CLI) e quelle
        il cui file non è più lì. Tocca il filesystem: fuori dal thread
        della GUI."""
        acknowledged = set(acknowledged)

        def keep(r: PendingReport) -> bool:
            if r.identity is not None and (r.identity.sha256, r.signature) in acknowledged:
                return False
            return still_there(r)

        with self._lock():
            entries = self._load_locked()
            kept = [r for r in entries if self._keep(r) and keep(r)]
            if len(kept) != len(entries):
                self._write_locked(kept)
        return PruneResult(sorted(kept, key=lambda r: r.found_at), len(entries) - len(kept))
