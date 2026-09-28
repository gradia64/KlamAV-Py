"""
Quando ha senso aggiornare le firme riavviando il freshclam locale.

L'aggiornamento dalla GUI (freshclam_service) riavvia l'unità freshclam di
QUESTA macchina e verifica l'esito confrontando la versione che clamd
riporta prima e dopo. Con clamd raggiunto via TCP la premessa può non
valere, e il riavvio non toccherebbe il database che clamd usa: il
confronto riporterebbe "nessun aggiornamento", un messaggio fuorviante.

Regola:

- socket Unix: sempre consentito (clamd e freshclam sono sulla stessa
  macchina, comportamento di sempre);
- TCP verso un host non-loopback: mai, il database si aggiorna dove gira
  clamd; versione e data restano visibili (il probe VERSION funziona via
  endpoint);
- TCP verso loopback: solo se la versione del daily riportata da clamd
  coincide con quella del database locale, cioè della DatabaseDirectory
  del freshclam.conf di questa macchina (/var/lib/clamav se non è
  indicata). Se non coincidono, o non c'è un database locale, clamd usa un
  altro database: tipicamente un container con la porta mappata su
  localhost, dove il freshclam dell'host non serve.

Un nome host diverso da "localhost" non viene risolto (niente DNS nel
thread della GUI): conta come remoto anche se punta a 127.0.0.1. L'errore
possibile è quindi un pulsante disabilitato in più, mai un riavvio inutile.

Con clamd irraggiungibile (nessuna versione da confrontare) il loopback è
consentito: è lo stato in cui l'aggiornamento serve di più, e l'esito lo
verifica comunque il worker.

Niente Qt, come freshclam_service.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .clamd_client import ClamdEndpoint
from .db_freshness import DbInfo

DEFAULT_DB_DIR = Path("/var/lib/clamav")
# freshclam.conf del freshclam che l'aggiornamento riavvia: Debian e Arch
# usano /etc/clamav, altre distribuzioni /etc; /usr/local/etc è il
# predefinito di una compilazione da sorgente. Vale il primo che esiste.
FRESHCLAM_CONF_PATHS = (
    Path("/etc/clamav/freshclam.conf"),
    Path("/etc/freshclam.conf"),
    Path("/usr/local/etc/freshclam.conf"),
)
# L'intestazione di un .cvd/.cld è un blocco di testo di 512 byte:
# "ClamAV-VDB:<data>:<versione>:<firme>:<livello>:<md5>:<dsig>:<autore>:<tempo>".
_HEADER_SIZE = 512
_HEADER_PREFIX = "ClamAV-VDB:"


@dataclass(frozen=True)
class UpdateAvailability:
    allowed: bool
    reason: str | None = None  # perché no, per l'utente; None se consentito


def is_loopback_host(host: str) -> bool:
    """True per "localhost" e per gli indirizzi di loopback (127.0.0.0/8,
    ::1, e 127.x in forma IPv4-mapped). I nomi non vengono risolti."""
    name = host.strip().rstrip(".").lower()
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])  # zona IPv6 (fe80::1%eth0)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


def parse_db_header(data: bytes) -> int | None:
    """Versione dall'intestazione di un .cvd/.cld; None se non riconosciuta."""
    text = data[:_HEADER_SIZE].decode("ascii", errors="replace")
    if not text.startswith(_HEADER_PREFIX):
        return None
    fields = text.split(":")
    try:
        return int(fields[2])
    except (IndexError, ValueError):
        return None


def parse_database_directory(text: str) -> Path | None:
    """DatabaseDirectory da un freshclam.conf; None se assente o non
    assoluta. Vale l'ultima occorrenza, come per le opzioni di ClamAV."""
    found = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "databasedirectory":
            value = parts[1].strip().strip('"').strip("'")
            if value.startswith("/"):
                found = Path(value)
    return found


def local_db_dir(conf_paths=None) -> Path:
    """
    Directory del database del freshclam locale.

    Prima era fissa su /var/lib/clamav: con una DatabaseDirectory diversa
    il confronto con clamd via TCP su loopback falliva sempre, e
    l'aggiornamento veniva disabilitato con un messaggio che parlava di un
    container. Il primo freshclam.conf esistente decide; senza
    DatabaseDirectory vale il predefinito di ClamAV.
    """
    for conf in FRESHCLAM_CONF_PATHS if conf_paths is None else conf_paths:
        try:
            text = Path(conf).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            break
        return parse_database_directory(text) or DEFAULT_DB_DIR
    return DEFAULT_DB_DIR


def local_daily_version(db_dir: Path | None = None) -> int | None:
    """Versione del daily locale (daily.cld o daily.cvd, la più alta se ci
    sono entrambi); None se manca o non si legge. Senza db_dir, la
    directory del freshclam locale (local_db_dir)."""
    if db_dir is None:
        db_dir = local_db_dir()
    versions = []
    for name in ("daily.cld", "daily.cvd"):
        try:
            with open(db_dir / name, "rb") as fh:
                version = parse_db_header(fh.read(_HEADER_SIZE))
        except OSError:
            continue
        if version is not None:
            versions.append(version)
    return max(versions) if versions else None


def update_availability(
    endpoint: ClamdEndpoint,
    clamd_info: DbInfo | None,
    local_version: Callable[[], int | None] = local_daily_version,
    db_dir: Callable[[], Path] = local_db_dir,
) -> UpdateAvailability:
    if not endpoint.is_tcp:
        return UpdateAvailability(True)

    where = endpoint.describe()
    if not is_loopback_host(endpoint.tcp_host):
        return UpdateAvailability(
            False,
            f"clamd è raggiunto via {where}: il suo database si aggiorna sulla "
            "macchina dove gira clamd. Qui ne vedi solo versione e data.",
        )

    if clamd_info is None:
        return UpdateAvailability(True)
    local = local_version()
    if local is None:
        return UpdateAvailability(
            False,
            f"Nessun database locale in {db_dir()}: clamd su {where} usa quello "
            "di un altro sistema (per esempio un container). Aggiornalo dove gira clamd.",
        )
    if local != clamd_info.version:
        return UpdateAvailability(
            False,
            f"clamd su {where} usa un database diverso da quello locale (daily "
            f"{clamd_info.version} contro {local}): per esempio clamd in un container. "
            "Aggiornalo dove gira clamd. Se hai appena aggiornato, clamd potrebbe non "
            "aver ancora ricaricato le firme: riprova fra qualche minuto.",
        )
    return UpdateAvailability(True)
