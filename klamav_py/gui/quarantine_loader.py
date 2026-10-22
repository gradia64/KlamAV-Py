"""
Caricamento della quarantena fuori dal thread della GUI.

Il costruttore di Quarantine prepara la directory (ensure_private_dir),
crea l'indice se manca e recupera le operazioni interrotte
(recover_interrupted); list_entries() mette da parte un indice corrotto;
corrupt_backups() e orphans() elencano la directory. Con una quarantena su
un filesystem lento (mount di rete, disco che degrada) ognuno di questi
passi bloccava la finestra: all'avvio, al cambio di cartella nelle
Impostazioni e a ogni aggiornamento della pagina Quarantena. Qui girano in
un QThread, e la pagina riceve un'istantanea da mostrare.

Il lavoro è lo stesso di prima, nelle stesse funzioni di quarantine.py:
indice con tetto, rinomina in .corrupt-*, _require_inside e verifiche di
inode restano dove sono. QThread e non un thread daemon (off_thread): il
recupero scrive (rinomina, indice), quindi alla chiusura _shutdown_workers
lo aspetta come gli altri worker. Come loro non ha parent Qt: il chiamante
lo rilascia con _retire_qthread().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from klamav_py.gui.off_thread import without_traceback
from klamav_py.quarantine import Quarantine, QuarantineEntry, RecoveryOutcome


@dataclass(frozen=True)
class QuarantineSnapshot:
    """Quarantena pronta (recupero fatto) e ciò che la pagina mostra."""

    quarantine: Quarantine
    entries: list[QuarantineEntry] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    orphans: list[Path] = field(default_factory=list)
    # Errore di lettura dell'indice (disco, permessi): la pagina lo mostra
    # al posto dell'elenco, come prima faceva refresh().
    error: str | None = None
    # Operazioni interrotte riprese da QUESTO caricamento (costruttore o
    # recover_interrupted), da mostrare una volta.
    recovered: list[RecoveryOutcome] = field(default_factory=list)


def load_quarantine(directory: Path, quarantine: Quarantine | None = None) -> QuarantineSnapshot:
    """
    Istantanea della quarantena in `directory`. Senza un oggetto per quella
    directory lo crea, con il recupero; con un oggetto già pronto (pulsante
    Aggiorna, dopo un ripristino) ripete il recupero e rilegge: un'operazione
    rimasta a metà in questa sessione (indice non scritto dopo lo
    spostamento) si completa senza riavviare la GUI. Un errore del
    costruttore arriva al chiamante: senza oggetto non c'è quarantena da
    mostrare. Tocca il filesystem: va eseguita nel worker.
    """
    reuse = quarantine is not None and quarantine.dir == directory
    recovered: list[RecoveryOutcome] = []
    if not reuse:
        quarantine = Quarantine(directory)
        recovered = list(quarantine.recovered)
    try:
        if reuse:
            recovered = quarantine.recover_interrupted()
        entries = quarantine.list_entries()
    except OSError as exc:
        return QuarantineSnapshot(quarantine, error=str(exc), recovered=recovered)
    try:
        backups, orphans = quarantine.corrupt_backups(), quarantine.orphans()
    except OSError:
        backups, orphans = [], []
    return QuarantineSnapshot(quarantine, entries, backups, orphans, recovered=recovered)


class QuarantineLoadWorker(QThread):
    # (directory caricata, QuarantineSnapshot oppure l'eccezione): la
    # directory permette alla pagina di scartare un esito superato da un
    # cambio di cartella.
    loaded = Signal(object, object)

    def __init__(self, directory: Path, quarantine: Quarantine | None = None, parent=None) -> None:
        super().__init__(parent)
        self._directory = Path(directory)
        self._quarantine = quarantine

    def run(self) -> None:
        try:
            result = load_quarantine(self._directory, self._quarantine)
        except Exception as exc:  # noqa: BLE001 - consegnata alla pagina
            # Senza traceback: tenuto in `result` legherebbe questo frame,
            # e quindi il worker, in un ciclo (vedi without_traceback).
            result = without_traceback(exc)
        self.loaded.emit(self._directory, result)
