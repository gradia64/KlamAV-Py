"""
Operazioni sulla quarantena chieste dall'utente, fuori dal thread della GUI.

Dalla 0.1.15 il recupero e le letture della quarantena girano in
QuarantineLoadWorker, ma ripristino ed eliminazione (pagina Quarantena) e
«Metti in quarantena» dei file selezionati (pagina Scansione) chiamavano
Quarantine nel thread della GUI: con la quarantena, o i file da spostare,
su un mount lento o su un disco che degrada la finestra si bloccava.

Le chiamate sono le stesse di prima (Quarantine.restore, delete,
quarantine_file): cambia solo il thread. Conferme ed esiti restano nel
thread della GUI: il chiamante chiede conferma prima di avviare il worker
e applica il risultato nella slot di `done`. QThread e non un thread
daemon (off_thread): le operazioni scrivono, quindi alla chiusura
_shutdown_workers le aspetta. Come gli altri worker non ha parent Qt: il
chiamante lo rilascia con _retire_qthread().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from klamav_py.quarantine import Quarantine, QuarantineEntry


@dataclass(frozen=True)
class QuarantineRequest:
    """Un file da mettere in quarantena (dalla lista della pagina Scansione)."""

    path: str
    signature: str | None = None


@dataclass
class ActionResult:
    """Esito di un'operazione, consegnato nel thread della GUI."""

    action: str
    # restore: il percorso ripristinato.
    target: Path | None = None
    # restore, delete: il messaggio dell'eccezione, se l'operazione è fallita.
    error: str | None = None
    # quarantine: (richiesta, voce) per i file spostati e (richiesta,
    # messaggio) per gli errori, nell'ordine della selezione.
    moved: list[tuple[QuarantineRequest, QuarantineEntry]] = field(default_factory=list)
    failed: list[tuple[QuarantineRequest, str]] = field(default_factory=list)


class QuarantineActionWorker(QThread):
    RESTORE = "restore"
    DELETE = "delete"
    QUARANTINE = "quarantine"

    done = Signal(object)  # ActionResult

    def __init__(self, quarantine: Quarantine, action: str, *, quarantined_path: str = "",
                 requests: list[QuarantineRequest] | None = None, parent=None) -> None:
        super().__init__(parent)
        if action not in (self.RESTORE, self.DELETE, self.QUARANTINE):
            raise ValueError(f"operazione sconosciuta: {action}")
        self.quarantine = quarantine
        self.action = action
        self._quarantined_path = quarantined_path
        self._requests = list(requests or ())

    def run(self) -> None:
        result = ActionResult(self.action)
        if self.action == self.QUARANTINE:
            for request in self._requests:
                # Alla chiusura della finestra: il file in corso si
                # completa, gli altri restano dove sono.
                if self.isInterruptionRequested():
                    break
                try:
                    entry = self.quarantine.quarantine_file(Path(request.path), request.signature)
                except Exception as exc:  # noqa: BLE001 - esito per file, come prima
                    result.failed.append((request, str(exc)))
                else:
                    result.moved.append((request, entry))
        else:
            try:
                if self.action == self.RESTORE:
                    result.target = self.quarantine.restore(self._quarantined_path)
                else:
                    self.quarantine.delete(self._quarantined_path)
            except Exception as exc:  # noqa: BLE001 - mostrato dalla pagina
                result.error = str(exc)
        self.done.emit(result)
