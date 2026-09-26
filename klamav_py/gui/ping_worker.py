"""
Worker eseguito in un QThread separato per verificare che clamd
risponda. Il timeout di default di ClamdClient è di 30s: se clamd è
raggiungibile come socket ma non risponde (demone appeso, non solo
assente), un ping sincrono sul thread della UI bloccherebbe l'avvio
della finestra fino al timeout. Qui gira sempre in background, quindi
la finestra appare subito indipendentemente dall'esito del ping.

Lo stesso worker serve il controllo periodico di MainWindow: lì si
passa un timeout breve, così un clamd appeso viene riconosciuto molto
prima del ping successivo invece di tenere occupato il worker per 30s.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..clamd_client import ClamdEndpoint, ClamdError


class PingWorker(QThread):
    result_ready = Signal(bool)  # True se clamd ha risposto correttamente

    def __init__(self, endpoint: ClamdEndpoint, parent=None,
                 timeout: Optional[float] = None) -> None:
        super().__init__(parent)
        self.endpoint = endpoint
        self.timeout = timeout

    def run(self) -> None:
        kwargs = {} if self.timeout is None else {"timeout": self.timeout}
        client = self.endpoint.new_client(**kwargs)
        try:
            alive = client.ping()
        except (ClamdError, OSError):
            alive = False
        self.result_ready.emit(alive)
