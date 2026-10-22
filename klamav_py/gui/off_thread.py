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


def without_traceback(exc: BaseException) -> BaseException:
    """
    L'eccezione da consegnare a un altro thread, senza traceback (anche
    nelle eccezioni concatenate). Tipo, argomenti e messaggio restano.

    Tenuta in un locale del frame che l'ha intercettata (result = exc), il
    suo traceback tiene vivo quel frame e, con f_back, quelli sopra: un
    ciclo che libera solo il garbage collector, nel thread che capita.
    Se il ciclo contiene un QThread o un widget (il worker in `self`, una
    pagina catturata dalla callback), Qt se li vede distruggere fuori dal
    thread della GUI: un segfault visto nella CI con Python 3.12 (0.1.17).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        current.__traceback__ = None
        current = current.__cause__ or current.__context__
    return exc


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
            result = without_traceback(exc)
        relay.delivered.emit(callback, result)

    threading.Thread(target=target, name="klamav-fs-check", daemon=True).start()
