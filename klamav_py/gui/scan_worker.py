"""
Worker eseguito in un QThread separato: la scansione (specie via
INSTREAM su directory grandi) può durare a lungo e non deve mai girare
nel thread della UI, altrimenti la finestra si blocca.

Comunica con la UI solo tramite segnali Qt, mai chiamando direttamente
widget da un altro thread.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..clamd_client import ClamdClient, ClamdError
from ..quarantine import Quarantine

# Intervallo minimo (secondi) tra due aggiornamenti di stato inviati alla
# UI. Su scansioni veloci (centinaia/migliaia di file al secondo) emettere
# un segnale Qt per OGNI file sommerge la coda eventi del thread GUI:
# ogni update di una QLabel il cui testo cambia (percorso file, contatori)
# forza Qt a ricalcolare il layout, e con decine di migliaia di
# ricalcoli al secondo il risultato visibile è uno sfarfallio/
# ridimensionamento continuo della finestra. Il conteggio interno resta
# comunque preciso al 100% indipendentemente dal throttling: cambia solo
# la frequenza con cui viene *mostrato*.
PROGRESS_THROTTLE_SECONDS = 0.15


class ScanWorker(QThread):
    result_ready = Signal(object)  # ScanResult, solo per infetti/errori
    scanning = Signal(str)  # path del file che si sta iniziando a scansionare (throttled)
    progress = Signal(int, int, int)  # (scansionati, infezioni, errori), throttled
    quarantined = Signal(str)  # path originale del file appena messo in quarantena
    error = Signal(str)
    finished_scan = Signal(int, int, int)  # (file scansionati, infezioni trovate, errori)

    def __init__(
        self,
        socket_path: str,
        target: Path,
        quarantine_dir: Optional[Path] = None,
        auto_quarantine: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.socket_path = socket_path
        self.target = target
        self.quarantine_dir = quarantine_dir
        self.auto_quarantine = auto_quarantine
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        client = ClamdClient(unix_socket=self.socket_path)
        quarantine = Quarantine(self.quarantine_dir) if self.quarantine_dir else None

        scanned = 0
        infections = 0
        errors = 0

        # Gate temporale condiviso tra l'aggiornamento "sto scansionando
        # X" e l'aggiornamento dei contatori: un'unica cadenza per
        # entrambi, così la UI riceve un "tick" di stato coerente invece
        # di due flussi di segnali indipendenti che si accavallano.
        last_emit = 0.0

        def on_file_start(p: Path) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit >= PROGRESS_THROTTLE_SECONDS:
                self.scanning.emit(str(p))
                last_emit = now

        try:
            for result in client.scan_stream(self.target, on_file_start=on_file_start):
                if self._stop_requested:
                    break
                scanned += 1

                if result.status == "ERROR":
                    errors += 1

                if result.infected and self.auto_quarantine and quarantine is not None:
                    try:
                        # Sposta il file in quarantena
                        quarantine.quarantine_file(Path(result.path), result.signature)
                        # NON SOVRASCRIVIAMO result.path!
                        # L'utente nella GUI deve vedere dov'era il file originale,
                        # non il percorso nascosto della quarantena.
                        self.quarantined.emit(result.path)
                    except Exception as exc:  # noqa: BLE001
                        self.error.emit(f"Quarantena fallita per {result.path}: {exc}")

                if result.infected:
                    infections += 1

                if result.infected or result.status == "ERROR":
                    # I file "puliti" (la stragrande maggioranza in una
                    # scansione tipica) non producono nessuna riga in
                    # lista: emettere un segnale per ognuno di essi è
                    # overhead puro, oltre che una delle cause dello
                    # sfarfallio della finestra durante scansioni grandi.
                    self.result_ready.emit(result)

                now = time.monotonic()
                if now - last_emit >= PROGRESS_THROTTLE_SECONDS:
                    self.progress.emit(scanned, infections, errors)
                    last_emit = now

        except (ClamdError, OSError) as exc:
            self.error.emit(f"Errore di comunicazione con clamd: {exc}")

        # Emissione finale non soggetta a throttling: garantisce che i
        # contatori mostrati combacino sempre col totale reale, anche se
        # l'ultimo tick periodico risale a prima dell'ultimo file scansionato.
        self.progress.emit(scanned, infections, errors)
        self.finished_scan.emit(scanned, infections, errors)
