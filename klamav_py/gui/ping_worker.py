"""
Worker eseguito in un QThread separato per verificare che clamd
risponda. Il timeout di default di ClamdClient è di 30s: se clamd è
raggiungibile come socket ma non risponde (demone appeso, non solo
assente), un ping sincrono sul thread della UI bloccherebbe l'avvio
della finestra fino al timeout. Qui gira sempre in background, quindi
la finestra appare subito indipendentemente dall'esito del ping.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from ..clamd_client import ClamdClient, ClamdError


class PingWorker(QThread):
    result_ready = Signal(bool)  # True se clamd ha risposto correttamente

    def __init__(self, socket_path: str, parent=None) -> None:
        super().__init__(parent)
        self.socket_path = socket_path

    def run(self) -> None:
        client = ClamdClient(unix_socket=self.socket_path)
        try:
            alive = client.ping()
        except (ClamdError, OSError):
            alive = False
        self.result_ready.emit(alive)
