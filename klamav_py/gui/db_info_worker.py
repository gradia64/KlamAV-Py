"""Lettura asincrona della versione del database firme da clamd.

Come gli altri worker (eccetto PingWorker) non ha parent Qt: il chiamante
lo rilascia con _retire_qthread().
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from klamav_py.clamd_client import ClamdClient, ClamdError
from klamav_py.db_freshness import DbInfo, parse_version_reply

PROBE_TIMEOUT = 5.0


def probe_db_info(socket_path: str, timeout: float = PROBE_TIMEOUT) -> DbInfo | None:
    """VERSION su clamd; None per qualunque errore di connessione o protocollo."""
    try:
        reply = ClamdClient(unix_socket=socket_path, timeout=timeout).version()
    except (OSError, ClamdError, ValueError):
        return None
    return parse_version_reply(reply)


class DbInfoWorker(QThread):
    result_ready = Signal(object)  # DbInfo | None

    def __init__(self, socket_path: str, parent=None) -> None:
        super().__init__(parent)
        self._socket_path = socket_path

    def run(self) -> None:
        self.result_ready.emit(probe_db_info(self._socket_path))
