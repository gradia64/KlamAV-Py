"""
Stato di salute di clamd durante la sessione della GUI.

Logica pura, senza Qt: la GUI le passa l'esito di ogni ping e reagisce
solo alle transizioni (WENT_DOWN / CAME_BACK), così avvisi e notifiche
partono una volta per evento invece che a ogni ping.

Isteresi: un singolo ping fallito non basta a dichiarare clamd fermo
(un RELOAD del database o un picco di carico possono far scadere un
ping isolato). Servono `fail_threshold` fallimenti consecutivi, salvo
`immediate=True` (ping di avvio: lì l'utente vede già un avviso).
"""

from __future__ import annotations

import enum
import time
from typing import Callable


class ClamdState(enum.Enum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class Transition(enum.Enum):
    WENT_DOWN = "went_down"
    CAME_BACK = "came_back"


class ClamdHealth:
    def __init__(self, fail_threshold: int = 2) -> None:
        if fail_threshold < 1:
            raise ValueError("fail_threshold deve essere almeno 1")
        self._threshold = fail_threshold
        self._failures = 0
        self.state = ClamdState.UNKNOWN

    @property
    def is_down(self) -> bool:
        return self.state is ClamdState.DOWN

    def observe(self, alive: bool, *, immediate: bool = False) -> Transition | None:
        if alive:
            self._failures = 0
            previous, self.state = self.state, ClamdState.UP
            return Transition.CAME_BACK if previous is ClamdState.DOWN else None

        self._failures += 1
        if self.state is ClamdState.DOWN:
            return None
        if immediate or self._failures >= self._threshold:
            self.state = ClamdState.DOWN
            return Transition.WENT_DOWN
        return None


class Throttle:
    """Al più un'azione ogni `min_interval` secondi (orologio monotono).

    Serve ai ping forzati dagli errori del Real-Time: un `git clone`
    durante un'interruzione di clamd produrrebbe centinaia di errori in
    pochi secondi, e ognuno chiederebbe un ping.
    """

    def __init__(self, min_interval: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._last: float | None = None

    def ready(self) -> bool:
        now = self._clock()
        if self._last is not None and now - self._last < self._min_interval:
            return False
        self._last = now
        return True
