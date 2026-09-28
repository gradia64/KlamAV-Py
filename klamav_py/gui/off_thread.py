"""
Controlli brevi sul filesystem fuori dal thread della GUI.

resolve() e stat() su un percorso che sta su un mount di rete "hard"
irraggiungibile non ritornano finché il server non torna: nel thread
principale bloccherebbero la finestra. Qui la funzione gira in un thread
daemon e il risultato torna nel thread della GUI con un segnale queued.

Thread daemon e non QThread: un controllo bloccato per sempre non deve
impedire l'uscita dell'applicazione, né far scattare "QThread: Destroyed
while thread is still running" alla chiusura (vedi _retire_qthread in
main_window). Il lavoro è solo lettura, interromperlo non lascia nulla a
metà.

Chi chiama deve tollerare risultati tardivi o superati: la callback
arriva quando arriva, e può trovare lo stato della pagina cambiato.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class _Relay(QObject):
    """Vive nel thread della GUI: il segnale emesso da un altro thread
    arriva queued e la callback gira qui."""

    delivered = Signal(object, object)  # (callback, risultato)

    def __init__(self) -> None:
        super().__init__()
        self.delivered.connect(self._deliver)

    @staticmethod
    def _deliver(callback: Callable[[Any], None], result: Any) -> None:
        callback(result)


_relay: _Relay | None = None


def run_off_gui_thread(fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
    """
    Esegue fn() in un thread daemon e chiama callback(risultato) nel
    thread della GUI. Un'eccezione di fn() arriva alla callback come
    risultato, invece di perdersi nel thread.

    Va chiamata dal thread della GUI: la prima chiamata vi crea il relay.
    """
    global _relay
    if _relay is None:
        _relay = _Relay()
    relay = _relay

    def target() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - consegnata al chiamante
            result = exc
        relay.delivered.emit(callback, result)

    threading.Thread(target=target, name="klamav-fs-check", daemon=True).start()
