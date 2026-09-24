"""Freschezza del database firme a partire dalla risposta VERSION di clamd.

Formato atteso: "ClamAV 1.4.2/27072/Tue Sep 22 09:25:00 2026".
La data è in formato ctime (inglese, ora locale dell'host clamd). Il parsing
NON usa strptime: QCoreApplication chiama setlocale(LC_ALL, "") su Unix e
con un locale italiano "%b" non riconoscerebbe "Sep".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}

DEFAULT_MAX_AGE = timedelta(hours=36)


@dataclass(frozen=True)
class DbInfo:
    engine: str          # es. "1.4.2"
    version: int         # es. 27072 (versione del daily)
    built: datetime      # naive, ora locale dell'host clamd

    def age(self, now: datetime | None = None) -> timedelta:
        delta = (now or datetime.now()) - self.built
        # Orologio sfasato o DB "dal futuro": mai un'età negativa.
        return max(delta, timedelta(0))

    def is_stale(self, max_age: timedelta = DEFAULT_MAX_AGE,
                 now: datetime | None = None) -> bool:
        return self.age(now) > max_age


def parse_version_reply(reply: str) -> DbInfo | None:
    """Restituisce None per qualunque risposta non riconosciuta.

    Include il caso di clamd senza database caricato ("ClamAV 1.4.2").
    """
    text = reply.replace("\0", "").strip()
    parts = text.split("/", 2)
    if len(parts) != 3:
        return None
    engine, db_version, stamp = parts
    if not engine.startswith("ClamAV "):
        return None
    try:
        version = int(db_version)
    except ValueError:
        return None

    # ctime allinea i giorni a una cifra con uno spazio doppio: split() lo assorbe.
    fields = stamp.split()
    if len(fields) != 5:
        return None
    _weekday, month_name, day, hms, year = fields
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        hour, minute, second = (int(x) for x in hms.split(":"))
        built = datetime(int(year), month, int(day), hour, minute, second)
    except ValueError:
        return None
    return DbInfo(engine.removeprefix("ClamAV ").strip(), version, built)


def should_update_on_startup(info: DbInfo | None, enabled: bool,
                             max_age: timedelta = DEFAULT_MAX_AGE,
                             now: datetime | None = None) -> bool:
    """Gate per startup_update.

    Con clamd irraggiungibile (info None) non si chiede nulla: a quel punto
    è l'avviso di salute del demone a dover parlare, non un prompt pkexec.
    """
    if not enabled or info is None:
        return False
    return info.is_stale(max_age, now)


def describe(info: DbInfo | None, max_age: timedelta = DEFAULT_MAX_AGE,
             now: datetime | None = None) -> str:
    """Testo per le label di stato (pagina Aggiornamento e Informazioni)."""
    if info is None:
        return "Database firme: stato sconosciuto (clamd non risponde)."
    hours = int(info.age(now).total_seconds() // 3600)
    age = f"{hours} h" if hours < 48 else f"{hours // 24} giorni"
    text = (f"Database firme: versione {info.version} del "
            f"{info.built:%d/%m/%Y %H:%M} ({age} fa) — ClamAV {info.engine}")
    if info.is_stale(max_age, now):
        text += ". Firme non aggiornate: conviene aggiornare."
    return text
