"""Lettura asincrona della versione del database firme da clamd.

Come gli altri worker (eccetto PingWorker) non ha parent Qt: il chiamante
lo rilascia con _retire_qthread().
"""
from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from klamav_py import freshclam_service as fs

from klamav_py.clamd_client import ClamdEndpoint, ClamdError
from klamav_py.db_freshness import DEFAULT_MAX_AGE, DbInfo, parse_version_reply

PROBE_TIMEOUT = 5.0


def probe_db_info(endpoint: ClamdEndpoint, timeout: float = PROBE_TIMEOUT) -> DbInfo | None:
    """VERSION su clamd; None per qualunque errore di connessione o protocollo."""
    try:
        reply = endpoint.new_client(timeout=timeout).version()
        return parse_version_reply(reply)
    # Qualunque errore è «stato sconosciuto», come promette il docstring:
    # un'eccezione inattesa usciva da run() senza emettere result_ready, il
    # worker non veniva mai rilasciato e ogni controllo successivo della
    # sessione era saltato (penetration test della 0.1.19).
    except Exception:  # noqa: BLE001
        return None


class DbInfoWorker(QThread):
    result_ready = Signal(object)  # DbInfo | None

    def __init__(self, endpoint: ClamdEndpoint, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint

    def run(self) -> None:
        self.result_ready.emit(confirm_with_journal(probe_db_info(self._endpoint)))


def confirm_with_journal(info: DbInfo | None) -> DbInfo | None:
    """Un database vecchio può essere comunque l'ultimo pubblicato: lo dice
    il demone freshclam nei controlli periodici. Si guarda il journal solo
    quando serve, cioè quando l'età da sola chiederebbe di aggiornare, e
    solo per la finestra DEFAULT_MAX_AGE.
    """
    if info is None or not info.needs_update():
        return info
    try:
        since = time.time() - DEFAULT_MAX_AGE.total_seconds()
        return info.confirmed(fs.recent_up_to_date_versions(since))
    except Exception:  # noqa: BLE001 - informazione di contorno
        return info
