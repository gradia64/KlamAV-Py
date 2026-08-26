"""
Worker eseguito in un QThread separato: la scansione (specie via
INSTREAM su directory grandi) può durare a lungo e non deve mai girare
nel thread della UI, altrimenti la finestra si blocca.

Comunica con la UI solo tramite segnali Qt, mai chiamando direttamente
widget da un altro thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..clamd_client import ClamdClient, ClamdError
from ..quarantine import Quarantine


class ScanWorker(QThread):
    result_ready = Signal(object)  # ScanResult
    scanning = Signal(str)  # path del file che si sta iniziando a scansionare
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
        try:
            for result in client.scan_stream(
                self.target,
                on_file_start=lambda p: self.scanning.emit(str(p)),
            ):
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
                    except Exception as exc:  # noqa: BLE001
                        self.error.emit(f"Quarantena fallita per {result.path}: {exc}")

                if result.infected:
                    infections += 1

                self.result_ready.emit(result)

        except (ClamdError, OSError) as exc:
            self.error.emit(f"Errore di comunicazione con clamd: {exc}")

        self.finished_scan.emit(scanned, infections, errors)
