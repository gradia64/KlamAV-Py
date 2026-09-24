"""Aggiornamento firme delegato all'unità systemd pacchettizzata di freshclam.

Sostituisce freshclam-update.sh: nessuno script passa per pkexec, solo
binari di sistema root-owned con argv fissi. freshclam gira come utente
clamav, con il sandboxing dell'unità della distribuzione.
"""
from __future__ import annotations

import enum
import os
import stat
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from klamav_py.db_freshness import DbInfo

# Debian/Ubuntu: clamav-freshclam.service. Il secondo nome copre le
# distribuzioni che usano "freshclam.service"; la scelta avviene a runtime
# tramite LoadState, quindi l'ordine conta solo se esistono entrambe.
UNIT_CANDIDATES: tuple[str, ...] = ("clamav-freshclam.service", "freshclam.service")
TRUSTED_BIN_DIRS: tuple[str, ...] = ("/usr/bin", "/bin")

# Codici di uscita documentati in pkexec(1).
PKEXEC_DISMISSED = 126
PKEXEC_NOT_AUTHORIZED = 127

Runner = Callable[..., subprocess.CompletedProcess]


class Outcome(enum.Enum):
    UPDATED = "updated"          # clamd ha caricato una versione nuova
    UNCHANGED = "unchanged"      # riavvio ok, nessun DB nuovo entro il timeout
    CANCELLED = "cancelled"      # dialogo di autenticazione chiuso
    DENIED = "denied"            # autorizzazione negata o errore polkit
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # uscita dell'applicazione


@dataclass(frozen=True)
class RestartResult:
    outcome: Outcome
    message: str
    db_info: DbInfo | None = None


def trusted_binary(name: str, dirs: Sequence[str] = TRUSTED_BIN_DIRS) -> str | None:
    """Percorso assoluto di un binario root-owned e non scrivibile da group/other."""
    for directory in dirs:
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)  # segue /bin -> usr/bin: si controlla il target
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue
        return path
    return None


def _show(unit: str, prop: str, run: Runner) -> str | None:
    systemctl = trusted_binary("systemctl")
    if systemctl is None:
        return None
    try:
        r = run([systemctl, "show", f"--property={prop}", "--value", unit],
                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def resolve_unit(run: Runner = subprocess.run) -> str | None:
    """Prima unità candidata effettivamente installata (LoadState=loaded).

    Un'unità mascherata ("masked") è una scelta dell'amministratore: si salta.
    """
    for unit in UNIT_CANDIDATES:
        if _show(unit, "LoadState", run) == "loaded":
            return unit
    return None


def is_active(unit: str, run: Runner = subprocess.run) -> bool:
    return _show(unit, "ActiveState", run) == "active"


def is_failed(unit: str, run: Runner = subprocess.run) -> bool:
    return _show(unit, "ActiveState", run) == "failed"


def build_restart_argv(unit: str) -> list[str] | None:
    if unit not in UNIT_CANDIDATES:
        raise ValueError(f"unità non ammessa: {unit!r}")
    pkexec = trusted_binary("pkexec")
    systemctl = trusted_binary("systemctl")
    if pkexec is None or systemctl is None:
        return None
    return [pkexec, systemctl, "restart", unit]


def journal_lines(unit: str, since_epoch: float,
                  run: Runner = subprocess.run) -> list[str]:
    """Righe del journal dell'unità dal riavvio in poi.

    Senza i permessi per il journal di sistema (gruppi adm/systemd-journal)
    l'output è vuoto: è solo informazione di contorno, mai un segnale di esito.
    """
    journalctl = trusted_binary("journalctl")
    if journalctl is None:
        return []
    try:
        r = run([journalctl, f"--unit={unit}", f"--since=@{int(since_epoch)}",
                 "--output=cat", "--no-pager", "--quiet", "--lines=500"],
                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]
