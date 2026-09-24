"""
Scadenza della scansione programmata della GUI.

Logica pura, senza Qt. Sostituisce un QTimer a intervallo fisso che
aveva due difetti strutturali:

1. ripartiva da zero a ogni avvio della GUI: con intervallo 24h e un PC
   acceso meno di 24h di fila la scansione non partiva MAI;
2. usava l'orologio monotono, che su Linux non avanza durante la
   sospensione: ogni suspend faceva slittare la scansione.

Qui la scadenza è "ultima esecuzione + intervallo" sull'orologio reale,
con l'ultima esecuzione persistita dal chiamante (QSettings): sopravvive
a riavvii e sospensioni, e una scadenza mancata viene recuperata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

HOUR = 3600.0
DAY = 24 * HOUR


def interval_seconds(interval: int, unit: str) -> float:
    """Intervallo della pianificazione in secondi (minimo un'ora).

    Niente più tetto a ~24,8 giorni: era un limite di QTimer, non del
    calendario.
    """
    n = max(int(interval), 1)
    return n * (DAY if unit == "Giorni" else HOUR)


def normalized_last_run(last_run: Optional[float], now: float) -> Optional[float]:
    """Un'ultima esecuzione "nel futuro" (orologio spostato indietro, NTP
    dopo un boot con RTC sbagliato) bloccherebbe la pianificazione fino a
    quella data: la si riporta a now."""
    if last_run is None:
        return None
    return min(float(last_run), now)


def next_due(last_run: float, interval_s: float) -> float:
    return last_run + interval_s


def is_due(now: float, last_run: Optional[float], interval_s: float) -> bool:
    last = normalized_last_run(last_run, now)
    if last is None:
        return False  # nessun riferimento: il chiamante fissa la base, non scansiona subito
    return now >= next_due(last, interval_s)


def describe_next(now: float, last_run: Optional[float], interval_s: float) -> str:
    last = normalized_last_run(last_run, now)
    if last is None:
        return ""
    due = next_due(last, interval_s)
    when = datetime.fromtimestamp(due).strftime("%d/%m alle %H:%M")
    if now >= due:
        return f"Prossima scansione programmata: in ritardo (prevista il {when}), parte appena possibile."
    return f"Prossima scansione programmata: {when}."
