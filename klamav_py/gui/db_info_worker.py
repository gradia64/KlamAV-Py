"""Lettura asincrona della versione del database firme da clamd.

Come gli altri worker (eccetto PingWorker) non ha parent Qt: il chiamante
lo rilascia con _retire_qthread().
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from klamav_py.clamd_client import ClamdEndpoint, ClamdError
from klamav_py.db_freshness import DbInfo, parse_version_reply

PROBE_TIMEOUT = 5.0


def probe_db_info(endpoint: ClamdEndpoint, timeout: float = PROBE_TIMEOUT) -> DbInfo | None:
    """VERSION su clamd; None per qualunque errore di connessione o protocollo."""
    try:
        reply = endpoint.new_client(timeout=timeout).version()
    except (OSError, ClamdError, ValueError):
        return None
    return parse_version_reply(reply)


class DbInfoWorker(QThread):
    result_ready = Signal(object)  # DbInfo | None

    def __init__(self, endpoint: ClamdEndpoint, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint

    def run(self) -> None:
        self.result_ready.emit(probe_db_info(self._endpoint))
